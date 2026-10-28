"""Quota gate, sandbox gate, experience sign-off, and the engine's refusals."""
import json
import subprocess

import httpx
import pytest

from cognitive_harness2 import quota, sandbox
from cognitive_harness2.engine import MAX_RUNS, Engine, Refused, Target
from cognitive_harness2.experience import ExperienceError, ExperienceStore, SignoffRequired
from cognitive_harness2.quota import QuotaVerdict
from cognitive_harness2.sandbox import SandboxResult, SandboxSpec, SandboxUnavailable

# ── quota ────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("status,payload,ok", [
    (200, {"data": {"free_model_daily_requests": 5}}, True),
    (200, {"data": {"free_model_daily_requests": 0}}, False),
    (200, {"data": {"free_model_daily_requests": {"remaining": 3}}}, True),
    (200, {"data": {"free_model_daily_requests": {"used": 50, "limit": 50}}}, False),
    (200, {"data": {"free_model_daily_requests": {"used": 10, "limit": 50}}}, True),
    (200, {"data": {"free_model_daily_requests": "lots"}}, False),
    (200, {"data": {"limit_remaining": 4.2}}, True),
    (200, {"data": {"limit_remaining": 0}}, False),
    (200, {"data": {"limit_remaining": None}}, True),
    (200, {"data": {}}, False),
    (200, {"unexpected": 1}, False),
    (200, None, False),
    (429, {"data": {"free_model_daily_requests": 99}}, False),
    (500, None, False),
    (None, None, False),
])
def test_quota_evaluation_table(status, payload, ok):
    assert quota.evaluate(status, payload).ok is ok


async def test_preflight_without_a_key_sends_nothing():
    def handler(request):  # pragma: no cover - must not run
        raise AssertionError("a request was sent without a key")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        v = await quota.preflight(None, c)
    assert not v.ok and "no OpenRouter API key" in v.reason


async def test_preflight_sends_the_key_as_a_bearer_and_never_reports_it():
    seen = {}
    def handler(request):
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(200, json={"data": {"free_model_daily_requests": 7}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        v = await quota.preflight("sk-secret", c)
    assert seen["auth"] == "Bearer sk-secret" and v.ok
    assert "sk-secret" not in repr(v)


async def test_preflight_fails_closed_on_a_network_error():
    def handler(request):
        raise httpx.ConnectError("down")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        v = await quota.preflight("k", c)
    assert not v.ok and "ConnectError" in v.reason


# ── sandbox ──────────────────────────────────────────────────────────────────────

def test_missing_sbx_refuses():
    with pytest.raises(SandboxUnavailable, match="fail closed"):
        sandbox.require_sbx(lambda _: None)


def test_present_sbx_is_returned():
    assert sandbox.require_sbx(lambda _: "/usr/bin/sbx") == "/usr/bin/sbx"


def test_static_diff_needs_an_explicit_opt_in():
    with pytest.raises(SandboxUnavailable, match="opt-in"):
        sandbox.static_diff("p", "a\n", "b\n", allow_worktree_diff=False)
    d = sandbox.static_diff("p", "a\n", "b\n", allow_worktree_diff=True)
    assert "-a" in d and "+b" in d and "a/p" in d


def test_sbx_runner_wraps_the_command_and_reports_a_timeout(monkeypatch):
    r = sandbox.SbxRunner("/x/sbx")
    assert r._argv(SandboxSpec(command=["echo", "hi"]))[-2:] == ["echo", "hi"]
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="sbx", timeout=1, output=b"partial")
    monkeypatch.setattr(subprocess, "run", boom)
    out = r.run(SandboxSpec(command=["sleep", "9"], timeout_s=1))
    assert out.timed_out and out.exit_code is None


# ── experience ───────────────────────────────────────────────────────────────────

def test_staging_writes_a_pending_item_and_commits_nothing(tmp_path):
    s = ExperienceStore(tmp_path)
    p = s.stage("python", "Prefer small prompts.")
    assert [x.id for x in s.pending()] == [p.id]
    assert not (tmp_path / "python").exists()


def test_approval_needs_a_named_human(tmp_path):
    s = ExperienceStore(tmp_path)
    p = s.stage("python", "Lesson")
    for who in (None, "", "   "):
        with pytest.raises(SignoffRequired):
            s.approve(p.id, approved_by=who)
    assert not (tmp_path / "python").exists()


def test_approval_commits_and_clears_the_pending_item(tmp_path):
    s = ExperienceStore(tmp_path)
    p = s.stage("python", "Lesson one\nmore detail")
    path = s.approve(p.id, approved_by="Aleksandr")
    assert "Lesson one" in path.read_text() and "Approved by Aleksandr" in path.read_text()
    assert p.id in (tmp_path / "python" / "INDEX.md").read_text()
    assert s.pending() == []


@pytest.mark.parametrize("domain", ["../x", "a/b", "A", "", "-x", ".hidden", "x" * 80])
def test_bad_domains_are_refused(tmp_path, domain):
    with pytest.raises(ExperienceError):
        ExperienceStore(tmp_path).stage(domain, "lesson")


@pytest.mark.parametrize("pid", ["../../etc/passwd", "zzzzzzzzzzzz", "abc", "0" * 12])
def test_bad_pending_ids_are_refused(tmp_path, pid):
    with pytest.raises(ExperienceError):
        ExperienceStore(tmp_path).approve(pid, approved_by="me")


def test_an_empty_lesson_is_refused(tmp_path):
    with pytest.raises(ExperienceError):
        ExperienceStore(tmp_path).stage("python", "  ")

# ── engine ───────────────────────────────────────────────────────────────────────

class FakeGenerator:
    def __init__(self):
        self.calls = 0
    async def generate(self, prompt, *, model):
        self.calls += 1
        return f"improved {self.calls}"


class FakeRunner:
    def __init__(self, codes=None):
        self.codes, self.seen = codes or {}, []
    def run(self, spec):
        self.seen.append(spec)
        text = list(spec.files.values())[0]
        return SandboxResult(self.codes.get(text, 0), f"out {text}", "", False)


class Probes:
    def __init__(self, verdict):
        self.verdict, self.calls = verdict, 0
    async def __call__(self):
        self.calls += 1
        return self.verdict


def make(tmp_path, *, base="http://127.0.0.1:11434/v1", model="llama3", runner=None, verdict=QuotaVerdict(True, "ok"),
         zdr_endpoints=frozenset()):
    gen, q = FakeGenerator(), Probes(verdict)
    def factory():
        if runner is None:
            raise SandboxUnavailable("no sbx (fail closed)")
        return runner
    eng = Engine(generator=gen, sandbox_factory=factory, store=ExperienceStore(tmp_path), quota_check=q,
                 base_url=base, model=model, zdr_endpoints=zdr_endpoints)
    return eng, gen, q


T = [Target("prompt", "be helpful")]


async def propose(eng, **kw):
    args = dict(task="t", targets=T, n=2, zdr="strict", classification="unstated", dry_run=False)
    return await eng.propose(**{**args, **kw})


async def test_a_protected_component_is_refused_before_any_generation(tmp_path):
    eng, gen, _ = make(tmp_path)
    with pytest.raises(Refused, match="protected"):
        await propose(eng, targets=[Target("egress_policy", "x")])
    assert gen.calls == 0


async def test_a_forbidden_endpoint_is_refused_before_generation_and_before_the_quota_call(tmp_path):
    eng, gen, q = make(tmp_path, base="https://openrouter.ai/api/v1", model="anthropic/claude")
    with pytest.raises(Refused, match="egress refused"):
        await propose(eng)
    assert gen.calls == 0 and q.calls == 0


async def test_an_exhausted_quota_aborts_probing_without_generating(tmp_path):
    eng, gen, q = make(tmp_path, base="https://openrouter.ai/api/v1", model="anthropic/claude",
                       verdict=QuotaVerdict(False, "daily free-model requests exhausted"))
    with pytest.raises(Refused, match="quota pre-flight: daily free-model requests exhausted"):
        await propose(eng, zdr="off", classification="open")
    assert q.calls == 1 and gen.calls == 0


async def test_a_local_endpoint_skips_the_openrouter_quota_check(tmp_path):
    eng, gen, q = make(tmp_path)
    out = await propose(eng, n=3)
    assert gen.calls == 3 and q.calls == 0 and len(out["candidates"]) == 3


async def test_dry_run_calls_nothing_and_works_without_a_model(tmp_path):
    eng, gen, q = make(tmp_path, model=None)
    out = await propose(eng, dry_run=True)
    assert out["dry_run"] and "local" in out["egress"] and gen.calls == 0 and q.calls == 0


async def test_a_real_run_without_a_model_is_refused(tmp_path):
    eng, _, _ = make(tmp_path, model=None)
    with pytest.raises(Refused, match="no generator model"):
        await propose(eng)


@pytest.mark.parametrize("n", [0, 9])
async def test_candidate_count_is_bounded(tmp_path, n):
    eng, _, _ = make(tmp_path)
    with pytest.raises(Refused, match="n must be"):
        await propose(eng, n=n)


async def test_probe_without_a_sandbox_fails_closed(tmp_path):
    eng, _, _ = make(tmp_path)
    run_id = (await propose(eng))["run_id"]
    with pytest.raises(Refused, match="fail closed"):
        eng.probe(run_id=run_id, command=["pytest"], timeout_s=5, allow_worktree_diff=False)


async def test_diff_only_opt_in_executes_nothing_and_composes_no_winner(tmp_path):
    eng, _, _ = make(tmp_path)
    run_id = (await propose(eng))["run_id"]
    out = eng.probe(run_id=run_id, command=["pytest"], timeout_s=5, allow_worktree_diff=True)
    assert out["mode"] == "diff_only" and all(r["exit_code"] is None for r in out["results"])
    comp = eng.compose(run_id)
    assert comp["winner"] is None and comp["requires_human_approval"] is True


async def test_sandboxed_probe_and_compose_pick_the_passing_candidate(tmp_path):
    runner = FakeRunner({"improved 1": 1, "improved 2": 0})
    eng, _, _ = make(tmp_path, runner=runner)
    run_id = (await propose(eng))["run_id"]
    eng.probe(run_id=run_id, command=["pytest"], timeout_s=5, allow_worktree_diff=False)
    assert [list(s.files) for s in runner.seen] == [["prompt"], ["prompt"]]
    comp = eng.compose(run_id)
    assert comp["requires_human_approval"] is True
    assert comp["patch"] == {"component": "prompt", "replacement": "improved 2"}


async def test_compose_and_reflect_need_a_probe_first(tmp_path):
    eng, _, _ = make(tmp_path)
    run_id = (await propose(eng))["run_id"]
    for call in (lambda: eng.compose(run_id), lambda: eng.reflect(run_id=run_id, domain="python")):
        with pytest.raises(Refused, match="harness2_probe first"):
            call()


async def test_reflect_stages_a_pending_lesson_and_never_commits(tmp_path):
    eng, _, _ = make(tmp_path, runner=FakeRunner())
    run_id = (await propose(eng))["run_id"]
    eng.probe(run_id=run_id, command=["pytest"], timeout_s=5, allow_worktree_diff=False)
    out = eng.reflect(run_id=run_id, domain="python")
    assert out["committed"] is False and "experience approve" in out["next"]
    assert not (tmp_path / "python").exists()
    assert len(ExperienceStore(tmp_path).pending()) == 1


def test_an_unknown_run_is_refused(tmp_path):
    eng, _, _ = make(tmp_path)
    with pytest.raises(Refused, match="unknown run_id"):
        eng.compose("nope")


async def test_stored_runs_are_bounded(tmp_path):
    eng, _, _ = make(tmp_path)
    for _ in range(MAX_RUNS + 6):
        await propose(eng, n=1)
    assert len(eng.runs) == MAX_RUNS
