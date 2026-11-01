"""The stage-1 runner: a local smoke run of the Harness2 method (slice 3b, WS-E part 2).

    python -m cognitive_harness2.stage1                 # = plan: read-only, prints what a run would do
    python -m cognitive_harness2.stage1 run --approve local-ollama [...]    # the ONLY place a real model is called
    python -m cognitive_harness2.stage1 selftest        # the whole pipeline with a FAKE opencode and a FAKE endpoint

What stage 1 is. `k = 1` of upstream's method on the synthetic `sum-signed` task: 3 proposer sessions (inv1, dom1,
agg1) and 4 solver rollouts (base1, inv1, dom1, agg1), 7 contained sessions in all, `modes = ('agg',)`, upstream's
reflection session skipped (`run_task(..., libraries=None)`), attempts forced to 1 (`contained_proposer.apply_overrides`).
Every session is `opencode` in a rootless Docker container, reaching Ollama on this machine's loopback through the host
bridge and the allow-list proxy. One model call at a time (the model gate). $0. Nothing leaves the machine.

Stage 1c (two flags, defaults unchanged): `--proposer-hint on|off` puts the runner's ENVIRONMENT RULES block (which harness
files are kept, which are read-only) in front of each proposer session (`proposer_hint`; on for the hard set by default), and
`--rollouts N` rolls the base and the delivered final N times each. Upstream fixes R = 4k, so the extra rollouts are made here,
after `run_task` (`run_extra_rollouts`), and count as sessions: P + R = 3 + (4 + 2 (N - 1)) must fit the profile's caps.sessions.

`plan` changes nothing. It reads the profile, asks the policy, runs the pinned read-only `docker info` and
`docker image inspect`, and sends ONE `GET /api/tags` to the loopback Ollama (no bytes beyond that, no model is loaded).
It creates no file, directory, container, network or socket, and prints the exact command that approves the sweep.

`run` does nothing at all without `--approve <channel>` AND a phrase typed on a terminal. Only then does it create the
run directory, build the `ExecutionGrant` (channel, sweep id, approver, expiry, caps), and start the first session
(whose host bridge is the first thing that opens a socket). Everything it makes is torn down in a `finally`: the
scratch workspaces, and any container or network of its sessions that survived their own teardown. It writes an audit
JSONL (events and numbers, never a prompt, an argv, an environment or a model's output) and a final `report.json`.

The profile comes from `--profile` or `COGNITIVE_HARNESS2_CHANNELS_FILE`. This module never writes under `~/.config`.
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import getpass
import http.client
import importlib
import io
import json
import math
import os
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from . import channels, egress, image, ollama_guard, proposer_hint, run_scope, sandbox, sum_signed as ss, sum_signed_hard as ss_hard, task_sets, upstream_egress, upstream_import
from . import session_runner as sr
from .contained_proposer import (OPENCODE_PROMPT, SessionConfig, build_command, make_proposer, make_selector_prop,
                                 session_env, TMP_EXEC)
from .model_gate import GATE
from .substrate import CognitiveSubstrate, ExecutionGrant, ExecutionRefused, SolverExecution, SolverJob
from .settings import ENV_CHANNELS_FILE
from .sweep_plan import CostCap, SweepCandidate, SweepPlan, SweepRefused, plan_sweep

CHANNEL = "local-ollama"
TASK = "household/sum-1"
K = 1
MODES = ("agg",)
ZDR = "strict"
CLASSIFICATION = "open"                      # the task, the harness and every prompt are synthetic
PER_SESSION_S = 600.0
GRANT_MARGIN_S = 600.0                       # the grant outlives the sweep's wall clock by this much
API_PATH = "/v1"                             # appended to the provider endpoint, as SessionConfig.api_path does
ENV_OPENCODE = "COGNITIVE_HARNESS2_OPENCODE"
ENV_CHANNELS = ENV_CHANNELS_FILE
OLLAMA_TIMEOUT_S = 3.0
EXCERPT = 160
FAKE_CLI = Path(__file__).with_name("selftest_fake_opencode.sh")
EXPECTED_P, EXPECTED_R = 3, 4                # upstream's budget identity at k=1 (P = 3k, R = 4k)
HINT_CHOICES = ("on", "off")                 # --proposer-hint
MAX_ROLLOUTS = 64                            # --rollouts: a sanity ceiling; the profile's caps.sessions is the real bound


@dataclass(frozen=True)
class RolloutPlan:
    """The session arithmetic of one sweep at `--rollouts n`, from upstream's own plan (P, R) plus what OUR runner adds.

    Upstream rolls base1, inv1, dom1 and agg1 once each (R = 4 at k=1) and has no knob for more: `run_task` builds
    `labels = [base1..basek]` and `recursion.run_parallel` rolls each candidate once. `n` is the number of rollouts of the
    base harness and of the delivered final: upstream's own one plus `n - 1` more of each, made after `run_task` by
    `run_extra_rollouts`. inv1 and dom1 are upstream's evidence for the aggregator and stay at one each."""
    n: int
    proposer: int               # P: upstream's proposer sessions
    upstream_rollouts: int      # upstream's R
    extra_base: int
    extra_final: int

    @property
    def rollouts(self) -> int:
        return self.upstream_rollouts + self.extra_base + self.extra_final

    @property
    def sessions(self) -> int:
        return self.proposer + self.rollouts


def rollout_plan(n: int, *, upstream_p: int, upstream_r: int, finals: int = K) -> RolloutPlan:
    """The arithmetic for `n` rollouts per arm: `finals` is the number of delivered finals (k, one per mode here)."""
    return RolloutPlan(n=n, proposer=upstream_p, upstream_rollouts=upstream_r, extra_base=n - 1, extra_final=(n - 1) * finals)
HOST_SESSION_WARNINGS = ("the session runs agent tools (shell and file access) on this host", "HOME and any file-based credentials")
CANDIDATES = (("systemprompt", "systemprompt.md"), ("memory", "LongTermMEMORY.md"),
              ("tool_guidance", "tool_descriptions/tool_guidance.md"))


class Stage1Error(RuntimeError):
    """A refusal or a failure with a message for the person running it. Nothing was started unless the text says so."""


def default_runs_dir(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    base = env.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "cognitive-engine" / "harness2-stage1"


def new_sweep_id() -> str:
    return f"stage1-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(2)}"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# ── settings ──────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Settings:
    profile: channels.ChannelProfile
    channel: str = CHANNEL
    classification: str = CLASSIFICATION
    zdr: str = ZDR
    task: str | None = None                  # None: the task set's default task
    task_set: str = task_sets.DEFAULT        # "easy" (sum-signed, the stage-1 task) or "hard" (sum-signed-hard, stage 1b)
    wall_s: float | None = None              # requested total wall clock; None: the profile's cap. Only ever lowers it.
    per_session_s: float = PER_SESSION_S
    cli: str | None = None                   # host path of opencode (--opencode / env / ~/.opencode/bin/opencode / PATH)
    runs_dir: Path = field(default_factory=default_runs_dir)   # PARENT of the per-sweep directory
    proposer_hint: str | None = None         # "on" / "off" (--proposer-hint); None: on for the hard set, off for the easy one
    rollouts: int = 1                        # --rollouts: rollouts of the base and of the delivered final (1 = upstream's own)

    def __post_init__(self) -> None:
        try:
            ts = task_sets.get(self.task_set)
            task = self.task if self.task is not None else ts.default_task
            ts.instance(task)
        except ValueError as e:
            raise Stage1Error(str(e)) from None
        object.__setattr__(self, "task", task)
        if self.proposer_hint not in (None, *HINT_CHOICES):
            raise Stage1Error(f"--proposer-hint must be one of: {', '.join(HINT_CHOICES)}")
        if not (isinstance(self.rollouts, int) and not isinstance(self.rollouts, bool) and 1 <= self.rollouts <= MAX_ROLLOUTS):
            raise Stage1Error(f"--rollouts must be a whole number from 1 to {MAX_ROLLOUTS}")

    @property
    def hint_on(self) -> bool:
        """Does a proposer session get the ENVIRONMENT RULES block? The flag when given, else on for the hard set only
        (so a command without the flag reproduces run #4 on the easy set, and the hard set gets the hint by default)."""
        return self.proposer_hint == "on" if self.proposer_hint is not None else self.task_set == "hard"

    @property
    def hint_source(self) -> str:
        return "--proposer-hint" if self.proposer_hint is not None else f"default for the {self.task_set} set"

    @property
    def ts(self) -> ss.TaskSet:
        return task_sets.get(self.task_set)

    @property
    def ch(self) -> channels.Channel:
        return self.profile.channels[self.channel]


def load_profile(path: str | None, environ: Mapping[str, str]) -> channels.ChannelProfile:
    """The profile named by `--profile` or the environment. Never the default file under ~/.config: stage 1 is explicit."""
    chosen = path or environ.get(ENV_CHANNELS)
    if not chosen or not chosen.strip():
        raise Stage1Error(f"no channel profile: pass --profile FILE or set {ENV_CHANNELS} "
                          "(a template is packages/harness2/examples/harness2-channels.stage1.toml)")
    p = Path(chosen).expanduser()
    if not p.is_absolute():
        p = Path.cwd() / p
    try:
        return channels.load_profile({ENV_CHANNELS: str(p)})
    except channels.ChannelProfileError as e:
        raise Stage1Error(str(e)) from None


def find_cli(explicit: str | None, environ: Mapping[str, str], which: Callable[[str], str | None] = shutil.which) -> str | None:
    """Where opencode is on the host, or None. An explicit path (`--opencode`, then the environment variable) is the only
    candidate when given: a wrong one is never quietly replaced by another binary. Executes nothing."""
    for named in (explicit, environ.get(ENV_OPENCODE)):
        if named:
            path = os.path.abspath(os.path.expanduser(named))
            return path if os.path.isfile(path) else None
    for cand in (str(Path.home() / ".opencode" / "bin" / "opencode"), which("opencode")):
        if cand and os.path.isfile(cand):
            return os.path.abspath(cand)
    return None


# ── preflight: checks that start nothing ──────────────────────────────────────────

@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str

    def as_readiness(self) -> dict:
        return {"ok": self.ok, "detail": self.detail}


@dataclass
class Preflight:
    checks: list[Check] = field(default_factory=list)
    verified: sandbox.VerifiedDocker | None = None
    image: image.ImageRef | None = None
    cli: str | None = None

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks)

    def failing(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]

    def readiness(self) -> dict[str, dict]:
        return {c.name: c.as_readiness() for c in self.checks}


def endpoint_target(ch: channels.Channel) -> tuple[str, int]:
    """(loopback IP literal, port) of a local channel's endpoint."""
    key = channels.endpoint_key(ch.endpoints[0])
    if key is None:
        raise Stage1Error("the channel's endpoint cannot be read")
    host, port = key
    return ("127.0.0.1" if host == "localhost" else host), port


def probe_ollama(host: str, port: int, model: str, timeout: float = OLLAMA_TIMEOUT_S) -> tuple[bool, str]:
    """One `GET /api/tags` on loopback: is Ollama up, and is `model` (e.g. `gemma4:12b`) installed? Loads no model."""
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", "/api/tags", headers={"Host": f"{host}:{port}", "Connection": "close"})
        resp = conn.getresponse()
        body = resp.read(1 << 20)
        if resp.status != 200:
            return False, f"{host}:{port} answered HTTP {resp.status} to GET /api/tags (is this Ollama?)"
        try:
            names = {str(m.get("name") or m.get("model") or "") for m in json.loads(body).get("models", [])}
        except (ValueError, AttributeError):
            return False, f"{host}:{port} did not return Ollama's model list"
    except (OSError, http.client.HTTPException) as e:
        return False, f"nothing usable on {host}:{port} ({type(e).__name__}); start it with `ollama serve`"
    finally:
        conn.close()
    want = {model, f"{model}:latest"} if ":" not in model else {model}
    if not names & want:
        shown = ", ".join(sorted(n for n in names if n)[:8]) or "none"
        return False, f"Ollama is up on {host}:{port} but {model} is not installed (has: {shown}); run `ollama pull {model}`"
    return True, f"Ollama answers on {host}:{port} and {model} is installed (not loaded or called)"


@dataclass
class Probes:
    """The few things `preflight` reaches outside the process for. Tests replace them; nothing else is touched."""
    gate: Callable[[], sandbox.VerifiedDocker] = image.gate_for_build
    resolve: Callable[[sandbox.VerifiedDocker], image.ImageRef] = image.resolve_image
    ollama: Callable[[str, int, str], tuple[bool, str]] = probe_ollama
    environ: Mapping[str, str] = field(default_factory=lambda: os.environ)
    which: Callable[[str], str | None] = shutil.which


def _guard(name: str, fn: Callable[[], tuple[bool, str]]) -> Check:
    try:
        ok, detail = fn()
    except Exception as e:  # noqa: BLE001 - a probe that breaks is a failed check, not a crash
        ok, detail = False, f"could not be checked ({type(e).__name__}: {str(e)[:160]})"
    return Check(name, ok, detail)


def preflight(s: Settings, probes: Probes | None = None) -> Preflight:
    """Every check, none of which starts anything. Only the pinned read-only docker calls and one loopback GET leave the process."""
    pr = probes or Probes()
    out = Preflight()
    ch = s.profile.channels.get(s.channel)

    def profile_ok() -> tuple[bool, str]:
        if not s.profile.declared:
            return False, f"no profile at {s.profile.source}"
        if ch is None:
            return False, f"channel {s.channel!r} is not declared in {s.profile.source} (declared: {', '.join(s.profile.channels) or 'none'})"
        if ch.kind != "opencode" or ch.retention != "local" or len(ch.endpoints) != 1 \
                or not all(h in egress.LOOPBACK for h, _ in ch.endpoint_keys):
            return False, f"channel {s.channel!r} must be an opencode channel with retention=local on one loopback endpoint"
        return True, f"{ch.name}: {ch.kind} -> {ch.endpoints[0]} model {ch.model}, retention {ch.retention}"
    out.checks.append(_guard("profile", profile_ok))
    if ch is None or not out.checks[-1].ok:
        return out

    def policy_ok() -> tuple[bool, str]:
        v = channels.decide(ch, zdr=s.zdr, classification=s.classification)
        return (True, f"allowed: zdr={s.zdr}, classification={s.classification}") if v.allowed else (False, "; ".join(v.reasons))
    out.checks.append(_guard("policy", policy_ok))

    def roots_ok() -> tuple[bool, str]:
        if not s.profile.workspace_roots:
            return False, "the profile declares no workspace_roots"
        root = s.profile.workspace_roots[0]
        if not os.path.isdir(root):
            return False, f"workspace root {root} does not exist; create it (mkdir -p {shlex.quote(root)})"
        if os.path.realpath(root) != root:
            return False, f"workspace root {root} contains a symlink (it is mounted at the same path); use {os.path.realpath(root)}"
        if not os.access(root, os.W_OK | os.X_OK):
            return False, f"workspace root {root} is not writable by you"
        return True, f"{root} exists and is writable"
    out.checks.append(_guard("workspace_roots", roots_ok))

    def docker_ok() -> tuple[bool, str]:
        out.verified = pr.gate()
        return True, f"rootless Docker verified at {out.verified.host}"
    out.checks.append(_guard("docker", docker_ok))

    def image_ok() -> tuple[bool, str]:
        if out.verified is None:
            return False, "not checked: Docker is not usable"
        out.image = pr.resolve(out.verified)
        return True, f"{out.image.tag} present locally (never built or pulled by a run)"
    out.checks.append(_guard("image", image_ok))

    def cli_ok() -> tuple[bool, str]:
        cli = find_cli(s.cli, pr.environ, pr.which)
        if cli is None:
            return False, f"opencode not found: pass --opencode PATH or set {ENV_OPENCODE}"
        real = os.path.realpath(cli)
        if os.path.basename(cli) != "opencode" or not os.path.isfile(real) or not os.access(real, os.X_OK):
            return False, f"{cli} must be an executable regular file named exactly 'opencode'"
        out.cli = cli
        return True, f"{cli} (mounted read-only at /opt/cli/opencode; not executed on the host)"
    out.checks.append(_guard("cli", cli_ok))

    def ollama_ok() -> tuple[bool, str]:
        host, port = endpoint_target(ch)
        return pr.ollama(host, port, ch.model.partition("/")[2])
    out.checks.append(_guard("ollama", ollama_ok))
    return out


# ── upstream, the surface, the plan ───────────────────────────────────────────────

def load_upstream(runs_dir: Path) -> upstream_import.Upstream:
    try:
        return upstream_import.import_upstream(runs_dir=runs_dir)
    except upstream_import.UpstreamImportError as e:
        raise Stage1Error(f"the pinned upstream cannot be imported: {e}") from None


def load_surface() -> Any:
    """upstream's OPENCODE_SURFACE. Imported after `import_upstream` (which read no .env); it must not touch os.environ."""
    before = dict(os.environ)
    mod = importlib.import_module("harness2.substrate.opencode")
    if dict(os.environ) != before:
        raise Stage1Error("importing upstream's opencode substrate changed os.environ; refusing to continue")
    return mod.OPENCODE_SURFACE


def proposer_decision(s: Settings, cwd: str) -> upstream_egress.Decision:
    """`check_proposer` for the exact argv and environment a stage-1 proposer session uses."""
    ch = s.ch
    base = f"http://{sr.PROXY_ALIAS}:{channels.endpoint_key(ch.endpoints[0])[1]}{API_PATH}"        # type: ignore[index]
    return upstream_egress.check_proposer(
        upstream_egress.ProposerConfig(
            kind="opencode", model=ch.model, base_url=ch.endpoints[0], env=session_env("opencode", model=ch.model, base_url=base),
            cwd=cwd, command=build_command("opencode", cli_container=sr.container_cli_path("/opt/cli/opencode"), model=ch.model)),
        profile=s.profile, channel=s.channel, zdr=s.zdr, classification=s.classification)


def build_plan(s: Settings, pre: Preflight, surface: Any) -> SweepPlan:
    """The `SweepPlan`, or `SweepRefused` listing every reason (including each failed preflight check)."""
    ch = s.ch
    root = s.profile.workspace_roots[0] if s.profile.workspace_roots else "/nonexistent"
    plan = plan_sweep(
        surface=surface, candidates=[SweepCandidate(label, path) for label, path in CANDIDATES], k=K, n_tasks=1,
        max_sessions=ch.caps.sessions, max_wall_seconds=s.wall_s if s.wall_s is not None else ch.caps.wall_s,
        max_cost_or_tokens=CostCap("usd", 0.01), proposer=proposer_decision(s, root), readiness=pre.readiness(),
        profile=s.profile, channel=s.channel, classification=s.classification, zdr=s.zdr)
    if (plan.planned_sessions, plan.planned_rollouts) != (EXPECTED_P, EXPECTED_R):
        raise SweepRefused([f"stage 1 expects P={EXPECTED_P} proposer sessions and R={EXPECTED_R} rollouts, "
                            f"but upstream plans P={plan.planned_sessions}, R={plan.planned_rollouts}"])
    rp = rollout_plan(s.rollouts, upstream_p=plan.planned_sessions, upstream_r=plan.planned_rollouts)
    if rp.sessions > ch.caps.sessions:
        raise SweepRefused([
            f"{rp.sessions} sessions are planned (P={rp.proposer} proposer + R={rp.rollouts} solver rollouts at --rollouts "
            f"{s.rollouts}) but channel {s.channel!r} allows caps.sessions = {ch.caps.sessions} in {s.profile.source}; "
            f"raise caps.sessions to at least {rp.sessions} there, or lower --rollouts"])
    # the extra rollouts are session budget too: the grant, the budget and the run-time checks are all made from this plan
    return dataclasses.replace(plan, planned_rollouts=rp.rollouts)


def approve_command(s: Settings, plan_cli: str | None, profile_path: str | None, wall_s: float | None = None) -> str:
    parts = ["uv", "run", "python", "-m", "cognitive_harness2.stage1", "run", "--approve", s.channel]
    if profile_path:
        parts += ["--profile", profile_path]
    if plan_cli:
        parts += ["--opencode", plan_cli]
    if s.task_set != task_sets.DEFAULT:                          # the default is not spelled out: an old command means what it did
        parts += ["--task-set", s.task_set]
    if s.proposer_hint is not None:                              # spelled out only when asked for: the default is in the plan
        parts += ["--proposer-hint", s.proposer_hint]
    if s.rollouts != 1:
        parts += ["--rollouts", str(s.rollouts)]
    if s.wall_s is not None:                                   # the figure the plan will actually hold to
        parts += ["--wall-s", f"{wall_s if wall_s is not None else s.wall_s:g}"]
    parts += ["--per-session-s", f"{s.per_session_s:g}", "--runs-dir", str(s.runs_dir)]
    return " ".join(shlex.quote(p) for p in parts)


def format_plan(s: Settings, pre: Preflight, plan: SweepPlan | None, refusals: Sequence[str], *, profile_path: str | None,
                with_command: bool = True) -> str:
    ch = s.ch if s.channel in s.profile.channels else None
    L: list[str] = ["STAGE 1: local smoke run, k=1  (this is a PLAN: nothing was started)", ""]
    if ch is not None:
        wall = plan.max_wall_seconds if plan else (s.wall_s or ch.caps.wall_s)
        rp = rollout_plan(s.rollouts, upstream_p=EXPECTED_P, upstream_r=EXPECTED_R)
        n = s.rollouts
        worst = rp.sessions * s.per_session_s
        L += [f"channel          {ch.name}  ({ch.kind}, retention {ch.retention}, max classification {ch.max_classification})",
              f"endpoint         {ch.endpoints[0]}{API_PATH}  (loopback; reached from the containers over the host bridge)",
              f"model            {ch.model}",
              f"classification   {s.classification}  (the {s.ts.spec_name} task, the harness and every prompt are synthetic)",
              f"task set         {s.task_set}  ({s.ts.description})",
              f"task             {s.task}, k={K}, modes={','.join(MODES)}, reflection skipped, attempts 1",
              f"proposer hint    {'on' if s.hint_on else 'off'}  ({s.hint_source}; the runner's ENVIRONMENT RULES block in front of "
              f"each proposer session's system text, never a solver's; --proposer-hint {'off' if s.hint_on else 'on'} flips it)",
              f"expected         P={rp.proposer} proposer sessions (inv1, dom1, agg1) and R={rp.rollouts} solver rollouts "
              + ("(base1, inv1, dom1, agg1)" if n == 1 else f"(base1..base{n}, inv1, dom1, agg1 and its {n - 1} repeats)")
              + ", one model call at a time",
              f"rollouts         {n}  " + ("(upstream's own: base1, inv1, dom1, agg1 once each)" if n == 1 else
                                          f"(base1..base{n} ({n}), the delivered final agg1 ({n}), and inv1, dom1 once each; "
                                          f"upstream rolls one base and one final, the other {n - 1} of each are this runner's)"),
              f"sessions         {rp.proposer} + {rp.rollouts} = {rp.sessions} sessions (profile cap {ch.caps.sessions})"
              + ("" if rp.sessions <= ch.caps.sessions else
                 f": OVER the cap, so this plan is refused; set caps.sessions to at least {rp.sessions}"),
              f"caps             sessions {ch.caps.sessions}, total wall {wall:g} s ({wall / 60:g} min), cost ${ch.caps.cost_usd:g}",
              f"timeouts         {s.per_session_s:g} s per session; worst case {worst:g} s for {rp.sessions} sessions"
              + (f", which is MORE than the {wall:g} s wall: the sweep stops starting sessions at the deadline" if worst > wall else ""),
              f"container        2 GB, 2 CPUs, 256 pids, read-only root, no capabilities, user 65534 (per session)",
              f"leaves machine   nothing: no hosted endpoint, no credential, no egress network",
              f"cost class       {plan.cost_class if plan else 'local'}  ($0)", ""]
    L.append("preflight (none of these started anything):")
    for c in pre.checks:
        L.append(f"  [{'ok' if c.ok else 'FAIL'}] {c.name:<16} {c.detail}")
    L.append("")
    if plan is not None and pre.ok:
        if with_command:
            L += ["READY. To approve this sweep, run (you will be asked to type a confirmation phrase):", "",
                  "  " + approve_command(s, pre.cli, profile_path, plan.max_wall_seconds), ""]
        L.append(f"A run writes under {s.runs_dir}/<sweep id>/ : audit.jsonl and report.json.")
        for w in plan.warnings:
            if w.startswith("max_cost_or_tokens"):            # the nominal cost request is clipped to the zero cap by design
                continue
            if w.startswith(HOST_SESSION_WARNINGS):            # written for a session on the host; these are contained
                continue
            L.append(f"note: {w}")
        L.append("note: check_proposer's two warnings about host tool access and HOME credentials are not shown: "
                 "every session here runs in a container with a tmpfs HOME and no credential")
    else:
        L.append("NOT READY. Nothing can be approved until these are fixed:")
        for r in refusals:
            L.append(f"  - {r}")
    return "\n".join(L) + "\n"


# ── audit ─────────────────────────────────────────────────────────────────────────

_WITHHELD = frozenset({"prompt", "mission", "stdout", "stderr", "env", "argv", "output", "text", "answer", "command",
                       "credential", "token", "secret", "password", "api_key"})
_SECRETISH = re.compile(r"(token|secret|password|passwd|api[_-]?key|credential)", re.I)


def clean(value: Any, depth: int = 0) -> Any:
    """JSON-safe, size-capped, escape-stripped. Anything that looks like a secret's name is withheld."""
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else None
    if isinstance(value, str):
        text = sr.sanitize_terminal(value)
        return text if len(text) <= 300 else text[:300] + "...[cut]"
    if depth >= 4:
        return "[deep]"
    if isinstance(value, Mapping):
        return {str(k)[:100]: ("[withheld]" if str(k).lower() in _WITHHELD or _SECRETISH.search(str(k)) else clean(v, depth + 1))
                for k, v in list(value.items())[:60]}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [clean(v, depth + 1) for v in list(value)[:60]]
    return clean(repr(value), depth + 1)


class Audit:
    """An append-only JSONL file (mode 0600), one line per event, flushed as it is written. Thread-safe."""

    def __init__(self, path: Path, clock: Callable[[], float] = time.monotonic):
        self.path, self._clock, self._t0 = Path(path), clock, clock()
        self._lock = threading.Lock()
        self._seq = 0
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        self._fh: TextIO | None = os.fdopen(fd, "a", encoding="utf-8")
        self.events: list[str] = []                              # event names, for tests and the report

    def emit(self, event: str, **fields: Any) -> None:
        with self._lock:
            if self._fh is None:
                return
            self._seq += 1
            row = {"seq": self._seq, "t": now_iso(), "elapsed_s": round(self._clock() - self._t0, 3), "event": event}
            row.update(clean(fields))
            self._fh.write(json.dumps(row, sort_keys=True) + "\n")
            self._fh.flush()
            self.events.append(event)

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                self._fh.close()
                self._fh = None


# ── approval and the grant ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Approval:
    approved_by: str
    mode: str                  # "tty" (a person typed the phrase) | "selftest"
    phrase: str                # the base phrase (channel and sweep id)
    at: str = field(default_factory=now_iso)
    typed: str = ""            # what was typed and matched: the phrase plus its nonce ("" for the selftest, nothing was typed)
    agent_env: Mapping[str, Any] = field(default_factory=dict)      # `agent_env_record`: the markers checked and the names found


def confirmation_phrase(channel: str, sweep_id: str) -> str:
    return f"approve {channel} {sweep_id}"


# Environment variables that mark a process as running inside an agent harness. The runner refuses to take an approval
# there and NO flag overrides it. Not a security boundary: a process that scrubs its own environment (`env -i`) defeats it.
# It makes the careless route (an agent running `stage1 run` and approving it itself) fail loudly instead of quietly working.
#
# The list matches what the harnesses SET in the processes they spawn, not what they READ. Reading is what a person's own
# profile does: `CODEX_HOME`, `OPENCODE_CONFIG`, `OPENCODE_API_KEY` are settings, and refusing them locked such a person out
# for good (there is no override). So:
#   - CLAUDECODE and CLAUDE_CODE_* : observed in a child of Claude Code (CLAUDECODE, CLAUDE_CODE_ENTRYPOINT,
#     CLAUDE_CODE_SESSION_ID, CLAUDE_CODE_EXECPATH, ...). Kept as a prefix: the set grows with the version. The cost is that a
#     CLAUDE_CODE_* setting exported in a person's profile (CLAUDE_CODE_USE_VERTEX, say) also refuses; unset it for the run.
#   - OPENCODE (exact): the marker opencode sets in its children. The binary's OPENCODE_* names are config knobs, so no prefix.
#   - CODEX_SANDBOX*: the markers codex sets in sandboxed commands (CODEX_SANDBOX, CODEX_SANDBOX_NETWORK_DISABLED). Not
#     CODEX_HOME, its config directory.
#   - ANTIGRAVITY*, GEMINI_CLI*, AGY_*: UNVERIFIED here (those CLIs were not run from this code), kept as prefixes on
#     purpose: a false refusal is fixed by `env -u NAME`, a false pass lets an agent approve its own sweep. If one of these
#     is a config variable of yours, the refusal names it; narrow this list when its real child markers are known.
AGENT_ENV_EXACT = ("CLAUDECODE", "OPENCODE")
AGENT_ENV_PREFIXES = ("CLAUDE_CODE_", "CODEX_SANDBOX", "ANTIGRAVITY", "GEMINI_CLI", "AGY_")
AGENT_ENV_SHOW = 12              # names listed in a refusal (and in its `env -u` command); the rest are counted
TTY_PATH = "/dev/tty"

NONCE_WORDS = ("amber", "anchor", "birch", "cobalt", "comet", "coral", "crane", "delta", "ember", "fennel", "flint",
               "garnet", "harbor", "indigo", "jasper", "juniper", "kelp", "lantern", "linden", "maple", "meadow",
               "nickel", "onyx", "orchid", "pebble", "quartz", "raven", "saffron", "sparrow", "thistle", "umber",
               "velvet", "willow", "yarrow", "zephyr", "cedar", "falcon", "granite", "heron", "ivory", "lagoon",
               "marble", "nectar", "opal", "pewter", "quill", "russet", "sable", "tundra", "valley")


class ApprovalRefused(Exception):
    """The approval gate declined to even ask: no controlling terminal, or an agent-harness environment."""


def agent_markers(environ: Mapping[str, str]) -> list[str]:
    """The names in `environ` that mark an agent-harness process (sorted; values are never read or shown)."""
    return sorted(k for k in environ if k in AGENT_ENV_EXACT or k.startswith(AGENT_ENV_PREFIXES))


def agent_env_record(environ: Mapping[str, str]) -> dict[str, Any]:
    """What the audit keeps of the environment check: the exact names and prefixes that were checked, and the names found
    (never a value). For a real approval `present` is empty, since a marker refuses; for the selftest it may not be."""
    return {"checked_exact": list(AGENT_ENV_EXACT), "checked_prefixes": list(AGENT_ENV_PREFIXES), "present": agent_markers(environ)}


def refuse_in_agent_environment(environ: Mapping[str, str]) -> None:
    found = agent_markers(environ)
    if found:
        shown = found[:AGENT_ENV_SHOW]
        more = f" and {len(found) - len(shown)} more" if len(found) > len(shown) else ""
        unset = "env " + " ".join(f"-u {n}" for n in shown) + " stage1 run ..."
        raise ApprovalRefused(
            "refusing to take an approval inside an agent-harness environment (" + ", ".join(shown) + more +
            "): the approval must come from a person at their own terminal. There is no flag to override this. "
            "From a plain terminal (one that is not an agent session) it just works; if one of these is your own setting, "
            f"drop it for the run: {unset}")


def new_nonce(words: int = 4) -> str:
    return " ".join(secrets.SystemRandom().sample(NONCE_WORDS, words))


class _Terminal:
    """The controlling terminal as one object: a read stream and a write stream on two descriptors of the same device
    (a text `r+` stream needs a seekable file, which a terminal is not)."""

    def __init__(self, rfd: int, wfd: int):
        self._r = os.fdopen(rfd, "r", encoding="utf-8", errors="replace")
        self._w = os.fdopen(wfd, "w", encoding="utf-8", errors="replace", buffering=1)

    def isatty(self) -> bool:
        return self._r.isatty() and self._w.isatty()

    def write(self, s: str) -> int:
        return self._w.write(s)

    def flush(self) -> None:
        self._w.flush()

    def readline(self) -> str:
        return self._r.readline()

    def close(self) -> None:
        for f in (self._w, self._r):
            with contextlib.suppress(OSError):
                f.close()


def open_controlling_terminal(path: str = TTY_PATH) -> _Terminal:
    """The process's controlling terminal, opened for reading and writing; OSError when there is none."""
    rfd = os.open(path, os.O_RDONLY | os.O_NOCTTY)
    try:
        wfd = os.open(path, os.O_WRONLY | os.O_NOCTTY)
    except OSError:
        os.close(rfd)
        raise
    return _Terminal(rfd, wfd)


def confirm_on_terminal(phrase: str, **kw: Any) -> bool:
    """`confirm_typed`, as a bool."""
    return confirm_typed(phrase, **kw) is not None


def confirm_typed(phrase: str, *, stdin: TextIO | None = None, stdout: TextIO | None = None,
                  environ: Mapping[str, str] | None = None, tty: TextIO | None = None,
                  tty_path: str = TTY_PATH, nonce: str | None = None) -> str | None:
    """The string that was typed (the phrase plus its nonce) if it was typed on the process's CONTROLLING terminal, else None.
    The phrase plus a fresh 4-word nonce must be typed exactly.

    What it stops: a pipe, redirected stdin or stdout, a script with no terminal (the confirmation is read from the
    controlling terminal, never from stdin), and anything inside a known agent-harness environment (raises
    `ApprovalRefused`; no flag overrides it). What it does NOT stop: a determined process of the same user that runs this
    under a pty it drives (python `pty.fork`, `script`, `expect`, `tmux send-keys`): it can read the phrase and type it
    back, nonce included, and it can scrub the environment markers. The human remains the control."""
    refuse_in_agent_environment(os.environ if environ is None else environ)
    sin, sout = stdin or sys.stdin, stdout or sys.stdout
    if not (sin.isatty() and sout.isatty()):
        return None
    owned = tty is None
    try:
        term = tty if tty is not None else open_controlling_terminal(tty_path)
    except OSError as e:
        raise ApprovalRefused(f"refusing to take an approval without a controlling terminal ({tty_path}: {e.strerror or e})") from e
    try:
        if not term.isatty():
            raise ApprovalRefused("refusing to take an approval: the controlling terminal is not a terminal")
        expected = f"{phrase} {nonce or new_nonce()}"
        term.write(f"To approve this sweep, type exactly (the last four words change on every run):\n\n  {expected}\n\n> ")
        term.flush()
        try:
            typed = term.readline()
        except (OSError, KeyboardInterrupt):
            return None
        return expected if typed.strip() == expected else None
    finally:
        if owned:
            with contextlib.suppress(OSError):
                term.close()


def approver_name() -> str:
    name = re.sub(r"[^\x20-\x7e]", "?", getpass.getuser() or "unknown").strip()[:80]
    return name or "unknown"


def make_grant(plan: SweepPlan, *, sweep_id: str, approval: Approval, per_session_s: float,
               now: float | None = None) -> ExecutionGrant:
    return ExecutionGrant.from_plan(plan, sweep_id=sweep_id, approved_by=approval.approved_by,
                                    ttl_s=plan.max_wall_seconds + GRANT_MARGIN_S,
                                    per_session_s=min(per_session_s, plan.max_wall_seconds), now=now)


# ── the solver's command line and environment (stage 1) ───────────────────────────

def solver_argv(job: SolverJob) -> tuple[str, ...]:
    """The solver session's argv: the SAME opencode shape as a proposer's (`run --standalone --auto --format json --model M
    PROMPT`, run in the workspace). PROBE-DERIVED from `opencode run --help`; never run against a real model."""
    one_line = " ".join(job.prompt.split())          # the command-line gate refuses control characters, newlines included
    return build_command("opencode", cli_container=job.cli, model=job.model, prompt=one_line)


def solver_env_builder(runner: sr.ProposerSessionRunner) -> Callable[[SolverJob], dict[str, str]]:
    """The provider configuration a solver session needs to reach the model: without it a rollout has no endpoint.
    (The earlier `opencode_env` set only an OPENCODE_PERMISSION policy, which opencode v2 does not read.)"""
    def build(job: SolverJob) -> dict[str, str]:
        local = runner.local_endpoint
        if local is None:
            raise Stage1Error("the solver runner has no local endpoint: stage 1 runs on a local channel only")
        return session_env("opencode", model=job.model, base_url=local + API_PATH)
    return build


def checked_solver_argv(s: Settings, env_builder: Callable[[SolverJob], dict[str, str]]) -> Callable[[SolverJob], tuple[str, ...]]:
    """`solver_argv`, vetted by `upstream_egress.check_proposer` (kind, model, endpoint, command shape, environment) for
    every rollout before it can start. A refusal raises `Stage1Error`, which kills that rollout and nothing else."""
    def build(job: SolverJob) -> tuple[str, ...]:
        argv = solver_argv(job)
        d = upstream_egress.check_proposer(
            upstream_egress.ProposerConfig(kind="opencode", model=job.model, base_url=s.ch.endpoints[0], env=env_builder(job),
                                           cwd=job.workspace, command=argv),
            profile=s.profile, channel=s.channel, zdr=s.zdr, classification=s.classification)
        if not d.allowed:
            raise Stage1Error("the solver command line was refused before anything started: " + "; ".join(d.reasons))
        return argv
    return build


# ── the audited adapter ───────────────────────────────────────────────────────────

class AuditedAdapter(ss.SumSignedAdapter):
    """`SumSignedAdapter` that records every rollout's verdict in the audit trail. It never sees a prompt."""

    def __init__(self, *a: Any, audit: Audit, **kw: Any):
        super().__init__(*a, **kw)
        self._audit = audit
        self._lock = threading.Lock()
        self.rollouts: dict[str, dict] = {}

    def validate_tree(self, tree: dict[str, str]) -> None:
        """Upstream calls this on every proposed harness. When the slot table is enforced (the hard set) the verdict is
        audited as `mutation_check`: which files changed, in which slots, and whether the tree was accepted. The refusal
        text is cut at its first quotation mark, so a line the model wrote never reaches the audit trail."""
        if not self.ts.enforce_slot_table:
            return super().validate_tree(tree)
        try:
            changes = self.tree_changes(tree)
        except Exception:  # noqa: BLE001 - the audit must never decide whether a tree is valid
            changes = {}
        try:
            super().validate_tree(tree)
        except ValueError as e:
            self._audit.emit("mutation_check", ok=False, changed=sorted(changes), slots=dict(sorted(changes.items())),
                             refused=re.split(r"[\"'`]", sr.sanitize_terminal(str(e)))[0][:240].strip())
            raise
        self._audit.emit("mutation_check", ok=True, changed=sorted(changes), slots=dict(sorted(changes.items())))

    def execute(self, task_id: str, harness_dir: Path | None, run_id: str, *, timeout: int | None = None) -> Path:
        label = run_id.rsplit("/", 1)[-1]
        t0 = time.monotonic()
        self._audit.emit("rollout_start", label=label, run_id=run_id, harness="base" if harness_dir is None else "candidate")
        rec: dict[str, Any] = {"label": label, "run_id": run_id}
        try:
            out = super().execute(task_id, harness_dir, run_id, timeout=timeout)
        except BaseException as e:
            rec.update(ok=False, passed=None, error=f"{type(e).__name__}: {sr.sanitize_terminal(str(e))[:240]}",
                       duration_s=round(time.monotonic() - t0, 3))
            self._finish(rec)
            raise
        verdict = self.score(out, task_id)
        raw = self._answer_bytes(out)
        metrics: dict = {}
        with contextlib.suppress(OSError, ValueError):
            metrics = json.loads((out / "metrics.json").read_text())
        rec.update(ok=True, passed=None if verdict is None else bool(verdict["all_pass"]), errors=list(metrics.get("errors") or []),
                   n_passed=None if verdict is None else verdict["n_passed"],
                   n_criteria=None if verdict is None else verdict["n_criteria"],
                   delivered=self.delivered(out), exit_code=metrics.get("exit_code"), timed_out=metrics.get("timed_out"),
                   n_events=metrics.get("n_events"), duration_s=round(time.monotonic() - t0, 3),
                   answer_excerpt=None if raw is None else sr.sanitize_terminal(raw[:EXCERPT].decode("utf-8", "replace")))
        self._finish(rec)
        return out

    def _finish(self, rec: dict) -> None:
        with self._lock:
            self.rollouts[rec["run_id"]] = rec
        self._audit.emit("rollout_verdict", **rec)


# ── teardown ──────────────────────────────────────────────────────────────────────

def _docker(verified: sandbox.VerifiedDocker, args: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run([verified.path, "-H", verified.host, *args], capture_output=True, text=True, timeout=60,
                          env=sandbox._pinned_env(), stdin=subprocess.DEVNULL)


def session_leftovers(verified: sandbox.VerifiedDocker | None, session_ids: Sequence[str]) -> list[str]:
    """Containers and networks still carrying the label of one of THIS sweep's sessions (never anyone else's)."""
    if verified is None:
        return []
    found: list[str] = []
    for sid in session_ids:
        for what, cmd in (("container", ["ps", "-a", "-q"]), ("network", ["network", "ls", "-q"])):
            try:
                ids = _docker(verified, [*cmd, "--filter", f"label={sr.LABEL}={sid}"]).stdout.split()
            except (OSError, subprocess.TimeoutExpired):
                found.append(f"{what}s of session {sid}: could not be listed")
                continue
            found += [f"{what} {i}" for i in ids]
    return found


def remove_leftovers(verified: sandbox.VerifiedDocker | None, items: Sequence[str]) -> None:
    if verified is None:
        return
    for item in items:
        what, _, ident = item.partition(" ")
        if what in ("container", "network") and re.fullmatch(r"[0-9a-f]{6,64}", ident):
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                _docker(verified, ["rm", "-f", ident] if what == "container" else ["network", "rm", ident])


def runtime_entries() -> set[str]:
    """The session and bridge directories in the runtime base. Under a run id (`run_scope`, set by the tests) only that
    run's own; a real sweep has none and reports every such directory, as before."""
    base = sr._runtime_base_dir()
    prefixes = (run_scope.dir_prefix("h2s-"), run_scope.dir_prefix("h2bridge-"))
    with contextlib.suppress(OSError):
        return {n for n in os.listdir(base) if n.startswith(prefixes)}
    return set()


# ── the sweep ─────────────────────────────────────────────────────────────────────

@dataclass
class SweepContext:
    settings: Settings
    plan: SweepPlan
    grant: ExecutionGrant
    approval: Approval
    verified: sandbox.VerifiedDocker
    image: image.ImageRef
    cli: str
    up: upstream_import.Upstream
    surface: Any
    run_dir: Path
    audit: Audit
    approved_for_real_model: bool = True          # approved by the user to call a REAL model (False: the selftest, a fake)
    run_task: Callable[..., Any] | None = None      # tests may replace upstream's run_task


@dataclass
class SweepOutcome:
    report: dict
    exit_code: int


def check_grant(ctx: "SweepContext", *, now: float | None = None) -> None:
    """The grant must be live, for THIS channel and classification, and no larger than the approved plan. Raises
    `ExecutionRefused` before anything is created or started."""
    g, p, s = ctx.grant, ctx.plan, ctx.settings
    if g.expired(time.time() if now is None else now):
        raise ExecutionRefused(f"the execution grant for sweep {g.sweep_id!r} has expired")
    if g.channel != s.channel or g.channel != p.channel:
        raise ExecutionRefused(f"the grant is for channel {g.channel!r}, not this sweep's {s.channel!r}")
    if g.classification != p.classification:
        raise ExecutionRefused("the grant was approved for a different classification than the plan")
    if (g.max_proposer_sessions > p.planned_sessions or g.max_solver_sessions > p.planned_rollouts
            or g.max_wall_s > p.max_wall_seconds):
        raise ExecutionRefused("the grant is larger than the approved plan")
    cap = s.ch.caps.sessions
    if g.max_sessions > cap:                  # the profile's own cap, even when plan and grant agree with each other
        raise ExecutionRefused(f"the grant allows {g.max_sessions} sessions but channel {s.channel!r} has "
                               f"caps.sessions = {cap}; raise it in the profile or lower --rollouts")


def _ledger_view(res: Any) -> dict | None:
    mode = (getattr(res, "modes", None) or {}).get("agg") or {}
    led = mode.get("ledger") if isinstance(mode, dict) else None
    return dict(led) if isinstance(led, dict) else None


def _stage_view(res: Any) -> list[dict]:
    mode = (getattr(res, "modes", None) or {}).get("agg") or {}
    return [{k: st.get(k) for k in ("label", "dim", "fell_back", "kept_base", "attempts", "usd", "cached")} |
            {"error": sr.sanitize_terminal(str(st.get("error") or ""))[:200]}
            for st in (mode.get("stages") or []) if isinstance(st, dict)]


def _natural(label: str) -> list:
    """Sort key putting `base2` before `base10` and `agg1` before `agg1.r2`."""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", label)]


def arm_of(label: str, final_label: str | None) -> str:
    """Which arm of the comparison a rollout belongs to: `base` (base1..baseN), `final` (the delivered final's own rollout
    and its repeats `<final>.r2`..) or `other` (inv1 and dom1, upstream's evidence for the aggregator: not compared)."""
    if re.fullmatch(r"base\d+", label):
        return "base"
    if final_label and (label == final_label or re.fullmatch(re.escape(final_label) + r"\.r\d+", label)):
        return "final"
    return "other"


def arm_rates(rollouts: Sequence[Mapping[str, Any]], final_label: str | None) -> dict:
    """Per arm: its labels, n (every rollout of the arm, a dead one included), how many passed, failed, or were unusable (the
    session died or the judge gave no verdict), and the pass rate over n (None for an empty arm). A dead rollout is NOT
    dropped from n: it counts as not passed, and `unusable` says how many there were."""
    out: dict[str, dict] = {}
    for arm in ("base", "final"):
        mine = sorted((r for r in rollouts if arm_of(r["label"], final_label) == arm), key=lambda r: _natural(r["label"]))
        passed = sum(1 for r in mine if r.get("passed") is True)
        failed = sum(1 for r in mine if r.get("passed") is False)
        unusable = sum(1 for r in mine if r.get("ok") is False or r.get("passed") is None)
        out[arm] = {"labels": [r["label"] for r in mine], "n": len(mine), "passed": passed, "failed": failed,
                    "unusable": unusable, "rate": passed / len(mine) if mine else None}
    return out


def fisher_two_sided(a: int, n1: int, b: int, n2: int) -> float:
    """Fisher's exact test, two-sided: `a` of `n1` passed in one arm, `b` of `n2` in the other. The probability, under 'the
    two arms have one pass rate', of a split at least as unlikely as the one seen."""
    total = a + b
    lo, hi = max(0, total - n2), min(n1, total)

    def p(x: int) -> float:
        return math.comb(n1, x) * math.comb(n2, total - x) / math.comb(n1 + n2, total)

    seen = p(a)
    return min(1.0, sum(p(x) for x in range(lo, hi + 1) if p(x) <= seen * (1 + 1e-9)))


def _selection_view(res: Any, rollouts: Sequence[Mapping[str, Any]]) -> dict | None:
    """What upstream's selection did with the one mode, and what our own verdicts say about it.

    At k=1 upstream does NOT choose between the base and a candidate: `selection.trivial_selection` takes the pool of the
    k finals (only `agg1`; the base is excluded by construction), drops it only if the rollout delivered nothing, and
    otherwise delivers it. `chosen_is_base` is therefore always false, and no score is read. So `adopted` here means:
    the delivered final is an `agg` harness that differs from the base (`chosen_tree_is_base` false). Whether that edit
    HELPED is a separate fact, from this runner's own verdicts on the base and chosen rollouts, which selection never sees.

    `verdict`, from one rollout each (`--rollouts 1`, an anecdote and said so in `evidence`): improved (base failed, chosen
    passed), regressed (the reverse), unchanged (same result), base-kept (no accepted edit reached the final), or unknown.
    From more rollouts it is a comparison of RATES and never a win: higher-rate, lower-rate or same-rate (base-kept and
    unknown as before), with `base_rate` / `chosen_rate` as passed-over-n and `fisher_p`, a two-sided exact test of the
    two counts, which `evidence` turns into words. A dead rollout counts in n as not passed."""
    sel = (getattr(res, "selections", None) or {}).get("agg")
    if not isinstance(sel, dict):
        return None
    by = {r["label"]: r.get("passed") for r in rollouts}
    chosen = sel.get("chosen_label") or None
    tree_is_base = sel.get("chosen_tree_is_base")
    adopted = bool(sel.get("ok") and chosen and tree_is_base is False)
    base_passed, chosen_passed = by.get("base1"), by.get(chosen) if chosen else None
    arms = arm_rates(rollouts, chosen)
    bn, cn = arms["base"], arms["final"]
    single = bn["n"] <= 1 and cn["n"] <= 1
    fisher_p: float | None = None
    evidence = ""
    if not adopted:
        verdict = "base-kept"
    elif single:
        evidence = "one sample each: an anecdote, not a measurement"
        if base_passed is False and chosen_passed is True:
            verdict = "improved"
        elif base_passed is True and chosen_passed is False:
            verdict = "regressed"
        elif base_passed is not None and base_passed == chosen_passed:
            verdict = "unchanged"
        else:
            verdict = "unknown"
    elif bn["unusable"] == bn["n"] or cn["unusable"] == cn["n"]:
        verdict = "unknown"
        evidence = "an arm has no usable rollout"
    else:
        fisher_p = fisher_two_sided(cn["passed"], cn["n"], bn["passed"], bn["n"])
        verdict = ("higher-rate" if cn["rate"] > bn["rate"] else "lower-rate" if cn["rate"] < bn["rate"] else "same-rate")
        evidence = (f"Fisher exact two-sided p={fisher_p:.3g}: a gap this size is unlikely to be chance; still one task and one "
                    "proposer draw, so it measures this final harness, not the method" if fisher_p < 0.05 else
                    f"Fisher exact two-sided p={fisher_p:.2f}: not significant at these sizes; report the rates and claim nothing")
    return {"ok": bool(sel.get("ok")), "trivial": bool(sel.get("trivial")), "chosen_label": chosen,
            "chosen_is_base": bool(sel.get("chosen_is_base")), "chosen_tree_is_base": tree_is_base, "adopted": adopted,
            "n_candidates": sel.get("n_candidates"), "dropped": list(sel.get("dropped") or [])[:10],
            "base_passed": base_passed, "chosen_passed": chosen_passed, "verdict": verdict,
            "base_rate": {"passed": bn["passed"], "n": bn["n"]}, "chosen_rate": {"passed": cn["passed"], "n": cn["n"]},
            "fisher_p": fisher_p, "evidence": evidence,
            "harness_fingerprint": sel.get("harness_fingerprint")}


def selection_line(report: Mapping[str, Any]) -> str:
    sel = report.get("selection")
    if not sel:
        return ""
    word = {True: "passed", False: "failed", None: "no verdict"}
    head = (f"selection {sel['verdict']}: final {sel['chosen_label']} "
            f"({'an edited harness was adopted' if sel['adopted'] else 'the base harness was kept'}); ")
    br, cr = sel.get("base_rate") or {}, sel.get("chosen_rate") or {}
    if max(br.get("n", 0), cr.get("n", 0)) > 1:
        return head + f"pass rate base {br['passed']}/{br['n']}, {sel['chosen_label']} {cr['passed']}/{cr['n']}" + \
            (f"; {sel['evidence']}" if sel.get("evidence") else "") + "\n"
    return head + (f"base1 {word[sel['base_passed']]}, {sel['chosen_label']} {word[sel['chosen_passed']]}"
                   + (f"; {sel['evidence']}" if sel.get("evidence") else "") + "\n")


def pass_rate_line(report: Mapping[str, Any]) -> str:
    """`pass rate base 4/8 (50%), final agg1 7/8 (88%)`: n_passed/n for both arms, from the report's `pass_rates`."""
    pr = report.get("pass_rates")
    if not pr:
        return ""

    def part(name: str, arm: Mapping[str, Any]) -> str:
        if not arm["n"]:
            return f"{name} none"
        extra = f", {arm['unusable']} unusable" if arm["unusable"] else ""
        return f"{name} {arm['passed']}/{arm['n']} ({arm['rate'] * 100:.0f}%{extra})"
    final = f"final {pr['final_label']}" if pr.get("final_label") else "final"
    one = pr["base"]["n"] <= 1 and pr["final"]["n"] <= 1 and pr["final"]["n"] > 0
    return f"pass rate {part('base', pr['base'])}, {part(final, pr['final'])}" + \
        ("  (one sample each: not a measurement)" if one else "") + "\n"


def run_extra_rollouts(adapter: Any, res: Any, *, up: Any, runs_root: Path, task: str, k: int, n: int, audit: Any,
                       exhausted: Callable[[], bool]) -> dict:
    """The rollouts upstream does not make: base2..baseN and `<final>.r2`..`<final>.rN`, after `run_task`, through the same
    adapter (so each is a real solver session under the grant and the budget, and each is audited and judged like the rest).

    Upstream rolls ONE base and ONE of each candidate; `run_task` takes no count. The extras use upstream's own run-id
    namespace (`Namespace.run_id`) and, for the final, the harness directory upstream wrote for the delivered final
    (`<task dir>/harnesses/<final>`), so they are the same harnesses upstream rolled once. They alternate (base2, final.r2,
    base3, ...) so a drift in the model or the GPU over the run does not fall on one arm. They run only when an edited
    harness was adopted: with no edit the final IS the base and there is nothing to compare. A rollout that dies is
    counted (and is not in the pass count); a spent budget stops the rest. Returns
    `{base, final, errors, skipped}`: the labels run, one `label: error` per failure, and why the rest did not run."""
    out: dict[str, Any] = {"base": [], "final": [], "errors": [], "skipped": None}
    if n <= 1:
        return out
    sel = (getattr(res, "selections", None) or {}).get("agg")
    chosen = sel.get("chosen_label") if isinstance(sel, dict) else None
    if not (isinstance(sel, dict) and sel.get("ok") and chosen):
        out["skipped"] = "upstream's selection gave no usable final"
    elif sel.get("chosen_tree_is_base") is not False:
        out["skipped"] = f"no edited harness was adopted (the final {chosen} is the base): nothing to compare, no extra rollout run"
    else:
        ns = up.schedule.Namespace(adapter.spec.name, adapter.substrate.name, "agg", k)
        hdir = ns.task_dir(runs_root, task) / "harnesses" / chosen
        if not hdir.is_dir():
            out["skipped"] = f"the harness directory of the final {chosen} is not on disk"
    if out["skipped"]:
        audit.emit("extra_rollouts_skipped", n=n, reason=out["skipped"])
        return out
    plan = [(label, None if arm == "base" else hdir) for i in range(2, n + 1)
            for arm, label in (("base", f"base{i}"), ("final", f"{chosen}.r{i}"))]
    audit.emit("extra_rollouts_start", n=n, chosen=chosen, planned=len(plan))
    for done, (label, harness_dir) in enumerate(plan):
        if exhausted():
            out["skipped"] = f"the sweep budget ran out: {done} of {len(plan)} extra rollouts ran"
            break
        try:
            adapter.execute(task, harness_dir, ns.run_id(task, label))
        except Exception as e:  # noqa: BLE001 - one dead rollout is a result (an unusable one), not the end of the sweep
            out["errors"].append(f"{label}: {type(e).__name__}: {sr.sanitize_terminal(str(e))[:200]}")
        out["base" if harness_dir is None else "final"].append(label)
    audit.emit("extra_rollouts_end", ran=len(out["base"]) + len(out["final"]), errors=len(out["errors"]), skipped=out["skipped"])
    return out


def sessions_view(starts: Sequence[Mapping[str, Any]], ends: Sequence[Mapping[str, Any]], *,
                  rollout_errors: Sequence[str], stage_errors: Sequence[str]) -> dict:
    """What happened to the sessions, from the runner's own `session_start` / `session_end` events (numbers and
    `host:port` strings; no prompt, no model output). `reached_model` counts sessions during which the egress proxy opened
    a connection to the host bridge, i.e. to the local model endpoint: the CLI got as far as the model's door. It does
    not say the model answered. `first_error` is the first CLI-reported cause (an `error` event of a solver session, else
    a proposer stage's error text)."""
    exit_codes: dict[str, int] = {}
    denied: dict[str, int] = {}
    reasons: set[str] = set()
    requests: dict[str, int] = {}
    reached = timed_out = 0
    for e in ends:
        key = "none" if e.get("exit_code") is None else str(e["exit_code"])
        exit_codes[key] = exit_codes.get(key, 0) + 1
        timed_out += bool(e.get("timed_out"))
        reached += bool(e.get("model_requests"))
        for h in e.get("denied_hosts") or []:
            denied[h] = denied.get(h, 0) + 1
        reasons.update(e.get("denied_reasons") or [])
        dr = e.get("denied_requests")
        if isinstance(dr, Mapping):
            for k, n in dr.items():
                if isinstance(k, str) and isinstance(n, int) and not isinstance(n, bool) and n > 0 and (k == "other" or " " in k):
                    requests[k] = requests.get(k, 0) + n
    first = next((sr.sanitize_terminal(x)[:160] for x in (*rollout_errors, *stage_errors) if x), None)
    # the status of a provider HTTP error in the CLI's own words ("... failed with HTTP 403"), to tell a refusal by the model
    # endpoint from a denial by the proxy (which has its own `denied_hosts`)
    http_status = re.search(r"\bHTTP (\d{3})\b", first or "")
    view = {"started": len(starts), "ended": len(ends), "reached_model": reached, "exit_codes": dict(sorted(exit_codes.items())),
            "timed_out": timed_out, "denied_hosts": dict(sorted(denied.items(), key=lambda kv: (-kv[1], kv[0]))),
            "denied_reasons": sorted(reasons), "first_error": first,
            "first_error_http": int(http_status.group(1)) if http_status else None}
    if requests:
        # what the route filter refused, as `METHOD path` (the proxy's sanitised form): most frequent first, at most
        # DENIED_REQUESTS_MAX keys, the remainder (and any per-session `other`) summed in a last `other`
        named = sorted(((k, n) for k, n in requests.items() if k != "other"), key=lambda kv: (-kv[1], kv[0]))
        kept, rest = named[:sr.DENIED_REQUESTS_MAX], named[sr.DENIED_REQUESTS_MAX:]
        other = requests.get("other", 0) + sum(n for _, n in rest)
        view["denied_requests"] = dict(kept) | ({"other": other} if other else {})
    return view


def _n(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def sessions_lines(report: Mapping[str, Any], *, max_hosts: int = 5) -> list[str]:
    """The sessions summary printed at the end of a run. On a failure it says what happened instead of just a status."""
    v = report.get("sessions_summary") or {}
    started, reached = v.get("started", 0), v.get("reached_model", 0)
    head = f"outcome   {_n(started, 'session')} started, {reached} reached the model"
    codes = v.get("exit_codes") or {}
    bad = [f"{n} exited rc={c}" for c, n in codes.items() if c not in ("0", "none")]
    if v.get("timed_out"):
        bad.append(f"{v['timed_out']} timed out")
    parts = [head + (": " + ", ".join(bad) if bad else (", all exited rc=0" if started and codes.get("0") == started else ""))]
    if v.get("first_error"):
        parts.append(f"first error: {v['first_error']}")
    denied = v.get("denied_hosts") or {}
    if denied:
        why = ", " + ", ".join(v["denied_reasons"]) if v.get("denied_reasons") else ""
        shown = [f"{h} ({_n(n, 'session')}{why})" for h, n in list(denied.items())[:max_hosts]]
        more = len(denied) - max_hosts
        parts.append("proxy denied: " + ", ".join(shown) + (f", +{more} more" if more > 0 else ""))
        if v.get("denied_requests"):
            reqs = list(v["denied_requests"].items())
            left = len(reqs) - max_hosts
            parts.append("denied requests: " + ", ".join(f"{k} x{n}" for k, n in reqs[:max_hosts]) + (f", +{left} more" if left > 0 else ""))
    elif v.get("first_error_http") in (401, 403) and reached:
        # the second real run: every session reached ollama, the proxy denied nothing, ollama itself answered 403
        parts.append(f"the proxy denied nothing, the model endpoint itself refused (HTTP {v['first_error_http']}); "
                     "a local Ollama answers 403 to a Host name or Origin header it does not accept")
    lines = ["; ".join(parts) + "\n"]
    if started and not reached:
        lines.append("          no session reached the model: nothing was asked of it. Fix the cause above, then run again.\n")
    return lines


def execute_sweep(ctx: SweepContext) -> SweepOutcome:
    """Run the approved sweep and tear everything down, whatever happens. Returns the report; never leaves a file out.

    Call this only after the approval gate: it is the first thing that starts a session (and so the first thing that
    opens the host bridge).
    """
    s, g, audit = ctx.settings, ctx.grant, ctx.audit
    ch = s.ch
    t0 = time.monotonic()
    started_at = now_iso()
    session_ids: list[str] = []
    starts: list[dict] = []
    ends: list[dict] = []
    ids_lock = threading.Lock()
    before_entries = runtime_entries()
    scratch = Path(s.profile.workspace_roots[0]) / g.sweep_id
    error: BaseException | None = None
    res: Any = None
    adapter: AuditedAdapter | None = None
    extra: dict[str, Any] = {"base": [], "final": [], "errors": [], "skipped": None}      # the rollouts OUR runner adds (n > 1)
    budget = g.make_budget()
    hint = proposer_hint.build_hint(ctx.surface) if s.hint_on else None
    hint_info = {"on": hint is not None, "source": s.hint_source, "sha": hint.sha if hint else None}

    def observer(event: str, fields: Mapping[str, Any]) -> None:
        if event in ("session_start", "session_end"):
            with ids_lock:
                if event == "session_start":
                    session_ids.append(str(fields.get("session")))
                (starts if event == "session_start" else ends).append(dict(fields))
        audit.emit(event, **fields)

    audit.emit("sweep_start", sweep_id=g.sweep_id, channel=g.channel, model=ch.model, endpoint=ch.endpoints[0],
               approved_for_real_model=ctx.approved_for_real_model, k=K, modes=list(MODES), task=s.task, task_set=s.task_set,
               proposer_hint=hint_info,
               caps=ch.caps.to_dict(),
               per_session_s=g.per_session_s, max_wall_s=g.max_wall_s,
               max_proposer_sessions=g.max_proposer_sessions, max_solver_sessions=g.max_solver_sessions)
    try:
        check_grant(ctx)
        scratch.mkdir(mode=0o755)
        proposer_scratch, solver_scratch = scratch / "proposer", scratch / "solver"
        proposer_scratch.mkdir(mode=0o755)
        results, adapter_root = ctx.run_dir / "results", ctx.run_dir / "adapter"
        results.mkdir()
        adapter_root.mkdir()
        runs_root = ctx.run_dir / "upstream-runs"
        runs_root.mkdir(exist_ok=True)

        solver_runner = sr.ProposerSessionRunner(
            ctx.verified, ctx.image, ch, zdr=s.zdr, classification=s.classification, role="solver", budget=budget,
            workspace_roots=s.profile.workspace_roots, observer=observer)
        solver_env = solver_env_builder(solver_runner)
        execution = SolverExecution(
            runner=solver_runner, grant=g, cli=ctx.cli, results_root=results, workspace_root=solver_scratch,
            task_loader=s.ts.solver_task, argv_builder=checked_solver_argv(s, solver_env), env_builder=solver_env,
            tmp_exec=TMP_EXEC["opencode"], gate=GATE, gate_wait_s=g.max_wall_s)
        substrate = CognitiveSubstrate(ctx.surface, execution=execution)

        session = SessionConfig(
            verified=ctx.verified, image=ctx.image, profile=s.profile, channel=s.channel, zdr=s.zdr,
            classification=s.classification, scratch_root=str(proposer_scratch), cli_paths={"opencode": ctx.cli},
            timeout_s=g.per_session_s, api_path=API_PATH, gate_wait_s=g.max_wall_s, budget=budget, observer=observer)
        prop = make_proposer(ctx.up, session, audit_dir=ctx.run_dir / "proposer-audit", timeout=int(g.per_session_s), hint=hint)
        spec = ctx.up.spec.BenchSpec(**{**ss.spec_fields(adapter_root), "name": s.ts.spec_name},
                                     prompts=ctx.up.spec.BenchPrompts(**s.ts.prompt_fields),
                                     channels={"proposer": prop.spec, "selector": prop.spec})
        adapter = AuditedAdapter(adapter_root, spec, substrate, results_root=results, timeout=int(g.per_session_s), audit=audit,
                                 taskset=s.ts)

        run = ctx.run_task or ctx.up.schedule.run_task
        audit.emit("run_task_start", task=s.task, k=K, modes=list(MODES), libraries=None)
        res = run(s.task, K, adapter=adapter, prop=prop, runs_root=runs_root, modes=MODES, libraries=None,
                  selector_prop=make_selector_prop(prop))
        audit.emit("run_task_end", status=getattr(res, "status", None))
        extra = run_extra_rollouts(adapter, res, up=ctx.up, runs_root=runs_root, task=s.task, k=K, n=s.rollouts, audit=audit,
                                   exhausted=lambda: budget.snapshot()["remaining_s"] <= 0)
    except BaseException as e:   # noqa: BLE001 - audited, torn down below, reported
        error = e
        audit.emit("error", type=type(e).__name__, message=sr.sanitize_terminal(str(e))[:300])
    finally:
        teardown = _teardown(ctx, scratch, session_ids, before_entries)
        audit.emit("teardown", **teardown)

    ledger = _ledger_view(res) if res is not None else None
    stages = _stage_view(res) if res is not None else []
    dirty = bool(teardown["docker_leftovers"] or teardown["scratch_left"] or teardown["gate_locked"])
    identity_ok = bool(ledger and ledger.get("P") == EXPECTED_P and ledger.get("R") == EXPECTED_R)
    if error is not None:
        status = "aborted" if isinstance(error, KeyboardInterrupt) else "failed"
    elif res is None:
        status = "failed"
    else:
        status = str(getattr(res, "status", "unknown"))
    if dirty:
        status = f"{status}+dirty"
    rollouts = sorted((adapter.rollouts.values() if adapter else []), key=lambda r: _natural(r["label"]))
    selection = _selection_view(res, rollouts) if res is not None else None
    final_label = selection["chosen_label"] if selection else None
    pass_rates = {"final_label": final_label, **arm_rates(rollouts, final_label)}
    rp = rollout_plan(s.rollouts, upstream_p=EXPECTED_P, upstream_r=EXPECTED_R)
    with ids_lock:
        summary = sessions_view(list(starts), list(ends),
                                rollout_errors=[x for r in rollouts for x in (r.get("errors") or [])],
                                stage_errors=[st["error"] for st in stages if st.get("error")])
    # A real run in which sessions started but none reached the model is a failure, whatever else the report says.
    no_model = ctx.approved_for_real_model and summary["started"] > 0 and summary["reached_model"] == 0
    snapshot = budget.snapshot()
    report = {
        "schema": 1, "kind": "stage1", "sweep_id": g.sweep_id, "status": status,
        # `real_model_call` used to be the approval flag, so a run where no session got past a proxy denial claimed a model call.
        # It is now true only when a session really reached the model endpoint; the approval is its own field.
        "approved_for_real_model": ctx.approved_for_real_model, "cli_sessions_started": summary["started"],
        "model_sessions_reached": summary["reached_model"],
        "real_model_call": bool(ctx.approved_for_real_model and summary["reached_model"] > 0), "sessions_summary": summary,
        "approved_by": g.approved_by, "approval_mode": ctx.approval.mode, "approved_at": ctx.approval.at,
        "started_at": started_at, "finished_at": now_iso(), "wall_s": round(time.monotonic() - t0, 3),
        "channel": {"name": ch.name, "kind": ch.kind, "model": ch.model, "endpoint": ch.endpoints[0],
                    "retention": ch.retention, "caps": ch.caps.to_dict()},
        "task": s.task, "task_set": s.task_set, "proposer_hint": hint_info, "k": K, "modes": list(MODES),
        "classification": s.classification,
        "grant": g.to_dict(),
        "ledger": ledger, "budget_identity": {"expected_P": EXPECTED_P, "expected_R": EXPECTED_R, "ok": identity_ok},
        "stages": stages, "selection": selection,
        "rollouts": [{k: r.get(k) for k in ("label", "run_id", "ok", "passed", "n_passed", "n_criteria", "delivered",
                                              "exit_code", "timed_out", "n_events", "duration_s", "error", "errors",
                                              "answer_excerpt")} | {"arm": arm_of(r["label"], final_label)}
                     for r in rollouts],
        # n_passed / n per arm, over every rollout of the arm (a dead one counts as not passed): the measurement `selection`
        # is judged on. `rollouts_plan` is the session arithmetic and what the extra rollouts did.
        "pass_rates": pass_rates,
        "rollouts_plan": {"n": rp.n, "upstream_rollouts": rp.upstream_rollouts, "extra_base_planned": rp.extra_base,
                          "extra_final_planned": rp.extra_final, "rollouts_planned": rp.rollouts,
                          "sessions_planned": rp.sessions, "extra_base_run": list(extra["base"]),
                          "extra_final_run": list(extra["final"]), "extra_errors": list(extra["errors"]),
                          "extra_skipped": extra["skipped"]},
        "rollouts_passed": sum(1 for r in rollouts if r.get("passed") is True),
        "rollouts_failed": sum(1 for r in rollouts if r.get("passed") is False or not r.get("ok")),
        "sessions": {"counts": snapshot["counts"], "refusals": snapshot["refusals"], "remaining_s": snapshot["remaining_s"],
                     "list": [{k: v for k, v in x.items() if k in ("id", "role", "timeout_s", "duration_s", "outcome")}
                              for x in snapshot["sessions"]]},
        "errors": ([f"{type(error).__name__}: {sr.sanitize_terminal(str(error))[:300]}"] if error else [])
                  + [sr.sanitize_terminal(str(x))[:300] for x in (getattr(res, "errors", None) or [])],
        "teardown": teardown, "audit": str(audit.path),
    }
    report_path = ctx.run_dir / "report.json"
    fd = os.open(report_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)
        fh.write("\n")
    audit.emit("sweep_end", status=status, wall_s=report["wall_s"], ledger=ledger, rollouts_passed=report["rollouts_passed"],
               rollouts_failed=report["rollouts_failed"], pass_rates=pass_rates, report=str(report_path))
    code = (130 if isinstance(error, KeyboardInterrupt) else 3 if dirty else
            1 if error is not None or res is None or not identity_ok or no_model else 0)
    return SweepOutcome(report, code)


def _teardown(ctx: SweepContext, scratch: Path, session_ids: Sequence[str], before_entries: set[str]) -> dict:
    """Remove what this sweep made. Each step is attempted whatever the others did."""
    with contextlib.suppress(Exception):
        shutil.rmtree(scratch, ignore_errors=True)
    left_docker = session_leftovers(ctx.verified, session_ids)
    if left_docker:
        remove_leftovers(ctx.verified, left_docker)
        left_docker = session_leftovers(ctx.verified, session_ids)
    new_entries = sorted(runtime_entries() - before_entries)
    return {"scratch_left": scratch.exists(), "docker_leftovers": left_docker, "sessions_seen": len(session_ids),
            "gate_locked": GATE.locked(), "new_runtime_entries": new_entries}


# ── plan and run, as commands ─────────────────────────────────────────────────────

def _settings_from(args: argparse.Namespace, environ: Mapping[str, str]) -> Settings:
    profile = load_profile(args.profile, environ)
    runs = Path(args.runs_dir).expanduser() if args.runs_dir else default_runs_dir(environ)
    if not runs.is_absolute():
        raise Stage1Error("--runs-dir must be an absolute path")
    wall = args.wall_s
    per = args.per_session_s if args.per_session_s is not None else PER_SESSION_S
    for name, v in (("--wall-s", wall), ("--per-session-s", per)):
        if v is not None and not (isinstance(v, (int, float)) and 0 < v < 7 * 24 * 3600 and v == v):
            raise Stage1Error(f"{name} must be a positive number of seconds")
    return Settings(profile=profile, channel=args.channel or CHANNEL, classification=args.classification, wall_s=wall,
                    per_session_s=float(per), cli=args.opencode, runs_dir=runs,
                    task_set=getattr(args, "task_set", None) or task_sets.DEFAULT,
                    proposer_hint=getattr(args, "proposer_hint", None), rollouts=getattr(args, "rollouts", 1))


def run_plan(args: argparse.Namespace, *, environ: Mapping[str, str], probes: Probes | None = None,
             out: TextIO | None = None) -> int:
    """`plan`: read-only. Exit 0 if a run could be approved, 2 if not."""
    out = out or sys.stdout
    s = _settings_from(args, environ)
    pr = probes or Probes(environ=environ)
    pre = preflight(s, pr)
    refusals = [f"{c.name}: {c.detail}" for c in pre.failing()]
    plan: SweepPlan | None = None
    if s.channel in s.profile.channels:
        try:
            load_upstream(Path(tempfile.gettempdir()) / "h2-stage1-plan-unused")      # a path value only: nothing is created
            plan = build_plan(s, pre, load_surface())
        except SweepRefused as e:
            refusals = list(dict.fromkeys([*refusals, *e.reasons]))
        except Stage1Error as e:
            refusals.append(str(e))
    out.write(format_plan(s, pre, plan, refusals, profile_path=args.profile or environ.get(ENV_CHANNELS)))
    return 0 if plan is not None and pre.ok else 2


def run_run(args: argparse.Namespace, *, environ: Mapping[str, str], probes: Probes | None = None,
            stdin: TextIO | None = None, stdout: TextIO | None = None,
            run_task: Callable[..., Any] | None = None, tty: TextIO | None = None) -> int:
    """`run`: the approval gate, then the sweep. Every refusal here happens before anything is created.
    `tty` is for tests: the stream the confirmation is read from (default: the controlling terminal, /dev/tty)."""
    out = stdout or sys.stdout
    err = lambda m: print(f"stage1: {m}", file=sys.stderr)       # noqa: E731
    try:
        refuse_in_agent_environment(environ)
    except ApprovalRefused as e:
        err(f"{e}; nothing was started")
        return 2
    if args.yes_i_am_the_user:
        err("--yes-i-am-the-user is not accepted by `run`: the non-interactive approval exists only for `selftest`")
        return 2
    if not args.approve:
        err(f"refusing to run: pass --approve {args.channel or CHANNEL} (run `plan` first; nothing was started)")
        return 2
    s = _settings_from(args, environ)
    if args.approve != s.channel:
        err(f"refusing to run: --approve names {args.approve!r} but this sweep is for channel {s.channel!r}; nothing was started")
        return 2
    sin = stdin or sys.stdin
    if not (sin.isatty() and out.isatty()):
        err("refusing to run: the approval phrase must be typed on an interactive terminal; nothing was started")
        return 2
    sweep_id = new_sweep_id()
    run_dir = s.runs_dir / sweep_id
    pr = probes or Probes(environ=environ)
    pre = preflight(s, pr)
    if not pre.ok:
        for c in pre.failing():
            err(f"preflight failed, {c.name}: {c.detail}")
        err("refusing to run; nothing was started")
        return 2
    try:
        up = load_upstream(run_dir / "upstream-runs")
        plan = build_plan(s, pre, (surface := load_surface()))
    except SweepRefused as e:
        for r in e.reasons:
            err(f"the sweep plan is refused: {r}")
        return 2
    out.write(format_plan(s, pre, plan, [], profile_path=None, with_command=False))
    phrase = confirmation_phrase(s.channel, sweep_id)
    try:
        typed = confirm_typed(phrase, stdin=sin, stdout=out, environ=environ, tty=tty)
    except ApprovalRefused as e:
        err(f"{e}; nothing was started")
        return 2
    if typed is None:
        err("the confirmation phrase was not typed exactly; nothing was started")
        return 2
    approval = Approval(approver_name(), "tty", phrase, typed=typed, agent_env=agent_env_record(environ))
    return _approved(s, plan, pre, up, surface, sweep_id, run_dir, approval, real=True, run_task=run_task, out=out)


def _approved(s: Settings, plan: SweepPlan, pre: Preflight, up: Any, surface: Any, sweep_id: str, run_dir: Path,
              approval: Approval, *, real: bool, run_task: Callable[..., Any] | None, out: TextIO,
              grant: ExecutionGrant | None = None) -> int:
    """From here on the sweep is approved: create the run directory, the grant and the audit, then run."""
    run_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
    audit = Audit(run_dir / "audit.jsonl")
    try:
        audit.emit("plan", plan=plan.to_dict(), preflight={c.name: c.ok for c in pre.checks})
        audit.emit("approval", approved_by=approval.approved_by, mode=approval.mode, sweep_id=sweep_id,
                   channel=s.channel, approved_for_real_model=real, phrase=approval.phrase, typed=approval.typed,
                   agent_env=dict(approval.agent_env))
        g = grant or make_grant(plan, sweep_id=sweep_id, approval=approval, per_session_s=s.per_session_s)
        audit.emit("grant", **g.to_dict())
        assert pre.verified is not None and pre.image is not None and pre.cli is not None
        ctx = SweepContext(settings=s, plan=plan, grant=g, approval=approval, verified=pre.verified, image=pre.image,
                           cli=pre.cli, up=up, surface=surface, run_dir=run_dir, audit=audit, approved_for_real_model=real,
                           run_task=run_task)
        outcome = execute_sweep(ctx)
    finally:
        audit.close()
    r = outcome.report
    out.write(f"\nstatus {r['status']}  wall {r['wall_s']:g} s  ledger {r['ledger'] and (r['ledger']['P'], r['ledger']['R'])}  "
              f"rollouts passed {r['rollouts_passed']}, failed {r['rollouts_failed']}\n")
    out.writelines(sessions_lines(r))
    out.write(pass_rate_line(r))
    out.write(selection_line(r))
    if r.get("rollouts_plan", {}).get("extra_skipped"):
        out.write(f"extra rollouts: {r['rollouts_plan']['extra_skipped']}\n")
    out.write(f"audit  {audit.path}\nreport {run_dir / 'report.json'}\n")
    return outcome.exit_code


# ── selftest ──────────────────────────────────────────────────────────────────────

class FakeOllama:
    """A throwaway stand-in for Ollama on a free loopback port: `GET /api/tags` lists the model, anything else answers 200.
    It records only request-line counts, never a body. Like a real loopback Ollama it refuses, with an empty 403, a `Host`
    that is not a loopback/private address, `localhost`, this machine's name or a `.localhost`/`.local`/`.internal` name,
    and a foreign `Origin` (`ollama_guard`); the refused requests are counted in `rejected`, not in `hits`."""

    def __init__(self, model: str = "gemma4:12b"):
        self.model, self.hits = model, 0
        self.rejected: list[tuple[int, str]] = []        # (status, the Host header) of every refused request
        outer = self

        import http.server

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:           # noqa: N802
                refused = ollama_guard.refusal(self.headers)
                if refused:
                    outer.rejected.append((refused, self.headers.get("Host") or ""))
                    self.send_response(refused)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                outer.hits += 1
                body = json.dumps({"models": [{"name": outer.model}]} if self.path.startswith("/api/tags") else {"data": []}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a: Any) -> None:
                pass
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, name="fake-ollama", daemon=True)

    def __enter__(self) -> "FakeOllama":
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(5)


def selftest_profile(root: Path, port: int, *, wall_s: int = 600, sessions: int = 7) -> channels.ChannelProfile:
    return channels.validate({
        "workspace_roots": [str(root)],
        "channels": {CHANNEL: {"kind": "opencode", "provider": "ollama", "endpoint": f"http://127.0.0.1:{port}/v1",
                               "model": "ollama/gemma4:12b", "retention": "local", "max_classification": "restricted",
                               "approval": "per-sweep", "caps": {"sessions": sessions, "wall_s": wall_s, "cost_usd": 0}}}},
        source="<selftest>")


# What the fake PROPOSER does in a selftest sweep (the `PROPOSER_SCRIPT` line of `selftest_fake_opencode.sh`):
#   sign-check          appends one line to systemprompt.md (the original stage-1 selftest behaviour)
#   format-rules        appends the notation-reading rules (`sum_signed_hard.FORMAT_RULES`) to systemprompt.md: a legal c1 edit
#   forbid-guardrails   appends to guardrails.md (c2, protected)                        -> refused by the harvest
#   forbid-executable   adds scripts/helper.py with the executable bit                  -> refused by the harvest
#   forbid-frontmatter  prepends a frontmatter block to systemprompt.md                 -> refused by the slot table
#   forbid-script       adds a plain scripts/helper.py (c7, not mutable)                -> refused by the slot table
SCENARIOS = ("sign-check", "format-rules", "forbid-guardrails", "forbid-executable", "forbid-frontmatter", "forbid-script")
REFUSED_BY = {"forbid-guardrails": "harvest", "forbid-executable": "harvest",
              "forbid-frontmatter": "slot-table", "forbid-script": "slot-table"}
REFUSAL_TEXT = {"forbid-guardrails": "protected", "forbid-executable": "executable",
                "forbid-frontmatter": "frontmatter", "forbid-script": "slot c7"}
DEFAULT_SCENARIO = {"easy": "sign-check", "hard": "format-rules"}
PROPOSER_MARKER = "PROPOSER_SCRIPT=sign-check\n"


def selftest_cli_text(scenario: str) -> str:
    """The fake opencode with its proposer behaviour set to `scenario`. Loud if the script no longer has the line."""
    if scenario not in SCENARIOS:
        raise Stage1Error(f"unknown selftest scenario {scenario!r}")
    text = FAKE_CLI.read_text()
    if text.count(PROPOSER_MARKER) != 1:
        raise Stage1Error(f"the fake opencode {FAKE_CLI.name} has no single {PROPOSER_MARKER.strip()!r} line to set")
    return text.replace(PROPOSER_MARKER, f"PROPOSER_SCRIPT={scenario}\n")


def harness_evidence(run_dir: Path, ts: ss.TaskSet, surface: Any) -> dict:
    """What the sweep left on disk about the three proposed harnesses, as plain data (read BEFORE a temp run dir is
    removed): which files each differs from the base in, whether the notation rules are in each `systemprompt.md`, the
    slot-table verdict on each diff (`classify_path` + `check_mutation`, run again here, independently of the adapter's
    own in-pipeline check) and the `mutation_check` events the pipeline audited."""
    from . import upstream_surface as us
    from .invariants import InvariantViolation

    dirs = sorted((run_dir / "upstream-runs").glob("*/*/harnesses"))
    if len(dirs) != 1:
        return {"error": f"expected one harnesses directory under {run_dir.name}/upstream-runs, found {len(dirs)}"}
    root = dirs[0]

    def files(d: Path) -> dict[str, str]:
        return {p.relative_to(d).as_posix(): p.read_text(encoding="utf-8", errors="replace")
                for p in sorted(d.rglob("*")) if p.is_file()}

    rules = ss_hard.FORMAT_RULES.strip()
    base = files(root / "base")
    ev: dict[str, Any] = {"base_has_rules": rules in base.get("systemprompt.md", ""), "changed": {}, "rules_in": [],
                          "slot_table_problems": {}}
    for label in ("inv1", "dom1", "agg1"):
        d = root / label
        if not d.is_dir():
            ev["changed"][label] = None
            continue
        cur = files(d)
        changed = sorted(p for p in {*base, *cur} if base.get(p) != cur.get(p))
        ev["changed"][label] = changed
        problems = []
        for p in changed:
            try:
                slot, _comp = us.classify_path(surface, p)
                us.check_mutation(slot, p, base.get(p, ""), cur.get(p, ""), spec=surface)
            except InvariantViolation as e:
                problems.append(f"{p}: {str(e)[:120]}")
        ev["slot_table_problems"][label] = problems
        if rules in cur.get("systemprompt.md", ""):
            ev["rules_in"].append(label)
    checks = []
    with contextlib.suppress(OSError, ValueError):
        for line in (run_dir / "audit.jsonl").read_text().splitlines():
            row = json.loads(line)
            if row.get("event") == "mutation_check":
                checks.append({k: row.get(k) for k in ("ok", "changed", "slots")})
    ev["mutation_checks"] = checks
    return ev


def proposer_audit_evidence(run_dir: Path) -> list[dict]:
    """What the proposer audit (`proposer-audit/<label>.<key>.meta.json` and `.system.md`) says about the hint, per stage
    session: the meta's `hint` flag, its hash, whether the recorded block matches that hash, and whether the system text the
    session was given starts with the block. Read by the selftest only; nothing here is printed or logged."""
    out = []
    for meta_path in sorted((run_dir / "proposer-audit").glob("*.meta.json")):
        with contextlib.suppress(OSError, ValueError):
            meta = json.loads(meta_path.read_text())
            system = meta_path.with_name(meta_path.name.removesuffix(".meta.json") + ".system.md")
            text = system.read_text(encoding="utf-8") if system.is_file() else ""
            block = meta.get("hint_block") or ""
            out.append({"label": meta.get("label"), "hint": meta.get("hint"), "hint_sha": meta.get("hint_sha"),
                        "block_matches_sha": bool(block) and proposer_hint.digest(block) == meta.get("hint_sha"),
                        "system_starts_with_block": bool(block) and text.startswith(block)})
    return out


def run_selftest(*, base: Path | None = None, out: TextIO | None = None, probes: Probes | None = None,
                 wall_s: int | None = None, per_session_s: float = 120.0,
                 grant_edit: Callable[[ExecutionGrant], ExecutionGrant] | None = None,
                 cli_text: str | None = None, run_task: Callable[..., Any] | None = None,
                 task_set: str = task_sets.DEFAULT, scenario: str | None = None, proposer_hint: str | None = None,
                 rollouts: int = 1) -> SweepOutcome:
    """The WHOLE pipeline, with a fake opencode and a fake endpoint: profile -> preflight -> plan -> grant -> contained
    sessions on the real rootless daemon -> report -> teardown. No model is called and no real CLI runs.

    `scenario` is what the fake PROPOSER does (see `SCENARIOS`); the default is the old sign-check line on the easy set and
    the scripted format-rules edit on the hard set. `cli_text`, when given, replaces the fake CLI whole.
    `proposer_hint` and `rollouts` are the stage-1 flags of the same names (the fake profile gets exactly the
    `caps.sessions` the rollout arithmetic needs, and `wall_s` defaults to 600 s plus 60 s per session beyond seven)."""
    out = out or sys.stdout
    if not FAKE_CLI.is_file():
        raise Stage1Error(f"the fake opencode {FAKE_CLI} is missing")
    if task_set not in task_sets.TASK_SETS:
        raise Stage1Error(f"unknown task set {task_set!r}; choose one of: {', '.join(task_sets.NAMES)}")
    scenario = scenario or DEFAULT_SCENARIO[task_set]
    if scenario not in SCENARIOS:
        raise Stage1Error(f"unknown selftest scenario {scenario!r}; choose one of: {', '.join(SCENARIOS)}")
    root = Path(tempfile.mkdtemp(prefix="h2-stage1-selftest-", dir=base)).resolve()
    try:
        os.chmod(root, 0o755)
        wsroot, bindir = root / "workspaces", root / "bin"
        wsroot.mkdir()
        bindir.mkdir()
        cli = bindir / "opencode"
        cli.write_text(cli_text if cli_text is not None else selftest_cli_text(scenario))
        cli.chmod(0o755)
        sane = rollouts if isinstance(rollouts, int) and not isinstance(rollouts, bool) and rollouts >= 1 else 1   # Settings refuses the rest
        rp = rollout_plan(sane, upstream_p=EXPECTED_P, upstream_r=EXPECTED_R)
        wall = wall_s if wall_s is not None else 600 + 60 * (rp.sessions - 7)
        with FakeOllama() as fake:
            s = Settings(profile=selftest_profile(wsroot, fake.port, wall_s=wall, sessions=rp.sessions), per_session_s=per_session_s,
                         cli=str(cli), runs_dir=root / "runs", task_set=task_set, proposer_hint=proposer_hint, rollouts=rollouts)
            pr = probes or Probes(environ={})
            pre = preflight(s, pr)
            if not pre.ok:
                raise Stage1Error("selftest preflight failed: " + "; ".join(f"{c.name}: {c.detail}" for c in pre.failing()))
            sweep_id = new_sweep_id()
            run_dir = s.runs_dir / sweep_id
            up = load_upstream(run_dir / "upstream-runs")
            surface = load_surface()
            plan = build_plan(s, pre, surface)
            approval = Approval(approver_name(), "selftest", confirmation_phrase(s.channel, sweep_id),
                                agent_env=agent_env_record(os.environ))
            grant = make_grant(plan, sweep_id=sweep_id, approval=approval, per_session_s=s.per_session_s)
            if grant_edit is not None:
                grant = grant_edit(grant)
            code = _approved(s, plan, pre, up, surface, sweep_id, run_dir, approval, real=False, run_task=run_task, out=out,
                             grant=grant)
            report = json.loads((run_dir / "report.json").read_text())
            report["_run_dir"] = str(run_dir)
            report["_scenario"] = scenario
            report["_harness"] = harness_evidence(run_dir, s.ts, surface)
            report["_proposer_audit"] = proposer_audit_evidence(run_dir)
            report["_fake_endpoint_hits"] = fake.hits
            report["_fake_endpoint_rejected"] = [list(x) for x in fake.rejected]
            return SweepOutcome(report, code)
    finally:
        if base is None:                                  # a caller-supplied base (a test's tmp_path) is its own to clean
            shutil.rmtree(root, ignore_errors=True)


def selftest_problems(outcome: SweepOutcome) -> list[str]:
    """Everything the selftest asserts about a finished fake sweep. Empty means it passed. The easy set is judged as it
    always was (all four rollouts correct, no stage fell back); the hard set by `hard_selftest_problems`."""
    r, bad = outcome.report, []
    hard = r.get("task_set") == "hard"
    plan = r.get("rollouts_plan") or {}
    ran_r = EXPECTED_R + len(plan.get("extra_base_run") or []) + len(plan.get("extra_final_run") or [])   # upstream's four + ours
    if outcome.exit_code != 0:
        bad.append(f"exit code {outcome.exit_code}, status {r.get('status')}")
    led = r.get("ledger") or {}
    if (led.get("P"), led.get("R")) != (EXPECTED_P, EXPECTED_R):
        bad.append(f"ledger P={led.get('P')} R={led.get('R')}, expected P={EXPECTED_P} R={EXPECTED_R}")
    counts = r["sessions"]["counts"]
    if (counts.get("proposer"), counts.get("solver")) != (EXPECTED_P, ran_r):
        bad.append(f"sessions run: proposer {counts.get('proposer')}, solver {counts.get('solver')}, expected {EXPECTED_P} and {ran_r}")
    if len(r["rollouts"]) != ran_r:
        bad.append(f"{len(r['rollouts'])} rollouts ran, expected {ran_r}")
    if plan.get("extra_errors"):
        bad.append(f"extra rollouts failed: {plan['extra_errors']}")
    if not hard:
        if r["rollouts_passed"] != ran_r:
            bad.append(f"rollouts: {r['rollouts_passed']} of {len(r['rollouts'])} passed, expected {ran_r} of {ran_r}")
        if any(st.get("fell_back") for st in r["stages"]):
            bad.append("a proposer stage fell back")
    t = r["teardown"]
    if t["scratch_left"] or t["docker_leftovers"] or t["gate_locked"]:
        bad.append(f"teardown left something: {t}")
    if r.get("real_model_call") or r.get("approved_for_real_model"):
        bad.append("a selftest report claims a real model call")
    for status, host in r.get("_fake_endpoint_rejected") or []:
        bad.append(f"the fake model endpoint refused a request with HTTP {status} as a real Ollama would: Host {host!r}")
    sv = r.get("sessions_summary") or {}
    if sv.get("started") != EXPECTED_P + ran_r or sv.get("reached_model") != sv.get("started"):
        bad.append(f"the fake model endpoint was reached by {sv.get('reached_model')} of {sv.get('started')} sessions, "
                   f"expected all {EXPECTED_P + ran_r}")
    if hard:
        bad += hard_selftest_problems(r)
    return bad + hint_selftest_problems(r)


def hint_selftest_problems(r: Mapping[str, Any]) -> list[str]:
    """What the selftest asserts about the proposer hint: the report says what the flag was, and the proposer audit of each
    of the three stage sessions agrees (hint true with the block, its matching hash and the block in front of the system
    text the session was given, or hint false with none of them). Nothing to check when a report carries neither."""
    info, audit = r.get("proposer_hint"), r.get("_proposer_audit")
    if info is None or audit is None:
        return []
    bad: list[str] = []
    if sorted(str(a.get("label", "")).split(".")[0] for a in audit) != ["agg1", "dom1", "inv1"]:
        bad.append(f"the proposer audit holds {[a.get('label') for a in audit]}, expected one session each for inv1, dom1 and agg1")
    for a in audit:
        if a.get("hint") is not info.get("on"):
            bad.append(f"{a.get('label')}: the audit says hint={a.get('hint')} but the sweep ran with hint={info.get('on')}")
        elif info.get("on") and not (a.get("hint_sha") == info.get("sha") and a.get("block_matches_sha") and a.get("system_starts_with_block")):
            bad.append(f"{a.get('label')}: the audit does not hold the block, its hash and the system text that carries it")
        elif not info.get("on") and (a.get("hint_sha") or a.get("system_starts_with_block")):
            bad.append(f"{a.get('label')}: hint is off but the audit records a block")
    return bad


def hard_selftest_problems(r: Mapping[str, Any]) -> list[str]:
    """What the hard-set selftest asserts about the improvement loop, for the scenario the fake proposer played.

    `format-rules` (the scripted proposer adds the notation rules to systemprompt.md, body only):
      * every proposer stage was accepted with a changed harness (not `fell_back`, not `kept_base`);
      * the edit was HARVESTED: the on-disk inv1, dom1 and agg1 harnesses differ from the base in `systemprompt.md` and
        nowhere else, carry the rules, and the base does not;
      * it passed the slot table: `check_mutation` run again here is silent, and the pipeline audited one `mutation_check`
        per stage with `ok` true and `{systemprompt.md: c1}`;
      * it RAN as a candidate and changed the outcome: base1 failed (it wrote the total of a reader that ignores the
        notations, 2 of 3 criteria) while inv1, dom1 and agg1 passed (the exact expected line);
      * it was ADOPTED by upstream's selection: the delivered final is agg1, chosen_is_base false, chosen_tree_is_base
        false, so `adopted` and the runner's verdict is `improved`.
    `forbid-*` (the proposer tries c2, an executable, a frontmatter block or a script): every stage fell back to the base,
    no harness differs from it, no rollout passes (a base harness fails this task), the delivered final is a base-equal
    agg1 so the runner says `base-kept`, and the refusal came from the layer the scenario targets (the harvest, or the
    slot table with `mutation_check` events whose `ok` is false)."""
    bad: list[str] = []
    scenario, task = r.get("_scenario"), r.get("task")
    try:
        inst = ss_hard.instance(task)
    except ValueError:
        return [f"the hard selftest ran an unknown task {task!r}"]
    stages = {st["label"]: st for st in r.get("stages") or []}
    rollouts = {x["label"]: x for x in r.get("rollouts") or []}
    sel = r.get("selection") or {}
    ev = r.get("_harness") or {}
    mchecks = ev.get("mutation_checks") or []
    plan = r.get("rollouts_plan") or {}
    n = plan.get("n", 1)
    extra_base, extra_final = list(plan.get("extra_base_run") or []), list(plan.get("extra_final_run") or [])
    if sorted(stages) != ["agg1", "dom1", "inv1"] or sorted(rollouts) != sorted(["agg1", "base1", "dom1", "inv1", *extra_base, *extra_final]):
        return bad + [f"unexpected stages {sorted(stages)} or rollouts {sorted(rollouts)}"]
    naive_line = f"Total: {ss_hard.naive_total(inst.input_text)}"
    for label in ["base1", *extra_base]:                    # every base rollout, upstream's and ours, is the weak reader's
        base = rollouts[label]
        if base.get("passed") is not False or base.get("n_passed") != 2 or (base.get("answer_excerpt") or "").strip() != naive_line:
            bad.append(f"{label} must fail on the total alone, with the weak reader's answer {naive_line!r}: "
                       f"passed={base.get('passed')} n_passed={base.get('n_passed')} answer={base.get('answer_excerpt')!r}")
    if "error" in ev:
        bad.append(ev["error"])
    if scenario == "format-rules":
        for label in ("inv1", "dom1", "agg1"):
            st, ro = stages[label], rollouts[label]
            if st.get("fell_back") or st.get("kept_base") or st.get("error"):
                bad.append(f"stage {label} was not accepted as a changed harness: {st}")
            if (ev.get("changed") or {}).get(label) != ["systemprompt.md"]:
                bad.append(f"{label}: the harvested harness changed {(ev.get('changed') or {}).get(label)}, expected only systemprompt.md")
            if label not in (ev.get("rules_in") or []):
                bad.append(f"{label}: the notation rules are not in the harvested systemprompt.md")
            if (ev.get("slot_table_problems") or {}).get(label):
                bad.append(f"{label}: the slot table refuses the diff: {ev['slot_table_problems'][label]}")
            if ro.get("passed") is not True or (ro.get("answer_excerpt") or "").strip() != inst.expected_line:
                bad.append(f"candidate {label} should pass with {inst.expected_line!r}: passed={ro.get('passed')} "
                           f"answer={ro.get('answer_excerpt')!r}")
        if ev.get("base_has_rules"):
            bad.append("the base harness already holds the notation rules")
        if len(mchecks) != 3 or any(c.get("ok") is not True or c.get("changed") != ["systemprompt.md"]
                                    or c.get("slots") != {"systemprompt.md": "c1"} for c in mchecks):
            bad.append(f"expected three accepted mutation_check events on systemprompt.md (c1), got {mchecks}")
        want = dict(ok=True, trivial=True, chosen_label="agg1", chosen_is_base=False, chosen_tree_is_base=False,
                    adopted=True, verdict="improved" if n == 1 else "higher-rate", base_passed=False, chosen_passed=True)
        if {k: sel.get(k) for k in want} != want:
            bad.append(f"selection should adopt the edited agg1 and be judged {want['verdict']}: {sel}")
        if n > 1:                                            # the rollouts of ours: all there, all as expected, reported as rates
            if len(extra_base) != n - 1 or len(extra_final) != n - 1 or plan.get("extra_skipped") or plan.get("extra_errors"):
                bad.append(f"expected {n - 1} extra base and {n - 1} extra final rollouts, run without error: {plan}")
            for label in extra_final:
                ro = rollouts[label]
                if ro.get("passed") is not True or (ro.get("answer_excerpt") or "").strip() != inst.expected_line:
                    bad.append(f"extra final rollout {label} should pass with {inst.expected_line!r}: passed={ro.get('passed')} "
                               f"answer={ro.get('answer_excerpt')!r}")
            pr = r.get("pass_rates") or {}
            if (pr.get("base") or {}).get("passed") != 0 or (pr.get("base") or {}).get("n") != n \
                    or (pr.get("final") or {}).get("passed") != n or (pr.get("final") or {}).get("n") != n:
                bad.append(f"pass rates should be base 0/{n} and final {n}/{n}: {pr}")
            if not (isinstance(sel.get("fisher_p"), float) and sel["fisher_p"] < 0.05) or "improved" in str(sel.get("verdict")):
                bad.append(f"{n} rollouts of each should give a significant rate gap and never the word improved: {sel}")
    elif scenario in REFUSED_BY:
        for label in ("inv1", "dom1", "agg1"):
            st, ro = stages[label], rollouts[label]
            if not st.get("fell_back") or REFUSAL_TEXT[scenario] not in (st.get("error") or ""):
                bad.append(f"stage {label} should have been refused ({REFUSAL_TEXT[scenario]!r}): {st}")
            if (ev.get("changed") or {}).get(label) != []:
                bad.append(f"{label}: a refused proposal left a different harness: {(ev.get('changed') or {}).get(label)}")
            if ro.get("passed") is not False:
                bad.append(f"{label}: the base-equal candidate should fail this task, passed={ro.get('passed')}")
        if REFUSED_BY[scenario] == "harvest" and mchecks:
            bad.append(f"the harvest should refuse before the slot table is asked, but {len(mchecks)} mutation_check events exist")
        if REFUSED_BY[scenario] == "slot-table" and (len(mchecks) != 3 or any(c.get("ok") is not False for c in mchecks)):
            bad.append(f"expected three refused mutation_check events, got {mchecks}")
        want = dict(ok=True, trivial=True, chosen_label="agg1", chosen_is_base=False, chosen_tree_is_base=True,
                    adopted=False, verdict="base-kept", base_passed=False, chosen_passed=False)
        if {k: sel.get(k) for k in want} != want:
            bad.append(f"selection should deliver a base-equal agg1 and the runner say base-kept: {sel}")
        if n > 1 and (extra_base or extra_final or "no edited harness was adopted" not in str(plan.get("extra_skipped"))):
            bad.append(f"with no edit adopted no extra rollout should run, and the plan should say why: {plan}")
    else:
        bad.append(f"the hard selftest has no expectations for scenario {scenario!r}")
    return bad


# ── command line ──────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m cognitive_harness2.stage1", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--profile", help=f"channel profile TOML (else ${ENV_CHANNELS})")
    common.add_argument("--channel", default=None, help=f"channel name (default {CHANNEL})")
    common.add_argument("--opencode", help=f"host path of the opencode binary (else ${ENV_OPENCODE}, ~/.opencode/bin/opencode, PATH)")
    common.add_argument("--wall-s", type=float, help="total wall clock for the sweep; can only LOWER the profile's wall_s")
    common.add_argument("--per-session-s", type=float, help=f"timeout of one session (default {PER_SESSION_S:g})")
    common.add_argument("--runs-dir", help="PARENT directory for the per-sweep run directory (default ~/.local/state/cognitive-engine/harness2-stage1)")
    common.add_argument("--classification", default=CLASSIFICATION, choices=channels.CLASSIFICATIONS)
    common.add_argument("--task-set", default=task_sets.DEFAULT, choices=task_sets.NAMES,
                        help="easy: sum-signed, 5 clean amounts, the base harness already passes it (default); "
                             "hard: sum-signed-hard, 12 amounts in mixed notations the base harness does not explain")
    common.add_argument("--proposer-hint", choices=HINT_CHOICES, default=None,
                        help="on: put the runner's ENVIRONMENT RULES (which harness files are mutable and which are "
                             "read-only) in front of each proposer session's system text; off: do not (run #4's behaviour). "
                             "Default: on for --task-set hard, off for easy")
    common.add_argument("--rollouts", type=int, default=1, metavar="N",
                        help=f"rollouts of the base harness AND of the delivered final, each (default 1 = upstream's own single "
                             f"one; 1..{MAX_ROLLOUTS}). Upstream rolls one of each, so the other N-1 are made by this runner "
                             "after it; sessions = 3 proposers + (4 + 2*(N-1)) rollouts must fit the profile's caps.sessions")
    sub.add_parser("plan", parents=[common], help="read-only: validate, preflight, print the plan and the approve command")
    run = sub.add_parser("run", parents=[common], help="approve and run the sweep (the only place a real model is called)")
    run.add_argument("--approve", metavar="CHANNEL", help="the channel you approve; a phrase must also be typed on a terminal")
    run.add_argument("--yes-i-am-the-user", action="store_true", help=argparse.SUPPRESS)
    st = sub.add_parser("selftest", help="the whole pipeline with a fake opencode and a fake endpoint; no model is called")
    st.add_argument("--keep-in", metavar="DIR", help="run inside DIR and keep the audit and report there (default: a temp dir, removed)")
    st.add_argument("--task-set", default=task_sets.DEFAULT, choices=task_sets.NAMES,
                    help="hard: also prove the improvement loop (an edit is harvested, checked, run as a candidate and adopted; "
                         "forbidden edits are refused), one sweep per scenario")
    st.add_argument("--proposer-hint", choices=HINT_CHOICES, default=None,
                    help="as for plan and run (default: on for --task-set hard, off for easy)")
    st.add_argument("--rollouts", type=int, default=1, metavar="N",
                    help="as for plan and run: N rollouts of the base and of the delivered final (the fake profile gets "
                         "caps.sessions = 3 + 4 + 2*(N-1) to match)")
    st.add_argument("--scenario", choices=SCENARIOS, help="run only this fake-proposer scenario (default: all of the task set's)")
    return p


def selftest_detail(report: Mapping[str, Any]) -> str:
    """The second line of a selftest's summary: the sessions that ran, the hint and the pass rates (the first line is pinned)."""
    sv, plan = report.get("sessions_summary") or {}, report.get("rollouts_plan") or {}
    hint = report.get("proposer_hint") or {}
    return (f"  {sv.get('started')} sessions (3 proposer + {sv.get('started', 0) - 3} rollouts; planned {plan.get('sessions_planned')}), "
            f"proposer hint {'on, ' + str(hint.get('sha')) if hint.get('on') else 'off'}; "
            + (pass_rate_line(report).strip() or "no pass rate"))


HARD_SCENARIOS = ("format-rules", "forbid-guardrails", "forbid-executable", "forbid-frontmatter", "forbid-script")


def run_hard_selftests(args: argparse.Namespace) -> int:
    """`selftest --task-set hard`: one fake sweep per scenario (the adopted edit, then each forbidden one)."""
    if args.scenario is not None and args.scenario not in SCENARIOS:
        raise Stage1Error(f"unknown selftest scenario {args.scenario!r}")
    failed = 0
    for scenario in ([args.scenario] if args.scenario else HARD_SCENARIOS):
        base = None
        if args.keep_in:
            base = Path(args.keep_in).resolve() / scenario
            base.mkdir(parents=True, exist_ok=True)
        outcome = run_selftest(base=base, task_set=args.task_set, scenario=scenario, out=io.StringIO(),
                               proposer_hint=args.proposer_hint, rollouts=args.rollouts)
        problems = selftest_problems(outcome)
        for p in problems:
            print(f"selftest[{args.task_set}/{scenario}] FAILED: {p}", file=sys.stderr)
        sel = outcome.report.get("selection") or {}
        print(f"selftest[{args.task_set}/{scenario}] {'FAILED' if problems else 'passed'}: "
              f"rollouts {outcome.report['rollouts_passed']}/{len(outcome.report['rollouts'])} correct, "
              f"selection {sel.get('verdict')} (final {sel.get('chosen_label')}, "
              f"{'edited harness adopted' if sel.get('adopted') else 'base harness kept'}), "
              f"teardown clean: {not outcome.report['teardown']['docker_leftovers'] and not outcome.report['teardown']['scratch_left']}")
        print(selftest_detail(outcome.report))
        failed += bool(problems)
    return 1 if failed else 0


def main(argv: Sequence[str] | None = None) -> int:
    args_in = list(sys.argv[1:] if argv is None else argv)
    if not args_in or args_in[0].startswith("-") and args_in[0] not in ("-h", "--help"):
        args_in.insert(0, "plan")
    parser = build_parser()
    args = parser.parse_args(args_in)
    try:
        if args.cmd == "plan":
            return run_plan(args, environ=os.environ)
        if args.cmd == "run":
            return run_run(args, environ=os.environ)
        if args.cmd == "selftest" and args.task_set != task_sets.DEFAULT:
            return run_hard_selftests(args)
        if args.cmd == "selftest":
            outcome = run_selftest(base=Path(args.keep_in).resolve() if args.keep_in else None, proposer_hint=args.proposer_hint,
                                 rollouts=args.rollouts)
            problems = selftest_problems(outcome)
            for p in problems:
                print(f"selftest FAILED: {p}", file=sys.stderr)
            print("selftest " + ("FAILED" if problems else "passed") +
                  f": P={outcome.report['ledger'] and outcome.report['ledger']['P']} "
                  f"R={outcome.report['ledger'] and outcome.report['ledger']['R']}, "
                  f"rollouts {outcome.report['rollouts_passed']}/{len(outcome.report['rollouts'])} correct, "
                  f"teardown clean: {not outcome.report['teardown']['docker_leftovers'] and not outcome.report['teardown']['scratch_left']}")
            print(selftest_detail(outcome.report))
            return 1 if problems else 0
        parser.print_help()
        return 2
    except Stage1Error as e:
        print(f"stage1: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("stage1: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
