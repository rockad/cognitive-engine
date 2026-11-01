"""The sbx model route's pure layer: how it reads `sbx policy ls --json`, what it calls "deny-all plus our own rule", the
sandbox names that make a leftover attributable to a dead owner, and the setting that opts in. No process is started.

The rule JSON below is what a real sbx v0.50.1 printed (2026-11-01): a global deny-all, and an HTTP allow scoped to one
sandbox (`resource_type: http`, `methods`, `http_targets`, `sandbox_id`)."""
from __future__ import annotations

import copy
import json
import re

import pytest

from cognitive_harness2 import egress_proxy, sbx_session as ss

PORT = 41234
NAME = "h2m-0-4242-aabbccddeeff"
OLLAMA = 11434

DENY_ALL = {"id": "default-deny-all", "name": "default-deny-all", "policy_name": "default-deny-all", "scope": "global",
            "applies_to": "all", "resource_type": "network", "decision": "deny", "resources": ["**"], "origin": "local",
            "layer": "local", "status": "active", "editable": False, "actions": ["net:connect:tcp", "net:connect:udp"]}
FS = [{"id": "default-fs-read-allow-all", "scope": "global", "resource_type": "filesystem:read", "decision": "allow",
       "resources": ["**"], "status": "active", "actions": ["fs:mount:read"]},
      {"id": "default-fs-write-allow-all", "scope": "global", "resource_type": "filesystem:write", "decision": "allow",
       "resources": ["**"], "status": "active", "actions": ["fs:mount:read", "fs:mount:write"]}]


PORT_FORWARD = {"id": "pf", "scope": "global", "resource_type": "port_forward", "decision": "allow", "resources": ["**"],
                "status": "active"}


def http_rule(sandbox: str, method: str, path: str, *, port: int = PORT, host: str = "localhost", rid: str | None = None,
              status: str = "active") -> dict:
    return {"id": rid or f"{method}-{path}-{sandbox}", "name": "n", "scope": f"sandbox:{sandbox}", "applies_to": f"sandbox:{sandbox}",
            "resource_type": "http", "decision": "allow", "resources": [f"{host}:{port}"], "origin": "scoped", "layer": "local",
            "status": status, "editable": True, "sandbox_id": sandbox, "actions": [f"http:request:{method.lower()}"],
            "methods": [method], "http_targets": [{"host": f"{host}:{port}", "path": path}]}


def ours(sandbox: str = NAME, **kw) -> list[dict]:
    return [http_rule(sandbox, m, p, **kw) for m, p in egress_proxy.OLLAMA_ROUTE_ALLOW]


def doc(*rules) -> str:
    flat = []
    for r in rules:
        flat.extend(r if isinstance(r, list) else [r])
    return json.dumps({"rules": flat})


def without(rules: list[dict], i: int) -> list[dict]:
    return rules[:i] + rules[i + 1:]


def mutated(rule: dict, **changes) -> dict:
    r = copy.deepcopy(rule)
    r.update(changes)
    return r


# ── selection: the global listing ─────────────────────────────────────────────────

ALLOWED_GLOBAL = [
    pytest.param(doc(DENY_ALL), id="deny-all only"),
    pytest.param(doc(FS, DENY_ALL), id="with the filesystem rules"),
    pytest.param(doc(FS, DENY_ALL, ours()), id="with the five rules of one of our sandboxes (a session is running)"),
    pytest.param(doc(FS, DENY_ALL, ours(), ours("h2m-ab12cd34-77-0123456789ab")), id="two sessions"),
    pytest.param(doc(DENY_ALL, http_rule("my-own-sandbox", "GET", "/**", host="example.com", port=443)),
                 id="a rule scoped to somebody else's sandbox does not apply to ours"),
]

REFUSED_GLOBAL = [
    pytest.param("not json", "did not return JSON", id="not json"),
    pytest.param(json.dumps({"nope": []}), "no `rules` list", id="no rules list"),
    pytest.param(json.dumps({"rules": ["x"]}), "malformed rule", id="a rule that is not an object"),
    pytest.param(doc({"id": "x"}), "resource_type", id="a rule without a type"),
    pytest.param(doc(FS), "deny-all", id="no deny-all at all"),
    pytest.param(doc(mutated(DENY_ALL, status="inactive")), "deny-all", id="an inactive deny-all"),
    pytest.param(doc(mutated(DENY_ALL, actions=["net:connect:tcp"])), "deny-all", id="a deny-all that is tcp only"),
    pytest.param(doc(mutated(DENY_ALL, resources=["example.com"])), "deny-all", id="a deny of one host is not deny-all"),
    pytest.param(doc(DENY_ALL, {**DENY_ALL, "id": "allow-all", "decision": "allow", "resources": ["**"]}), "global",
                 id="a global allow-all"),
    pytest.param(doc(DENY_ALL, {**DENY_ALL, "id": "allow-one", "decision": "allow", "resources": ["localhost:41234"]}), "global",
                 id="a global allow of one host:port"),
    pytest.param(doc(DENY_ALL, mutated(http_rule("x", "GET", "/**"), scope="global", applies_to="all")), "global",
                 id="a global http allow"),
    pytest.param(doc(DENY_ALL, PORT_FORWARD), "unknown resource type", id="an unknown rule type is refused, not skipped"),
    pytest.param(doc(DENY_ALL, {**PORT_FORWARD, "decision": "deny"}), "unknown resource type", id="an unknown type is refused whatever it decides"),
    pytest.param(doc(DENY_ALL, {**PORT_FORWARD, "resource_type": "dns"}), "unknown resource type", id="a dns rule type"),
    pytest.param(doc(DENY_ALL, mutated(DENY_ALL, id="odd", decision="maybe")), "decision", id="an unknown decision"),
    pytest.param(doc(DENY_ALL, mutated(DENY_ALL, id="odd", decision="allow", scope="team:acme")), "scope",
                 id="an unknown scope is refused, not ignored"),
    pytest.param(doc(DENY_ALL, mutated(DENY_ALL, id="odd", decision="allow", scope="sandbox:" + NAME, resource_type="network",
                                       resources=[f"localhost:{PORT}"], actions=["net:connect:tcp"])), "ours",
                 id="a raw tcp allow scoped to an h2m sandbox is not one of ours"),
    pytest.param(doc(DENY_ALL, http_rule(NAME, "GET", "/api/tags", host="example.com", port=443)), "ours",
                 id="an h2m sandbox allowed another host"),
    pytest.param(doc(DENY_ALL, http_rule(NAME, "DELETE", "/api/delete")), "ours", id="an h2m sandbox allowed DELETE"),
    pytest.param(doc(DENY_ALL, http_rule(NAME, "POST", "/api/delete")), "ours", id="an h2m sandbox allowed a path not on the list"),
    pytest.param(doc(DENY_ALL, mutated(http_rule(NAME, "GET", "/api/tags"), resources=["localhost:41234", "example.com:443"])),
                 "ours", id="an h2m sandbox allowed a second resource in one rule"),
    pytest.param(doc(DENY_ALL, mutated(http_rule(NAME, "GET", "/api/tags"), resources=["localhost:11434"],
                                       http_targets=[{"host": "localhost:11434", "path": "/api/tags"}])), "ours",
                 id="an h2m sandbox allowed the real Ollama's port"),
]


@pytest.mark.parametrize("text", ALLOWED_GLOBAL)
def test_selection_accepts_deny_all_alone_and_with_our_own_rules(text):
    assert ss.model_policy_problem(text, ollama_port=OLLAMA) is None


@pytest.mark.parametrize("text, needle", REFUSED_GLOBAL)
def test_selection_refuses_everything_else(text, needle):
    why = ss.model_policy_problem(text, ollama_port=OLLAMA)
    assert why and needle in why


# ── a session: the listing that applies to ONE sandbox ────────────────────────────

def session_doc(*extra, rules=None) -> str:
    return doc(FS, DENY_ALL, ours() if rules is None else rules, *extra)


def test_the_sandbox_listing_with_exactly_our_five_rules_passes():
    assert ss.sandbox_policy_problem(session_doc(), NAME, PORT, ollama_port=OLLAMA) is None


def test_extra_deny_rules_only_restrict_and_pass():
    deny = mutated(DENY_ALL, id="deny-x", resources=["evil.example"])
    assert ss.sandbox_policy_problem(session_doc(deny), NAME, PORT, ollama_port=OLLAMA) is None


SESSION_REFUSED = [
    pytest.param(session_doc(rules=without(ours(), 0)), "missing", id="a rule is missing"),
    pytest.param(session_doc(rules=[*ours(), ours()[0]]), "twice", id="a rule is there twice"),
    pytest.param(session_doc(http_rule(NAME, "GET", "/api/ps")), "unexpected", id="an extra path"),
    pytest.param(session_doc(http_rule(NAME, "DELETE", "/api/delete")), "unexpected", id="an extra method"),
    pytest.param(session_doc(rules=[mutated(ours()[0], methods=["POST"], actions=["http:request:post"],
                                            http_targets=[{"host": f"localhost:{PORT}", "path": "/api/tags"}]), *ours()[1:]]),
                 "missing", id="a rule with the wrong method instead of the right one"),
    pytest.param(session_doc(rules=[mutated(ours()[0], methods=["GET", "POST"]), *ours()[1:]]), "unexpected",
                 id="a rule that allows two methods"),
    pytest.param(session_doc(rules=[mutated(ours()[0], http_targets=[{"host": f"localhost:{PORT}", "path": "/**"}]), *ours()[1:]]),
                 "unexpected", id="a rule widened to every path"),
    pytest.param(session_doc(rules=ours(port=PORT + 1)), "missing", id="the rules name another port"),
    pytest.param(session_doc(rules=[*ours()[:-1], http_rule(NAME, "POST", "/api/show", status="inactive")]), "missing",
                 id="an inactive rule does not count"),
    pytest.param(session_doc({"id": "g", "scope": "global", "resource_type": "network", "decision": "allow",
                              "resources": ["example.com"], "status": "active", "actions": ["net:connect:tcp"]}),
                 "unexpected", id="a global allow that appeared after selection"),
    pytest.param(session_doc({"id": "s", "scope": f"sandbox:{NAME}", "resource_type": "network", "decision": "allow",
                              "resources": [f"localhost:{PORT}"], "status": "active", "actions": ["net:connect:tcp"]}),
                 "unexpected", id="a raw tcp allow of our own port (it would let a CONNECT tunnel through)"),
    pytest.param(session_doc(http_rule("someone-else", "GET", "/api/tags")), "another sandbox",
                 id="the listing shows a rule scoped to another sandbox"),
    pytest.param(session_doc(PORT_FORWARD), "unknown resource type", id="a rule kind that is not on the allow-list"),
    pytest.param(session_doc({**PORT_FORWARD, "decision": "deny"}), "unknown resource type", id="an unknown kind that denies"),
    pytest.param(session_doc({"id": "no-type"}), "resource_type", id="a rule with no resource type"),
    pytest.param(session_doc(rules=[]), "missing", id="no rules at all"),
    pytest.param(doc(FS, ours()), "deny-all", id="no deny-all"),
    pytest.param("not json", "JSON", id="garbage"),
]


@pytest.mark.parametrize("text, needle", SESSION_REFUSED)
def test_the_sandbox_listing_is_refused_for_anything_but_our_five_rules(text, needle):
    why = ss.sandbox_policy_problem(text, NAME, PORT, ollama_port=OLLAMA)
    assert why and needle in why


def test_the_proxy_port_may_not_be_the_real_ollama_port():
    with pytest.raises(ValueError):
        ss.sandbox_policy_problem(session_doc(rules=ours(port=OLLAMA)), NAME, OLLAMA, ollama_port=OLLAMA)


# ── names ─────────────────────────────────────────────────────────────────────────

def test_names_round_trip_with_and_without_a_run_scope():
    n = ss.sandbox_name("aabbccddeeff", pid=4242, scope=None)
    assert n == "h2m-0-4242-aabbccddeeff"
    assert ss.parse_sandbox_name(n) == (None, 4242, "aabbccddeeff")
    s = ss.sandbox_name("112233445566", pid=7, scope="deadbeef")
    assert s == "h2m-deadbeef-7-112233445566"
    assert ss.parse_sandbox_name(s) == ("deadbeef", 7, "112233445566")
    assert re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", s)


@pytest.mark.parametrize("name", ["harness2-abc123", "h2m-", "h2m-x-1-aabbccddeeff", "h2m-0-notpid-aabbccddeeff",
                                  "h2m-0-1-short", "xh2m-0-1-aabbccddeeff", "h2m-0-1-aabbccddeeffaa", "", "H2M-0-1-aabbccddeeff"])
def test_a_name_that_is_not_ours_does_not_parse(name):
    assert ss.parse_sandbox_name(name) is None


# ── the setting ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("environ, mode, bad", [
    ({}, "docker", False),
    ({ss.ENV_MODEL_SANDBOX: ""}, "docker", False),
    ({ss.ENV_MODEL_SANDBOX: "  "}, "docker", False),
    ({ss.ENV_MODEL_SANDBOX: "docker"}, "docker", False),
    ({ss.ENV_MODEL_SANDBOX: " Docker "}, "docker", False),
    ({ss.ENV_MODEL_SANDBOX: "sbx"}, "sbx", False),
    ({ss.ENV_MODEL_SANDBOX: " SBX "}, "sbx", False),
    ({ss.ENV_MODEL_SANDBOX: "auto"}, "docker", True),
    ({ss.ENV_MODEL_SANDBOX: "sbx,docker"}, "docker", True),
    ({ss.ENV_MODEL_SANDBOX: "1"}, "docker", True),
    ({ss.ENV_MODEL_SANDBOX: "microvm"}, "docker", True),
])
def test_only_an_explicit_sbx_selects_the_sbx_route(environ, mode, bad):
    got_mode, problem = ss.model_sandbox_setting(environ)
    assert got_mode == mode
    assert bool(problem) is bad


def test_the_generic_sandbox_setting_does_not_select_the_model_route():
    """`COGNITIVE_HARNESS2_SANDBOX=sbx` is for candidate execution; model sessions need their own explicit opt-in."""
    assert ss.model_sandbox_setting({"COGNITIVE_HARNESS2_SANDBOX": "sbx"}) == ("docker", None)
    assert ss.ENV_MODEL_SANDBOX == "COGNITIVE_HARNESS2_MODEL_SANDBOX"


def test_only_a_version_the_live_test_passed_on_is_verified():
    """Adding a version here is the act of verifying the model route against it: run
    `COGNITIVE_HARNESS2_SBX_LIVE=1 pytest tests/test_sbx_session.py -k live` on it first."""
    assert ss.VERIFIED_SBX_MODEL_VERSIONS == frozenset({"0.50.1"})


# ── names that carry their owner, and who may call a sandbox stale ────────────────

import os  # noqa: E402

linux_only = pytest.mark.skipif(ss.current_owner() is None, reason="the owner identity needs /proc (Linux)")
ME = ss.Owner(boot="0badc0de", ns="f0000000", start=5000)


def test_a_name_carries_the_owner_and_round_trips():
    n = ss.sandbox_name("aabbccddeeff", pid=4242, scope="deadbeef", owner=ME)
    assert n == "h2m-deadbeef-4242-0badc0de-f0000000-1388-aabbccddeeff"
    assert ss.parse_sandbox_name(n) == ("deadbeef", 4242, "aabbccddeeff")
    assert ss.parse_owner(n) == ME
    assert re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", n)
    wide = ss.sandbox_name("aabbccddeeff", pid=4194304, scope="deadbeef", owner=ss.Owner("ffffffff", "ffffffff", 0xFFFFFFFFFF))
    assert re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", wide)                     # the longest it can get still fits


def test_a_legacy_name_has_no_owner_and_is_never_judged():
    legacy = ss.sandbox_name("aabbccddeeff", pid=4242, scope=None)
    assert ss.parse_owner(legacy) is None and ss.parse_sandbox_name(legacy) == (None, 4242, "aabbccddeeff")
    assert ss.is_stale(legacy, ME) is False                                    # a pid alone proves nothing


@pytest.mark.parametrize("name", ["h2m-0-1-0badc0de-f0000000-zz-aabbccddeeff", "h2m-0-1-0badc0d-f0000000-1-aabbccddeeff",
                                  "h2m-0-1-0badc0de-f0000000-aabbccddeeff", "h2m-0-1-0badc0de-f0000000-1-aabbccddeeffaa"])
def test_a_half_formed_owner_does_not_parse(name):
    assert ss.parse_sandbox_name(name) is None


@linux_only
def test_my_own_name_carries_my_own_identity():
    me = ss.current_owner()
    assert me is not None and ss.parse_owner(ss.sandbox_name("aabbccddeeff")) == me
    assert ss.parse_sandbox_name(ss.sandbox_name("aabbccddeeff"))[1] == os.getpid()


def stale(name: str, me: ss.Owner | None = ME, **proc) -> bool:
    return ss.is_stale(name, me, proc_start=proc.get("proc_start", lambda pid: None))


def owned(pid: int, owner: ss.Owner) -> str:
    return ss.sandbox_name("aabbccddeeff", pid=pid, owner=owner)


def test_same_host_same_namespace_a_gone_process_is_stale_and_a_living_one_is_not():
    assert stale(owned(77, ME), proc_start=lambda pid: None) is True                     # no such process
    assert stale(owned(77, ME), proc_start=lambda pid: ME.start) is False                # alive, the very process
    assert stale(owned(77, ME), proc_start=lambda pid: ME.start + 1) is True             # the pid was recycled by another process


def test_another_pid_namespace_is_not_judged_because_its_pids_mean_nothing_here():
    other = ss.Owner(ME.boot, "f0000001", ME.start)
    assert stale(owned(77, other), proc_start=lambda pid: None) is False                 # not even when pid 77 does not exist here


def test_another_boot_is_stale_because_no_process_survives_a_reboot():
    old = ss.Owner("11111111", ME.ns, ME.start)
    assert stale(owned(77, old), proc_start=lambda pid: ME.start) is True
    assert stale(owned(77, ss.Owner("11111111", "f0000001", 3)), proc_start=lambda pid: ME.start) is True


def test_nothing_is_judged_when_my_own_identity_is_unknown_or_the_process_table_is_unreadable():
    assert ss.is_stale(owned(77, ME), None, proc_start=lambda pid: None) is False

    def unreadable(pid):
        raise OSError("hidepid")
    assert stale(owned(77, ME), proc_start=unreadable) is False


@linux_only
def test_the_process_start_time_is_read_from_proc():
    start = ss._proc_start(os.getpid())
    assert isinstance(start, int) and start > 0 and ss._proc_start(os.getpid()) == start
    assert ss._proc_start(2 ** 22 + 12345) is None
