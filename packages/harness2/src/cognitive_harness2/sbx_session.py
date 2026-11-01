"""The sbx route for MODEL sessions (opt-in; README, "sbx model route").

A model session is a proposer or solver `opencode` that calls the local Ollama. On Docker it reaches it through a proxy
container and a host bridge. This module is the same session on an `sbx` microVM: the SAME `egress_proxy.py` (route filter,
model pinning) runs as a host process on `127.0.0.1:<P>`, and the sandbox is allowed to reach exactly that port, as five
HTTP rules (one per `OLLAMA_ROUTE_ALLOW` pair) scoped to that one sandbox. The global deny-all policy is never touched.

This file holds the PURE layer and the selection: how `sbx policy ls --json` is read, what "deny-all plus our own rule"
means, the sandbox names, and which runtime model sessions use. `SbxSessionRunner` (below it) is the runner.
"""
from __future__ import annotations

import contextlib
import json
import math
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import run_scope, sandbox
from . import session_runner as sr
from .egress_proxy import OLLAMA_ROUTE_ALLOW
from .host_bridge import HostBridge

ENV_MODEL_SANDBOX = "COGNITIVE_HARNESS2_MODEL_SANDBOX"
RUNTIME_DOCKER, RUNTIME_SBX, RUNTIME_REFUSED = "docker", "sbx", "refused"

# The sbx versions the model route has been verified on by its live test (tests/test_sbx_session.py, COGNITIVE_HARNESS2_SBX_LIVE=1).
# Separate from `sandbox.VERIFIED_SBX_VERSIONS` (candidate execution): that flow does not use rules, a proxy or the exec of a CLI.
VERIFIED_SBX_MODEL_VERSIONS: frozenset[str] = frozenset({"0.50.1"})

NAME_PREFIX = "h2m"
# groups: 1 scope, 2 pid, then (3 boot, 4 pid namespace, 5 start time) together or not at all, 6 session id
_NAME = re.compile(r"^h2m-(?:([0-9a-f]{8,32})|0)-([0-9]{1,10})(?:-([0-9a-f]{8})-([0-9a-f]{1,8})-([0-9a-f]{1,10}))?-([0-9a-f]{12})$")
_SCOPE_SANDBOX = "sandbox:"
_DENY_ACTIONS = frozenset({"net:connect:tcp", "net:connect:udp"})
_RULE_HOST = re.compile(r"^localhost:([0-9]{1,5})$")
_ROUTES = frozenset(OLLAMA_ROUTE_ALLOW)


# ── the setting ───────────────────────────────────────────────────────────────────

def model_sandbox_setting(environ: Mapping[str, str]) -> tuple[str, str | None]:
    """(mode, a problem with the value). Unset, empty and `docker` mean Docker; `sbx` means sbx. Anything else is a problem,
    not a default: a typo must not silently run model sessions on a runtime the user did not ask for."""
    raw = environ.get(ENV_MODEL_SANDBOX) or ""
    value = raw.strip().lower()
    if value in ("", "docker"):
        return RUNTIME_DOCKER, None
    if value == "sbx":
        return RUNTIME_SBX, None
    return RUNTIME_DOCKER, f"{ENV_MODEL_SANDBOX}={raw!r} is not recognised (use docker, or sbx to opt in; there is no auto)"


# ── names ─────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Owner:
    """Who made a sandbox, as far as a later process can tell. `boot` is the first 8 hex digits of the kernel's boot id (every
    process of an earlier boot is gone), `ns` the pid-namespace inode in hex (a pid means something only inside its own
    namespace, and a devcontainer or a second WSL distro shares the sbx daemon but not our pids), `start` the process start time in
    clock ticks since boot (a recycled pid has another one)."""
    boot: str
    ns: str
    start: int


def _proc_start(pid: int) -> int | None:
    """The start time (clock ticks since boot) of process `pid` in THIS pid namespace, or None when there is no such process (or it
    is a zombie or dead). `OSError` when /proc cannot say: the caller then judges nothing."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return None
    try:
        fields = raw[raw.rindex(")") + 2:].split()          # after the `(comm)`, which may contain spaces and parentheses
        state, start = fields[0], int(fields[19])
    except (ValueError, IndexError):
        raise OSError(f"cannot read /proc/{pid}/stat") from None
    return None if state in ("Z", "X") else start


def current_owner() -> Owner | None:
    """This process's `Owner`, or None when it cannot be read (not Linux): its sandboxes then carry no owner and are never swept."""
    try:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip().replace("-", "")[:8].lower()
        ns = re.fullmatch(r"pid:\[([0-9]+)\]", os.readlink("/proc/self/ns/pid"))
        start = _proc_start(os.getpid())
    except OSError:
        return None
    if not (re.fullmatch(r"[0-9a-f]{8}", boot) and ns and start is not None and len(format(int(ns.group(1)), "x")) <= 8):
        return None
    return Owner(boot, format(int(ns.group(1)), "x"), start)


def sandbox_name(session_id: str, *, pid: int | None = None, scope: str | None = None, owner: Owner | None = None) -> str:
    """`h2m-<run scope or 0>-<owner pid>-<boot>-<pid namespace>-<start time>-<session id>`. A leftover can be attributed to its
    owner, and shown to be dead, from its name alone (sbx has no labels). With no `pid` the name is this process's, with its
    own `Owner` when /proc can say. A `pid` given without an `owner` makes the legacy name `h2m-<scope>-<pid>-<id>`, which carries
    no proof of anything and is never swept."""
    if not re.fullmatch(r"[0-9a-f]{12}", session_id):
        raise ValueError("the session id must be 12 lowercase hex characters")
    if pid is None:
        pid, owner = os.getpid(), owner or current_owner()
    tail = f"-{owner.boot}-{owner.ns}-{owner.start:x}" if owner is not None else ""
    return f"{NAME_PREFIX}-{scope or '0'}-{pid}{tail}-{session_id}"


def parse_sandbox_name(name: str) -> tuple[str | None, int, str] | None:
    """(scope, pid, session id) of a sandbox name this module made, else None."""
    m = _NAME.fullmatch(name) if isinstance(name, str) else None
    return (m.group(1), int(m.group(2)), m.group(6)) if m else None


def parse_owner(name: str) -> Owner | None:
    """The `Owner` in a sandbox name this module made; None for a legacy name or one that is not ours."""
    m = _NAME.fullmatch(name) if isinstance(name, str) else None
    return Owner(m.group(3), m.group(4), int(m.group(5), 16)) if m and m.group(3) else None


def is_stale(name: str, me: Owner | None, *, proc_start: Callable[[int], int | None] = _proc_start) -> bool:
    """True only when `name` is ours AND carries an owner AND that owner is provably gone, seen from `me`:
    - another boot: every process of it is gone, so yes;
    - the same boot, another pid namespace: its pids mean nothing here, so NO (it may well be alive);
    - the same boot and namespace: the pid is not running, or is running with another start time (it was recycled).
    A legacy name (no owner), no identity of `me`, or an unreadable /proc judges nothing. A pid alone is never proof."""
    parsed, owner = parse_sandbox_name(name), parse_owner(name)
    if parsed is None or owner is None or me is None:
        return False
    if owner.boot != me.boot:
        return True
    if owner.ns != me.ns:
        return False
    try:
        start = proc_start(parsed[1])
    except OSError:
        return False
    return start is None or start != owner.start


# ── reading `sbx policy ls --json` ────────────────────────────────────────────────

def _rules(policy_json: str) -> tuple[list[dict] | None, str | None]:
    try:
        doc = json.loads(policy_json)
    except ValueError:
        return None, "`sbx policy ls --json` did not return JSON"
    rules = doc.get("rules") if isinstance(doc, dict) else None
    if not isinstance(rules, list):
        return None, "`sbx policy ls --json` has no `rules` list"
    if not all(isinstance(r, dict) for r in rules):
        return None, "`sbx policy ls --json` has a malformed rule"
    return rules, None


def _is_net(rule: dict) -> bool:
    return sandbox.sbx_rule_is_network(rule.get("resource_type"))


def _sandbox_of(rule: dict) -> str | None:
    scope = rule.get("scope")
    return scope[len(_SCOPE_SANDBOX):] if isinstance(scope, str) and scope.startswith(_SCOPE_SANDBOX) else None


def _is_deny_all(rule: dict) -> bool:
    return (rule.get("resource_type") == "network" and rule.get("decision") == "deny" and rule.get("scope") == "global"
            and rule.get("status") == "active" and isinstance(rule.get("resources"), list) and "**" in rule["resources"]
            and isinstance(rule.get("actions"), list) and _DENY_ACTIONS <= set(rule["actions"]))


def _our_rule_key(rule: dict, port: int | None = None) -> tuple[str, str, int] | None:
    """(method, path, port) when `rule` is exactly an HTTP allow of one `OLLAMA_ROUTE_ALLOW` pair at `localhost:<port>`
    (`port` given: that port only), else None. One resource, one method, one target, the pair on the list."""
    if rule.get("resource_type") != "http" or rule.get("decision") != "allow":
        return None
    res, methods, targets = rule.get("resources"), rule.get("methods"), rule.get("http_targets")
    if not (isinstance(res, list) and len(res) == 1 and isinstance(methods, list) and len(methods) == 1
            and isinstance(targets, list) and len(targets) == 1 and isinstance(targets[0], dict)):
        return None
    m = _RULE_HOST.fullmatch(res[0]) if isinstance(res[0], str) else None
    if not m or targets[0].get("host") != res[0]:
        return None
    rport = int(m.group(1))
    method, path = methods[0], targets[0].get("path")
    if not (1 <= rport <= 65535) or (port is not None and rport != port) or (method, path) not in _ROUTES:
        return None
    return method, path, rport


def model_policy_problem(policy_json: str, *, ollama_port: int | None = None) -> str | None:
    """Selection: None only when the GLOBAL listing says deny-all stands and nothing but sessions of this module allows
    anything.

    Required: an active global `**` deny for tcp and udp. Refused: any network or http ALLOW rule whose scope is `global`;
    any scope that is neither `global` nor `sandbox:<name>`; a rule scoped to an `h2m-` sandbox that is not one of ours
    (an HTTP allow of `localhost:<port>` for a pair on `OLLAMA_ROUTE_ALLOW`, never `ollama_port`). A rule scoped to some
    other sandbox does not apply to a sandbox this module creates (the per-sandbox listing of every session is the
    authoritative check, `sandbox_policy_problem`) and is ignored."""
    rules, problem = _rules(policy_json)
    if problem:
        return problem
    assert rules is not None
    deny_all = False
    for rule in rules:
        type_problem = sandbox.sbx_rule_type_problem(rule)      # a type nobody has observed is foreign: refused, never skipped
        if type_problem:
            return type_problem
        if not _is_net(rule):
            continue
        decision = rule.get("decision")
        if decision not in ("allow", "deny"):
            return f"a network rule has the unknown decision {decision!r}"
        scope, owner = rule.get("scope"), _sandbox_of(rule)
        if scope != "global" and owner is None:
            return f"a network rule has the unknown scope {scope!r}"
        if decision == "deny":
            deny_all = deny_all or _is_deny_all(rule)
            continue
        if scope == "global":
            return f"a global network allow rule exists ({rule.get('name') or rule.get('id')!r}): the policy is not deny-all"
        if parse_sandbox_name(owner or "") is None:
            continue
        key = _our_rule_key(rule)
        if key is None or (ollama_port is not None and key[2] == ollama_port):
            return f"a rule scoped to the h2m sandbox {owner!r} is not one of ours ({rule.get('id')!r})"
    if not deny_all:
        return "no active global network deny-all rule (`**`, tcp and udp): the policy is not deny-all"
    return None


def sandbox_policy_problem(policy_json: str, name: str, port: int, *, ollama_port: int | None = None) -> str | None:
    """A session's authoritative check: `sbx policy ls --json NAME` (the rules that APPLY to NAME) must hold the active
    global deny-all and exactly our five HTTP allow rules for `localhost:<port>` scoped to NAME, and nothing else that allows
    anything (a global allow that appeared after selection, a raw tcp allow of our own port, a sixth path, a widened method)."""
    if ollama_port is not None and port == ollama_port:
        raise ValueError("the session proxy's port must not be the model server's port")
    rules, problem = _rules(policy_json)
    if problem:
        return problem
    assert rules is not None
    deny_all = False
    seen: dict[tuple[str, str], int] = {}
    unexpected: list[str] = []
    foreign: list[str] = []
    for rule in rules:
        type_problem = sandbox.sbx_rule_type_problem(rule)
        if type_problem:
            return type_problem
        if not _is_net(rule):
            continue
        owner, decision = _sandbox_of(rule), rule.get("decision")
        if decision == "deny":
            deny_all = deny_all or _is_deny_all(rule)
            continue
        if decision != "allow":
            unexpected.append(f"{rule.get('id')!r} (decision {decision!r})")
            continue
        if owner is not None and owner != name:
            foreign.append(f"{rule.get('id')!r} (scoped to {owner!r})")
            continue
        key = _our_rule_key(rule, port) if owner == name else None
        if key is None:
            unexpected.append(f"{rule.get('id')!r} ({rule.get('resource_type')} allow of {rule.get('resources')!r}, scope {rule.get('scope')!r})")
        elif rule.get("status") != "active":
            continue                          # an inactive copy of one of ours allows nothing; it does not count as present either
        else:
            seen[(key[0], key[1])] = seen.get((key[0], key[1]), 0) + 1
    parts: list[str] = []
    if foreign:
        parts.append("a rule scoped to another sandbox appears in this sandbox's listing: " + ", ".join(foreign))
    if unexpected:
        parts.append("unexpected allow rule(s): " + ", ".join(unexpected))
    missing = sorted(p for p in _ROUTES if p not in seen)
    if missing:
        parts.append("missing rule(s): " + ", ".join(f"{m} {p}" for m, p in missing))
    twice = sorted(p for p, n in seen.items() if n > 1)
    if twice:
        parts.append("rule(s) present twice: " + ", ".join(f"{m} {p}" for m, p in twice))
    if not deny_all:
        parts.append("no active global network deny-all rule (`**`, tcp and udp)")
    return "; ".join(parts) or None


# ── selection ─────────────────────────────────────────────────────────────────────

def model_probes(environ: Mapping[str, str]) -> sandbox.SbxProbes:
    """The real read-only probes of `sandbox.real_sbx_probes`, with the policy question this route asks."""
    base = sandbox.real_sbx_probes(environ)
    env = sandbox._sbx_env(environ)

    def policy(path: str) -> str | None:
        p = sandbox._run_readonly([path, "policy", "ls", "--json"], env)
        if p.returncode == 0:
            return model_policy_problem(p.stdout)
        text = sandbox._first_line(p.stderr or p.stdout)
        return ("global network policy is not initialised (`sbx policy init <profile>`)"
                if "not been initialized" in text else f"`sbx policy ls` failed: {text}")

    return base._replace(policy_problem=policy)


@dataclass(frozen=True)
class ModelRuntime:
    """Which runtime model sessions use, and why not when the asked-for one is unusable. `kind` is `docker`, `sbx`, or
    `refused` (the setting's value is not recognised); `ok` is False for a refusal: nothing falls back."""
    kind: str
    ok: bool
    sbx_path: str | None = None
    version: str | None = None
    reasons: tuple[str, ...] = ()
    codes: tuple[str, ...] = ()

    @property
    def line(self) -> str:
        """One line for the stage-1 plan: which runtime model sessions will use."""
        if self.kind == RUNTIME_DOCKER:
            return (f"model sessions run in: rootless Docker (the default; {ENV_MODEL_SANDBOX} is not set to sbx)")
        if self.ok:
            return (f"model sessions run in: sbx microVM {self.version} at {self.sbx_path} ({ENV_MODEL_SANDBOX}=sbx; the model "
                    "route is sandbox-scoped HTTP rules to our egress proxy only; rootless Docker is not used for sessions)")
        return (f"model sessions: REFUSED, {ENV_MODEL_SANDBOX} asks for sbx and it cannot be used (no fallback to Docker): "
                + "; ".join(self.reasons))


def choose_model_runtime(environ: Mapping[str, str] | None = None, *, which: Callable[[str], str | None] = shutil.which,
                         probes: sandbox.SbxProbes | None = None, verified: frozenset[str] | None = None) -> ModelRuntime:
    """Docker unless `COGNITIVE_HARNESS2_MODEL_SANDBOX=sbx` is set. With it: the version must be in `verified` (default
    `VERIFIED_SBX_MODEL_VERSIONS`), `/dev/kvm` and the daemon must work, the global policy must pass `model_policy_problem`
    and both host-reach settings must be off (the checks `sandbox.inspect_sbx` makes, with this route's policy question).
    A problem is a refusal that names every reason; it is never a switch to Docker. Read-only; never starts sbx."""
    environ = os.environ if environ is None else environ
    mode, bad = model_sandbox_setting(environ)
    if bad:
        return ModelRuntime(RUNTIME_REFUSED, False, reasons=(bad,), codes=(sandbox.WHY_BAD_SETTING,))
    if mode == RUNTIME_DOCKER:
        return ModelRuntime(RUNTIME_DOCKER, True)
    env = {**environ, sandbox.ENV_SANDBOX: "sbx"}      # the generic setting neither selects nor blocks this route
    rep = sandbox.inspect_sbx(which, environ=env, probes=probes if probes is not None else model_probes(env),
                              verified=VERIFIED_SBX_MODEL_VERSIONS if verified is None else verified)
    return ModelRuntime(RUNTIME_SBX, rep.selected, rep.path, rep.version, rep.reasons, tuple(p.code for p in rep.problems))


# ═══ the runner ═══════════════════════════════════════════════════════════════════════════════════════════════════════

GATEWAY_PROXY = re.compile(r"^http://gateway\.docker\.internal:[0-9]{2,5}$")
VM_MODEL_HOST = "host.docker.internal"      # the sbx proxy maps it to the HOST's loopback; the policy engine names it `localhost`
ROUTE_HOSTS = ("localhost", VM_MODEL_HOST)  # the `Host` names our proxy accepts on its listener (the sbx proxy sends `localhost:<port>`)
_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
_PROXY_START_ATTEMPTS = 3
_PROXY_READY_S = 30.0
_PROXY_STOP_GRACE_S = 3.0
_MAX_PROXY_LOG_BYTES = 8 * 1024 * 1024
_SBX_EXEC_FAILURE = sandbox._SBX_EXEC_FAILURE
_MEM = re.compile(r"^([1-9][0-9]{0,5})([kmg])$")
_ADDED = re.compile(r"\bAdded rule (\S+)")


def _free_ports(n: int, avoid: Sequence[int | None] = ()) -> list[int]:
    """`n` distinct free loopback TCP ports (all bound at once, then released: another process can still take one before the
    proxy does, which is why the proxy start is retried)."""
    socks: list[socket.socket] = []
    try:
        while len(socks) < n:
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            if s.getsockname()[1] in avoid:
                s.close()
                continue
            socks.append(s)
        return [s.getsockname()[1] for s in socks]
    finally:
        for s in socks:
            s.close()


def _die_with_parent() -> None:
    """In the child, before exec: SIGTERM me when the process that started me dies (Linux `PR_SET_PDEATHSIG`), so a hard kill
    of the runner cannot leave the host proxy listening for a stale sandbox. Best effort; other platforms get nothing."""
    try:
        import ctypes
        ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGTERM)
    except Exception:  # noqa: BLE001
        pass


_TERMINATE_SIGNALS = tuple(s for s in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGHUP", None)) if s is not None)


@contextlib.contextmanager
def _terminate_as_interrupt():
    """SIGTERM and SIGHUP raise `KeyboardInterrupt` for the duration, so they take the path Ctrl-C takes (the `finally`, the
    shielded teardown). Previous handlers are restored after, also if the body raises. Only the main thread can install a signal
    handler; anywhere else this does nothing (and a kill there is a hard kill, which the next run's sweep cleans up)."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def handler(signum, frame):  # noqa: ARG001
        raise KeyboardInterrupt(f"terminated by {signal.Signals(signum).name}")

    saved: dict[int, object] = {}
    for sig in _TERMINATE_SIGNALS:
        with contextlib.suppress(ValueError, OSError):
            saved[sig] = signal.signal(sig, handler)
    try:
        yield
    finally:
        for sig, previous in saved.items():
            with contextlib.suppress(ValueError, OSError, TypeError):
                signal.signal(sig, previous)


@contextlib.contextmanager
def _shielded_teardown():
    """The teardown ignores SIGINT and SIGTERM (`session_runner._shielded_from_interrupts`) and SIGHUP too, so a second signal
    cannot cut it short. Handlers are restored after."""
    with sr._shielded_from_interrupts():
        hup = getattr(signal, "SIGHUP", None)
        previous = None
        if hup is not None and threading.current_thread() is threading.main_thread():
            with contextlib.suppress(ValueError, OSError):
                previous = signal.signal(hup, signal.SIG_IGN)
        try:
            yield
        finally:
            if previous is not None:
                with contextlib.suppress(ValueError, OSError, TypeError):
                    signal.signal(hup, previous)


def _memory_arg(memory: str) -> str:
    """`--memory` for sbx: the session limit, but never below sbx's 512 MiB minimum."""
    m = _MEM.fullmatch(memory)
    if not m:
        raise sr.SessionRefused("memory must look like 256m or 2g")
    mib = int(m.group(1)) * {"k": 1 / 1024, "m": 1, "g": 1024}[m.group(2)]
    return memory if mib >= 512 else "512m"


def _cpus_arg(cpus: str) -> str:
    return str(max(1, math.ceil(float(cpus))))


@dataclass
class _Live:
    """What one session has created so far, for the teardown (which must work from any point of failure)."""
    base: Path | None = None
    proxy: subprocess.Popen | None = None
    client: subprocess.Popen | None = None
    rule_ids: list[str] = field(default_factory=list)
    log_text: str = ""
    port: int | None = None


class SbxSessionRunner(sr.ProposerSessionRunner):
    """`ProposerSessionRunner`'s contract on an sbx microVM, for a LOCAL-model channel only.

    Same `authorize`, budget, observer, result and teardown semantics as the Docker runner; different plumbing (README, "sbx model
    route"). It never touches the global policy and never uses Docker. Every failure before the CLI starts raises
    `SessionRefused` (preflight) or `SessionError` (after something was created, with the teardown done); a sandbox, rule, process or
    directory that cannot be removed is a `SessionCleanupError` naming it."""

    def __init__(self, sbx_path: str, channel: sr.channels.Channel, *, zdr: str, classification: str | None,
                 proposer_decision: sr.upstream_egress.Decision | None = None, workspace_roots: Sequence[str] = (),
                 role: str = "proposer", budget: sr.SweepBudget | None = None,
                 observer: Callable[[str, Mapping[str, object]], None] | None = None,
                 environ: Mapping[str, str] | None = None, verified: frozenset[str] | None = None,
                 control_timeout_s: int = 60, create_timeout_s: int = 600, python: str | None = None,
                 proxy_script: str | None = None):
        if role not in sr.ROLES:
            raise sr.SessionRefused(f"the session role must be one of {sr.ROLES}")
        if budget is not None and not isinstance(budget, sr.SweepBudget):
            raise sr.SessionRefused("budget must be a SweepBudget")
        if observer is not None and not callable(observer):
            raise sr.SessionRefused("observer must be a callable (event, fields)")
        if not isinstance(channel, sr.channels.Channel):
            raise sr.SessionRefused("the channel must be a declared channels.Channel")
        self.verified, self.image = None, None                   # no Docker: nothing here may reach for either
        self.channel, self.zdr, self.classification, self.proposer_decision = channel, zdr, classification, proposer_decision
        self.workspace_roots, self.egress_network, self._test_allow_cidrs = tuple(workspace_roots), None, ()
        self.role, self.budget, self.observer, self.observer_errors = role, budget, observer, 0
        keys = [k for k in channel.endpoint_keys if k[0] in sr.egress.LOOPBACK]
        self.ollama_port: int | None = keys[0][1] if len(keys) == 1 else None
        self.environ = dict(os.environ if environ is None else environ)
        self.sbx_path = sbx_path
        self.verified_versions = VERIFIED_SBX_MODEL_VERSIONS if verified is None else verified
        self.control_timeout_s = control_timeout_s
        self.python = python or sys.executable
        self.proxy_script = proxy_script or egress_proxy_file()
        self._sbx = sandbox.SbxRunner(sbx_path, environ=self.environ, verified=self.verified_versions,
                                      control_timeout_s=control_timeout_s, create_timeout_s=create_timeout_s,
                                      policy_problem=lambda text: model_policy_problem(text, ollama_port=self.ollama_port))

    # ── authorisation: the Docker runner's, plus what this route cannot serve ────────

    def authorize(self, spec: sr.SessionSpec) -> sr.Plan:
        plan = super().authorize(spec)
        if not plan.local:
            raise sr.SessionRefused("the sbx route serves local-model channels only (a hosted channel needs a route to the internet)")
        if spec.credential is not None:
            raise sr.SessionRefused("the sbx route carries no credential file: a local model needs none")
        if ":" in plan.workspace:
            raise sr.SessionRefused("the workspace path contains ':' (sbx reads a `:ro` suffix as a mode)")
        return plan

    def _gate(self) -> None:
        """The stale sweep (best effort), then the preflight, run for every session (the daemon, policy or settings can change
        between runs): the same checks `SbxRunner` makes, with this route's reading of the policy. The sweep comes FIRST: a sandbox
        leaked by a hard-killed run is a running VM with a read-write mount, and a preflight that refuses (a version, a policy, a
        setting) must not be the reason it stays."""
        with contextlib.suppress(Exception):
            self.sweep_stale()
        try:
            self._sbx._preflight()
        except sandbox.SandboxUnavailable as e:
            raise sr.SessionRefused(f"sbx cannot be used for a model session: {e}") from None

    # ── helpers ───────────────────────────────────────────────────────────────────

    def _ctl(self, *args: str, timeout: int | None = None, what: str | None = None) -> subprocess.CompletedProcess:
        return self._sbx._ctl(self._sbx._argv(*args), self.control_timeout_s if timeout is None else timeout, what or args[0])

    def _must(self, *args: str, what: str, timeout: int | None = None) -> subprocess.CompletedProcess:
        try:
            p = self._ctl(*args, timeout=timeout, what=what)
        except sandbox.SandboxUnavailable as e:
            raise sr.SessionError(str(e)) from None
        if p.returncode != 0:
            raise sr.SessionError(f"`sbx {what}` failed (exit {p.returncode}): {sandbox._first_line(p.stderr or p.stdout)}")
        return p

    # ── the stale sweep ───────────────────────────────────────────────────────────

    def sweep_stale(self) -> list[str]:
        """`sweep_stale_sandboxes` with this runner's sbx, environment and control timeout. Run before every session's preflight."""
        return sweep_stale_sandboxes(self.sbx_path, self.environ, timeout=self.control_timeout_s)

    reap_all = sweep_stale

    # ── the host proxy ────────────────────────────────────────────────────────────

    def _proxy_config(self, plan: sr.Plan, bridge: HostBridge, port: int, forward_port: int) -> dict:
        sock = str(bridge.socket_path)
        allow = [list(pair) for pair in OLLAMA_ROUTE_ALLOW]
        models = list(plan.allowed_models)
        return {"listen_host": "127.0.0.1", "listen_port": forward_port, "allow": [],
                "unix_routes": [{"host": h, "port": port, "unix": sock, "allow": allow, "allowed_models": models} for h in ROUTE_HOSTS],
                "listeners": [{"port": port, "unix": sock}]}

    def _start_proxy(self, plan: sr.Plan, base: Path, bridge: HostBridge, live: _Live) -> int:
        """The SAME `egress_proxy.py` the Docker route runs in a container, as a host process on 127.0.0.1:<port> (a random free
        port that is never the model server's). Its output goes to a file (no pipe to fill). Retried when another process takes
        the port first."""
        last = "no output"
        for _ in range(_PROXY_START_ATTEMPTS):
            port, forward = _free_ports(2, avoid=[self.ollama_port])
            cfg = base / "proxy.json"
            cfg.write_text(json.dumps(self._proxy_config(plan, bridge, port, forward)))
            os.chmod(cfg, 0o600)
            logf = base / "proxy.log"
            with open(logf, "wb") as out:
                live.proxy = subprocess.Popen([self.python, "-I", self.proxy_script, "--config", str(cfg)], stdin=subprocess.DEVNULL,
                                              stdout=out, stderr=subprocess.STDOUT, start_new_session=True, close_fds=True,
                                              preexec_fn=_die_with_parent, env={"PATH": _PATH, "LANG": "C.UTF-8"})
            deadline = time.monotonic() + _PROXY_READY_S
            while time.monotonic() < deadline:
                text = logf.read_text(errors="replace")
                if sr.READY_LINE in text:
                    live.port = port
                    return port
                if live.proxy.poll() is not None:
                    last = sandbox._first_line(text.strip().splitlines()[-1] if text.strip() else "")
                    break
                time.sleep(0.05)
            else:
                last = "it did not print its ready line in time"
            self._stop_proxy(live)
        raise sr.SessionError(f"the host egress proxy did not start: {last}")

    def _stop_proxy(self, live: _Live) -> None:
        """Stop it gracefully (SIGTERM: it prints its final ACCESS-SUMMARY), else kill the group; then read its log. Idempotent."""
        proc = live.proxy
        if proc is not None and proc.poll() is None:
            for sig, wait in ((signal.SIGTERM, _PROXY_STOP_GRACE_S), (signal.SIGKILL, 5.0)):
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, sig)
                try:
                    proc.wait(timeout=wait)
                    break
                except subprocess.TimeoutExpired:
                    continue
        if live.base is not None:
            with contextlib.suppress(OSError):
                with open(live.base / "proxy.log", "rb") as f:
                    live.log_text = f.read(_MAX_PROXY_LOG_BYTES).decode("utf-8", "replace")

    # ── the sandbox ───────────────────────────────────────────────────────────────

    def _create(self, name: str, plan: sr.Plan, live: _Live, port: int) -> None:
        lim = plan.spec.limits
        self._must("create", "shell", "--name", name, "--no-progress", "--cpus", _cpus_arg(lim.cpus), "--memory", _memory_arg(lim.memory),
                   "--pull", "missing", "--skills", "off", plan.workspace, what="create", timeout=self._sbx.create_timeout_s)
        if name not in self._must("ls", "-q", what="ls").stdout.split():
            raise sr.SessionError(f"sandbox {name} is not listed by `sbx ls -q` after create")
        for method, path in OLLAMA_ROUTE_ALLOW:
            p = self._must("policy", "allow", "network", "--sandbox", name, f"localhost:{port}", "--method", method, "--path", path,
                           what="policy allow network")
            m = _ADDED.search(p.stdout)
            if not m:
                raise sr.SessionError("`sbx policy allow network` did not say which rule it added")
            live.rule_ids.append(m.group(1))
        listing = self._must("policy", "ls", "--json", name, what="policy ls").stdout
        problem = sandbox_policy_problem(listing, name, port, ollama_port=self.ollama_port)
        if problem:
            raise sr.SessionError(f"the sandbox's policy is not deny-all plus our rules: {problem}")
        targets = [*sandbox.SBX_DENIED_TARGETS, f"localhost:{self.ollama_port}", f"localhost:{port + 1 if port < 65535 else port - 1}"]
        for target in targets:
            p = self._ctl(*self._sbx._check_argv(name, target)[1:], what="policy check network")
            try:
                verdict = json.loads(p.stdout)
            except ValueError:
                verdict = None
            if not (isinstance(verdict, dict) and verdict.get("allowed") is False and p.returncode == 1):
                raise sr.SessionError(f"the sandbox's network policy does not deny {target} "
                                      f"(exit {p.returncode}: {sandbox._first_line(p.stderr or p.stdout)})")

    def _gateway(self, name: str) -> str:
        out = self._must("exec", "--", name, "printenv", "HTTP_PROXY", what="exec printenv").stdout.strip()
        if not GATEWAY_PROXY.fullmatch(out):
            raise sr.SessionError(f"the sandbox's HTTP_PROXY is {out!r}, not the sbx gateway: its network path is not the one this "
                                  "route was verified on")
        return out

    def _install(self, name: str, plan: sr.Plan) -> None:
        cli = f"{sr.CLI_DIR}/{os.path.basename(plan.cli_host)}"
        self._must("exec", "-u", "root", "--", name, "mkdir", "-p", sr.CLI_DIR, what="exec mkdir")
        self._must("cp", plan.cli_host, f"{name}:{sr.CLI_DIR}/", what="cp", timeout=300)
        self._must("exec", "-u", "root", "--", name, "chmod", "0755", cli, what="exec chmod")
        if self._ctl("exec", "--", name, "test", "-x", cli, what="exec test").returncode != 0:
            raise sr.SessionError(f"the CLI is not executable in the sandbox at {cli}")
        for d in plan.spec.tmpfs_dirs:
            self._must("exec", "--", name, "mkdir", "-p", d, what="exec mkdir")

    def _vm_env(self, plan: sr.Plan, port: int, gateway: str) -> dict[str, str]:
        """The COMPLETE environment of the CLI (`env -i`): nothing of the host's, nothing of the VM's own (its `*_API_KEY=proxy-managed`
        placeholders and `GH_TOKEN` are not passed on). The sbx gateway is its proxy; the VM's own loopback is the only thing not
        proxied. The logical model URL (the Docker route's `http://harness2-proxy.internal:<port>`) is replaced, in values only, with
        the origin the sbx proxy maps to the host: `http://host.docker.internal:<port>`."""
        env = {"HOME": sr.HOME, "TMPDIR": "/tmp", "PATH": _PATH, "TERM": "dumb", "NO_COLOR": "1", "LANG": "C.UTF-8",
               "HTTP_PROXY": gateway, "HTTPS_PROXY": gateway, "http_proxy": gateway, "https_proxy": gateway,
               "NO_PROXY": sr.SESSION_NO_PROXY, "no_proxy": sr.SESSION_NO_PROXY, "NODE_USE_ENV_PROXY": "1"}
        logical = f"http://{sr.PROXY_ALIAS}:{plan.local_port}"
        real = f"http://{VM_MODEL_HOST}:{port}"
        env.update({k: v.replace(logical, real) for k, v in plan.spec.env.items()})
        return env

    @staticmethod
    def _session_script() -> str:
        return 'ulimit -u "$1" && ulimit -c 0 && shift && exec "$@"'

    def _session_argv(self, name: str, plan: sr.Plan, env: Mapping[str, str]) -> list[str]:
        spec = plan.spec
        return [self.sbx_path, "exec", *(["-i"] if spec.stdin is not None else []), "-w", plan.workspace, "--", name,
                "bash", "-c", self._session_script(), "bash", str(spec.limits.pids), "env", "-i",
                *(f"{k}={v}" for k, v in env.items()), *spec.argv]

    def _run_client(self, argv: list[str], plan: sr.Plan, live: _Live) -> tuple[int | None, bytes, bytes, bool, bool]:
        spec = plan.spec
        out, err, cut = bytearray(), bytearray(), [False]
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE if spec.stdin is not None else subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=sandbox._sbx_env(self.environ), start_new_session=True)
        live.client = proc
        threads = [threading.Thread(target=sandbox._drain, args=(s, spec.limits.max_output_bytes, b, cut), daemon=True)
                   for s, b in ((proc.stdout, out), (proc.stderr, err))]
        if spec.stdin is not None:
            def feed() -> None:
                with contextlib.suppress(OSError):
                    proc.stdin.write(spec.stdin)  # type: ignore[union-attr]
                    proc.stdin.close()            # type: ignore[union-attr]
            threads.append(threading.Thread(target=feed, daemon=True))
        for t in threads:
            t.start()
        timed_out = False
        try:
            code: int | None = proc.wait(timeout=spec.timeout_s)
        except subprocess.TimeoutExpired:
            timed_out, code = True, None
            self._kill_client(live)
        for t in threads:
            t.join(timeout=sandbox._PROBE_TIMEOUT_S)
        return code, bytes(out), bytes(err), timed_out, cut[0]

    def _kill_client(self, live: _Live) -> None:
        proc = live.client
        if proc is not None and proc.poll() is None:
            self._sbx._kill_client(proc)

    # ── teardown ──────────────────────────────────────────────────────────────────

    def _remove_rules(self, name: str, ids: Sequence[str]) -> None:
        """Remove each rule by id (`--force`: there is no terminal). Best effort: `sbx rm` takes the rest with the sandbox, and
        the leftover check reports a rule that is still there."""
        for rule in ids:
            with contextlib.suppress(sandbox.SandboxUnavailable):
                self._ctl("policy", "rm", "network", "--sandbox", name, "--id", rule, "--force", what="policy rm network")

    def _remove_sandbox(self, name: str) -> None:
        with contextlib.suppress(sandbox.SandboxUnavailable):
            self._sbx._destroy(name)

    def _teardown_sbx(self, name: str, live: _Live, stack: contextlib.ExitStack, cleanup: sr._Cleanup) -> None:
        """Every step runs whatever happened to the one before it. Order: the client; the rules (the route goes first); the sandbox;
        the proxy (its final summary is read); the bridge; the private directory."""
        cleanup.run(self._kill_client, live)
        cleanup.run(self._remove_rules, name, live.rule_ids)
        cleanup.run(self._remove_sandbox, name)
        cleanup.run(self._stop_proxy, live)
        cleanup.run(sr.ProposerSessionRunner._close_stack, stack)
        if live.base is not None:
            cleanup.run(shutil.rmtree, live.base, True)

    def _leftovers_sbx(self, name: str, live: _Live) -> list[str]:
        left: list[str] = []
        try:
            rules = self._ctl("policy", "ls", "--json", what="policy ls")
            doc = json.loads(rules.stdout) if rules.returncode == 0 else None
            if doc is None:
                left.append(f"rules of {name}: could not be listed")
            else:
                left += [f"rule {r.get('id')} of sandbox {name}" for r in doc.get("rules", [])
                         if isinstance(r, dict) and _sandbox_of(r) == name]
        except (sandbox.SandboxUnavailable, ValueError, AttributeError):
            left.append(f"rules of {name}: could not be listed")
        try:
            ls = self._ctl("ls", "-q", what="ls")
            if ls.returncode != 0:
                left.append(f"sandbox {name}: could not be listed")
            elif name in ls.stdout.split():
                left.append(f"sandbox {name}")
        except sandbox.SandboxUnavailable:
            left.append(f"sandbox {name}: could not be listed")
        if live.proxy is not None and live.proxy.poll() is None:
            left.append(f"process {live.proxy.pid} (host egress proxy)")
        if live.base is not None and live.base.exists():
            left.append(f"directory {live.base}")
        return left

    @staticmethod
    def _reclaim_host(workspace: str) -> bool:
        """The session's files are the host user's (virtiofs maps them), but a mode the session chose (a directory at 000) can
        still stop harvest and removal: make every directory u+rwx and every file u+rw, never following a link."""
        ok, stack = True, [workspace]
        while stack:
            d = stack.pop()
            try:
                if not os.path.islink(d):
                    os.chmod(d, os.lstat(d).st_mode | 0o700)
                with os.scandir(d) as it:
                    for e in it:
                        if e.is_symlink():
                            continue
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        else:
                            os.chmod(e.path, os.lstat(e.path).st_mode | 0o600)
            except OSError:
                ok = False
        return ok

    # ── a session ─────────────────────────────────────────────────────────────────

    def _execute(self, plan: sr.Plan) -> sr.SessionResult:
        """One session. For its whole length (main thread) SIGTERM and SIGHUP raise `KeyboardInterrupt`, like Ctrl-C, so a plain
        `kill`, a systemd stop, `timeout` or a closed terminal runs the same shielded teardown instead of leaving the microVM (with
        the workspace mounted read-write and its allow rules) behind. The previous handlers are restored afterwards."""
        with _terminate_as_interrupt():
            return self._execute_session(plan)

    def _execute_session(self, plan: sr.Plan) -> sr.SessionResult:
        spec = plan.spec
        run_id = secrets.token_hex(6)
        name = sandbox_name(run_id, scope=run_scope.current())
        live, stack = _Live(), contextlib.ExitStack()
        raw: tuple[int | None, bytes, bytes, bool, bool] | None = None
        error: BaseException | None = None
        reclaimed: bool | None = None
        t0 = time.monotonic()
        self._notify("session_start", session=run_id, timeout_s=spec.timeout_s, tmp_exec=spec.tmp_exec)
        try:
            live.base = Path(tempfile.mkdtemp(prefix=run_scope.dir_prefix("h2m-"), dir=sr._runtime_base_dir())).resolve()
            os.chmod(live.base, 0o700)
            bridge = stack.enter_context(HostBridge(*plan.bridge_target))              # type: ignore[misc]
            port = self._start_proxy(plan, live.base, bridge, live)
            self._create(name, plan, live, port)
            gateway = self._gateway(name)
            self._install(name, plan)
            raw = self._run_client(self._session_argv(name, plan, self._vm_env(plan, port, gateway)), plan, live)
            code, _, err, timed_out, _ = raw
            if not timed_out and (code == 125 or (code == 255 and _SBX_EXEC_FAILURE.match(err.decode("utf-8", "replace")))):
                raise sr.SessionError(f"`sbx exec` could not run the session (exit {code}): "
                                      f"{sandbox._first_line(err.decode('utf-8', 'replace'))}")
        except BaseException as e:   # noqa: BLE001 - recorded, cleanup runs, then re-raised
            error = e
        finally:
            cleanup = sr._Cleanup()
            with _shielded_teardown():
                self._teardown_sbx(name, live, stack, cleanup)
                if spec.reclaim_workspace:
                    reclaimed = cleanup.run(self._reclaim_host, plan.workspace, default=False)
                leftovers = tuple(cleanup.run(self._leftovers_sbx, name, live, default=[]))
        if error is None and cleanup.interrupt is not None:
            error = cleanup.interrupt
        log = sr.access_log_lines(live.log_text)
        code, out, err, timed_out, cut = raw if raw is not None else (None, b"", b"", False, False)
        self._notify("session_end", session=run_id, exit_code=None if raw is None else code, timed_out=None if raw is None else timed_out,
                     truncated=None if raw is None else cut, duration_s=round(time.monotonic() - t0, 3), reclaimed=reclaimed,
                     leftovers=len(leftovers), error=None if error is None else type(error).__name__, **sr.egress_summary(log))
        if error is not None:
            if leftovers:
                raise sr.SessionCleanupError(leftovers) from error
            raise error
        note = f"\n[output truncated at {spec.limits.max_output_bytes} bytes]\n" if cut else ""
        stdout = sr.redact(sr.sanitize_terminal(out.decode("utf-8", "replace")), []) + note
        stderr = sr.redact(sr.sanitize_terminal(err.decode("utf-8", "replace")), [])
        report = sr.workspace_harvest.inspect_workspace(plan.workspace, spec.harvest, credentials=[]) if spec.harvest is not None else None
        result = sr.SessionResult(run_id, code, stdout, "timed out\n" + stderr if timed_out else stderr, timed_out, cut,
                                  time.monotonic() - t0, log, report, reclaimed, leftovers, self.role)
        if leftovers:
            raise sr.SessionCleanupError(leftovers, result)
        return result


def egress_proxy_file() -> str:
    from . import egress_proxy
    return egress_proxy.__file__


# ═══ choosing the runner, and the sweep's leftover check ═════════════════════════════════════════════════════════════

def make_runner(runtime: ModelRuntime | None, verified: sandbox.VerifiedDocker | None, image: object, channel: sr.channels.Channel,
                *, environ: Mapping[str, str] | None = None, **kw) -> sr.ProposerSessionRunner:
    """The session runner for `runtime`: `ProposerSessionRunner` (Docker) for `docker` or None, `SbxSessionRunner` for a usable
    `sbx`. A runtime that is not usable (`ok` False: sbx asked for and unusable, or an unrecognised setting) is a
    `SessionRefused` naming every reason: there is no Docker runner to fall back to when sbx was asked for. `kw` is
    `ProposerSessionRunner`'s; the two Docker-only options (`egress_network`, `_test_allow_cidrs`) are refused on sbx."""
    if runtime is None or (runtime.kind == RUNTIME_DOCKER and runtime.ok):
        return sr.ProposerSessionRunner(verified, image, channel, **kw)            # type: ignore[arg-type]
    if runtime.kind != RUNTIME_SBX or not runtime.ok or not runtime.sbx_path:
        raise sr.SessionRefused("model sessions cannot run: " + ("; ".join(runtime.reasons) or "the runtime is not usable")
                                + " (there is no fallback to Docker)")
    if kw.pop("egress_network", None) is not None or kw.pop("_test_allow_cidrs", ()):
        raise sr.SessionRefused("egress_network and test_allow_cidrs are Docker options: the sbx route has no egress network")
    return SbxSessionRunner(runtime.sbx_path, channel, environ=environ, **kw)


def sweep_stale_sandboxes(sbx_path: str, environ: Mapping[str, str] | None = None, *, timeout: int = 60) -> list[str]:
    """Remove the `h2m-` sandboxes that are provably leaks of a dead run (`is_stale`: an owner in the name, seen from this process;
    never a pid alone, never another pid namespace's, never a name without an owner, never a sandbox that is not ours), with a run
    scope set only that scope's. A sandbox's rules go with it. Returns `sandbox NAME` for each removed.

    Read-only until it has something to remove, and it never starts the daemon: nothing is run unless the daemon socket already
    answers. A listing that fails removes nothing. Called by every `SbxSessionRunner` before its preflight, and by `stage1 run`
    before its own, whatever the model runtime is."""
    environ = os.environ if environ is None else environ
    if not sandbox._sbx_daemon_reachable(environ):
        return []
    env = sandbox._sbx_env(environ)
    try:
        ls = subprocess.run([sbx_path, "ls", "-q"], capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, env=env)
    except (subprocess.TimeoutExpired, OSError):
        return []
    if ls.returncode != 0:
        return []
    scope, me, removed = run_scope.current(), current_owner(), []
    for name in ls.stdout.split():
        parsed = parse_sandbox_name(name)
        if parsed is None or (scope is not None and parsed[0] != scope) or not is_stale(name, me):
            continue
        with contextlib.suppress(subprocess.TimeoutExpired, OSError):
            subprocess.run([sbx_path, "rm", "--force", name], capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL, env=env)
        removed.append(f"sandbox {name}")
    return removed


def sweep_stale_if_installed(environ: Mapping[str, str], which: Callable[[str], str | None] = shutil.which) -> list[str]:
    """`sweep_stale_sandboxes` for whatever `sbx` is on PATH; nothing at all when there is none. Best effort: never raises."""
    try:
        path = which("sbx")
        return sweep_stale_sandboxes(path, environ) if path else []
    except Exception:  # noqa: BLE001
        return []


def leftover_sandboxes(sbx_path: str, session_ids: Sequence[str], *, environ: Mapping[str, str] | None = None) -> list[str]:
    """`sbx sandbox NAME` for every sandbox of THESE sessions (by session id, in the `h2m-` name) that still exists."""
    ids = set(session_ids)
    if not ids:
        return []
    env = sandbox._sbx_env(os.environ if environ is None else environ)
    try:
        p = subprocess.run([sbx_path, "ls", "-q"], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL, env=env)
    except (subprocess.TimeoutExpired, OSError):
        return ["sbx sandboxes: could not be listed"]
    if p.returncode != 0:
        return ["sbx sandboxes: could not be listed"]
    return [f"sbx sandbox {n}" for n in p.stdout.split() if (parsed := parse_sandbox_name(n)) and parsed[2] in ids]


def remove_leftover_sandboxes(sbx_path: str, items: Sequence[str], *, environ: Mapping[str, str] | None = None) -> None:
    """`sbx rm --force` each `sbx sandbox NAME` item (its rules go with it). Best effort; the caller lists again."""
    env = sandbox._sbx_env(os.environ if environ is None else environ)
    for item in items:
        if item.startswith("sbx sandbox ") and parse_sandbox_name(item[len("sbx sandbox "):]):
            with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                subprocess.run([sbx_path, "rm", "--force", item[len("sbx sandbox "):]], capture_output=True, timeout=60,
                               stdin=subprocess.DEVNULL, env=env)
