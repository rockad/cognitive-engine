"""The solver side of a k=1 run on the REAL rootless daemon, driven by a FAKE CLI (tests/fixtures/fake-cli `solve`).

No model is called and no real CLI runs. The "remote model endpoint" is a throwaway container on a throwaway
network. Each test builds a full rig: grant -> shared SweepBudget -> solver ProposerSessionRunner ->
CognitiveSubstrate.execute -> SumSignedAdapter -> the busybox verifier in a second container. These do not skip.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from harness2.substrate.opencode import OPENCODE_SURFACE

from cognitive_harness2 import channels, sandbox, session_runner as sr, sum_signed as ss, upstream_egress
from cognitive_harness2.substrate import CognitiveSubstrate, ExecutionGrant, ExecutionRefused, SolverExecution
from cognitive_harness2.sweep_budget import BudgetExhausted

pytestmark = pytest.mark.requires_rootless_docker

FIX = Path(__file__).resolve().parent / "fixtures"
CLI = str(FIX / "fake-cli")
UPSTREAM_HOST = "upstream.harness2.test"
TASK = "household/sum-1"
INST = ss.instance(TASK)
OK = upstream_egress.Decision(True, ("approved",))


def docker(verified, *args, timeout=60) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("DOCKER_")}
    return subprocess.run([verified.path, "-H", verified.host, *args], capture_output=True, text=True, timeout=timeout, env=env)


class _SecretsRecorder:
    """Stands in for `secrets` inside session_runner and notes every session run id it hands out."""

    def __init__(self, ids: list[str]):
        self.ids = ids

    def __getattr__(self, name):
        return getattr(secrets, name)

    def token_hex(self, n=None):
        v = secrets.token_hex(n)
        if n == 6:
            self.ids.append(v)
        return v


@pytest.fixture(autouse=True)
def nothing_left_behind(verified, monkeypatch):
    """Nothing of THIS test's sessions remains. (The daemon is shared with other agents' tests, so the check is
    scoped to the run ids this test's sessions were given, not to every labelled container.)"""
    ids: list[str] = []
    monkeypatch.setattr(sr, "secrets", _SecretsRecorder(ids))
    yield
    monkeypatch.undo()
    for rid in ids:
        mine = docker(verified, "ps", "-a", "-q", "--filter", f"label={sr.LABEL}={rid}").stdout.split()
        mine += docker(verified, "network", "ls", "-q", "--filter", f"label={sr.LABEL}={rid}").stdout.split()
        assert mine == [], f"session {rid} left {mine}"
        assert not [n for n in os.listdir(sr._runtime_base_dir()) if rid in n]


@pytest.fixture(scope="module")
def egress_world(verified, proposer_image, tmp_path_factory):
    """A throwaway egress network with one 'remote' endpoint on it: upstream.harness2.test:9000."""
    suffix = secrets.token_hex(4)
    net, up = f"h2t-{suffix}-egr", f"h2t-{suffix}-up"
    assert docker(verified, "network", "create", "--label", "cognitive-harness2.test=1", net).returncode == 0
    script = tmp_path_factory.mktemp("up") / "upstream.py"
    script.write_bytes((FIX / "upstream.py").read_bytes())
    script.chmod(0o644)
    script.parent.chmod(0o755)
    try:
        p = docker(verified, "run", "-d", "--name", up, "--label", "cognitive-harness2.test=1", "--network", net,
                   "--network-alias", UPSTREAM_HOST, "--user", "65534:65534", "--read-only", "--cap-drop", "ALL",
                   "--pull", "never", "--mount", f"type=bind,src={script},dst=/srv/upstream.py,readonly",
                   proposer_image.id, "python3", "-I", "/srv/upstream.py")
        assert p.returncode == 0, p.stderr
        subnet = docker(verified, "network", "inspect", "-f", "{{(index .IPAM.Config 0).Subnet}}", net).stdout.strip()
        deadline = time.monotonic() + 15
        while "listening" not in docker(verified, "logs", up).stdout and time.monotonic() < deadline:
            time.sleep(0.2)
        yield SimpleNamespace(network=net, subnet=subnet)
    finally:
        docker(verified, "rm", "-f", up)
        docker(verified, "network", "rm", net)


def channel() -> channels.Channel:
    text = textwrap.dedent(f"""
        [channels.t]
        kind = "opencode"
        provider = "openrouter"
        endpoint = "https://{UPSTREAM_HOST}:9000"
        model = "openrouter/test-model"
        retention = "zdr-attested"
        max_classification = "open"
        approval = "per-sweep"
        [channels.t.caps]
        sessions = 7
        wall_s = 1800
        cost_usd = 1
    """)
    return channels.parse_text(text, source="test").channels["t"]


def grant(**kw) -> ExecutionGrant:
    base = dict(channel="t", sweep_id="sweep-e2e", approved_by="tests", expires_at=time.time() + 3600,
                max_proposer_sessions=3, max_solver_sessions=4, max_wall_s=1800.0, per_session_s=120.0,
                classification="open")
    base.update(kw)
    return ExecutionGrant(**base)


class Rig:
    """grant -> budget -> solver runner -> substrate -> adapter, on a real daemon and a fake CLI."""

    def __init__(self, verified, image, world, tmp, *, g=None, tmp_exec=False, with_execution=True, budget=None):
        self.verified, self.image, self.world, self.tmp = verified, image, world, tmp
        self.grant = g or grant()
        self.budget = budget or self.grant.make_budget()
        self.wsroot, self.results = tmp / "wsroot", tmp / "results"
        self.ch = channel()
        self.runner = self.make_runner("solver")
        self.execution = SolverExecution(
            runner=self.runner, grant=self.grant, cli=CLI, results_root=self.results, workspace_root=self.wsroot,
            task_loader=ss.solver_task, tmp_exec=tmp_exec,
            argv_builder=lambda j: (j.cli, "solve", j.run_id.split("-")[0], j.workspace), env_builder=None)
        self.substrate = CognitiveSubstrate(OPENCODE_SURFACE, execution=self.execution if with_execution else None)
        spec = SimpleNamespace(name=ss.SPEC_NAME, install_mode="workspace_agents_md", obs_trunc=50_000)
        self.verifier = sandbox.DockerRunner(verified.path, docker_host=verified.host)
        self.adapter = ss.SumSignedAdapter(tmp / "root", spec, self.substrate, results_root=self.results, timeout=120,
                                           verifier=self.verifier)

    def make_runner(self, role: str) -> sr.ProposerSessionRunner:
        return sr.ProposerSessionRunner(
            self.verified, self.image, self.ch, zdr="strict", classification="open", role=role, budget=self.budget,
            proposer_decision=OK if role == "proposer" else None, workspace_roots=(str(self.wsroot),),
            egress_network=self.world.network, _test_allow_cidrs=[self.world.subnet])

    def run(self, mode: str, n: int = 1) -> Path:
        return self.adapter.execute(TASK, None, f"{mode}-{n}")

    def metrics(self, d: Path) -> dict:
        return json.loads((d / "metrics.json").read_text())


@pytest.fixture
def rig(verified, proposer_image, egress_world, tmp_path):
    return lambda **kw: Rig(verified, proposer_image, egress_world, tmp_path, **kw)


def both_scores(r: Rig, d: Path) -> dict:
    """The pure-code score and the container verifier's must agree."""
    pure, container = r.adapter.score(d, TASK), r.adapter.container_score(d, TASK)
    assert pure == container
    return pure


# ── a right, a wrong and a malformed answer, scored by both implementations ───────────────────

def test_a_right_answer_is_collected_scored_and_cleaned_up(rig):
    r = rig()
    d = r.run("right")
    assert both_scores(r, d) == {"n_passed": 3, "n_criteria": 3, "all_pass": True}
    assert (d / "output" / "answer.txt").read_text() == INST.expected_line + "\n"
    m = r.metrics(d)
    assert m["role"] == "solver" and m["exit_code"] == 0 and m["timed_out"] is False and m["delivered"] is True
    assert m["collected"] == ["output/answer.txt"] and m["n_events"] == 4 and m["harvest"]["ok"] is True
    assert m["reclaimed"] is True and m["egress"] == []
    assert json.loads((d / ".adapt_complete").read_text())["run_id"] == "right-1"
    assert r.adapter.delivered(d) and r.adapter.artifacts_intact(d)
    assert not list(r.wsroot.iterdir())                                   # the workspace is gone
    s = r.budget.snapshot()
    assert s["counts"] == {"proposer": 0, "solver": 1, "total": 1} and s["sessions"][0]["outcome"] == "ok"


def test_the_trajectory_renders_the_session_and_shows_the_harness_the_solver_saw(rig):
    r = rig()
    d = r.run("right")
    t = r.adapter.trajectory(d, obs_trunc=1000)
    assert "[ASSISTANT] starting (right)" in t
    assert "[ACTION] bash({\"command\": \"cat input.txt\"})" in t
    assert "[OBSERVATION] " + INST.input_text.strip() in t
    assert "AGENTS: You are a careful bookkeeper" in t                    # the base harness was rendered into AGENTS.md


def test_a_modified_harness_is_staged_rendered_and_released(rig, tmp_path):
    r = rig()
    h = tmp_path / "h"
    h.mkdir()
    (h / "systemprompt.md").write_text("Custom harness line one.\n")
    (h / "guardrails.md").write_text("")
    staged_before = set(r.substrate._staged)
    d = r.adapter.execute(TASK, h, "right-1")
    assert "AGENTS: Custom harness line one." in r.adapter.trajectory(d, obs_trunc=1000)
    assert json.loads((d / ".adapt_complete").read_text())["harness"] == ss.fingerprint(h)
    assert set(r.substrate._staged) == staged_before == set()            # the read-only staging dir was released


def test_a_wrong_answer_is_scored_wrong(rig):
    r = rig()
    d = r.run("wrong")
    got = both_scores(r, d)
    assert got == {"n_passed": 2, "n_criteria": 3, "all_pass": False}
    assert r.metrics(d)["delivered"] is True


def test_a_malformed_answer_is_scored_malformed(rig):
    r = rig()
    d = r.run("malformed")
    assert both_scores(r, d) == {"n_passed": 1, "n_criteria": 3, "all_pass": False}


def test_no_answer_and_an_answer_outside_output_are_both_no_answer(rig):
    r = rig()
    for mode in ("none", "outside"):
        d = r.run(mode)
        assert both_scores(r, d) == {"n_passed": 0, "n_criteria": 3, "all_pass": False}, mode
        assert r.metrics(d)["delivered"] is False and not r.adapter.delivered(d)
        assert not (d / "answer.txt").exists() and not (d / "output").exists() or not list((d / "output").glob("*"))


def test_the_solver_cannot_change_the_task_or_the_harness_text(rig):
    r = rig()
    d = r.run("tamper")
    t = r.adapter.trajectory(d, obs_trunc=2000)
    assert "overwrite input.txt: DENIED" in t and "overwrite AGENTS.md: DENIED" in t
    assert "rm input.txt: DENIED still-there" in t
    assert both_scores(r, d)["all_pass"] is True                          # and the input it read was the real one


def test_harvest_sanitisation_applies_to_the_solver_workspace(rig):
    r = rig()
    d = r.run("mess")
    m = r.metrics(d)
    assert m["collected"] == ["output/answer.txt"]
    rules = {f["rule"] for f in m["harvest"]["findings"]}
    assert {"symlink-escape", "special-file", "executable", "hardlink", "file-too-large", "bad-name",
            "protected-path"} <= rules          # (reclaim`s chown strips the setuid bit before harvest sees it)
    assert sorted(p.relative_to(d).as_posix() for p in d.rglob("*") if p.is_file() or p.is_symlink()) == sorted([
        ".adapt_complete", "metrics.json", "opencode_session.jsonl", "output/answer.txt"])
    assert both_scores(r, d)["all_pass"] is True
    assert not list(r.wsroot.iterdir())


def test_tmp_exec_defaults_to_off_and_can_be_turned_on(rig):
    off = rig()
    t = off.adapter.trajectory(off.run("tmpexec"), obs_trunc=1000)
    assert "exec_tmp=DENIED" in t and "exec_home=DENIED" in t
    on = rig(tmp_exec=True)
    t = on.adapter.trajectory(on.run("tmpexec", 2), obs_trunc=1000)
    assert "exec_tmp=ALLOWED" in t and "exec_home=ALLOWED" in t


# ── the budget ──────────────────────────────────────────────────────────────────────────────────

def test_budget_exhaustion_stops_further_sessions(rig, monkeypatch):
    r = rig(g=grant(max_solver_sessions=2, max_proposer_sessions=1))
    started = []
    real = sr.subprocess.Popen

    def counting(argv, *a, **k):
        if isinstance(argv, list) and "run" in argv and any(str(x).endswith("-session") for x in argv):
            started.append(argv[argv.index("--name") + 1])
        return real(argv, *a, **k)
    monkeypatch.setattr(sr.subprocess, "Popen", counting)
    r.run("right", 1)
    r.run("right", 2)
    with pytest.raises(RuntimeError, match="did not produce metrics.json.*session cap"):
        r.run("right", 3)
    assert len(started) == 2                                              # the third never reached docker
    assert not (r.results / "right-3" / "metrics.json").exists()
    assert not (r.results / "right-3" / ".adapt_complete").exists()
    assert r.budget.snapshot()["counts"]["solver"] == 2
    assert not list(r.wsroot.iterdir())


def test_the_budget_is_shared_between_proposer_and_solver_sessions(rig):
    r = rig(g=grant(max_proposer_sessions=1, max_solver_sessions=2))
    proposer = r.make_runner("proposer")
    ws = r.wsroot / "pws"
    ws.mkdir(parents=True)
    ws.chmod(0o777)
    spec = sr.SessionSpec(argv=("/opt/cli/fake-cli", "echo", "hi"), workspace=str(ws), cli=CLI, timeout_s=60)
    assert proposer.run(spec).role == "proposer"
    with pytest.raises(BudgetExhausted, match="proposer session cap"):
        proposer.run(spec)
    r.run("right", 1)
    r.run("right", 2)
    with pytest.raises(RuntimeError, match="session cap"):
        r.run("right", 3)
    assert r.budget.snapshot()["counts"] == {"proposer": 1, "solver": 2, "total": 3}
    shutil.rmtree(ws)


def test_a_spent_wall_clock_starts_nothing(rig, monkeypatch):
    r = rig(g=grant(max_wall_s=5.0, per_session_s=5.0))
    time.sleep(0)                                                        # (the budget's clock is real)
    monkeypatch.setattr(r.budget, "_clock", lambda: time.monotonic() + 100)
    with pytest.raises(RuntimeError, match="wall-clock budget"):
        r.run("right")
    assert r.budget.snapshot()["counts"]["total"] == 0


def test_a_hung_solver_is_killed_at_the_per_session_timeout_and_leaves_nothing(rig):
    r = rig(g=grant(per_session_s=6.0, max_wall_s=600.0))
    t0 = time.monotonic()
    d = r.adapter.execute(TASK, None, "hang-1", timeout=3600)            # asks for an hour; gets 6 seconds
    took = time.monotonic() - t0
    m = r.metrics(d)
    assert m["timed_out"] is True and m["exit_code"] is None and m["timeout_s"] == 6.0
    assert took < 6 + 40, took
    assert m["delivered"] is False and both_scores(r, d)["n_passed"] == 0
    assert r.budget.snapshot()["sessions"][0]["outcome"] == "timed_out"
    assert not list(r.wsroot.iterdir())                                   # (the autouse check covers the containers)
    assert "[ASSISTANT] starting (hang)" in r.adapter.trajectory(d, obs_trunc=500)   # events before the hang survive


def test_a_session_with_no_events_is_a_dead_chain_and_is_not_cached(rig):
    r = rig()
    with pytest.raises(RuntimeError, match="no parseable events"):
        r.run("noevents")
    d = r.results / "noevents-1"
    assert not (d / "metrics.json").exists() and not (d / ".adapt_complete").exists()


# ── no grant, nothing starts ───────────────────────────────────────────────────────────────────

@pytest.fixture
def docker_traps(monkeypatch):
    calls = []

    def trap(*a, **k):
        calls.append(a[:1])
        raise AssertionError("docker (or any process) was started")
    monkeypatch.setattr(subprocess, "run", trap)
    monkeypatch.setattr(subprocess, "Popen", trap)
    return calls


def assert_nothing_made(r: Rig, calls):
    assert calls == [] and not r.results.exists() and not r.wsroot.exists()


def test_no_wiring_means_no_docker_call(rig, docker_traps):
    r = rig(with_execution=False)
    with pytest.raises(ExecutionRefused, match="no ExecutionGrant"):
        r.run("right")
    assert_nothing_made(r, docker_traps)


def test_an_expired_grant_means_no_docker_call(rig, docker_traps):
    r = rig(g=grant(expires_at=time.time() - 1))
    with pytest.raises(ExecutionRefused, match="expired"):
        r.run("right")
    assert_nothing_made(r, docker_traps)


def test_a_grant_for_the_wrong_channel_means_no_docker_call(rig, docker_traps):
    r = rig(g=grant(channel="elsewhere"))
    with pytest.raises(ExecutionRefused, match="grant is for channel 'elsewhere'"):
        r.run("right")
    assert_nothing_made(r, docker_traps)


def test_a_missing_grant_object_means_no_docker_call(rig, docker_traps):
    r = rig()
    r.substrate._execution = SolverExecution(**{**r.execution.__dict__, "grant": None})
    with pytest.raises(ExecutionRefused, match="no ExecutionGrant"):
        r.run("right")
    assert_nothing_made(r, docker_traps)
