"""`harness2_trigger`: the read-only pull check. Schema is closed, answers are advisory,
and nothing runs: no sandbox, no model, no quota call unless asked, no file."""
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from cognitive_harness2 import settings, triggers
from cognitive_harness2.engine import Engine
from cognitive_harness2.experience import ExperienceStore
from cognitive_harness2.quota import QuotaVerdict
from cognitive_harness2.sandbox import SandboxResult, SandboxUnavailable
from cognitive_harness2.server import build_server

LOCAL = "http://127.0.0.1:11434/v1"
OPENROUTER = "https://openrouter.ai/api/v1"


class FakeRunner:
    def __init__(self):
        self.runs = []

    def run(self, spec):
        self.runs.append(spec)
        return SandboxResult(0, "", "")


class Probes:
    """Every collaborator that must stay untouched, recorded."""

    def __init__(self, *, sandbox_ok=True, quota=QuotaVerdict(True, "9 left")):
        self.runner, self.sandbox_ok, self.quota, self.quota_calls, self.factory_calls = FakeRunner(), sandbox_ok, quota, 0, 0

    def factory(self):
        self.factory_calls += 1
        if not self.sandbox_ok:
            raise SandboxUnavailable("no sbx or rootless docker (fail closed)")
        return self.runner

    async def quota_check(self):
        self.quota_calls += 1
        return self.quota


class ExplodingGenerator:
    async def generate(self, prompt, *, model):
        raise AssertionError("harness2_trigger must never call a model")


def make(tmp_path, *, base_url=LOCAL, model="llama3", zdr_endpoints=frozenset(), **kw):
    p = Probes(**kw)
    eng = Engine(generator=ExplodingGenerator(), sandbox_factory=p.factory, store=ExperienceStore(tmp_path / "exp"),
                 quota_check=p.quota_check, base_url=base_url, model=model, zdr_endpoints=zdr_endpoints)
    s = settings.from_env({settings.ENV_EXPERIENCE_ROOT: str(tmp_path / "exp")})
    return Client(build_server(eng, s)), p, eng


async def ask(client, **args):
    async with client as c:
        return (await c.call_tool("harness2_trigger", args)).structured_content


# ── decisions ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("n,fires", [(2, False), (3, True), (4, True)])
async def test_below_at_above_the_retry_limit(tmp_path, n, fires):
    client, _, _ = make(tmp_path)
    out = await ask(client, consecutive_failures=n)
    assert out["advise"] is fires
    assert out["trigger"] == (triggers.RETRIES_EXHAUSTED if fires else None)


async def test_nothing_known_is_no_advice_and_no_cost(tmp_path):
    client, _, _ = make(tmp_path)
    out = await ask(client)
    assert out["advise"] is False and out["trigger"] is None and out["cost_class"] == "none"
    assert out["next"] is None


async def test_repeated_identical_failure(tmp_path):
    client, _, _ = make(tmp_path)
    out = await ask(client, failure_signatures=["E1", "E1", "E1"])
    assert out["advise"] and out["trigger"] == triggers.RETRIES_EXHAUSTED and "identical" in out["reason"]


async def test_judge_failure_and_low_score_and_explicit(tmp_path):
    client, _, _ = make(tmp_path)
    assert (await ask(client, judge_passed=False))["trigger"] == triggers.JUDGE_FAILED
    assert (await ask(client, judge_score=0.49))["trigger"] == triggers.JUDGE_SCORE
    assert (await ask(client, judge_score=0.5))["advise"] is False
    assert (await ask(client, on_demand=True))["trigger"] == triggers.EXPLICIT


async def test_advice_always_requires_human_approval_and_is_read_only(tmp_path):
    client, _, _ = make(tmp_path)
    for args in ({}, {"on_demand": True}, {"consecutive_failures": 9}):
        out = await ask(client, **args)
        assert out["requires_human_approval"] is True and out["read_only"] is True


# ── readiness blockers ───────────────────────────────────────────────────────────

async def test_sandbox_unavailable_is_not_ready_but_the_trigger_still_advises(tmp_path):
    client, _, _ = make(tmp_path, sandbox_ok=False)
    out = await ask(client, consecutive_failures=3)
    assert out["advise"] is True and out["ready"] is False
    assert out["readiness"]["sandbox"]["runtime"] == "unavailable"
    assert any("fail closed" in b for b in out["blockers"])


async def test_egress_refused_is_not_ready(tmp_path):
    client, _, _ = make(tmp_path, base_url="https://api.example.com/v1")
    out = await ask(client, consecutive_failures=3)
    assert out["advise"] is True and out["ready"] is False
    assert out["readiness"]["egress"]["ok"] is False and "egress refused" in out["readiness"]["egress"]["detail"]


async def test_free_tier_model_is_refused_under_strict_zdr(tmp_path):
    client, _, _ = make(tmp_path, base_url="https://openrouter.ai/api/v1", model="openrouter/free")
    out = await ask(client, consecutive_failures=3)
    assert out["readiness"]["egress"]["ok"] is False


async def test_quota_exhausted_is_not_ready_when_checked(tmp_path):
    client, probes, _ = make(tmp_path, base_url=OPENROUTER, zdr_endpoints=frozenset({"openrouter.ai"}),
                             quota=QuotaVerdict(False, "daily free-model requests exhausted"))
    out = await ask(client, consecutive_failures=3, check_quota=True)
    assert out["ready"] is False and out["readiness"]["quota"]["status"] == "checked"
    assert any("exhausted" in b for b in out["blockers"]) and probes.quota_calls == 1


async def test_quota_is_not_called_unless_asked_and_unknown_is_unknown_not_ready_and_not_a_blocker(tmp_path):
    client, probes, _ = make(tmp_path, base_url=OPENROUTER, zdr_endpoints=frozenset({"openrouter.ai"}))
    out = await ask(client, consecutive_failures=3)
    assert probes.quota_calls == 0
    assert out["readiness"]["quota"]["status"] == "not_checked" and out["readiness"]["quota"]["ok"] is None
    assert out["advise"] is True and out["ready"] is None and out["blockers"] == []


async def test_quota_preflight_is_skipped_for_a_local_endpoint(tmp_path):
    client, probes, _ = make(tmp_path)
    out = await ask(client, consecutive_failures=3, check_quota=True)
    assert out["readiness"]["quota"]["status"] == "not_applicable" and probes.quota_calls == 0


async def test_no_model_configured_is_not_ready(tmp_path):
    client, _, _ = make(tmp_path, model=None)
    out = await ask(client, consecutive_failures=3)
    assert out["ready"] is False and out["readiness"]["model"]["ok"] is False


@pytest.mark.parametrize("base_url,model,zdr_endpoints,cls", [
    (LOCAL, "llama3", frozenset(), "local"),
    ("https://zdr.example.com/v1", "big-model", frozenset({"zdr.example.com"}), "metered"),
])
async def test_cost_class(tmp_path, base_url, model, zdr_endpoints, cls):
    client, _, _ = make(tmp_path, base_url=base_url, model=model, zdr_endpoints=zdr_endpoints)
    assert (await ask(client, consecutive_failures=3))["cost_class"] == cls


async def test_ready_advice_points_at_the_explicit_tools(tmp_path):
    client, _, _ = make(tmp_path)
    out = await ask(client, consecutive_failures=3)
    assert out["advise"] and out["ready"] is True
    assert "harness2_propose" in out["next"] and out["readiness"]["sandbox"]["runtime"] == "FakeRunner"


# ── advise (a trigger fired) is independent of ready (tri-state readiness) ───────

READINESS_CASES = {   # name -> (make kwargs, expected ready, expected blockers present)
    "ok": (dict(), True, False),
    "bad": (dict(sandbox_ok=False), False, True),
    "unchecked": (dict(base_url=OPENROUTER, zdr_endpoints=frozenset({"openrouter.ai"})), None, False),
    # a known-bad check wins over an unchecked one: ready is false, not unknown
    "bad_and_unchecked": (dict(base_url=OPENROUTER, zdr_endpoints=frozenset({"openrouter.ai"}), model=None),
                          False, True),
}


@pytest.mark.parametrize("fired", [True, False])
@pytest.mark.parametrize("state", sorted(READINESS_CASES))
async def test_advise_and_ready_are_independent(tmp_path, fired, state):
    kwargs, ready, has_blockers = READINESS_CASES[state]
    client, probes, eng = make(tmp_path, **kwargs)
    out = await ask(client, **({"consecutive_failures": 3} if fired else {}))
    assert out["advise"] is fired
    assert out["ready"] is ready
    assert bool(out["blockers"]) is has_blockers
    assert (out["trigger"] is not None) is fired
    assert out["requires_human_approval"] is True and out["read_only"] is True
    assert "trigger_fired" not in out
    assert probes.runner.runs == [] and eng.runs == {} and probes.quota_calls == 0


async def test_ready_is_true_only_when_every_check_is_positively_ok(tmp_path):
    client, probes, _ = make(tmp_path, base_url=OPENROUTER, zdr_endpoints=frozenset({"openrouter.ai"}))
    out = await ask(client, consecutive_failures=3, check_quota=True)
    assert {k: v["ok"] for k, v in out["readiness"].items()} == {
        "sandbox": True, "egress": True, "quota": True, "model": True}
    assert out["ready"] is True and probes.quota_calls == 1


async def test_next_is_offered_unless_a_check_is_known_bad(tmp_path):
    for state, has_next in (("ok", True), ("unchecked", True), ("bad", False)):
        client, _, _ = make(tmp_path / state, **READINESS_CASES[state][0])
        out = await ask(client, consecutive_failures=3)
        assert (out["next"] is not None) is has_next
    client, _, _ = make(tmp_path / "unfired")
    assert (await ask(client))["next"] is None


# ── closed, strict schema ────────────────────────────────────────────────────────

@pytest.mark.parametrize("args", [
    {"consecutive_failurez": 3},                  # unknown argument
    {"routing_confidence": 0.1},                  # not an input: routing never triggers
    {"attempts_without_progress": 3},             # stagnation is not an RFC 004 §2.3 trigger: old callers are rejected
    {"attempts_without_progress": 0},
    {"consecutive_failures": "three"},            # wrong type
    {"consecutive_failures": -1},                 # out of range
    {"consecutive_failures": 1001},
    {"judge_score": 1.5},
    {"judge_score": "high"},
    {"zdr": "maybe"},
    {"classification": "public"},
    {"failure_signatures": "E1"},                 # not a list
    {"failure_signatures": [1, 2]},               # not strings
    {"failure_signatures": ["x" * 201]},          # too long
    {"failure_signatures": ["x"] * 21},           # too many
    {"on_demand": "yes please"},
])
async def test_bad_arguments_are_tool_errors(tmp_path, args):
    client, _, _ = make(tmp_path)
    async with client as c:
        with pytest.raises(ToolError):
            await c.call_tool("harness2_trigger", args)


async def test_the_input_schema_is_closed_and_exposes_no_free_form_text(tmp_path):
    client, _, _ = make(tmp_path)
    async with client as c:
        tool = next(t for t in await c.list_tools() if t.name == "harness2_trigger")
    props = tool.input_schema["properties"]
    assert set(props) == {"on_demand", "judge_passed", "judge_score", "consecutive_failures", "failure_signatures",
                          "zdr", "classification", "check_quota"}
    assert tool.input_schema.get("required", []) == []
    assert tool.input_schema.get("additionalProperties") is False


# ── read-only: nothing runs ──────────────────────────────────────────────────────

async def test_nothing_executes_even_when_everything_fires_and_is_ready(tmp_path):
    client, probes, eng = make(tmp_path)
    for args in ({"on_demand": True}, {"consecutive_failures": 9}, {"judge_passed": False},
                 {"failure_signatures": ["E"] * 5}):
        assert (await ask(client, **args))["advise"] is True
    assert probes.runner.runs == []                 # no sandbox command, ever
    assert eng.runs == {}                           # no run was created
    assert probes.quota_calls == 0                  # no network pre-flight unasked
    assert not (tmp_path / "exp").exists() or not any((tmp_path / "exp").iterdir())   # no file written


async def test_nothing_executes_when_the_sandbox_is_unavailable_either(tmp_path):
    client, probes, eng = make(tmp_path, sandbox_ok=False)
    await ask(client, on_demand=True)
    assert probes.runner.runs == [] and eng.runs == {} and probes.factory_calls == 1


async def test_the_signatures_are_not_echoed_back(tmp_path):
    client, _, _ = make(tmp_path)
    out = await ask(client, failure_signatures=["secret-token-abc"] * 3)
    assert "secret-token-abc" not in repr(out)


# ── the tri-state rule itself (pure) ─────────────────────────────────────────────

@pytest.mark.parametrize("oks,expected", [
    ([True, True, True, True], True),
    ([True, True, None, True], None),
    ([None, None, None, None], None),
    ([True, False, True, True], False),
    ([None, False, True, True], False),    # known-bad beats unchecked
    ([], None),                            # nothing checked is never "ready"
    ([True, "yes"], None),                 # only a literal True counts as positively ok
])
def test_tri_state_ready(oks, expected):
    from cognitive_harness2.advice import tri_state_ready
    assert tri_state_ready({f"c{i}": {"ok": ok} for i, ok in enumerate(oks)}) is expected
