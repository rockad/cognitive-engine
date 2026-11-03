"""Seam 4: `plan_sweep` produces a capped, human-gated plan or refuses. It never starts a sweep.

The Proposer decision is a REAL seam-2 `Decision`, the surface is the real upstream OpenCode surface
and the budget identity comes from upstream's `harness2.core.ledger` (dev group).
"""
from __future__ import annotations

import math
import sys
from types import SimpleNamespace

import pytest

from h2_profiles import PROFILE, WORK, make_profile

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


LOCAL_CFG = {"kind": "opencode", "model": "ollama/qwen3:30b", "base_url": LOCAL, "env": CLEAN_ENV,
             "cwd": WORK}


def decide(config=LOCAL_CFG, channel="local", *, profile=PROFILE, classification="open", **kw):
    return check_proposer(config, profile=profile, channel=channel, classification=classification, **kw)


@pytest.fixture
def decision():
    d = decide()
    assert d.allowed
    return d


def good(surface, decision, **over):
    args = dict(
        surface=surface,
        candidates=[SweepCandidate("sp", "systemprompt.md"), SweepCandidate("tools", "tool_descriptions/tool_guidance.md")],
        k=1, n_tasks=2, max_sessions=6, max_wall_seconds=3600, max_cost_or_tokens=CostCap("usd", 5.0),
        proposer=decision, readiness=OK_READINESS, profile=PROFILE, channel="local", classification="open",
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


def test_the_cost_class_comes_from_advice(surface):
    d = decide({"kind": "opencode", "model": "ollama/x", "base_url": "https://zdr.example/v1", "env": CLEAN_ENV},
               "zdr-host")
    assert d.allowed
    assert plan_sweep(**good(surface, d, channel="zdr-host")).cost_class == "metered"


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
    # a tokens cap cannot be compared with dollars, but the channel always has a usd cap of its own
    plan_sweep(**good(surface, decision, ledger=SimpleNamespace(P=0, usd=4.0), max_cost_or_tokens=CostCap("tokens", 5)))
    assert "channel" in text(refused(**good(surface, decision, ledger=SimpleNamespace(P=0, usd=999.0),
                                            max_cost_or_tokens=CostCap("tokens", 5))))


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

def test_a_refused_proposer_decision_refuses_the_plan(surface):
    d = decide({"kind": "opencode", "model": "ollama/x", "base_url": "https://api.openai.com/v1", "env": CLEAN_ENV})
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


def test_every_reason_is_reported_together(surface):
    d = decide({"kind": "claude_cli", "model": "claude-x"})
    e = refused(**good(surface, d, max_sessions=None, readiness={"sandbox": {"ok": False, "detail": "x"}},
                       candidates=[SweepCandidate("g", "guardrails.md")]))
    t = text(e)
    for needle in ("max_sessions", "sandbox", "candidate", "proposer"):
        assert needle in t, needle


# ── the channel: declared, allowed for this sweep, and the source of the hard caps ─

def test_the_plan_records_its_channel_classification_and_the_channels_caps(surface, decision):
    p = plan_sweep(**good(surface, decision))
    assert (p.channel, p.classification) == ("local", "open")
    assert p.channel_caps == {"sessions": 30, "wall_s": 3600.0, "cost_usd": 5.0}
    d = p.to_dict()
    assert d["channel"] == "local" and d["classification"] == "open" and d["channel_caps"]["sessions"] == 30
    assert p.requires_human_approval is True


def test_requested_caps_are_clipped_to_the_channels_and_the_plan_says_so(surface, decision):
    p = plan_sweep(**good(surface, decision, max_sessions=900, max_wall_seconds=10**5,
                          max_cost_or_tokens=CostCap("usd", 500.0)))
    assert (p.max_sessions, p.max_wall_seconds, p.max_cost_or_tokens) == (30, 3600.0, CostCap("usd", 5.0))
    assert sum("channel's cap" in w for w in p.warnings) == 3


def test_a_request_under_the_channels_caps_is_kept(surface, decision):
    p = plan_sweep(**good(surface, decision, max_sessions=7, max_wall_seconds=60, max_cost_or_tokens=CostCap("usd", 1.0)))
    assert (p.max_sessions, p.max_wall_seconds, p.max_cost_or_tokens) == (7, 60, CostCap("usd", 1.0))
    assert not any("channel's cap" in w for w in p.warnings)


def test_a_plan_that_needs_more_than_the_channels_session_cap_is_refused(surface, decision):
    # k=5 needs 15 sessions per task; the channel allows 30, however much was requested
    e = refused(**good(surface, decision, k=5, n_tasks=3, max_sessions=1000))
    assert "45" in text(e) and "max_sessions 30" in text(e) and "channel's cap" in text(e)
    plan_sweep(**good(surface, decision, k=5, n_tasks=2, max_sessions=1000))


def test_the_ledger_is_held_to_the_channels_usd_cap_too(surface, decision):
    e = refused(**good(surface, decision, ledger=SimpleNamespace(P=0, usd=5.0), max_cost_or_tokens=CostCap("usd", 100.0)))
    assert "channel 'local'" in text(e)


@pytest.mark.parametrize("over", [{"channel": "nope"}, {"channel": None}, {"channel": ""}, {"channel": 5},
                                  {"profile": None}, {"profile": {}}, {"profile": "p"}])
def test_an_undeclared_channel_or_a_missing_profile_refuses_the_plan(surface, decision, over):
    t = text(refused(**good(surface, decision, **over)))
    assert "profile" in t or "channel" in t


def test_a_profile_with_no_file_refuses_and_says_how_to_create_one(surface, decision):
    from cognitive_harness2.channels import ChannelProfile
    e = refused(**good(surface, decision, profile=ChannelProfile(source="/x/p.toml", declared=False)))
    assert "/x/p.toml" in text(e) and "[channels.<name>]" in text(e)


@pytest.mark.parametrize("cls", [None, "gated", "restricted", "unstated", "junk"])
def test_a_missing_or_stricter_classification_than_the_decision_is_refused(surface, decision, cls):
    e = refused(**good(surface, decision, classification=cls))
    assert "classification" in text(e)


def test_a_missing_classification_is_restricted_and_noted(surface):
    d = decide(classification=None)
    assert d.view["classification"] == "restricted"
    p = plan_sweep(**good(surface, d, classification=None))
    assert p.classification == "restricted" and any("treated as restricted" in w for w in p.warnings)


def test_a_decision_made_for_another_channel_cannot_be_reused(surface):
    other = decide({"kind": "opencode", "model": "ollama/x", "base_url": "https://zdr.example/v1", "env": CLEAN_ENV},
                   "zdr-host")
    assert other.allowed
    e = refused(**good(surface, other))          # the sweep names channel "local"
    assert "channel 'zdr-host'" in text(e)


def test_a_decision_made_under_another_zdr_mode_cannot_be_reused(surface):
    off = decide(zdr="off")
    assert off.allowed
    assert "zdr" in text(refused(**good(surface, off)))


def test_the_channel_policy_is_rechecked_by_the_plan(surface):
    # a forged-looking but real decision for the claude channel under zdr=off ...
    d = decide({"kind": "claude_cli", "model": "claude-x", "base_url": "https://api.anthropic.com", "env": CLEAN_ENV},
               "claude", zdr="off")
    assert d.allowed
    plan_sweep(**good(surface, d, channel="claude", zdr="off"))
    # ... is not usable under the default strict mode, whatever the decision says
    e = refused(**good(surface, d, channel="claude"))
    assert "rule retention-vs-zdr" in text(e)
    e = refused(**good(surface, d, channel="claude", zdr="off", classification="gated"))
    assert "rule unknown-retention-open-only" in text(e)


def test_a_restricted_sweep_on_a_gated_channel_is_refused(surface):
    d = decide({"kind": "opencode", "model": "openrouter/anthropic/claude-x", "base_url": "https://openrouter.ai/api/v1",
                "env": CLEAN_ENV}, "openrouter", classification="gated")
    assert d.allowed
    plan_sweep(**good(surface, d, channel="openrouter", classification="gated"))
    assert "classification-ceiling" in text(refused(**good(surface, d, channel="openrouter", classification="restricted")))


# ── a zero USD channel cap (a local model that must spend nothing) ─────────────────


def test_a_zero_usd_channel_cap_plans_and_clips_the_request_to_zero(surface, decision):
    from cognitive_harness2 import channels as ch
    raw = {"workspace_roots": [WORK.rsplit("/", 1)[0]],
           "channels": {"local": {"kind": "opencode", "provider": "ollama", "endpoint": LOCAL, "model": "ollama/qwen3:30b",
                                  "retention": "local", "max_classification": "restricted", "approval": "per-sweep",
                                  "caps": {"sessions": 30, "wall_s": 3600, "cost_usd": 0}}}}
    prof = ch.validate(raw, source="/p/z.toml")
    p = plan_sweep(**good(surface, decision, profile=prof, ledger=SimpleNamespace(P=0, usd=0.0)))
    assert p.max_cost_or_tokens == CostCap("usd", 0) and p.channel_caps["cost_usd"] == 0
    assert any("channel's cap 0" in w for w in p.warnings)
    plan_sweep(**good(surface, decision, profile=prof))                      # no ledger at all
    e = refused(**good(surface, decision, profile=prof, ledger=SimpleNamespace(P=0, usd=0.01)))
    assert "already spent" in text(e)
