"""Seam 2: where an upstream Proposer session may send content, decided BEFORE a session exists.

Upstream's `Proposer` (harness2.core.proposer) runs an agentic CLI session (`opencode` through
bun, or the `claude` CLI) with file and shell access. Reading its source at the pinned commit:

  * it has NO endpoint field. The claude channel sets CLAUDE_CODE_USE_VERTEX=1, the opencode
    channel sets GOOGLE_VERTEX_PROJECT, so out of the box both go to Vertex AI;
  * `env()` hands the session the WHOLE process environment minus five Anthropic variables, so
    every other credential in the shell is inherited;
  * the claude channel runs `claude --dangerously-skip-permissions`, i.e. it spends the user's
    Claude subscription quota, and neither channel isolates the shell it gives the model.

`check_proposer` therefore takes what the session would be given (CLI kind, model, the endpoint
the operator declares, environment, working directory, command line) and returns a `Decision`
without starting anything. It is default-deny:

  * the endpoint must be DECLARED (it cannot be read off the CLI), pass `egress.check_endpoint`,
    and be consistent with the model provider: a loopback address is trusted only for a local
    provider (ollama, lmstudio, ...), because a loopback gateway can forward to a hosted model;
  * Vertex, Bedrock and any other hosted cloud route are refused;
  * credential-LOOKING environment variable names are refused unless listed in `allowed_env`;
    endpoint variables (`*_BASE_URL`, proxies) must pass the same gate;
  * a `claude` CLI is refused unless `settings.allow_claude_cli_proposer` is set, and that flag
    is necessary, not sufficient: the endpoint rules still apply.

Reasons name environment variables by NAME only. No value, URL userinfo, query, argument or prompt
text is ever copied into a reason, a warning or the redacted view.

This module imports nothing from upstream, so it works without the `upstream` extra.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any
from urllib.parse import urlparse

from . import egress
from .settings import ENV_ALLOW_CLAUDE_PROPOSER, Settings

KINDS = ("claude_cli", "opencode")  # == harness2.core.spec.CHANNELS (pinned by a test)
_KIND_ALIASES = {"claude": "claude_cli"}
_FIELDS = ("kind", "model", "base_url", "env", "cwd", "command", "project", "region")

LOCAL_PROVIDERS = frozenset({"ollama", "lmstudio", "llamacpp", "llama.cpp", "llama-cpp", "local", "localai", "vllm"})
_EXE_KIND = {"claude": "claude_cli", "claude-code": "claude_cli", "bun": "opencode", "opencode": "opencode"}

_NON_SECRET_SUFFIXES = ("_BASE_URL", "_API_BASE", "_ENDPOINT", "_API_URL", "_MODEL", "_REGION", "_LOCATION",
                        "_PROJECT", "_PROJECT_ID")
_SECRET_WORDS = re.compile(
    r"(^|_)(API_?KEY|APIKEY|KEY|TOKEN|SECRET|PASSWORD|PASSWD|PASS|CREDENTIALS?|AUTH|COOKIE|BEARER|PRIVATE)(_|$)")
_SECRET_PREFIXES = ("AWS_", "AZURE_", "ANTHROPIC_", "OPENAI_", "OPENROUTER_", "JEV_", "GITHUB_", "GH_", "GITEA_",
                    "GITLAB_", "HF_", "HUGGINGFACE_", "GPG_")
_ENDPOINT_SUFFIXES = ("_BASE_URL", "_API_BASE", "_ENDPOINT", "_API_URL")
_PROXIES = frozenset({"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"})
_VERTEX_ENV_PREFIXES = ("GOOGLE_VERTEX_", "ANTHROPIC_VERTEX_", "VERTEXAI_")
_CLOUD_FLAGS = ("CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_BEDROCK")
_TRUTHY = frozenset({"1", "true", "yes", "on"})


class ProposerConfigError(ValueError):
    """The configuration cannot be read. `check_proposer` turns this into a refusal."""


@dataclass(frozen=True)
class ProposerConfig:
    """What an upstream Proposer session would be given. Field names follow upstream where it has them
    (`spec.kind`, `spec.model`, `project`, `region`); `base_url`, `env`, `cwd` and `command` are the
    things upstream leaves implicit and this gate needs explicit."""
    kind: str
    model: str
    base_url: str | None = None   # the endpoint the operator declares the session will use
    env: Mapping[str, str] | None = None  # None: the session inherits the process environment
    cwd: str | None = None        # None: upstream makes a fresh temporary directory
    command: tuple[str, ...] | None = None
    project: str = ""
    region: str = ""


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reasons: tuple[str, ...]      # why it was refused; when allowed, why it was approved
    warnings: tuple[str, ...] = ()
    view: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def to_dict(self) -> dict:
        return {"allowed": self.allowed, "reasons": list(self.reasons), "warnings": list(self.warnings),
                "view": json.loads(json.dumps(dict(self.view)))}


# ── reading a configuration ───────────────────────────────────────────────────────

def _str_or_none(raw: Mapping[str, Any], key: str) -> str | None:
    v = raw.get(key)
    if v is not None and not isinstance(v, str):
        raise ProposerConfigError(f"{key} must be a string")
    return v


def parse_config(raw: Any) -> ProposerConfig:
    """A `ProposerConfig` from a config or a mapping, or raise `ProposerConfigError` (messages carry no values)."""
    if isinstance(raw, ProposerConfig):
        raw = {k: getattr(raw, k) for k in _FIELDS}
    if not isinstance(raw, Mapping):
        raise ProposerConfigError("a proposer configuration must be a mapping or a ProposerConfig")
    unknown = sorted(str(k) for k in raw if k not in _FIELDS)
    if unknown:
        raise ProposerConfigError(f"unknown field(s): {', '.join(unknown)}")

    kind = raw.get("kind")
    if not isinstance(kind, str):
        raise ProposerConfigError("kind must be a string")
    kind = kind.strip().lower()
    kind = _KIND_ALIASES.get(kind, kind)
    if kind not in KINDS:
        raise ProposerConfigError(f"kind is not one of {', '.join(KINDS)}")
    model = raw.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ProposerConfigError("model must be a non-empty string")

    env = raw.get("env")
    if env is not None:
        if not isinstance(env, Mapping) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
            raise ProposerConfigError("env must map strings to strings")
        env = MappingProxyType(dict(env))
    command = raw.get("command")
    if command is not None:
        if (isinstance(command, (str, bytes)) or not isinstance(command, (list, tuple)) or not command
                or not all(isinstance(a, str) for a in command)):
            raise ProposerConfigError("command must be a non-empty list of strings")
        command = tuple(command)
    project, region = raw.get("project") or "", raw.get("region") or ""
    if not isinstance(project, str) or not isinstance(region, str):
        raise ProposerConfigError("project and region must be strings")
    return ProposerConfig(kind=kind, model=model.strip(), base_url=_str_or_none(raw, "base_url"), env=env,
                          cwd=_str_or_none(raw, "cwd"), command=command, project=project, region=region)


def from_upstream(proposer: Any, *, base_url: str | None = None, cwd: str | None = None,
                  command: tuple[str, ...] | None = None, env: Mapping[str, str] | None = None) -> ProposerConfig:
    """Read an upstream `Proposer` (duck-typed: `.spec.kind`, `.spec.model`, `.project`, `.region`, `.env()`).

    Nothing is started: `Proposer.env()` only copies the process environment. Upstream carries no endpoint,
    so `base_url` is whatever the operator declares. Pass `env` to give the session a scrubbed environment
    instead of upstream's (which is the whole process environment).
    """
    try:
        spec = proposer.spec
        values = {"kind": spec.kind, "model": spec.model, "project": proposer.project or "",
                  "region": proposer.region or "", "base_url": base_url, "cwd": cwd, "command": command,
                  "env": dict(proposer.env()) if env is None else dict(env)}
    except Exception as e:  # noqa: BLE001 - anything unreadable is "not a Proposer", whatever it raised
        raise ProposerConfigError(f"not an upstream Proposer ({type(e).__name__})") from None
    return parse_config(values)


# ── pure helpers ──────────────────────────────────────────────────────────────────

def _is_secret_name(name: str) -> bool:
    up = name.upper()
    if up.endswith(_NON_SECRET_SUFFIXES):
        return False
    return up.startswith(_SECRET_PREFIXES) or bool(_SECRET_WORDS.search(up))


def _split_url(raw: Any) -> tuple[str | None, str | None, bool, str]:
    """(host, sanitised `scheme://host[:port]`, has userinfo, problem). No part of `raw` is returned beyond that."""
    if not isinstance(raw, str) or not raw.strip():
        return None, None, False, "no URL"
    try:
        p = urlparse(raw.strip())
        host, port = p.hostname, p.port
        # userinfo, a query or a fragment can each carry a credential; none is ever kept or shown
        creds = p.username is not None or p.password is not None or bool(p.query) or bool(p.fragment)
    except ValueError:
        return None, None, False, "not a valid URL"
    if p.scheme not in ("http", "https"):
        return None, None, False, "the scheme is not http or https"
    if not host:
        return None, None, creds, "no host"
    shown = f"[{host}]" if ":" in host else host
    return host, f"{p.scheme}://{shown}" + (f":{port}" if port else ""), creds, ""


def _bad_cwd(cwd: str) -> str | None:
    if not cwd.strip():
        return "is empty"
    if any(unicodedata.category(c) in ("Cc", "Cf", "Cs", "Co", "Cn") for c in cwd):
        return "contains control or invisible characters"
    if not os.path.isabs(cwd):
        return "is not an absolute path"
    path = PurePosixPath(os.path.normpath(cwd))
    home = PurePosixPath(os.path.normpath(os.path.expanduser("~")))
    if path == PurePosixPath("/") or path == home or path in home.parents:
        return "is the filesystem root, the home directory or one of its ancestors"
    return None


def _view_env(names: dict[str, list[str]], other: int, source: str) -> dict:
    return {"source": source, **{k: sorted(v) for k, v in names.items()}, "other": other}


# ── the gate ──────────────────────────────────────────────────────────────────────

def _evaluate(c: ProposerConfig, settings: Settings, zdr: str, classification: str | None,
              allowed_env: Collection[str], environ: Mapping[str, str] | None) -> Decision:
    reasons: list[str] = []
    ok: list[str] = []
    warnings: list[str] = []

    # CLI kind, from the declared kind and from the executable
    exe = os.path.basename(c.command[0]).lower() if c.command else None
    exe_kind = _EXE_KIND.get(exe) if exe else None
    if exe is not None and exe_kind is None:
        reasons.append(f"the executable {exe!r} is not a CLI this gate recognises (expected claude, bun or opencode)")
    elif exe_kind is not None and exe_kind != c.kind:
        reasons.append(f"the executable ({exe_kind}) does not match the declared kind ({c.kind})")
    is_claude = c.kind == "claude_cli" or exe_kind == "claude_cli"
    if is_claude:
        if not settings.allow_claude_cli_proposer:
            reasons.append(
                "a claude CLI proposer spends the user's Claude subscription quota and is refused unless "
                f"{ENV_ALLOW_CLAUDE_PROPOSER} names it (settings.allow_claude_cli_proposer)")
        else:
            warnings.append("a claude CLI proposer was explicitly allowed; it can spend Claude subscription quota")

    provider = "anthropic" if c.kind == "claude_cli" else (c.model.split("/", 1)[0].lower() if "/" in c.model else "")
    if c.kind == "opencode" and not provider:
        reasons.append("an opencode model id must be provider-prefixed (provider/model), so its provider is unknown")
    if c.kind == "claude_cli" and "/" in c.model:
        reasons.append("a claude_cli model id must be a bare Anthropic id, so its provider is unknown")

    # the endpoint
    host, shown, creds, problem = _split_url(c.base_url) if c.base_url is not None else (None, None, False, "")
    if c.base_url is None:
        reasons.append("the endpoint cannot be determined: upstream's Proposer has no endpoint field, "
                       "so base_url must be declared")
    elif problem:
        reasons.append(f"base_url is unusable ({problem}); the endpoint cannot be determined")
    if creds:
        reasons.append("base_url embeds credentials (userinfo, a query or a fragment); refused")

    # environment
    given = c.env is not None
    env = dict(c.env) if given else dict(os.environ if environ is None else environ)
    allow = frozenset(allowed_env)
    groups: dict[str, list[str]] = {"secret_like": [], "allowed": [], "endpoint_vars": []}
    vertex_env: list[str] = []
    other = 0
    for name, value in env.items():
        up = name.upper()
        if up.startswith(_VERTEX_ENV_PREFIXES) or up == "CLOUD_ML_REGION" or (
                up in _CLOUD_FLAGS and value.strip().lower() in _TRUTHY):
            vertex_env.append(name)
        if up in _PROXIES or up.endswith(_ENDPOINT_SUFFIXES):
            groups["endpoint_vars"].append(name)
            h, s, ucreds, why = _split_url(value)
            if why or ucreds or not _approved(h, zdr, s, c.model, classification, settings):
                reasons.append(f"environment variable {name} points the session at an endpoint that is not "
                               "loopback or zero-retention approved (or cannot be read)")
        elif name in allow:
            groups["allowed"].append(name)
        elif _is_secret_name(name):
            groups["secret_like"].append(name)
        else:
            other += 1
    if groups["secret_like"]:
        reasons.append(
            "the session would inherit credential-like environment variables that were not explicitly allowed: "
            f"{', '.join(sorted(groups['secret_like']))} (names only)"
            + ("" if given else "; no explicit env was given, so the whole process environment is inherited"))

    # Vertex and other hosted cloud routes
    evidence = []
    if "vertex" in provider:
        evidence.append("model provider")
    if host and host.endswith("googleapis.com"):
        evidence.append("endpoint host")
    if c.project:
        evidence.append("a Vertex project is configured")
    if vertex_env:
        evidence.append("environment " + ", ".join(sorted(vertex_env)))
    if evidence:
        reasons.append(f"Vertex AI or another hosted cloud route is refused ({'; '.join(evidence)})")

    # endpoint policy and provider consistency
    if shown is not None and not problem:
        try:
            ok.append(egress.check_endpoint(zdr=zdr, base_url=shown, model=c.model, classification=classification,
                                            zdr_endpoints=settings.zdr_endpoints))
        except egress.EgressDenied as e:
            reasons.append(f"egress refused: {e}")
        if host in egress.LOOPBACK and provider not in LOCAL_PROVIDERS:
            reasons.append(
                f"the model provider {provider or 'unknown'!r} is a hosted service; a loopback address does not make "
                "it local (a gateway on this machine can forward content off it)")

    # command line
    flags: list[str] = []
    if c.command:
        for arg in c.command[1:]:
            if arg.startswith("-"):
                flags.append(arg.split("=", 1)[0])
        bad = sorted({f for f in flags if _SECRET_WORDS.search(f.lstrip("-").replace("-", "_").upper())})
        if bad:
            reasons.append(f"the command line carries credential-like flags: {', '.join(bad)} (names only)")

    # working directory
    if c.cwd is None:
        cwd_view = "fresh temporary directory (upstream default)"
    else:
        cwd_view = c.cwd if not _bad_cwd(c.cwd) and len(c.cwd) < 512 else None
        if (why := _bad_cwd(c.cwd)) is not None:
            reasons.append(f"the working directory {why}")

    warnings.append("the session runs agent tools (shell and file access) on this host in its working directory; "
                    "this gate does not isolate it")
    warnings.append("HOME and any file-based credentials under it (gcloud ADC, ~/.claude, ~/.aws, opencode auth) are "
                    "visible to the session and cannot be listed here")

    view = {
        "kind": c.kind, "model": c.model, "provider": provider or None, "endpoint": shown,
        "cwd": cwd_view,
        "command": ({"executable": exe, "flags": sorted(set(flags))} if c.command else None),
        "env": _view_env(groups, other, "explicit" if given else "process environment"),
        "claude_cli_allowed": bool(settings.allow_claude_cli_proposer),
    }
    return Decision(not reasons, tuple(reasons or ok or ["approved"]), tuple(warnings), MappingProxyType(view))


def _approved(host: str | None, zdr: str, shown: str | None, model: str, classification: str | None,
              settings: Settings) -> bool:
    if host is None or shown is None:
        return False
    try:
        egress.check_endpoint(zdr=zdr, base_url=shown, model=model, classification=classification,
                              zdr_endpoints=settings.zdr_endpoints)
    except egress.EgressDenied:
        return False
    return True


def check_proposer(config: Any, settings: Settings, *, zdr: str = "strict", classification: str | None = None,
                   allowed_env: Collection[str] = frozenset(), environ: Mapping[str, str] | None = None) -> Decision:
    """Decide whether a Proposer session may be created at all. Never raises; never starts anything.

    `config` is a `ProposerConfig`, a mapping with its fields, or an upstream `Proposer` (read with
    `from_upstream`, with no declared endpoint). `environ` stands in for the process environment when
    the config leaves `env` unset.
    """
    try:
        if not isinstance(config, (ProposerConfig, Mapping)) and hasattr(config, "spec") and callable(getattr(config, "env", None)):
            parsed = from_upstream(config)
        else:
            parsed = parse_config(config)
    except ProposerConfigError as e:
        return Decision(False, (f"unparseable proposer configuration: {e}",), (),
                        MappingProxyType({"kind": None, "endpoint": None}))
    try:
        return _evaluate(parsed, settings, zdr, classification, allowed_env, environ)
    except Exception as e:  # noqa: BLE001 - a gate that fails open is worse than one that over-refuses
        return Decision(False, (f"internal error while checking the proposer ({type(e).__name__}); refused",), (),
                        MappingProxyType({"kind": parsed.kind, "endpoint": None}))
