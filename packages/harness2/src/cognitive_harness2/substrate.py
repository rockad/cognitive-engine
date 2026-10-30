"""Seam 3: `CognitiveSubstrate`, upstream's Substrate Protocol: staging and readiness (slice 3a), and a GATED
`execute` for contained solver sessions (slice 3b, WS-E).

Upstream's `Substrate` (harness2.substrate.base) is five members: `surface`, `materialise`,
`execute`, `read_trajectory` and `preflight`, plus a `name`:

  * `surface`      the upstream `SurfaceSpec` it was built with, refused at construction unless
                   `upstream_surface.check_surface` recognises its shape
  * `materialise`  stages the editable tree for a later run: read-only, under the hardened staging
                   (`DockerRunner._stage`, so DockerRunner's key rules decide every file name), no
                   symlinks, UTF-8 text only, capped in count and size
  * `preflight`    `require_rootless_docker`, then the quota pre-flight only if the caller wired
                   one in; raises `PreflightError` (a RuntimeError, as upstream's contract says)
                   carrying the remediation; starts nothing
  * `read_trajectory` pure code: renders a session's JSONL stream as `[ACTION]/[OBSERVATION]` text
  * `execute`      runs one contained SOLVER session per run id, and ONLY with an `ExecutionGrant`

`execute` is gated. There is no boolean, setting or environment variable that turns it on: a substrate built
without a `SolverExecution` (or whose grant is missing, expired, or issued for another channel or classification)
raises `ExecutionRefused` before it touches the filesystem or docker. The grant is the stage-1 approval gate's
output (`ExecutionGrant.from_plan` takes a `SweepPlan`, which always `requires_human_approval`); it carries the
channel, a sweep id, who approved, an expiry and hard caps, and the substrate refuses a runner whose
`SweepBudget` is larger than the grant.

This module imports nothing from upstream. `materialise` returns `StagedInstall`, which has the
same fields as upstream's `Install`, because importing `harness2.substrate` runs `harness2.config`,
which reads `$CWD/.env` into `os.environ`; a test pins the two field lists together.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import channels, quota as quota_mod
from . import session_runner as sr
from . import trajectory, workspace_harvest
from .invariants import InvariantViolation
from .sandbox import (
    DEFAULT_IMAGE,
    DockerRunner,
    SandboxSpec,
    SandboxUnavailable,
    _stage_name,
    _validate_stage_names,
    require_rootless_docker,
)
from .model_gate import GateTimeout, ModelGate
from .sweep_budget import BudgetExhausted, SweepBudget
from .upstream_surface import AllowedSurface, check_surface

NOT_ENABLED = "execution is not enabled: no ExecutionGrant"

# harness2.core.spec.INSTALL_MODES at the pinned commit (a test pins the two together)
INSTALL_MODES = ("workspace_agents_md", "home_dir_jinja", "workspace_config", "codex_root")
_CODEX_MODE = "codex_root"

MAX_FILES = 512
MAX_FILE_BYTES = 1 << 20
MAX_TOTAL_BYTES = 8 << 20


class PreflightError(RuntimeError):
    """A readiness check failed. The message carries the remediation; nothing was started."""


@dataclass(frozen=True)
class StagedInstall:
    """The fields of upstream's `Install`: where the tree was staged, a digest of its source, extras."""
    staging_dir: Path
    fingerprint: str
    extras: Mapping[str, str] = field(default_factory=dict)



# ── execution: the grant, the task, the wiring ──────────────────────────────────────

class ExecutionRefused(RuntimeError):
    """`execute` was asked to run without a valid grant, or outside it. Nothing was created or started."""


_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_WHO = re.compile(r"[^\x00-\x1f\x7f]{1,80}")
MAX_GRANT_TTL_S = 24 * 3600
MAX_PROMPT_BYTES = 64 * 1024
MAX_TASK_FILES = 64
OUTPUT_DIR = "output"


def _pos_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0


def _pos_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


@dataclass(frozen=True)
class ExecutionGrant:
    """What the stage-1 approval gate hands over: permission to run THIS sweep, on THIS channel, until `expires_at`.

    It is plain data. Nothing in it starts anything; `CognitiveSubstrate.execute` checks it on every call
    (expiry, channel, classification, and that the runner's `SweepBudget` is no larger than these caps).
    Build one from the approved plan with `from_plan`.
    """
    channel: str                    # the declared channel's name
    sweep_id: str                   # an opaque id for the audit trail
    approved_by: str                # who approved (a name, not a credential)
    expires_at: float               # epoch seconds (the substrate's `clock`, time.time by default)
    max_proposer_sessions: int
    max_solver_sessions: int
    max_wall_s: float               # for the whole sweep
    per_session_s: float            # for one session
    classification: str = "restricted"

    def __post_init__(self) -> None:
        bad: list[str] = []
        if not isinstance(self.channel, str) or not self.channel.strip():
            bad.append("channel must be the declared channel's name")
        if not isinstance(self.sweep_id, str) or not _SAFE_ID.fullmatch(self.sweep_id):
            bad.append("sweep_id must be 1 to 128 letters, digits, . _ -")
        if not isinstance(self.approved_by, str) or not _WHO.fullmatch(self.approved_by) or not self.approved_by.strip():
            bad.append("approved_by must name who approved (1 to 80 printable characters)")
        if not _pos_num(self.expires_at):
            bad.append("expires_at must be a finite epoch time")
        for name in ("max_proposer_sessions", "max_solver_sessions"):
            if not _pos_int(getattr(self, name)):
                bad.append(f"{name} must be a positive integer")
        for name in ("max_wall_s", "per_session_s"):
            if not _pos_num(getattr(self, name)):
                bad.append(f"{name} must be a positive finite number")
        if _pos_num(self.max_wall_s) and _pos_num(self.per_session_s) and self.per_session_s > self.max_wall_s:
            bad.append("per_session_s cannot exceed max_wall_s")
        if self.classification not in channels.CLASSIFICATIONS:
            bad.append(f"classification must be one of {channels.CLASSIFICATIONS}")
        if bad:
            raise ValueError("invalid ExecutionGrant: " + "; ".join(bad))

    @property
    def max_sessions(self) -> int:
        return self.max_proposer_sessions + self.max_solver_sessions

    def expired(self, now: float) -> bool:
        return not now < self.expires_at

    @classmethod
    def from_plan(cls, plan: Any, *, sweep_id: str, approved_by: str, ttl_s: float,
                  per_session_s: float = 600.0, now: float | None = None) -> "ExecutionGrant":
        """The grant for an approved `sweep_plan.SweepPlan`: its channel, classification and wall cap, and the
        session counts it planned (P proposer sessions, R rollouts). The caller is the approval gate; this
        refuses a plan that does not require approval or has nothing to run."""
        if getattr(plan, "requires_human_approval", None) is not True:
            raise ValueError("only a SweepPlan that requires human approval can be granted")
        if not _pos_num(ttl_s) or ttl_s > MAX_GRANT_TTL_S:
            raise ValueError(f"ttl_s must be positive and at most {MAX_GRANT_TTL_S}")
        t = time.time() if now is None else now
        return cls(channel=plan.channel, sweep_id=sweep_id, approved_by=approved_by, expires_at=t + ttl_s,
                   max_proposer_sessions=plan.planned_sessions, max_solver_sessions=plan.planned_rollouts,
                   max_wall_s=plan.max_wall_seconds, per_session_s=min(per_session_s, plan.max_wall_seconds),
                   classification=plan.classification)

    def make_budget(self, *, clock: Callable[[], float] = time.monotonic) -> SweepBudget:
        """The `SweepBudget` for this grant. Build it when the sweep starts; its wall clock starts then."""
        return SweepBudget(self.max_wall_s, self.per_session_s, self.max_sessions, clock=clock,
                           role_caps={"proposer": self.max_proposer_sessions, "solver": self.max_solver_sessions})

    def to_dict(self) -> dict:
        return {"channel": self.channel, "sweep_id": self.sweep_id, "approved_by": self.approved_by,
                "expires_at": self.expires_at, "max_proposer_sessions": self.max_proposer_sessions,
                "max_solver_sessions": self.max_solver_sessions, "max_wall_s": self.max_wall_s,
                "per_session_s": self.per_session_s, "classification": self.classification}


@dataclass(frozen=True)
class SolverTask:
    """What a solver session is given besides the harness: the task text and the input files."""
    prompt: str
    files: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.prompt, str) or not self.prompt.strip() or "\0" in self.prompt \
                or len(self.prompt.encode("utf-8", "replace")) > MAX_PROMPT_BYTES:
            raise ValueError("the task prompt must be non-empty text without NUL, at most 64 KiB")
        if not isinstance(self.files, Mapping) or len(self.files) > MAX_TASK_FILES:
            raise ValueError(f"the task files must be a mapping of at most {MAX_TASK_FILES}")
        for name, body in self.files.items():
            try:
                _stage_name(name)
            except InvariantViolation as e:
                raise ValueError(f"task file name {name!r} is not allowed: {e}") from None
            if name == "AGENTS.md" or name == OUTPUT_DIR or name.startswith(OUTPUT_DIR + "/"):
                raise ValueError(f"task file {name!r} collides with the harness text or the output directory")
            if not isinstance(body, str) or len(body.encode("utf-8", "replace")) > MAX_FILE_BYTES:
                raise ValueError(f"task file {name!r} must be text of at most {MAX_FILE_BYTES} bytes")


@dataclass(frozen=True)
class SolverJob:
    """What an argv builder is told about one session."""
    task_ref: str
    run_id: str
    prompt: str
    workspace: str
    model: str
    cli: str                        # the CLI's path INSIDE the container (argv[0])


def opencode_argv(job: SolverJob) -> tuple[str, ...]:
    """`opencode run <prompt> --dir <workspace> --model <m> --format json`, the shape upstream's reference launcher
    builds (`OpencodeSubstrate.run_argv`). UNVERIFIED against a real binary: the first real call settles it."""
    return (job.cli, "run", job.prompt, "--dir", job.workspace, "--model", job.model, "--title", "adapt",
            "--format", "json")


def opencode_env(job: SolverJob) -> dict[str, str]:
    """Upstream's permission fence: the session may touch its own workspace and nothing else. (Its output-token
    ceiling variable is not set here: `session_runner` refuses env names that look like credentials, and
    `..._OUTPUT_TOKEN_MAX` contains TOKEN.)"""
    allow = {"*": "deny", job.workspace: "allow", f"{job.workspace}/**": "allow"}
    return {"OPENCODE_PERMISSION": json.dumps({"external_directory": allow})}


@dataclass(frozen=True)
class SolverExecution:
    """Everything `execute` needs, wired explicitly. Giving a substrate one is half of enabling execution; the
    other half is a live `grant`.

    `runner` must be a `ProposerSessionRunner(role="solver", budget=...)`. `results_root/<run_id>` is where a
    run's files go; `workspace_root` is where per-run workspaces are made (under the channel profile's workspace
    roots) and removed again. `task_loader(task_ref)` returns the `SolverTask`.
    """
    runner: Any
    grant: ExecutionGrant | None
    cli: str
    results_root: Path
    workspace_root: Path
    task_loader: Callable[[str], SolverTask]
    model: str | None = None                                   # default: the channel's model
    argv_builder: Callable[[SolverJob], Sequence[str]] = opencode_argv
    env_builder: Callable[[SolverJob], Mapping[str, str]] | None = opencode_env
    credential: sr.Credential | None = None
    limits: sr.SessionLimits = sr.SessionLimits()
    tmpfs_dirs: tuple[str, ...] = ()
    tmp_exec: bool = False                                     # a bun-compiled CLI may need True: settled by the first real call
    harvest: workspace_harvest.HarvestLimits = workspace_harvest.HarvestLimits()
    gate: ModelGate | None = None                              # the one-slot model gate: one model call at a time
    gate_wait_s: float | None = None                           # how long a session may wait for it (None: 4 x its timeout)
    clock: Callable[[], float] = time.time
    stream_name: str = "opencode_session.jsonl"

    def __post_init__(self) -> None:
        if not isinstance(self.runner, sr.ProposerSessionRunner):
            raise TypeError("runner must be a session_runner.ProposerSessionRunner")
        if self.grant is not None and not isinstance(self.grant, ExecutionGrant):
            raise TypeError("grant must be an ExecutionGrant (or None, which refuses every execute)")
        if not callable(self.task_loader) or not callable(self.argv_builder) or not callable(self.clock):
            raise TypeError("task_loader, argv_builder and clock must be callables")
        for p in (self.results_root, self.workspace_root):
            if not isinstance(p, Path) or not p.is_absolute():
                raise ValueError("results_root and workspace_root must be absolute Paths")

    @property
    def model_name(self) -> str:
        return self.model or self.runner.channel.model


# ── helpers for execute ─────────────────────────────────────────────────────────────

def render_agents_md(files: Mapping[str, str], always_on: Iterable[str]) -> str:
    """The harness as the AGENTS.md the solver CLI appends to its prompt: the always-on files, in surface order,
    joined by a rule. (Skills, subagents and scripts are not rendered: see the README's honest limits.)"""
    parts = [files[n].strip() for n in always_on if n in files and files[n].strip()]
    return "\n\n---\n\n".join(parts) + ("\n" if parts else "")


def _copy_regular(src: str, dst: Path, limit: int) -> bool:
    """Copy one regular, single-link file out of a workspace without following a link. False if refused."""
    try:
        fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink > 1 or st.st_size > limit:
            return False
        with os.fdopen(fd, "rb", closefd=False) as f:
            data = f.read(limit + 1)
    finally:
        os.close(fd)
    if len(data) > limit:
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(data)
    dst.chmod(0o644)
    return True


def _write_private(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o644)


def _error_json(run_dir: Path, exc: BaseException | str) -> None:
    msg = exc if isinstance(exc, str) else f"{type(exc).__name__}: {exc}"
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_private(run_dir / "error.json", json.dumps({"error": sr.sanitize_terminal(msg)[:500]}))

def openrouter_quota_check(api_key: str | None, *, client: Any = None) -> Callable[[], quota_mod.QuotaVerdict]:
    """A synchronous quota check for `CognitiveSubstrate(quota_check=...)`: one `GET /api/v1/key`, no model call.

    Works from plain code and from inside a running event loop. Without a key it returns a refusal
    and makes no request.
    """
    def check() -> quota_mod.QuotaVerdict:
        coro = quota_mod.preflight(api_key, client)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()
    return check


def _fingerprint(files: Mapping[str, str]) -> str:
    h = hashlib.sha256()
    for rel in sorted(files):
        h.update(rel.encode())
        h.update(b"\0")
        h.update(files[rel].encode())
        h.update(b"\0")
    return h.hexdigest()[:16]


def _read_tree(root: Path) -> dict[str, str]:
    """Every regular UTF-8 file under `root` as rel -> text. Symlinks and special files are refused."""
    if root.is_symlink():
        raise ValueError(f"{root.name!r} is a symlink; refusing to stage through it")
    if not root.is_dir():
        raise ValueError(f"{str(root)!r} is not a directory")
    files: dict[str, str] = {}
    total = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in [*dirnames, *filenames]:
            p = Path(dirpath, name)
            if p.is_symlink():
                raise ValueError(f"{p.relative_to(root).as_posix()!r} is a symlink; refusing to stage it")
        for name in sorted(filenames):
            p = Path(dirpath, name)
            rel = p.relative_to(root).as_posix()
            st = p.lstat()
            if not stat.S_ISREG(st.st_mode):
                raise ValueError(f"{rel!r} is not a regular file; refusing to stage it")
            if len(files) >= MAX_FILES:
                raise ValueError(f"the tree has more than {MAX_FILES} files; refusing to stage it")
            if st.st_size > MAX_FILE_BYTES:
                raise ValueError(f"{rel!r} is larger than {MAX_FILE_BYTES} bytes; refusing to stage it")
            total += st.st_size
            if total > MAX_TOTAL_BYTES:
                raise ValueError(f"the tree is larger than {MAX_TOTAL_BYTES} bytes in total; refusing to stage it")
            try:
                files[rel] = p.read_bytes().decode("utf-8")
            except UnicodeDecodeError:
                raise ValueError(f"{rel!r} is not valid UTF-8 text; refusing to stage it") from None
    return files


class CognitiveSubstrate:
    """Upstream's Substrate Protocol: staging and readiness only. Execution is slice 3b."""

    name = "cognitive"

    def __init__(self, surface_spec: Any, *,
                 which: Callable[[str], str | None] = shutil.which,
                 run: Callable[..., subprocess.CompletedProcess] | None = None,
                 environ: Mapping[str, str] | None = None,
                 image: str = DEFAULT_IMAGE,
                 quota_check: Callable[[], quota_mod.QuotaVerdict] | None = None,
                 execution: SolverExecution | None = None):
        """`surface_spec` is an upstream SurfaceSpec (e.g. `OPENCODE_SURFACE`). `quota_check`, when given,
        is called by `preflight` after the Docker gate; leave it out and no quota request is ever made.
        `execution`, when given, is the wiring `execute` runs with; without it (or without a live grant in it)
        `execute` raises `ExecutionRefused`."""
        if execution is not None and not isinstance(execution, SolverExecution):
            raise TypeError("execution must be a SolverExecution (there is no boolean switch)")
        self._execution = execution
        self._allowed = check_surface(surface_spec)  # raises InvariantViolation for a shape it does not know
        self._spec = surface_spec
        self._which, self._environ, self._image, self._quota_check = which, environ, image, quota_check
        # resolved at call time, so a patched subprocess.run is what runs
        self._run = run or (lambda *a, **k: subprocess.run(*a, **k))
        self._staged: set[Path] = set()

    # ── Substrate ────────────────────────────────────────────────────────────────
    def surface(self) -> Any:
        return self._spec

    def allowed_surface(self) -> AllowedSurface:
        """What the optimizer may mutate on this surface (see `upstream_surface`)."""
        return self._allowed

    def materialise(self, tree_dir: Path, spec: Any, ctx: Mapping[str, str], *,
                    changed: Iterable[str] | None = None) -> StagedInstall:
        """Stage `tree_dir` read-only and return where.

        `changed` names the paths the optimizer edited; every one must be on the mutable surface or
        `InvariantViolation` is raised before anything is read. Every staged name (the tree's and the
        `file:<rel>` entries of `ctx`) must satisfy DockerRunner's key rules. Nothing is staged on refusal.
        The staged directory is removed with `release`.
        """
        mode = getattr(spec, "install_mode", None)
        if mode not in INSTALL_MODES:
            raise ValueError(f"install_mode {mode!r} is not one of {INSTALL_MODES}")
        if (mode == _CODEX_MODE) != (getattr(self._spec, "name", None) == "codex"):
            raise ValueError(f"install_mode {mode!r} does not belong to the {self._spec.name} surface")
        if changed is not None:
            check_surface(self._spec, tree=list(changed))

        root = Path(tree_dir)
        files = _read_tree(root)
        if not any(a in files for a in self._spec.always_on):
            raise ValueError(f"{str(root)!r} is not a {self._spec.name} harness tree (no always-on file present)")

        extra: dict[str, str] = {}
        for key, body in ctx.items():
            if not isinstance(key, str):
                raise ValueError("ctx keys must be strings")
            if key.startswith("file:"):
                if not isinstance(body, str):
                    raise ValueError("ctx file contents must be strings")
                rel = key[len("file:"):]
                if rel in files or rel in extra:
                    raise ValueError(f"ctx file {rel!r} collides with a file already in the tree")
                extra[rel] = body
        staged_files = {**files, **extra}
        _validate_stage_names(staged_files)  # DockerRunner's key rules, before anything is created

        stage = DockerRunner("docker")._stage(SandboxSpec(command=("true",), files=staged_files))
        try:
            for dirpath, _dirs, _files in os.walk(stage, topdown=False):
                os.chmod(dirpath, 0o555)
        except BaseException:
            self._remove(stage)
            raise
        self._staged.add(stage)
        return StagedInstall(stage, _fingerprint(files), {"install_mode": mode, "read_only": "true"})

    def read_trajectory(self, stream_path: Path, *, obs_trunc: int, max_chars: int, args_trunc: int = 400) -> str:
        """Pure code: the session stream rendered as `[ACTION]/[OBSERVATION]` text (see `trajectory`)."""
        return trajectory.render_events(Path(stream_path), obs_trunc=obs_trunc, max_chars=max_chars,
                                        args_trunc=args_trunc)

    # ── execute: gated ───────────────────────────────────────────────────────────

    def _authorised(self) -> SolverExecution:
        """The wiring, or `ExecutionRefused`. Re-checked on every call; touches neither disk nor docker."""
        ex = self._execution
        if ex is None or ex.grant is None:
            raise ExecutionRefused(NOT_ENABLED)
        g, runner = ex.grant, ex.runner
        now = ex.clock()
        if not isinstance(now, (int, float)) or isinstance(now, bool) or not math.isfinite(now):
            raise ExecutionRefused("the substrate's clock gave no usable time")
        if g.expired(now):
            raise ExecutionRefused(f"the execution grant for sweep {g.sweep_id!r} has expired")
        if g.channel != runner.channel.name:
            raise ExecutionRefused(f"the grant is for channel {g.channel!r}, not the runner's {runner.channel.name!r}")
        if g.classification != channels.effective_classification(runner.classification):
            raise ExecutionRefused("the grant was approved for a different classification than the runner's")
        if runner.role != "solver":
            raise ExecutionRefused("the runner is not a solver runner (role='solver')")
        b = runner.budget
        if b is None:
            raise ExecutionRefused("the runner has no SweepBudget: unbudgeted execution is refused")
        if (b.total_wall_s > g.max_wall_s or b.per_session_s > g.per_session_s or b.max_sessions > g.max_sessions
                or b.role_caps.get("solver", b.max_sessions) > g.max_solver_sessions
                or b.role_caps.get("proposer", b.max_sessions) > g.max_proposer_sessions):
            raise ExecutionRefused("the runner's SweepBudget is larger than the grant's caps")
        return ex

    def execute(self, task_ref: str, install: Any, run_ids: list[str], timeout: int) -> list[Path]:
        """Run one contained solver session per run id and return the run directories, in order.

        Each run directory gets `opencode_session.jsonl` (the session's stdout), `output/` (the files the session
        left under `output/` that harvest accepted), and `metrics.json`; `.adapt_complete` is the ADAPTER's to
        write. A run that could not be made (the budget is spent, the session was refused, the stream had no
        events) leaves `error.json` instead of `metrics.json`; a teardown that left anything behind RAISES.
        """
        ex = self._authorised()
        if not isinstance(task_ref, str):
            raise ValueError("task_ref must be a string")
        task = ex.task_loader(task_ref)
        if not isinstance(task, SolverTask):
            raise ValueError("the task loader must return a SolverTask")
        if not isinstance(run_ids, (list, tuple)) or not run_ids or len(set(run_ids)) != len(run_ids) \
                or not all(isinstance(r, str) and _SAFE_ID.fullmatch(r) for r in run_ids):
            raise ValueError("run_ids must be a non-empty list of distinct names (letters, digits, . _ -)")
        if not _pos_num(timeout):
            raise ValueError("timeout must be a positive number of seconds")
        staging = getattr(install, "staging_dir", None)
        if staging is None:
            raise ValueError("install must be an Install (it has no staging_dir)")
        harness = _read_tree(Path(staging))
        agents_md = render_agents_md(harness, self._spec.always_on)
        dirs: list[Path] = []
        for rid in run_ids:
            self._authorised()                      # the grant can expire between runs
            dirs.append(self._run_one(ex, task_ref, task, agents_md, getattr(install, "fingerprint", ""), rid,
                                      float(timeout)))
        return dirs

    def _run_one(self, ex: SolverExecution, task_ref: str, task: SolverTask, agents_md: str, fingerprint: str,
                 run_id: str, timeout: float) -> Path:
        g = ex.grant
        assert g is not None
        run_dir = ex.results_root / run_id
        if run_dir.exists() and any(run_dir.iterdir()):
            raise ValueError(f"run directory {run_id!r} already holds files; remove it first (the adapter does)")
        run_dir.mkdir(parents=True, exist_ok=True)
        ex.workspace_root.mkdir(parents=True, exist_ok=True)
        ws = Path(tempfile.mkdtemp(prefix=f"h2ws-{run_id[:24]}-", dir=ex.workspace_root)).resolve()
        removed = False
        try:
            # The container user (65534) is not the host user, so the directory must be world-writable. The sticky
            # bit stops it unlinking or renaming the files placed here (AGENTS.md, the inputs): it owns none of them.
            ws.chmod(0o1777)
            if agents_md:
                _write_private(ws / "AGENTS.md", agents_md)
            for name, body in task.files.items():
                p = ws / name
                p.parent.mkdir(parents=True, exist_ok=True)
                _write_private(p, body)
            (ws / OUTPUT_DIR).mkdir()
            (ws / OUTPUT_DIR).chmod(0o1777)
            job = SolverJob(task_ref, run_id, task.prompt, str(ws), ex.model_name, sr.container_cli_path(ex.cli))
            spec = sr.SessionSpec(argv=tuple(ex.argv_builder(job)), workspace=str(ws), cli=ex.cli,
                                  timeout_s=min(timeout, g.per_session_s),
                                  env=dict(ex.env_builder(job)) if ex.env_builder else {}, credential=ex.credential,
                                  limits=ex.limits, tmpfs_dirs=ex.tmpfs_dirs, tmp_exec=ex.tmp_exec, harvest=ex.harvest)
            try:
                if ex.gate is None:
                    result = ex.runner.run(spec)
                else:           # a single GPU serves one model call at a time, whoever makes it
                    wait = ex.gate_wait_s if ex.gate_wait_s is not None else 4 * spec.timeout_s
                    with ex.gate.hold(wait, label=f"solver:{run_id[:40]}"):
                        result = ex.runner.run(spec)
            except (BudgetExhausted, GateTimeout, sr.SessionRefused, sr.SessionError, SandboxUnavailable) as e:
                _error_json(run_dir, e)
                return run_dir
            except sr.SessionCleanupError as e:
                _error_json(run_dir, e)
                raise
            return self._record(ex, g, task_ref, fingerprint, run_id, spec.timeout_s, result, ws, run_dir)
        finally:
            shutil.rmtree(ws, ignore_errors=True)
            removed = not ws.exists()
            if not removed:                         # a leak is never silent
                _error_json(run_dir, f"workspace {ws} could not be removed")
                raise sr.SessionCleanupError([f"directory {ws}"])

    def _record(self, ex: SolverExecution, g: ExecutionGrant, task_ref: str, fingerprint: str, run_id: str,
                timeout_s: float, result: sr.SessionResult, ws: Path, run_dir: Path) -> Path:
        stream = result.stdout
        n_events = trajectory.count_events(stream)
        _write_private(run_dir / ex.stream_name, stream)
        report = result.harvest
        collected: list[str] = []
        if report is not None:
            for rel in report.files:
                if rel.startswith(OUTPUT_DIR + "/") and _copy_regular(str(ws / rel), run_dir / rel,
                                                                      ex.harvest.max_file_bytes):
                    collected.append(rel)
        if n_events == 0:
            _error_json(run_dir, f"the session stream had no parseable events (exit code {result.exit_code}, "
                                 f"timed out: {result.timed_out}); not recorded as a run")
            return run_dir
        metrics = {
            "run_id": run_id, "task_ref": task_ref, "role": result.role, "sweep_id": g.sweep_id, "channel": g.channel,
            "model": ex.model_name, "session": result.run_id, "install_fingerprint": fingerprint,
            "exit_code": result.exit_code, "timed_out": result.timed_out, "truncated": result.truncated,
            "duration_s": round(result.duration_s, 3), "timeout_s": timeout_s, "n_events": n_events,
            "delivered": bool(collected), "collected": collected, "reclaimed": result.reclaimed,
            "harvest": None if report is None else {
                "ok": report.ok, "total_bytes": report.total_bytes, "n_files": len(report.files),
                "findings": [{"path": f.path, "rule": f.rule} for f in report.findings][:100]},
            "egress": list(result.egress_log)[:100],
            "errors": trajectory.error_events(stream),
            **{k: v for k, v in sr.egress_summary(result.egress_log).items() if k in ("model_requests", "denied_hosts")},
            "stderr_tail": result.stderr[-2000:],
        }
        _write_private(run_dir / "metrics.json", json.dumps(metrics, indent=2, sort_keys=True))
        return run_dir

    def preflight(self) -> None:
        """Rootless-Docker gate, then the caller's quota check if one was given. Starts nothing."""
        try:
            require_rootless_docker(self._which, run=self._run, environ=self._environ, image=self._image)
        except SandboxUnavailable as e:
            raise PreflightError(
                f"rootless Docker is not usable: {e}. Remediation: install rootless Docker "
                f"(`dockerd-rootless-setuptool.sh install`), start the daemon, point DOCKER_HOST at its unix "
                f"socket, then run `docker pull {self._image.split('@')[0]}` once for the pinned sandbox image. "
                f"Nothing was started.") from e
        if self._quota_check is None:
            return
        try:
            verdict = self._quota_check()
        except Exception as e:  # noqa: BLE001 - fail closed, and never repeat the message (it may hold a key)
            raise PreflightError(f"the quota pre-flight could not run ({type(e).__name__}). Remediation: check the "
                                 "OpenRouter key and network, or use a local endpoint. Nothing was started.") from None
        if not isinstance(verdict, quota_mod.QuotaVerdict):
            raise PreflightError("the quota pre-flight returned an unreadable verdict. Nothing was started.")
        if not verdict.ok:
            raise PreflightError(f"the quota pre-flight refused: {verdict.reason}. Remediation: wait for the quota "
                                 "to reset or use a local endpoint. Nothing was started.")

    # ── cleanup ──────────────────────────────────────────────────────────────────
    def release(self, install: StagedInstall) -> None:
        """Remove a directory this substrate staged. Anything else is refused."""
        stage = Path(install.staging_dir)
        if stage not in self._staged:
            raise ValueError(f"{str(stage)!r} was not staged by this substrate; refusing to remove it")
        self._remove(stage)
        self._staged.discard(stage)

    @staticmethod
    def _remove(stage: Path) -> None:
        for dirpath, dirnames, _files in os.walk(stage, topdown=True):
            os.chmod(dirpath, 0o755)
        shutil.rmtree(stage, ignore_errors=True)
