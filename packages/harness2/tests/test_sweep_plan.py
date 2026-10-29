"""Seam 4: `plan_sweep` produces a capped, human-gated plan or refuses. It never starts a sweep.

The Proposer decision is a REAL seam-2 `Decision`, the surface is the real upstream OpenCode surface
and the budget identity comes from upstream's `harness2.core.ledger` (dev group).
"""
from __future__ import annotations

import math
import sys
from types import SimpleNamespace

import pytest

from cognitive_harness2 import settings
from cognitive_harness2.quota import QuotaVerdict
from cognitive_harness2.sweep_plan import (
    CostCap,
    SweepCandidate,
    SweepPlan,
    SweepRefused,
    plan_sweep,
)
from cognitive_harness2.upstream_egress import Decision, check_proposer

LOCAL = "http://127.0.0.1:11434/v1"
CLEAN_ENV = {"PATH": "/usr/bin", "HOME": "/tmp/h"}
OK_READINESS = {"sandbox": {"ok": True, "detail": "usable"}, "egress": {"ok": True, "detail": "local"},
                "quota": {"ok": True, "detail": "n/a"}, "model": {"ok": True, "detail": "m"}}


@pytest.fixture(scope="module")
def surface():
    from harness2.substrate.opencode import OPENCODE_SURFACE
    return OPENCODE_SURFACE


@pytest.fixture
def st(tmp_path):
    return settings.from_env({settings.ENV_EXPERIENCE_ROOT: str(tmp_path / "exp")})


@pytest.fixture
def decision(st):
    d = check_proposer({"kind": "opencode", "model": "ollama/qwen3", "base_url": LOCAL, "env": CLEAN_ENV,
                        "cwd": "/tmp/work"}, st)
    assert d.allowed
    return d


def good(surface, decision, **over):
    args = dict(
        surface=surface,
        candidates=[SweepCandidate("sp", "systemprompt.md"), SweepCandidate("tools", "tool_descriptions/tool_guidance.md")],
        k=1, n_tasks=2, max_sessions=6, max_wall_seconds=3600, max_cost_or_tokens=CostCap("usd", 5.0),
        proposer=decision, readiness=OK_READINESS,
    )
    args.update(over)
    return args


def refused(**kw) -> SweepRefused:
    with pytest.raises(SweepRefused) as ei:
        plan_sweep(**kw)
    return ei.value


def text(e: SweepRefused) -> str:
    return " | ".join(e.reasons)


# ── the plan ──────────────────────────────────────────────────────────────────────

def test_a_complete_plan_is_returned_and_needs_human_approval(surface, decision):
    p = plan_sweep(**good(surface, decision))
    assert isinstance(p, SweepPlan)
    assert p.requires_human_approval is True
    assert (p.max_sessions, p.max_wall_seconds, p.max_cost_or_tokens) == (6, 3600, CostCap("usd", 5.0))
    assert [c.label for c in p.candidates] == ["sp", "tools"]
    assert p.cost_class == "local"          # advice.cost_class on a loopback endpoint
    assert p.ready is True
    assert (p.planned_sessions, p.planned_rollouts) == (6, 8)     # upstream's P = 3k and R = 4k, per task
    assert p.proposer["endpoint"] == "http://127.0.0.1:11434"
    assert p.to_dict()["requires_human_approval"] is True


def test_the_plan_cannot_be_built_without_the_gate(surface, decision):
    p = plan_sweep(**good(surface, decision))
    with pytest.raises(TypeError):
        SweepPlan(requires_human_approval=False)  # type: ignore[call-arg]
    with pytest.raises(AttributeError):
        p.requires_human_approval = False  # type: ignore[misc]
    with pytest.raises(AttributeError):
        p.max_sessions = 10**9  # type: ignore[misc]


def test_the_cost_class_comes_from_advice(surface, st):
    d = check_proposer({"kind": "opencode", "model": "ollama/x", "base_url": "https://zdr.example/v1", "env": CLEAN_ENV},
                       settings.from_env({settings.ENV_EXPERIENCE_ROOT: "/tmp/exp-x", settings.ENV_ZDR_ENDPOINTS: "zdr.example"}))
    assert d.allowed
    assert plan_sweep(**good(surface, d)).cost_class == "metered"


def test_unknown_readiness_is_allowed_and_recorded(surface, decision):
    rd = {**OK_READINESS, "quota": {"ok": None, "status": "not_checked", "detail": "not checked"}}
    p = plan_sweep(**good(surface, decision, readiness=rd))
    assert p.ready is None
    assert p.readiness["quota"]["ok"] is None


def test_the_proposers_warnings_travel_with_the_plan(surface, decision):
    p = plan_sweep(**good(surface, decision))
    assert any("host" in w for w in p.warnings)


# ── hard caps ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["max_sessions", "max_wall_seconds", "max_cost_or_tokens"])
def test_a_missing_cap_is_refused(surface, decision, name):
    e = refused(**good(surface, decision, **{name: None}))
    assert name in text(e) and "missing" in text(e)


def test_a_cap_left_out_of_the_call_is_refused_not_defaulted(surface, decision):
    args = good(surface, decision)
    for name in ("max_sessions", "max_wall_seconds", "max_cost_or_tokens"):
        a = dict(args)
        del a[name]
        assert name in text(refused(**a))


BAD_NUMBERS = [0, 0.0, -1, -0.5, math.inf, -math.inf, math.nan, True, False, "5", "inf", [5], None, 10**400]


@pytest.mark.parametrize("value", BAD_NUMBERS, ids=repr)
def test_bad_wall_clock_caps_are_refused(surface, decision, value):
    assert "max_wall_seconds" in text(refused(**good(surface, decision, max_wall_seconds=value)))


@pytest.mark.parametrize("value", [*BAD_NUMBERS, 1.5, 2.0])
def test_bad_session_caps_are_refused(surface, decision, value):
    assert "max_sessions" in text(refused(**good(surface, decision, max_sessions=value)))


@pytest.mark.parametrize("amount", [0, -1, math.inf, math.nan, True, "5", None])
def test_bad_cost_caps_are_refused(surface, decision, amount):
    e = refused(**good(surface, decision, max_cost_or_tokens=CostCap("usd", amount)))  # type: ignore[arg-type]
    assert "max_cost_or_tokens" in text(e)


@pytest.mark.parametrize("cap", [5, 5.0, "5 usd", {"usd": 5}, ("usd", 5), CostCap("eur", 5), CostCap("", 5), CostCap(None, 5)])  # type: ignore[arg-type]
def test_a_cost_cap_must_name_its_unit(surface, decision, cap):
    assert "max_cost_or_tokens" in text(refused(**good(surface, decision, max_cost_or_tokens=cap)))


def test_tokens_are_a_valid_unit(surface, decision):
    assert plan_sweep(**good(surface, decision, max_cost_or_tokens=CostCap("tokens", 200_000))).max_cost_or_tokens.unit == "tokens"


def test_every_bad_cap_is_reported_at_once(surface, decision):
    e = refused(**good(surface, decision, max_sessions=0, max_wall_seconds=None, max_cost_or_tokens=CostCap("usd", 0)))
    assert len(e.reasons) >= 3


# ── the ledger budget ─────────────────────────────────────────────────────────────

def test_a_plan_larger_than_its_own_session_cap_is_refused(surface, decision):
    e = refused(**good(surface, decision, k=2, n_tasks=2, max_sessions=11))   # needs 3k*tasks = 12
    assert "12" in text(e) and "max_sessions" in text(e)
    assert plan_sweep(**good(surface, decision, k=2, n_tasks=2, max_sessions=12)).planned_sessions == 12


@pytest.mark.parametrize("k", [0, -1, 1.5, True, "1", None])
def test_k_must_be_a_positive_integer(surface, decision, k):
    assert "k" in text(refused(**good(surface, decision, k=k)))


@pytest.mark.parametrize("n", [0, -1, 1.5, True, "1", None])
def test_n_tasks_must_be_a_positive_integer(surface, decision, n):
    assert "n_tasks" in text(refused(**good(surface, decision, n_tasks=n)))


def test_sessions_already_spent_count_against_the_cap(surface, decision):
    spent = SimpleNamespace(P=4, usd=0.0)
    e = refused(**good(surface, decision, ledger=spent))     # 4 spent + 6 planned > 6
    assert "ledger" in text(e)
    assert plan_sweep(**good(surface, decision, ledger=SimpleNamespace(P=0, usd=0.0))).ready is True


def test_money_already_spent_counts_against_a_usd_cap(surface, decision):
    assert "ledger" in text(refused(**good(surface, decision, ledger=SimpleNamespace(P=0, usd=5.0))))
    plan_sweep(**good(surface, decision, ledger=SimpleNamespace(P=0, usd=4.99)))
    plan_sweep(**good(surface, decision, ledger=SimpleNamespace(P=0, usd=999.0), max_cost_or_tokens=CostCap("tokens", 5)))


def test_an_unreadable_ledger_is_refused(surface, decision):
    assert "ledger" in text(refused(**good(surface, decision, ledger=object())))
    assert "ledger" in text(refused(**good(surface, decision, ledger=SimpleNamespace(P="x", usd=0.0))))


def test_a_real_upstream_ledger_is_consulted(surface, decision):
    from harness2.core.ledger import Ledger
    led = Ledger(mode="agg", task_id="t", k=1)
    led.session("inv1", 0.0)
    assert "ledger" in text(refused(**good(surface, decision, ledger=led, max_sessions=6)))
    plan_sweep(**good(surface, decision, ledger=led, max_sessions=7))


def test_the_budget_identity_is_upstreams(surface, decision):
    from harness2.core import ledger
    p = plan_sweep(**good(surface, decision, k=3, n_tasks=1, max_sessions=100))
    assert (p.planned_sessions, p.planned_rollouts) == (ledger.expected_sessions(3), ledger.expected_rollouts(3))


def test_without_the_upstream_extra_it_refuses_rather_than_guessing(surface, decision, monkeypatch):
    monkeypatch.setitem(sys.modules, "harness2.core.ledger", None)   # makes the import raise ImportError
    e = refused(**good(surface, decision))
    assert "upstream" in text(e) and "uv sync --extra upstream" in text(e)


# ── readiness ─────────────────────────────────────────────────────────────────────

def test_a_known_blocker_is_refused_and_named(surface, decision):
    rd = {**OK_READINESS, "sandbox": {"ok": False, "detail": "fail closed: no docker"}}
    e = refused(**good(surface, decision, readiness=rd))
    assert "sandbox" in text(e) and "no docker" in text(e)


def test_a_known_blocker_beats_unknowns(surface, decision):
    rd = {"sandbox": {"ok": None}, "egress": {"ok": False, "detail": "denied"}}
    assert "egress" in text(refused(**good(surface, decision, readiness=rd)))


@pytest.mark.parametrize("rd", [None, "ready", [], {}, {"sandbox": True}, {"sandbox": {}}, {"sandbox": {"ok": "yes"}},
                                {"sandbox": {"ok": 1}}])
def test_missing_or_malformed_readiness_is_refused(surface, decision, rd):
    assert "readiness" in text(refused(**good(surface, decision, readiness=rd)))


def test_the_quota_verdict_is_consulted(surface, decision):
    e = refused(**good(surface, decision, quota=QuotaVerdict(False, "daily free-model requests exhausted")))
    assert "quota" in text(e) and "exhausted" in text(e)
    p = plan_sweep(**good(surface, decision, quota=QuotaVerdict(True, "5 left")))
    assert p.readiness["quota"]["ok"] is True


def test_a_quota_verdict_of_the_wrong_type_is_refused(surface, decision):
    assert "quota" in text(refused(**good(surface, decision, quota=True)))


def test_no_quota_verdict_leaves_quota_as_the_caller_reported_it(surface, decision):
    rd = {**OK_READINESS, "quota": {"ok": None, "detail": "not checked"}}
    assert plan_sweep(**good(surface, decision, readiness=rd)).readiness["quota"]["ok"] is None


# ── the proposer decision ─────────────────────────────────────────────────────────

def test_a_refused_proposer_decision_refuses_the_plan(surface, st):
    d = check_proposer({"kind": "opencode", "model": "ollama/x", "base_url": "https://api.openai.com/v1", "env": CLEAN_ENV}, st)
    assert not d.allowed
    e = refused(**good(surface, d))
    assert "proposer" in text(e) and "api.openai.com" in text(e)


@pytest.mark.parametrize("bad", [None, True, {"allowed": True}, SimpleNamespace(allowed=True, view={}), "allowed"])
def test_only_a_real_decision_counts(surface, bad):
    assert "proposer" in text(refused(**good(surface, bad)))


def test_a_forged_allowed_flag_needs_a_decision_object(surface):
    forged = Decision(True, ("approved",), (), {})   # allowed, but with no endpoint: cannot be costed
    assert "proposer" in text(refused(**good(surface, forged)))


# ── candidates ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cands", [
    [],
    None,
    [SweepCandidate("g", "guardrails.md")],                      # c2 protected
    [SweepCandidate("p", "plugin/gate.ts")],                      # c8
    [SweepCandidate("s", "scripts/x.py")],                        # c7
    [SweepCandidate("h", "hooks/x.md")],
    [SweepCandidate("g", ".git/config")],
    [SweepCandidate("t", "../systemprompt.md")],
    [SweepCandidate("a", "systemprompt.md"), SweepCandidate("a", "LongTermMEMORY.md")],     # duplicate label
    [SweepCandidate("a", "systemprompt.md"), SweepCandidate("b", "systemprompt.md")],       # duplicate path
    [SweepCandidate("", "systemprompt.md")],
    [SweepCandidate(5, "systemprompt.md")],                       # type: ignore[arg-type]
    ["systemprompt.md"],
    [SweepCandidate(f"c{i}", f"sub_agents/a{i}/AGENT.md") for i in range(9)],               # more than the engine's cap
])
def test_bad_candidates_are_refused(surface, decision, cands):
    assert "candidate" in text(refused(**good(surface, decision, candidates=cands)))


def test_the_allowed_candidates_are_exactly_the_mutable_slots(surface, decision):
    paths = ["systemprompt.md", "LongTermMEMORY.md", "tool_descriptions/tool_guidance.md", "sub_agents/h/AGENT.md"]
    p = plan_sweep(**good(surface, decision, candidates=[SweepCandidate(f"c{i}", x) for i, x in enumerate(paths)]))
    assert len(p.candidates) == 4


def test_an_unrecognised_surface_is_refused(decision):
    assert "surface" in text(refused(**good(object(), decision)))


def test_every_reason_is_reported_together(surface, st):
    d = check_proposer({"kind": "claude_cli", "model": "claude-x"}, st)
    e = refused(**good(surface, d, max_sessions=None, readiness={"sandbox": {"ok": False, "detail": "x"}},
                       candidates=[SweepCandidate("g", "guardrails.md")]))
    t = text(e)
    for needle in ("max_sessions", "sandbox", "candidate", "proposer"):
        assert needle in t, needle
