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
the operator declares, environment, working directory, command line) plus the channel profile
(`channels.py`) and the NAME of the channel the sweep wants, and returns a `Decision` without
starting anything. It is default-deny:

  * the channel must be DECLARED in the profile, and the Proposer configuration must match it
    exactly on kind, provider, endpoint host and port, and model; an undeclared channel is denied;
  * the channel's retention must satisfy the zdr mode (`strict`: only local or zdr-attested) and
    the sweep's classification must be within the channel's ceiling (`unknown` retention carries
    only `open`). ZDR is the user's declaration, never verified here;
  * the endpoint must be DECLARED (it cannot be read off the CLI) and a `local` channel must use a
    local provider (ollama, lmstudio, ...), because a loopback gateway can forward to a hosted model;
  * Vertex, Bedrock and any other hosted cloud route are refused;
  * the command line must be exactly the expected argv for the kind (`COMMAND_SHAPES`: the executable, an
    allow-list of flags, the expected positionals); any other flag or endpoint-looking value is refused;
  * credential-LOOKING environment variable names are refused unless listed in `allowed_env`;
    endpoint variables (`*_BASE_URL`) must point at one of the channel's declared endpoints, and ANY proxy
    variable (HTTP_PROXY, NO_PROXY, no_proxy, ...) is refused outright because the runner sets those itself.

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
from types import MappingProxyType
from typing import Any
from urllib.parse import urlparse

from . import channels
from .channels import Channel, ChannelProfile

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
_PROXY_WORD = "PROXY"   # any variable whose name contains it: HTTP_PROXY, no_proxy, SOCKS_PROXY, GIT_PROXY_COMMAND ...
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


def _cwd_problem(cwd: str, profile: Any) -> str | None:
    """Why `cwd` cannot be a Proposer working directory, or None. Reads the filesystem (stat only).

    The text rules (absolute, no control characters, not the root, home or an ancestor, and the protected-path
    rules of `invariants.assert_path_unprotected` on the normalised path) apply to the path as given AND to what
    it resolves to through symlinks; and the resolved directory must sit under a workspace root declared in the
    profile. Undeclared roots mean no explicit working directory is accepted.
    """
    if (why := channels.bad_directory(cwd)) is not None:
        return why
    roots = profile.workspace_roots if isinstance(profile, ChannelProfile) else ()
    if not roots:
        return "cannot be accepted: the channel profile declares no workspace_roots"
    try:
        real = os.path.realpath(cwd, strict=True)
    except (OSError, ValueError):
        return "does not exist or cannot be resolved"
    if not os.path.isdir(real):
        return "is not a directory"
    if (why := channels.bad_directory(real)) is not None:
        return f"resolves to {ascii(real[:120])}, which {why}"
    for root in roots:
        try:
            resolved = os.path.realpath(root, strict=True)
        except (OSError, ValueError):
            continue
        if os.path.commonpath([real, resolved]) == resolved:
            return None
    return "resolves outside every declared workspace root"


# ── the command line: ALLOW-LISTED per kind ───────────────────────────────────────
# Scanning a command for bad words is not enough: a flag such as `--api-base=...`, `--proxy ...` or
# `--endpoint ...` can outrank the environment. So the command must be exactly the expected argv for the
# kind: the executable, then only the flags below (each at most once, values checked), then exactly the
# positionals below. Anything else is refused. Value kinds: None (the flag takes no value), json (the
# literal `json`), word (a short lowercase word), path (an absolute path that is not a URL), cwd (the
# configuration's own working directory), model (the configuration's own model id), text (free text: it may
# not be a flag, a response file, a control string, or ITSELF a URL, host:port or `KEY=value` setting).
# This list is the policy, so widening it is a deliberate edit here and nowhere else.

@dataclass(frozen=True)
class CommandShape:
    executables: tuple[str, ...]
    flags: Mapping[str, str | None]
    positionals: Mapping[str, tuple[str, ...]]   # executable -> expected positionals; `<prompt>` is free text


COMMAND_SHAPES: Mapping[str, CommandShape] = MappingProxyType({
    # `claude --dangerously-skip-permissions -p --output-format json --effort E [--append-system-prompt S]`
    "claude_cli": CommandShape(
        executables=("claude", "claude-code"),
        flags=MappingProxyType({"--dangerously-skip-permissions": None, "-p": None, "--output-format": "json",
                                "--effort": "word", "--append-system-prompt": "text"}),
        positionals=MappingProxyType({"claude": (), "claude-code": ()}),
    ),
    # `bun run --cwd C --conditions=browser src/index.ts run PROMPT --dir D --model M --format json [--variant V]`
    "opencode": CommandShape(
        executables=("bun", "opencode"),
        flags=MappingProxyType({"--cwd": "path", "--conditions=browser": None, "--dir": "cwd", "--model": "model",
                                "--format": "json", "--variant": "word"}),
        positionals=MappingProxyType({"bun": ("run", "src/index.ts", "run", "<prompt>"),
                                      "opencode": ("run", "<prompt>")}),
    ),
})

_WORD = re.compile(r"[a-z0-9_-]{1,16}")
_SETTINGLIKE = re.compile(
    r"(?i)^[a-z][a-z0-9+.-]*://"                                         # a URL
    r"|^[^\s=]*(base|endpoint|proxy|gateway|server|host|url|api)[^\s=]*="  # an endpoint setting, KEY=value
    r"|^[\w.\-\[\]:]+:\d{2,5}(/|$)")                                      # host:port
_DASHES = "−"                                                         # MINUS SIGN is not category Pd


def _dashlike(token: str) -> bool:
    t = token.lstrip()
    t = unicodedata.normalize("NFKC", t)
    return bool(t) and (t[0] == "-" or unicodedata.category(t[0]) == "Pd" or t[0] in _DASHES)


def _flag_name(token: str) -> str:
    """A flag token as shown in a reason: the part before `=`, escaped, truncated. Never a value."""
    return ascii(token.split("=", 1)[0][:40])


def _value_problem(kind: str | None, value: str, c: ProposerConfig) -> str | None:
    if _dashlike(value) or value.lstrip().startswith("@"):
        return "its value is a flag or a response file"
    if any(unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Co", "Cn") for ch in value):
        return "its value contains control or invisible characters"
    if len(value) > (4096 if kind == "text" else 512):
        return "its value is too long"
    if _SETTINGLIKE.match(value.strip()):
        return "its value looks like a URL, host or endpoint setting"
    if kind == "json" and value != "json":
        return "its value must be json"
    if kind == "word" and not _WORD.fullmatch(value):
        return "its value must be a short lowercase word"
    if kind == "path" and not os.path.isabs(value):
        return "its value must be an absolute path"
    if kind == "cwd" and (c.cwd is None or value != c.cwd):
        return "its value must equal the configuration's working directory"
    if kind == "model" and value != c.model:
        return "its value must equal the configuration's model"
    return None


def _command_problems(c: ProposerConfig, exe: str | None) -> list[str]:
    """Why the command is not the expected argv for the kind. Names flags (escaped, truncated), never values."""
    assert c.command
    shape = COMMAND_SHAPES.get(c.kind)
    if shape is None:
        return ["no command shape is declared for this kind"]
    problems: list[str] = []
    args = c.command[1:]
    seen: set[str] = set()
    positionals: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        i += 1
        if any(unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Co", "Cn") for ch in a):
            problems.append("an argument contains control or invisible characters")
            continue
        if a == "--" or a.strip() == "--":
            problems.append("the `--` end-of-options marker is refused (flags after it would be smuggled)")
            continue
        if a.lstrip().startswith("@"):
            problems.append("a response file (@file) argument is refused")
            continue
        if _dashlike(a):
            if a not in shape.flags:
                problems.append(f"the flag {_flag_name(a)} is not on the allow-list for {c.kind}")
                continue
            if a in seen:
                problems.append(f"the flag {_flag_name(a)} is repeated")
                continue
            seen.add(a)
            vkind = shape.flags[a]
            if vkind is None:
                continue
            if i >= len(args):
                problems.append(f"the flag {_flag_name(a)} is missing its value")
                continue
            why = _value_problem(vkind, args[i], c)
            i += 1
            if why:
                problems.append(f"the flag {_flag_name(a)}: {why}")
            continue
        positionals.append(a)
    expected = shape.positionals.get(exe or "")
    if expected is None:
        pass                                                   # an unrecognised executable is reported elsewhere
    elif len(positionals) != len(expected):
        problems.append(f"expected {len(expected)} positional argument(s), found {len(positionals)}")
    else:
        for got, want in zip(positionals, expected):
            if want == "<prompt>":
                why = _value_problem("text", got, c)
                if why:
                    problems.append(f"the prompt argument: {why}")
            elif got != want:
                problems.append("a positional argument is not the expected one")
    return problems

def _endpoint_key(raw: Any) -> tuple[str, int] | None:
    return channels.endpoint_key(raw) if isinstance(raw, str) else None


def _declared(profile: Any, name: Any) -> tuple[Channel | None, str]:
    """(the declared channel, '') or (None, why not). The message points at the profile file."""
    if not isinstance(profile, ChannelProfile):
        return None, "rule undeclared-channel: no channel profile was given, so no channel is declared"
    if not profile.declared:
        return None, (f"rule undeclared-channel: no channel profile exists at {profile.source}, so no channel is "
                      f"declared; {channels.HOWTO}")
    if not isinstance(name, str) or name not in profile.channels:
        shown = ascii(str(name)[:40])
        declared = ", ".join(sorted(profile.channels)) or "none"
        return None, (f"rule undeclared-channel: channel {shown} is not declared in {profile.source} "
                      f"(declared: {declared}); add it there, {channels.HOWTO}")
    return profile.channels[name], ""


def _view_env(names: dict[str, list[str]], other: int, source: str) -> dict:
    return {"source": source, **{k: sorted(v) for k, v in names.items()}, "other": other}


# ── the gate ──────────────────────────────────────────────────────────────────────

def _evaluate(c: ProposerConfig, profile: Any, channel_name: Any, zdr: str, classification: str | None,
              allowed_env: Collection[str], environ: Mapping[str, str] | None) -> Decision:
    reasons: list[str] = []
    ok: list[str] = []
    warnings: list[str] = []
    cls = channels.effective_classification(classification)

    # the declared channel: no declaration, no session
    ch, why = _declared(profile, channel_name)
    if ch is None:
        reasons.append(why)
    else:
        warnings.extend(ch.warnings)
    cname = ch.name if ch else None

    def refuse(rule: str, text: str) -> None:
        reasons.append(f"channel {cname!r}: rule {rule}: {text}" if cname else f"rule {rule}: {text}")

    # CLI kind, from the declared kind and from the executable
    exe = os.path.basename(c.command[0]).lower() if c.command else None
    exe_kind = _EXE_KIND.get(exe) if exe else None
    if exe is not None and exe_kind is None:
        refuse("command-shape", f"the executable {exe!r} is not a CLI this gate recognises (expected claude, bun or opencode)")
    elif exe_kind is not None and exe_kind != c.kind:
        refuse("command-shape", f"the executable ({exe_kind}) does not match the declared kind ({c.kind})")
    if c.kind == "claude_cli" or exe_kind == "claude_cli":
        warnings.append("a claude CLI proposer can spend the user's Claude subscription quota")

    provider = "anthropic" if c.kind == "claude_cli" else (c.model.split("/", 1)[0].lower() if "/" in c.model else "")
    if c.kind == "opencode" and not provider:
        refuse("provider", "an opencode model id must be provider-prefixed (provider/model), so its provider is unknown")
    if c.kind == "claude_cli" and "/" in c.model:
        refuse("provider", "a claude_cli model id must be a bare Anthropic id, so its provider is unknown")

    # the endpoint
    host, shown, creds, problem = _split_url(c.base_url) if c.base_url is not None else (None, None, False, "")
    key = _endpoint_key(c.base_url) if host and not problem else None
    if c.base_url is None:
        refuse("endpoint", "the endpoint cannot be determined: upstream's Proposer has no endpoint field, "
                           "so base_url must be declared")
    elif problem:
        refuse("endpoint", f"base_url is unusable ({problem}); the endpoint cannot be determined")
    if creds:
        refuse("endpoint", "base_url embeds credentials (userinfo, a query or a fragment); refused")

    # environment
    given = c.env is not None
    env = dict(c.env) if given else dict(os.environ if environ is None else environ)
    allow = frozenset(allowed_env)
    groups: dict[str, list[str]] = {"secret_like": [], "allowed": [], "endpoint_vars": []}
    vertex_env: list[str] = []
    proxy_env: list[str] = []
    other = 0
    for name, value in env.items():
        up = name.upper()
        if up.startswith(_VERTEX_ENV_PREFIXES) or up == "CLOUD_ML_REGION" or (
                up in _CLOUD_FLAGS and value.strip().lower() in _TRUTHY):
            vertex_env.append(name)
        if _PROXY_WORD in up:
            proxy_env.append(name)
        elif up.endswith(_ENDPOINT_SUFFIXES):
            groups["endpoint_vars"].append(name)
            k = _endpoint_key(value)
            h_ok = k is not None and ch is not None and k in ch.endpoint_keys
            if not h_ok or _split_url(value)[2]:
                refuse("endpoint", f"environment variable {name} points the session at an endpoint that is not one "
                                   "of the channel's declared endpoints (or cannot be read)")
        elif name in allow:
            groups["allowed"].append(name)
        elif _is_secret_name(name):
            groups["secret_like"].append(name)
        else:
            other += 1
    if proxy_env:
        # The container runner (slice 3b) sets the proxy variables itself, and NO_PROXY can bypass them, so no
        # proxy-ish variable may arrive in the configuration, whatever it points at and even if listed in allowed_env.
        refuse("proxy-env", f"proxy variables are refused outright: {", ".join(sorted(proxy_env))} (names only; "
                            "proxy settings are set by the runner, never by the configuration)")
    if groups["secret_like"]:
        refuse("credential-env",
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
        refuse("vertex", f"Vertex AI or another hosted cloud route is refused ({'; '.join(evidence)})")

    # the Proposer configuration must match the declared channel exactly
    if ch is not None:
        if c.kind != ch.kind:
            refuse("kind-mismatch", f"the configuration is {c.kind}, the channel is declared as {ch.kind}")
        if provider != ch.provider:
            refuse("provider-mismatch", f"the configuration's provider is {provider or 'unknown'!r}, "
                                        f"the channel is declared for {ch.provider!r}")
        if key is not None and key not in ch.endpoint_keys:
            refuse("endpoint-mismatch", f"the configuration's endpoint {key[0]}:{key[1]} is not one of the channel's "
                                        f"declared endpoints ({', '.join(sorted(f'{h}:{p}' for h, p in ch.endpoint_keys))})")
        if c.model != ch.model:
            refuse("model-mismatch", f"the configuration's model {c.model!r} is not the channel's declared model "
                                     f"{ch.model!r}")
        if ch.retention == "local" and provider not in LOCAL_PROVIDERS:
            refuse("local-provider", f"the model provider {provider or 'unknown'!r} is a hosted service; "
                                     "retention=local needs a local provider (a gateway on this machine can forward "
                                     "content off it)")
        verdict = channels.decide(ch, zdr=zdr, classification=classification)
        reasons.extend(verdict.reasons)
        if verdict.allowed:
            ok.append(f"channel {ch.name!r} allowed: retention={ch.retention} (declared, not verified), zdr={zdr}, "
                      f"{cls} content within max_classification={ch.max_classification}")

    # command line
    flags: list[str] = []
    if c.command:
        for arg in c.command[1:]:
            if _dashlike(arg):
                name = arg.split("=", 1)[0].strip()
                flags.append(name if name.isascii() and name.isprintable() and len(name) <= 40 else "<unprintable flag>")
        bad = sorted({f for f in flags if _SECRET_WORDS.search(f.lstrip("-").replace("-", "_").upper())})
        if bad:
            refuse("command-shape", f"the command line carries credential-like flags: {', '.join(bad)} (names only)")
        for problem in _command_problems(c, exe):
            refuse("command-shape", f"the command line is not the expected argv for {c.kind}: {problem}")

    # working directory
    if c.cwd is None:
        cwd_view = "fresh temporary directory (upstream default)"
    else:
        why = _cwd_problem(c.cwd, profile)
        cwd_view = c.cwd if why is None and len(c.cwd) < 512 else None
        if why is not None:
            refuse("working-directory", f"the working directory {why}")

    warnings.append("the session runs agent tools (shell and file access) on this host in its working directory; "
                    "this gate does not isolate it")
    warnings.append("HOME and any file-based credentials under it (gcloud ADC, ~/.claude, ~/.aws, opencode auth) are "
                    "visible to the session and cannot be listed here")

    view = {
        "kind": c.kind, "model": c.model, "provider": provider or None, "endpoint": shown,
        "cwd": cwd_view,
        "command": ({"executable": exe, "flags": sorted(set(flags))} if c.command else None),
        "env": _view_env(groups, other, "explicit" if given else "process environment"),
        "channel": cname, "retention": ch.retention if ch else None, "zdr": zdr, "classification": cls,
        "max_classification": ch.max_classification if ch else None,
        "caps": ch.caps.to_dict() if ch else None, "approval": ch.approval if ch else None,
    }
    return Decision(not reasons, tuple(reasons or ok or ["approved"]), tuple(warnings), MappingProxyType(view))


def check_proposer(config: Any, *, profile: Any, channel: Any, zdr: str = "strict", classification: str | None = None,
                   allowed_env: Collection[str] = frozenset(), environ: Mapping[str, str] | None = None) -> Decision:
    """Decide whether a Proposer session may be created at all. Never raises; never starts anything.

    `config` is a `ProposerConfig`, a mapping with its fields, or an upstream `Proposer` (read with
    `from_upstream`, with no declared endpoint). `profile` is the loaded `ChannelProfile` and `channel` the
    name of the channel in it. `classification` is the SWEEP's (the strictest of its sources, see
    `channels.sweep_classification`); missing or unknown means restricted. `environ` stands in for the
    process environment when the config leaves `env` unset.
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
        return _evaluate(parsed, profile, channel, zdr, classification, allowed_env, environ)
    except Exception as e:  # noqa: BLE001 - a gate that fails open is worse than one that over-refuses
        return Decision(False, (f"internal error while checking the proposer ({type(e).__name__}); refused",), (),
                        MappingProxyType({"kind": parsed.kind, "endpoint": None}))
