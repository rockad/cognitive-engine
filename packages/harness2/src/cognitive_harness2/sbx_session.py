"""The sbx route for MODEL sessions (opt-in; README, "sbx model route").

A model session is a proposer or solver `opencode` that calls the local Ollama. On Docker it reaches it through a proxy
container and a host bridge. This module is the same session on an `sbx` microVM: the SAME `egress_proxy.py` (route filter,
model pinning) runs as a host process on `127.0.0.1:<P>`, and the sandbox is allowed to reach exactly that port, as five
HTTP rules (one per `OLLAMA_ROUTE_ALLOW` pair) scoped to that one sandbox. The global deny-all policy is never touched.

This file holds the PURE layer and the selection: how `sbx policy ls --json` is read, what "deny-all plus our own rule"
means, the sandbox names, and which runtime model sessions use. `SbxSessionRunner` (below it) is the runner.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from . import sandbox
from .egress_proxy import OLLAMA_ROUTE_ALLOW

ENV_MODEL_SANDBOX = "COGNITIVE_HARNESS2_MODEL_SANDBOX"
RUNTIME_DOCKER, RUNTIME_SBX, RUNTIME_REFUSED = "docker", "sbx", "refused"

# The sbx versions the model route has been verified on by its live test (tests/test_sbx_session.py, COGNITIVE_HARNESS2_SBX_LIVE=1).
# Separate from `sandbox.VERIFIED_SBX_VERSIONS` (candidate execution): that flow does not use rules, a proxy or the exec of a CLI.
VERIFIED_SBX_MODEL_VERSIONS: frozenset[str] = frozenset()

NAME_PREFIX = "h2m"
_NAME = re.compile(r"^h2m-(?:([0-9a-f]{8,32})|0)-([0-9]{1,10})-([0-9a-f]{12})$")
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

def sandbox_name(session_id: str, *, pid: int | None = None, scope: str | None = None) -> str:
    """`h2m-<run scope or 0>-<owner pid>-<session id>`: a leftover can be attributed to a dead owner (and, in tests, to one
    pytest process) from its name alone. sbx has no labels."""
    if not re.fullmatch(r"[0-9a-f]{12}", session_id):
        raise ValueError("the session id must be 12 lowercase hex characters")
    return f"{NAME_PREFIX}-{scope or '0'}-{os.getpid() if pid is None else pid}-{session_id}"


def parse_sandbox_name(name: str) -> tuple[str | None, int, str] | None:
    """(scope, pid, session id) of a sandbox name this module made, else None."""
    m = _NAME.fullmatch(name) if isinstance(name, str) else None
    return (m.group(1), int(m.group(2)), m.group(3)) if m else None


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
    kind = rule.get("resource_type")
    return isinstance(kind, str) and (kind.startswith("network") or kind == "http")


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
        kind = rule.get("resource_type")
        if not isinstance(kind, str):
            return "`sbx policy ls --json` has a rule without a resource_type"
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


def stale_rule_sandboxes(policy_json: str) -> list[str]:
    """The `h2m-` sandboxes that currently own a network or http rule, sorted. Unreadable input gives none."""
    rules, problem = _rules(policy_json)
    if problem or rules is None:
        return []
    return sorted({o for r in rules if _is_net(r) and (o := _sandbox_of(r)) and parse_sandbox_name(o)})


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
