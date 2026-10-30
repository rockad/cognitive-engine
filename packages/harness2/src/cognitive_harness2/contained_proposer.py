"""Upstream's `Proposer`, bound to contained sessions (slice 3b, WS-C).

Upstream's `Proposer.call` makes a temporary directory, copies the evidence into `context/`, runs ONE agent
CLI session in it through `self._claude_cli` or `self._opencode`, and (with `harvest=`) copies an edited
subtree back. Its sessions run on the host, inherit the whole process environment and force Vertex AI.
`ContainedProposer` keeps upstream's orchestration and replaces what a session IS:

  * `env()` never reads `os.environ` and sets no Vertex variable. It is the small environment of the
    CONTAINER (the runner sets HOME, PATH and the proxy variables itself);
  * `_claude_cli` and `_opencode` (upstream's two dispatch targets) both run `_contained`, which routes on the
    REAL kind of the channel (`opencode`, `claude_cli` or `agy_cli`). Upstream's `ChannelSpec` accepts only its
    own two kinds, so an `agy_cli` channel is carried by an `opencode`-kind spec and `real_kind` says what it is;
  * a session is built as an exact argv, checked by `upstream_egress.check_proposer` (a refusal starts nothing),
    and run ONLY through `ProposerSessionRunner` (two containers, one `--internal` network, proxy allow-list);
    the container is the fence, not anything in the CLI's own flags;
  * the model gate (`model_gate.GATE`) is held around every session, so a single GPU serves one call at a time;
  * the workspace the container sees is a private copy under `scratch_root`; what comes back is inspected first
    (`workspace_harvest`) and ANY finding, symlink or refused file fails the session (nothing is copied back).
    Only inspected regular files are copied into upstream's temporary directory, from where upstream harvests;
  * stdout is parsed with upstream's own parsers (`_parse_json`, `_opencode_stdout`, `_opencode_cost`); the reply is
    the `(text, cost, err)` tuple `Proposer.call` expects, with cost 0.0 for a local channel.

Deliberate deviations from upstream defaults (also in the README):

  * `attempts=1`, and `recursion.STAGE_ATTEMPTS` / `recursion.TRANSPORT_ATTEMPTS` (and `config.TRANSPORT_ATTEMPTS`)
    are set to 1 by `apply_overrides`, once, when the class is built. A failed local session is not retried blind;
  * calls are serialised by the model gate (upstream allows 2 at a time through its own semaphore);
  * a session is bounded by `SessionConfig.timeout_s` (600 s) as well as upstream's `timeout=`.

Nothing here imports upstream at module import time: `contained_proposer_class(upstream)` takes the result of
`upstream_import.import_upstream`, so the import stays deliberate and verified.
"""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import channels, upstream_egress, workspace_harvest
from .image import ImageRef
from .model_gate import GATE, ModelGate
from .sandbox import VerifiedDocker
from .session_runner import (HOME, Credential, ProposerSessionRunner, SessionLimits, SessionResult, SessionSpec,
                             container_cli_path)
from .upstream_import import Upstream

CLI_NAMES = {"opencode": "opencode", "claude_cli": "claude", "agy_cli": "agy"}   # check_proposer reads the kind off this name
CARRIER_KIND = {"opencode": "opencode", "claude_cli": "claude_cli", "agy_cli": "opencode"}   # upstream ChannelSpec kinds
OPENCODE_PROMPT = ("Read MISSION.md first. Do what it asks. Reply with the requested output as "
                   "your final message AND save it verbatim to output/answer.md.")
AGY_PROMPT = OPENCODE_PROMPT
CLAUDE_SYSTEM_POINTER = "Follow the SYSTEM INSTRUCTIONS block at the top of the message."
# Per kind: does the CLI unpack native code under HOME or /tmp (needs exec there)? UNCONFIRMED for all three
# until the first real session; opencode (a bun-compiled binary) is the likeliest, so it gets it.
TMP_EXEC = {"opencode": True, "claude_cli": False, "agy_cli": False}
# opencode writes a sqlite database, a models cache and config under XDG dirs; they live in the session's tmpfs HOME.
OPENCODE_XDG = {"XDG_DATA_HOME": f"{HOME}/.local/share", "XDG_STATE_HOME": f"{HOME}/.local/state",
                "XDG_CACHE_HOME": f"{HOME}/.cache", "XDG_CONFIG_HOME": f"{HOME}/.config"}
DEFAULT_HARVEST = workspace_harvest.HarvestLimits(max_files=5000, max_file_bytes=4 * 1024 * 1024,
                                                   max_total_bytes=64 * 1024 * 1024)
ATTEMPT_OVERRIDES = (("recursion", "STAGE_ATTEMPTS"), ("recursion", "TRANSPORT_ATTEMPTS"), ("config", "TRANSPORT_ATTEMPTS"))


class ContainedProposerError(RuntimeError):
    """The binding is misconfigured, or upstream no longer looks like the pinned commit."""


class ProposerRefused(RuntimeError):
    """`check_proposer` refused the session; nothing was started. The reasons never carry argument values."""


class WorkspaceRefused(RuntimeError):
    """The session's workspace failed inspection; nothing was copied back."""


# ── pure builders (no upstream, no docker) ────────────────────────────────────────

def build_command(kind: str, *, cli_container: str, model: str, effort: str = "high",
                  prompt: str = OPENCODE_PROMPT, max_budget_usd: float | None = None) -> tuple[str, ...]:
    """The exact argv for a kind. Each one is an instance of `upstream_egress.COMMAND_SHAPES[kind]`."""
    if kind == "opencode":
        return (cli_container, "run", "--standalone", "--auto", "--format", "json", "--model", model, prompt)
    if kind == "claude_cli":
        argv = [cli_container, "-p", "--output-format", "json", "--permission-mode", "bypassPermissions",
                "--no-session-persistence", "--model", model, "--effort", effort,
                "--append-system-prompt", CLAUDE_SYSTEM_POINTER]
        if max_budget_usd is not None:
            argv += ["--max-budget-usd", f"{max_budget_usd:g}"]
        return tuple(argv)
    if kind == "agy_cli":
        argv = [cli_container, f"--print={prompt}", "--dangerously-skip-permissions", f"--model={model}"]
        if effort == "low":
            argv.append("--effort=low")
        return tuple(argv)
    raise ContainedProposerError(f"unknown kind {kind!r}")


def opencode_config(model: str, base_url: str) -> str:
    """OPENCODE_CONFIG_CONTENT: an OpenAI-compatible provider whose base URL is the one endpoint the proxy allows.

    ⚠️ The exact schema is UNCONFIRMED against opencode v2.0.25 until the first real call."""
    provider, _, model_key = model.partition("/")
    return json.dumps({
        "provider": {provider: {"npm": "@ai-sdk/openai-compatible", "name": provider, "options": {"baseURL": base_url},
                                "models": {model_key: {"name": model_key}}}},
        "autoupdate": False, "share": "disabled"}, separators=(",", ":"))


def session_env(kind: str, *, model: str, base_url: str | None) -> dict[str, str]:
    """The container environment beyond what the runner fixes (HOME, PATH, proxies...). Never read from os.environ."""
    if kind == "opencode":
        if not base_url:
            raise ContainedProposerError("an opencode session needs the base URL of its provider")
        return {"OPENCODE_CONFIG_CONTENT": opencode_config(model, base_url), "OPENCODE_DISABLE_MODELS_FETCH": "1",
                "OPENCODE_DISABLE_AUTOUPDATE": "1", **OPENCODE_XDG}
    if kind == "claude_cli":
        return {"DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
    if kind == "agy_cli":
        return {}
    raise ContainedProposerError(f"unknown kind {kind!r}")


def _open_up(root: str) -> None:
    """The container user is not the host user: make the copied tree writable by it (links are left alone)."""
    for base, dirs, files in os.walk(root, followlinks=False):
        for d in [base, *(os.path.join(base, x) for x in dirs)]:
            if not os.path.islink(d):
                os.chmod(d, 0o777)
        for f in files:
            p = os.path.join(base, f)
            if not os.path.islink(p):
                os.chmod(p, (os.lstat(p).st_mode & 0o777) | 0o666)


def apply_overrides(up: Upstream, *, stage_attempts: int = 1, transport_attempts: int = 1) -> dict[str, int]:
    """Set upstream's retry constants. Explicit, documented, and loud when upstream renamed one."""
    applied: dict[str, int] = {}
    for modname, name in ATTEMPT_OVERRIDES:
        mod = getattr(up, modname)
        if not hasattr(mod, name):
            raise ContainedProposerError(
                f"upstream {mod.__name__} no longer has {name} (pinned commit {up.commit[:12]}); "
                "the attempt override cannot be applied, so retries would silently stay at upstream's default")
        value = stage_attempts if name == "STAGE_ATTEMPTS" else transport_attempts
        setattr(mod, name, value)
        applied[f"{modname}.{name}"] = value
    return applied


# ── configuration ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SessionConfig:
    verified: VerifiedDocker
    image: ImageRef
    profile: channels.ChannelProfile
    channel: str                           # the name in the profile
    zdr: str
    classification: str | None
    scratch_root: str                      # a directory this process owns, inside a profile workspace root
    cli_paths: Mapping[str, str]           # kind -> host path of the CLI, named opencode / claude / agy
    credentials: Mapping[str, Credential] = field(default_factory=dict, repr=False)
    egress_network: str | None = None
    timeout_s: float = 600.0
    api_path: str = "/v1"                  # appended to the provider endpoint for an opencode base URL
    limits: SessionLimits = SessionLimits()
    harvest_limits: workspace_harvest.HarvestLimits = DEFAULT_HARVEST
    gate: ModelGate = GATE
    gate_wait_s: float | None = None       # None: four times the session's wall clock
    max_budget_usd: float | None = None    # claude only
    test_allow_cidrs: tuple[str, ...] = ()   # TESTS ONLY: let the proxy reach a throwaway upstream

    def __post_init__(self) -> None:
        ch = self.profile.channels.get(self.channel)
        if ch is None:
            raise ContainedProposerError(f"channel {self.channel!r} is not declared in the profile {self.profile.source}")
        root = Path(self.scratch_root)
        if not root.is_absolute() or not root.is_dir() or os.path.realpath(root) != str(root):
            raise ContainedProposerError("scratch_root must be an existing, absolute, symlink-free directory")
        if not any(str(root) == r.rstrip("/") or str(root).startswith(r.rstrip("/") + "/") for r in self.profile.workspace_roots):
            raise ContainedProposerError("scratch_root is not under a workspace_roots entry of the channel profile")
        cli = self.cli_paths.get(ch.kind)
        if not cli or os.path.basename(cli) != CLI_NAMES[ch.kind]:
            raise ContainedProposerError(f"cli_paths needs a path named {CLI_NAMES[ch.kind]!r} for kind {ch.kind}")
        if not (isinstance(self.timeout_s, (int, float)) and self.timeout_s > 0):
            raise ContainedProposerError("timeout_s must be above 0")

    @property
    def channel_obj(self) -> channels.Channel:
        return self.profile.channels[self.channel]


# ── the class ─────────────────────────────────────────────────────────────────────

_CLASSES: dict[int, tuple[type, Any]] = {}
_LOCK = threading.Lock()


def contained_proposer_class(up: Upstream) -> type:
    """The `ContainedProposer` class for this upstream (built once; applies the attempt overrides on first build)."""
    with _LOCK:
        hit = _CLASSES.get(id(up.proposer))
        if hit is not None and hit[1] is up.proposer:
            return hit[0]
        apply_overrides(up)
        cls = _build_class(up)
        _CLASSES[id(up.proposer)] = (cls, up.proposer)
        return cls


def _build_class(up: Upstream) -> type:
    Base = up.proposer.Proposer

    @dataclass
    class ContainedProposer(Base):    # type: ignore[valid-type, misc]
        session: SessionConfig | None = None
        real_kind: str = ""

        def __post_init__(self) -> None:
            super().__post_init__()
            s = self.session
            if s is None:
                raise ContainedProposerError("a ContainedProposer needs a SessionConfig")
            if self.real_kind != s.channel_obj.kind:
                raise ContainedProposerError(f"real_kind {self.real_kind!r} is not the kind of channel {s.channel!r} "
                                             f"({s.channel_obj.kind})")
            if self.spec.kind != CARRIER_KIND[self.real_kind]:
                raise ContainedProposerError(f"a {self.real_kind} channel is carried by a {CARRIER_KIND[self.real_kind]} "
                                             f"ChannelSpec, not {self.spec.kind}")
            if self.spec.model != s.channel_obj.model:
                raise ContainedProposerError("the ChannelSpec's model is not the declared channel's model")

        # ── the environment and the runner ────────────────────────────────────

        def _runner(self, decision: upstream_egress.Decision | None) -> ProposerSessionRunner:
            s = self.session
            assert s is not None
            return ProposerSessionRunner(s.verified, s.image, s.channel_obj, zdr=s.zdr, classification=s.classification,
                                         proposer_decision=decision, workspace_roots=s.profile.workspace_roots,
                                         egress_network=s.egress_network, _test_allow_cidrs=s.test_allow_cidrs)

        def _base_url(self) -> str | None:
            if self.real_kind != "opencode":
                return None
            s = self.session
            assert s is not None
            local = self._runner(None).local_endpoint
            return (local or s.channel_obj.endpoints[0]) + s.api_path

        def env(self) -> dict[str, str]:       # replaces upstream: no os.environ, no Vertex
            return session_env(self.real_kind, model=self.spec.model, base_url=self._base_url())

        # ── upstream's two dispatch targets ───────────────────────────────────

        def _claude_cli(self, cwd: Path, mission: str, system: str, wall: int) -> tuple[str, float, str]:
            return self._contained(cwd, mission, system, wall)

        def _opencode(self, cwd: Path, mission: str, system: str, wall: int) -> tuple[str, float, str]:
            return self._contained(cwd, mission, system, wall)

        # ── one contained session ─────────────────────────────────────────────

        def _contained(self, cwd: Path, mission: str, system: str, wall: int) -> tuple[str, float, str]:
            s, kind = self.session, self.real_kind
            assert s is not None
            ch = s.channel_obj
            eff = min(float(wall), float(s.timeout_s), float(ch.caps.wall_s))
            cli_host = s.cli_paths[kind]
            cli_container = container_cli_path(cli_host)
            ws = Path(tempfile.mkdtemp(prefix="s-", dir=s.scratch_root))
            try:
                os.chmod(ws, 0o777)
                for child in Path(cwd).iterdir():
                    dst = ws / child.name
                    if child.is_dir() and not child.is_symlink():
                        shutil.copytree(child, dst, symlinks=True)
                    else:
                        shutil.copy2(child, dst, follow_symlinks=False)
                prompt_text = f"{system}\n\n---\n\n{mission}" if system else mission
                stdin: bytes | None = None
                if kind == "claude_cli":
                    stdin = (f"# SYSTEM INSTRUCTIONS\n\n{system}\n\n# END\n\n{mission}" if system else mission).encode()
                else:
                    (ws / "MISSION.md").write_text(prompt_text, encoding="utf-8")
                    (ws / "output").mkdir(exist_ok=True)
                    if kind == "opencode":
                        (ws / "opencode.json").write_text(json.dumps({"agent": {"build": {"steps": self.spec.steps}}}),
                                                          encoding="utf-8")
                _open_up(str(ws))
                argv = build_command(kind, cli_container=cli_container, model=self.spec.model, effort=self.spec.effort,
                                     max_budget_usd=s.max_budget_usd)
                env = self.env()
                decision = upstream_egress.check_proposer(
                    upstream_egress.ProposerConfig(kind=kind, model=self.spec.model, base_url=ch.endpoints[0], env=env,
                                                   cwd=str(ws), command=argv),
                    profile=s.profile, channel=s.channel, zdr=s.zdr, classification=s.classification)
                if not decision.allowed:
                    raise ProposerRefused("the proposer session was refused before anything started: "
                                          + "; ".join(decision.reasons))
                spec = SessionSpec(argv=argv, workspace=str(ws), cli=cli_host, timeout_s=eff, env=env,
                                   credential=s.credentials.get(kind), stdin=stdin, limits=s.limits,
                                   tmp_exec=TMP_EXEC[kind], harvest=s.harvest_limits)
                runner = self._runner(decision)
                with s.gate.hold(s.gate_wait_s if s.gate_wait_s is not None else 4 * eff, label=f"proposer:{kind}"):
                    result = runner.run(spec)
                if result.timed_out:
                    raise subprocess.TimeoutExpired(cli_container, eff)
                report = result.harvest
                if report is not None and (not report.ok or report.links):
                    rules = sorted({f.rule for f in report.findings} | ({"symlink"} if report.links else set()))
                    raise WorkspaceRefused("the session workspace failed inspection (" + ", ".join(rules)
                                           + "); nothing was copied back")
                text, cost, err = self._parse(result, ws, report)
                if text.strip():
                    self._bring_back(ws, Path(cwd), report)
                return text, (0.0 if ch.retention == "local" else cost), err
            finally:
                shutil.rmtree(ws, ignore_errors=True)
                if ws.exists():
                    raise ContainedProposerError(f"the scratch workspace {ws} could not be removed")

        def _parse(self, result: SessionResult, ws: Path, report: workspace_harvest.HarvestReport | None
                   ) -> tuple[str, float, str]:
            P = up.proposer
            kind = self.real_kind
            if kind == "claude_cli":
                payload = P._parse_json(result.stdout)
                text = str(payload.get("result") or payload.get("text") or "")
                err = ""
                if payload.get("is_error") or payload.get("subtype") not in (None, "success"):
                    err = f"CLI error: {payload.get('subtype')} {str(text)[:200]}"
                    text = ""
                elif result.exit_code != 0 and not text.strip():
                    err = f"rc={result.exit_code}: {(result.stderr or '')[-300:]}"
                return text, float(payload.get("total_cost_usd") or 0.0), err
            cost = P._opencode_cost(result.stdout) if kind == "opencode" else 0.0
            vetted = report is not None and "output/answer.md" in report.files
            ans = ws / "output" / "answer.md"
            if vetted and ans.is_file() and not ans.is_symlink():
                body = ans.read_text(errors="replace")
                if body.strip():
                    return body, cost, ""
            if kind == "opencode":
                text, err = P._opencode_stdout(result.stdout)
            else:
                text, err = result.stdout.strip(), ""
            if text.strip():
                return text, cost, ""
            tail = P._strip_oc_banner(result.stderr or "").strip()[-300:]
            return "", cost, f"rc={result.exit_code}: {err or tail or 'empty reply'}"

        def _bring_back(self, ws: Path, cwd: Path, report: workspace_harvest.HarvestReport | None) -> None:
            """Replace upstream's temp directory content with the INSPECTED regular files of the session."""
            if report is None:
                return
            for child in list(cwd.iterdir()):
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
            root = os.path.realpath(cwd)
            for rel in report.files:
                src, dst = ws / rel, Path(root) / rel
                if os.path.commonpath([os.path.realpath(dst.parent), root]) != root:
                    raise WorkspaceRefused("a harvested path leaves the directory it is copied into")
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst, follow_symlinks=False)
                os.chmod(dst, 0o644)

        # ── the Reply says which channel it really came from ──────────────────

        def _write_audit(self, r: Any, mission: str, system: str, cache_key: str = "") -> None:
            r.channel = self.real_kind
            super()._write_audit(r, mission, system, cache_key)

        def _cached(self, label: str, cache_key: str = "", *, require_workspace: bool = False) -> Any:
            r = super()._cached(label, cache_key, require_workspace=require_workspace)
            if r is not None:
                r.channel = self.real_kind
            return r

    return ContainedProposer


# ── construction ──────────────────────────────────────────────────────────────────

def make_proposer(up: Upstream, session: SessionConfig, *, audit_dir: Path | str | None = None, effort: str = "high",
                  steps: int = 120, timeout: int | None = None, attempts: int = 1) -> Any:
    """A `ContainedProposer` for the session's channel. `attempts=1` is the deviation from upstream's default of 2."""
    ch = session.channel_obj
    cls = contained_proposer_class(up)
    spec = up.spec.ChannelSpec(kind=CARRIER_KIND[ch.kind], model=ch.model, effort=effort, steps=steps,
                               timeout=int(timeout if timeout is not None else session.timeout_s))
    return cls(spec, audit_dir=Path(audit_dir) if audit_dir else None, attempts=attempts, project="", region="",
               session=session, real_kind=ch.kind)


def make_selector_prop(proposer: Any, *, channel: str | None = None) -> Any:
    """The selector's proposer, to pass as `selector_prop=` so upstream's `_selector_for` never builds a plain
    `Proposer` (which would run on the host, with Vertex). Same channel unless `channel` names another declared one."""
    s: SessionConfig = proposer.session
    if channel is not None and channel != s.channel:
        s = dataclasses.replace(s, channel=channel)
    ch = s.channel_obj
    spec = type(proposer.spec)(kind=CARRIER_KIND[ch.kind], model=ch.model, effort=proposer.spec.effort,
                               steps=proposer.spec.steps, timeout=proposer.spec.timeout)
    audit = Path(proposer.audit_dir) / "selector" if proposer.audit_dir else None
    return type(proposer)(spec, audit_dir=audit, attempts=proposer.attempts, project="", region="", session=s,
                          real_kind=ch.kind)
