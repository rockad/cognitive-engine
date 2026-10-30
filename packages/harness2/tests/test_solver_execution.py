"""`ExecutionGrant`, `SolverExecution` and `CognitiveSubstrate.execute`, with no daemon.

The runner is REAL (its authorisation, budget and timeout clipping run); only the two methods that touch docker,
`_gate` and `_execute`, are replaced by a stand-in that writes into the workspace the way a session would. The
same flows run against the real rootless daemon, with a fake CLI, in test_solver_integration.py.
"""
from __future__ import annotations

import dataclasses
import json
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness2.substrate.opencode import OPENCODE_SURFACE

from cognitive_harness2 import channels, sandbox, session_runner as sr, sum_signed, workspace_harvest
from cognitive_harness2 import substrate as sub
from cognitive_harness2.image import ImageRef
from cognitive_harness2.substrate import (CognitiveSubstrate, ExecutionGrant, ExecutionRefused, SolverExecution,
                                          SolverJob, SolverTask, StagedInstall)
from cognitive_harness2.sweep_budget import BudgetExhausted, SweepBudget
from cognitive_harness2.sweep_plan import SweepPlan

SOCK = "unix:///run/user/1000/docker.sock"
V = sandbox.VerifiedDocker("/usr/bin/docker", SOCK)
IMG = ImageRef("cognitive-harness2-proposer:0123456789ab", "sha256:" + "5" * 64)
ALWAYS_ON = ("systemprompt.md", "guardrails.md", "LongTermMEMORY.md", "tool_descriptions/tool_guidance.md")
SURFACE = OPENCODE_SURFACE
TASK = "household/sum-1"
INST = sum_signed.instance(TASK)
GOOD = INST.expected_line + "\n"

EVENTS = "\n".join([
    json.dumps({"type": "text", "part": {"type": "text", "text": "adding"}}),
    json.dumps({"type": "tool_use", "part": {"type": "tool", "tool": "bash", "state": {
        "status": "completed", "input": {"command": "cat input.txt"}, "output": "1.00\n"}}}),
]) + "\n"


class Wall:
    """The wall clock the grant is checked against, moved by hand."""
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class Mono:
    def __init__(self):
        self.t = 50.0

    def __call__(self):
        return self.t


def grant(**kw) -> ExecutionGrant:
    base = dict(channel="local", sweep_id="sweep-1", approved_by="Aleksandr", expires_at=1_000_000.0 + 3600,
                max_proposer_sessions=3, max_solver_sessions=4, max_wall_s=2700.0, per_session_s=600.0,
                classification="restricted")
    base.update(kw)
    return ExecutionGrant(**base)


@pytest.fixture
def world(tmp_path):
    cli = tmp_path / "bin" / "fake-cli"
    cli.parent.mkdir()
    cli.write_text("#!/bin/sh\nexit 0\n")
    cli.chmod(0o755)
    tree = tmp_path / "harness"
    tree.mkdir()
    (tree / "systemprompt.md").write_text("Be careful.\n")
    (tree / "guardrails.md").write_text("Never guess.\n")
    (tree / "LongTermMEMORY.md").write_text("")
    (tree / "tool_descriptions").mkdir()
    (tree / "tool_descriptions" / "tool_guidance.md").write_text("Use bash.\n")
    (tree / "skills").mkdir()
    (tree / "skills" / "x.md").write_text("not rendered\n")
    return SimpleNamespace(tmp=tmp_path, cli=str(cli), tree=tree, wsroot=tmp_path / "wsroot",
                           results=tmp_path / "results", wall=Wall(), mono=Mono())


def make_runner(world, g, *, channel="local", classification="restricted", role="solver", budget="grant", zdr="strict"):
    ch = channels.parse_text(
        f'[channels.local]\nkind = "opencode"\nprovider = "ollama"\nendpoint = "http://127.0.0.1:11434"\n'
        f'model = "ollama/gemma4:12b"\nretention = "local"\nmax_classification = "restricted"\napproval = "per-sweep"\n'
        f'[channels.local.caps]\nsessions = 7\nwall_s = 2700\ncost_usd = 1\n'
        f'[channels.other]\nkind = "opencode"\nprovider = "ollama"\nendpoint = "http://127.0.0.1:11435"\n'
        f'model = "ollama/x"\nretention = "local"\nmax_classification = "restricted"\napproval = "per-sweep"\n'
        f'[channels.other.caps]\nsessions = 7\nwall_s = 2700\ncost_usd = 1\n', source="t").channels[channel]
    b = g.make_budget(clock=world.mono) if budget == "grant" else budget
    return sr.ProposerSessionRunner(V, IMG, ch, zdr=zdr, classification=classification, role=role, budget=b,
                                    workspace_roots=(str(world.wsroot),))


class Session:
    """What the fake `_execute` does for one session, and what it saw."""

    def __init__(self, write=None, stdout=EVENTS, **result_kw):
        self.write, self.stdout, self.result_kw = write, stdout, result_kw
        self.plans: list = []
        self.seen: list[dict] = []

    def __call__(self, plan):
        spec = plan.spec
        ws = Path(spec.workspace)
        self.plans.append(plan)
        self.seen.append({"entries": sorted(p.relative_to(ws).as_posix() for p in ws.rglob("*")),
                          "agents": (ws / "AGENTS.md").read_text() if (ws / "AGENTS.md").exists() else None,
                          "input": (ws / "input.txt").read_text() if (ws / "input.txt").exists() else None,
                          "ws_mode": stat.S_IMODE(os.stat(ws).st_mode),
                          "out_mode": stat.S_IMODE(os.stat(ws / "output").st_mode),
                          "ws": str(ws)})
        if self.write:
            self.write(ws)
        harvest = workspace_harvest.inspect_workspace(str(ws), spec.harvest) if spec.harvest else None
        kw = dict(exit_code=0, stderr="", timed_out=False, truncated=False, duration_s=1.5, egress_log=())
        kw.update(self.result_kw)
        return sr.SessionResult("sess123", stdout=self.stdout, harvest=harvest, reclaimed=True, role="solver", **kw)


def make_substrate(world, monkeypatch, session, *, g="default", runner_kw=None, loader=sum_signed.solver_task, **ex_kw):
    g = grant() if g == "default" else g
    runner = make_runner(world, g, **(runner_kw or {})) if g is not None else make_runner(world, grant(), **(runner_kw or {}))
    monkeypatch.setattr(runner, "_gate", lambda: None)
    monkeypatch.setattr(runner, "_execute", session)
    ex = SolverExecution(runner=runner, grant=g, cli=world.cli, results_root=world.results, workspace_root=world.wsroot,
                         task_loader=loader, clock=world.wall, argv_builder=lambda j: (j.cli, "solve", j.run_id),
                         env_builder=None, **ex_kw)
    return CognitiveSubstrate(SURFACE, execution=ex), ex, runner


def install(world):
    return StagedInstall(world.tree, "fp1", {"harness": "base"})


# ── the grant ───────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("kw", [
    dict(channel=""), dict(channel=3), dict(sweep_id=""), dict(sweep_id="has space"), dict(sweep_id="../x"),
    dict(sweep_id="x" * 200), dict(approved_by=""), dict(approved_by="  "), dict(approved_by="a\nb"),
    dict(expires_at=0), dict(expires_at=float("nan")), dict(expires_at=float("inf")), dict(expires_at=True),
    dict(max_proposer_sessions=0), dict(max_solver_sessions=-1), dict(max_solver_sessions=1.5),
    dict(max_wall_s=0), dict(max_wall_s=float("inf")), dict(per_session_s=-1), dict(per_session_s=5000.0),
    dict(classification="secret"),
])
def test_a_malformed_grant_cannot_be_built(kw):
    with pytest.raises(ValueError, match="invalid ExecutionGrant"):
        grant(**kw)


def test_grant_properties_and_budget():
    g = grant()
    assert g.max_sessions == 7 and g.expired(g.expires_at) and g.expired(g.expires_at + 1) and not g.expired(1.0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        g.channel = "x"  # type: ignore[misc]
    b = g.make_budget(clock=Mono())
    s = b.snapshot()
    assert (s["total_wall_s"], s["per_session_s"], s["max_sessions"], s["role_caps"]) == \
        (2700.0, 600.0, 7, {"proposer": 3, "solver": 4})
    d = g.to_dict()
    assert json.dumps(d) and d["sweep_id"] == "sweep-1" and "approved_by" in d


def test_a_grant_comes_from_an_approved_plan():
    plan = SweepPlan(channel="local", classification="restricted", k=1, n_tasks=1, planned_sessions=3,
                     planned_rollouts=4, max_sessions=3, max_wall_seconds=2700)
    g = ExecutionGrant.from_plan(plan, sweep_id="s1", approved_by="me", ttl_s=900, now=100.0)
    assert (g.channel, g.classification, g.max_proposer_sessions, g.max_solver_sessions, g.max_wall_s) == \
        ("local", "restricted", 3, 4, 2700) and g.expires_at == 1000.0 and g.per_session_s == 600.0
    for bad in (0, -1, float("nan"), 10**9):
        with pytest.raises(ValueError):
            ExecutionGrant.from_plan(plan, sweep_id="s1", approved_by="me", ttl_s=bad)
    with pytest.raises(ValueError, match="requires human approval"):
        ExecutionGrant.from_plan(SimpleNamespace(requires_human_approval=False), sweep_id="s", approved_by="m", ttl_s=1)
    with pytest.raises(ValueError):
        ExecutionGrant.from_plan(dataclasses.replace(plan, planned_rollouts=0), sweep_id="s", approved_by="me", ttl_s=60)


# ── no grant, no execution ──────────────────────────────────────────────────────────

class Untouched:
    """Asserts a refusal created nothing: no process, no directory."""

    def __init__(self, world, monkeypatch):
        self.world = world

        def trap(*a, **k):
            raise AssertionError("a process was started")
        monkeypatch.setattr(subprocess, "run", trap)
        monkeypatch.setattr(subprocess, "Popen", trap)

    def check(self):
        assert not self.world.results.exists() and not self.world.wsroot.exists()


def refuse(world, monkeypatch, sub_, match):
    t = Untouched(world, monkeypatch)
    with pytest.raises(ExecutionRefused, match=match):
        sub_.execute(TASK, install(world), ["r1"], 60)
    t.check()


def test_a_substrate_with_no_wiring_refuses(world, monkeypatch):
    refuse(world, monkeypatch, CognitiveSubstrate(SURFACE), "no ExecutionGrant")


def test_a_wiring_without_a_grant_refuses(world, monkeypatch):
    s, _, _ = make_substrate(world, monkeypatch, Session(), g=None)
    s._execution = dataclasses.replace(s._execution, grant=None)
    refuse(world, monkeypatch, s, "no ExecutionGrant")


def test_an_expired_grant_refuses(world, monkeypatch):
    s, ex, _ = make_substrate(world, monkeypatch, Session())
    world.wall.t = ex.grant.expires_at
    refuse(world, monkeypatch, s, "expired")
    world.wall.t = ex.grant.expires_at + 99
    refuse(world, monkeypatch, s, "expired")


def test_a_grant_for_another_channel_refuses(world, monkeypatch):
    s, _, _ = make_substrate(world, monkeypatch, Session(), g=grant(channel="other"))
    refuse(world, monkeypatch, s, "grant is for channel 'other'")


def test_a_grant_for_another_classification_refuses(world, monkeypatch):
    s, _, _ = make_substrate(world, monkeypatch, Session(), g=grant(classification="open"))
    refuse(world, monkeypatch, s, "different classification")


def test_a_proposer_runner_or_an_unbudgeted_runner_refuses(world, monkeypatch):
    s, _, _ = make_substrate(world, monkeypatch, Session(), runner_kw=dict(role="proposer"))
    refuse(world, monkeypatch, s, "not a solver runner")
    s, _, _ = make_substrate(world, monkeypatch, Session(), runner_kw=dict(budget=None))
    refuse(world, monkeypatch, s, "no SweepBudget")


@pytest.mark.parametrize("kw", [dict(total=3600.0, per=600.0, n=7), dict(total=2700.0, per=900.0, n=7),
                                dict(total=2700.0, per=600.0, n=8), dict(total=2700.0, per=600.0, n=7, caps=(3, 5)),
                                dict(total=2700.0, per=600.0, n=7, caps=(4, 4))])
def test_a_budget_larger_than_the_grant_refuses(world, monkeypatch, kw):
    caps = kw.pop("caps", None)
    b = SweepBudget(kw["total"], kw["per"], kw["n"], clock=world.mono,
                    role_caps=None if caps is None else {"proposer": caps[0], "solver": caps[1]})
    s, _, _ = make_substrate(world, monkeypatch, Session(), runner_kw=dict(budget=b))
    refuse(world, monkeypatch, s, "larger than the grant")


def test_a_smaller_budget_is_fine(world, monkeypatch):
    b = SweepBudget(100.0, 50.0, 2, clock=world.mono)
    s, _, _ = make_substrate(world, monkeypatch, Session(), runner_kw=dict(budget=b))
    (d,) = s.execute(TASK, install(world), ["r1"], 60)
    assert (d / "metrics.json").is_file()


def test_the_grant_is_rechecked_between_run_ids(world, monkeypatch):
    sess = Session()
    orig = sess.__call__

    def advancing(plan):
        out = orig(plan)
        world.wall.t += 4000                 # the grant lapses while the first session runs
        return out
    s, ex, _ = make_substrate(world, monkeypatch, advancing)
    with pytest.raises(ExecutionRefused, match="expired"):
        s.execute(TASK, install(world), ["r1", "r2"], 60)
    assert (world.results / "r1" / "metrics.json").is_file() and not (world.results / "r2").exists()


def test_the_wiring_is_type_checked(world, monkeypatch):
    s, ex, runner = make_substrate(world, monkeypatch, Session())
    with pytest.raises(TypeError):
        dataclasses.replace(ex, runner=object())
    with pytest.raises(TypeError):
        dataclasses.replace(ex, grant="yes")
    with pytest.raises(ValueError):
        dataclasses.replace(ex, results_root=Path("relative"))
    with pytest.raises(TypeError):
        dataclasses.replace(ex, task_loader=3)


# ── a granted run ──────────────────────────────────────────────────────────────────

def test_a_granted_run_builds_the_workspace_runs_the_session_and_records_everything(world, monkeypatch):
    def write(ws):
        (ws / "output" / "answer.txt").write_text(GOOD)
        (ws / "notes.txt").write_text("outside output: never collected\n")

    sess = Session(write=write, stderr="warn")
    s, ex, runner = make_substrate(world, monkeypatch, sess)
    (d,) = s.execute(TASK, install(world), ["run-1"], 300)
    assert d == world.results / "run-1"
    seen = sess.seen[0]
    # the workspace the session saw
    assert seen["entries"] == ["AGENTS.md", "input.txt", "output"]
    assert seen["agents"] == "Be careful.\n\n---\n\nNever guess.\n\n---\n\nUse bash.\n"      # always-on files only
    assert seen["input"] == INST.input_text
    assert seen["ws_mode"] == 0o1777 and seen["out_mode"] == 0o1777          # sticky: the solver cannot unlink the inputs
    # the session it was given
    spec = sess.plans[0].spec
    assert spec.argv == ("/opt/cli/fake-cli", "solve", "run-1") and spec.timeout_s == 300
    assert spec.tmp_exec is False and spec.credential is None and spec.workspace == seen["ws"]
    # what was left
    assert (d / "opencode_session.jsonl").read_text() == EVENTS
    assert (d / "output" / "answer.txt").read_text() == GOOD
    assert not (d / "notes.txt").exists() and not (d / "output" / "notes.txt").exists()
    m = json.loads((d / "metrics.json").read_text())
    assert m["run_id"] == "run-1" and m["task_ref"] == TASK and m["role"] == "solver" and m["sweep_id"] == "sweep-1"
    assert m["channel"] == "local" and m["model"] == "ollama/gemma4:12b" and m["n_events"] == 2
    assert m["delivered"] is True and m["collected"] == ["output/answer.txt"] and m["timeout_s"] == 300
    assert m["exit_code"] == 0 and m["timed_out"] is False and m["install_fingerprint"] == "fp1"
    assert m["harvest"]["ok"] is True and m["stderr_tail"] == "warn"
    assert not (d / "error.json").exists()
    # the workspace is gone; nothing else was made
    assert not Path(seen["ws"]).exists() and list(world.wsroot.iterdir()) == []
    # the budget saw a solver session
    snap = runner.budget.snapshot()
    assert snap["counts"] == {"proposer": 0, "solver": 1, "total": 1} and snap["sessions"][0]["outcome"] == "ok"
    # the adapter marks it complete, and the verdict is pure code over what was collected
    assert sum_signed.grade_answer((d / "output" / "answer.txt").read_bytes(), INST.expected_total)["all_pass"]


def test_the_timeout_is_clipped_to_the_grant_and_the_budget(world, monkeypatch):
    sess = Session()
    s, _, _ = make_substrate(world, monkeypatch, sess)
    s.execute(TASK, install(world), ["r1"], 3600)
    assert sess.plans[0].spec.timeout_s == 600.0


def test_several_run_ids_run_in_order_and_share_the_budget(world, monkeypatch):
    sess = Session()
    s, _, runner = make_substrate(world, monkeypatch, sess)
    dirs = s.execute(TASK, install(world), ["a", "b", "c"], 60)
    assert [d.name for d in dirs] == ["a", "b", "c"] and runner.budget.snapshot()["counts"]["solver"] == 3
    assert [p.spec.argv[2] for p in sess.plans] == ["a", "b", "c"]


def test_budget_exhaustion_stops_further_sessions_and_says_why(world, monkeypatch):
    sess = Session()
    b = SweepBudget(2700.0, 600.0, 2, clock=world.mono, role_caps={"solver": 2})
    s, _, _ = make_substrate(world, monkeypatch, sess, runner_kw=dict(budget=b))
    dirs = s.execute(TASK, install(world), ["a", "b", "c", "d"], 60)
    assert [d.name for d in dirs] == ["a", "b", "c", "d"] and len(sess.plans) == 2
    assert (dirs[0] / "metrics.json").is_file() and (dirs[1] / "metrics.json").is_file()
    for d in dirs[2:]:
        assert not (d / "metrics.json").exists()
        assert "session cap (2)" in json.loads((d / "error.json").read_text())["error"]
    assert list(world.wsroot.iterdir()) == []


def test_a_spent_deadline_starts_no_session(world, monkeypatch):
    sess = Session()
    s, _, runner = make_substrate(world, monkeypatch, sess)
    world.mono.t += 99999
    (d,) = s.execute(TASK, install(world), ["a"], 60)
    assert sess.plans == [] and "wall-clock budget" in json.loads((d / "error.json").read_text())["error"]


def test_a_session_with_no_parseable_events_is_not_recorded_as_a_run(world, monkeypatch):
    for stdout in ("", "just text\n", "[1,2]\n{broken\n"):
        sess = Session(write=lambda ws: (ws / "output" / "answer.txt").write_text(GOOD), stdout=stdout, exit_code=1)
        s, _, _ = make_substrate(world, monkeypatch, sess)
        (d,) = s.execute(TASK, install(world), [f"dead-{abs(hash(stdout))}"], 60)
        assert not (d / "metrics.json").exists()
        assert "no parseable events" in json.loads((d / "error.json").read_text())["error"]


def test_a_timed_out_run_is_recorded_with_what_it_left(world, monkeypatch):
    sess = Session(write=lambda ws: (ws / "output" / "answer.txt").write_text(GOOD), timed_out=True, exit_code=None)
    s, _, runner = make_substrate(world, monkeypatch, sess)
    (d,) = s.execute(TASK, install(world), ["r1"], 60)
    m = json.loads((d / "metrics.json").read_text())
    assert m["timed_out"] is True and m["exit_code"] is None and m["delivered"] is True
    assert runner.budget.snapshot()["sessions"][0]["outcome"] == "timed_out"


def test_harvest_sanitisation_applies_to_the_solver_workspace(world, monkeypatch):
    def write(ws):
        out = ws / "output"
        (out / "answer.txt").write_text(GOOD)
        (out / "extra.md").write_text("fine\n")
        os.symlink("/etc/passwd", out / "passwd")                    # a link out
        os.symlink("extra.md", out / "inner")                        # a link that stays inside: still not copied
        (out / "run.sh").write_text("#!/bin/sh\n")
        (out / "run.sh").chmod(0o755)                                # executable
        (out / "su").write_text("x")
        (out / "su").chmod(0o4755)                                   # setuid
        (out / "h1").write_text("h")
        os.link(out / "h1", out / "h2")                              # hard links
        (out / "big.txt").write_bytes(b"x" * 600_000)                # over the per-file cap
        (out / ("esc" + "\x1b[2J" + "name")).write_text("x")         # terminal escape in the name
        (out / "sub").mkdir()
        (out / "sub" / "n.md").write_text("nested\n")
        os.mkfifo(out / "pipe")
        (ws / ".git").mkdir()
        (ws / ".git" / "config").write_text("[x]\n")
        (ws / "answer.txt").write_text(GOOD)                         # outside output/

    sess = Session(write=write)
    s, _, _ = make_substrate(world, monkeypatch, sess)
    (d,) = s.execute(TASK, install(world), ["r1"], 60)
    m = json.loads((d / "metrics.json").read_text())
    assert sorted(m["collected"]) == ["output/answer.txt", "output/extra.md", "output/sub/n.md"]
    got = sorted(p.relative_to(d).as_posix() for p in (d / "output").rglob("*") if p.is_file())
    assert got == ["output/answer.txt", "output/extra.md", "output/sub/n.md"]
    assert not any(p.is_symlink() for p in d.rglob("*"))
    rules = {f["rule"] for f in m["harvest"]["findings"]}
    assert {"symlink-escape", "executable", "suid-bit", "hardlink", "file-too-large", "bad-name", "special-file",
            "protected-path"} <= rules
    assert m["harvest"]["ok"] is False
    assert not (d / "answer.txt").exists()
    for f in m["harvest"]["findings"]:
        assert "\x1b" not in f["path"]


def test_a_credential_written_by_the_model_is_not_collected(world, monkeypatch):
    secret = "SENTINEL-solver-credential-1f2e3d"
    cred = sr.Credential("/home/agent/.tok", secret)

    def write(ws):
        (ws / "output" / "answer.txt").write_text(f"Total: 1.00 {secret}\n")
        (ws / "output" / "ok.txt").write_text("fine\n")

    class WithCred(Session):
        def __call__(self, plan):
            self.plans.append(plan)
            ws = Path(plan.spec.workspace)
            write(ws)
            harvest = workspace_harvest.inspect_workspace(str(ws), plan.spec.harvest, credentials=[secret])
            return sr.SessionResult("s", 0, EVENTS, "", False, False, 1.0, (), harvest, True, role="solver")
    s, _, _ = make_substrate(world, monkeypatch, WithCred(), credential=cred)
    (d,) = s.execute(TASK, install(world), ["r1"], 60)
    m = json.loads((d / "metrics.json").read_text())
    assert m["collected"] == ["output/ok.txt"] and "credential-in-file" in {f["rule"] for f in m["harvest"]["findings"]}
    assert secret not in "".join(p.read_text() for p in d.rglob("*") if p.is_file())


def test_a_leftover_from_the_session_is_never_swallowed(world, monkeypatch):
    class Leaky(Session):
        def __call__(self, plan):
            super().__call__(plan)
            raise sr.SessionCleanupError(["container deadbeef"])
    s, _, runner = make_substrate(world, monkeypatch, Leaky())
    with pytest.raises(sr.SessionCleanupError, match="container deadbeef"):
        s.execute(TASK, install(world), ["r1", "r2"], 60)
    assert "container deadbeef" in json.loads((world.results / "r1" / "error.json").read_text())["error"]
    assert not (world.results / "r2").exists()
    assert list(world.wsroot.iterdir()) == []
    assert runner.budget.snapshot()["sessions"][0]["outcome"] == "leftovers"


def test_a_session_error_is_recorded_for_the_run_not_raised(world, monkeypatch):
    class Boom(Session):
        def __call__(self, plan):
            super().__call__(plan)
            raise sr.SessionError("docker network create failed: boom")
    s, _, runner = make_substrate(world, monkeypatch, Boom())
    (d,) = s.execute(TASK, install(world), ["r1"], 60)
    assert "boom" in json.loads((d / "error.json").read_text())["error"] and not (d / "metrics.json").exists()
    assert list(world.wsroot.iterdir()) == [] and runner.budget.snapshot()["sessions"][0]["outcome"] == "error"


def test_a_refused_session_gives_its_slot_back_and_is_recorded(world, monkeypatch):
    s, ex, runner = make_substrate(world, monkeypatch, Session())
    s._execution = dataclasses.replace(ex, argv_builder=lambda j: ("/bin/sh", "-c", "id"))      # argv[0] is not the CLI
    (d,) = s.execute(TASK, install(world), ["r1"], 60)
    assert "argv[0] must be the mounted CLI path" in json.loads((d / "error.json").read_text())["error"]
    assert runner.budget.snapshot()["counts"]["total"] == 0 and list(world.wsroot.iterdir()) == []


def test_a_workspace_that_cannot_be_removed_is_a_loud_failure(world, monkeypatch):
    s, _, _ = make_substrate(world, monkeypatch, Session())
    monkeypatch.setattr(sub.shutil, "rmtree", lambda *a, **k: None)
    with pytest.raises(sr.SessionCleanupError, match="directory"):
        s.execute(TASK, install(world), ["r1"], 60)
    assert "could not be removed" in json.loads((world.results / "r1" / "error.json").read_text())["error"]


def test_inputs_are_validated_before_anything_is_made(world, monkeypatch):
    s, _, _ = make_substrate(world, monkeypatch, Session())
    t = Untouched(world, monkeypatch)
    for bad_ids in ([], ["a", "a"], ["../x"], ["a/b"], [""], [3], "run", ["x" * 200], [".hidden"]):
        with pytest.raises(ValueError, match="run_ids"):
            s.execute(TASK, install(world), bad_ids, 60)
    for bad_t in (0, -1, float("nan"), None, "60", True):
        with pytest.raises(ValueError, match="timeout"):
            s.execute(TASK, install(world), ["a"], bad_t)
    with pytest.raises(ValueError, match="unknown sum-signed task"):
        s.execute("nope/sum-1", install(world), ["a"], 60)
    with pytest.raises(ValueError, match="staging_dir"):
        s.execute(TASK, object(), ["a"], 60)
    with pytest.raises(ValueError):
        s.execute(TASK, StagedInstall(world.tmp / "not-a-dir", "f", {}), ["a"], 60)
    t.check()


def test_an_existing_run_directory_is_never_overwritten(world, monkeypatch):
    s, _, _ = make_substrate(world, monkeypatch, Session())
    (world.results / "r1").mkdir(parents=True)
    (world.results / "r1" / "old.txt").write_text("keep")
    with pytest.raises(ValueError, match="already holds files"):
        s.execute(TASK, install(world), ["r1"], 60)
    assert (world.results / "r1" / "old.txt").read_text() == "keep"


def test_a_harness_with_a_symlink_is_refused_not_followed(world, monkeypatch):
    os.symlink("/etc/passwd", world.tree / "evil")
    s, _, _ = make_substrate(world, monkeypatch, Session())
    with pytest.raises(ValueError, match="symlink"):
        s.execute(TASK, install(world), ["r1"], 60)


# ── the task and the helpers ───────────────────────────────────────────────────────

def test_a_solver_task_is_validated():
    SolverTask("do it", {"input.txt": "1\n", "data/x.csv": "a,b\n"})
    for bad in (dict(prompt=""), dict(prompt="  "), dict(prompt="a\0b"), dict(prompt="x" * 70000),
                dict(prompt="p", files={"AGENTS.md": "x"}), dict(prompt="p", files={"output/a": "x"}),
                dict(prompt="p", files={"../x": "x"}), dict(prompt="p", files={"/abs": "x"}),
                dict(prompt="p", files={"a": 3}), dict(prompt="p", files={"a": "x" * (2 << 20)}),
                dict(prompt="p", files={".git/config": "x"}), dict(prompt="p", files=[1]),
                dict(prompt="p", files={f"f{i}": "x" for i in range(70)})):
        with pytest.raises(ValueError):
            SolverTask(**bad)


def test_render_agents_md_follows_the_surface_order_and_skips_blanks():
    files = {"guardrails.md": "G\n", "systemprompt.md": "S\n", "LongTermMEMORY.md": "  \n", "skills/a.md": "no"}
    assert sub.render_agents_md(files, ALWAYS_ON) == "S\n\n---\n\nG\n"
    assert sub.render_agents_md({}, ALWAYS_ON) == ""


def test_the_default_opencode_argv_and_env():
    job = SolverJob("t", "r", "the prompt", "/w/ws", "ollama/m", "/opt/cli/opencode")
    assert sub.opencode_argv(job) == ("/opt/cli/opencode", "run", "the prompt", "--dir", "/w/ws", "--model",
                                      "ollama/m", "--title", "adapt", "--format", "json")
    env = sub.opencode_env(job)
    assert json.loads(env["OPENCODE_PERMISSION"]) == {"external_directory": {"*": "deny", "/w/ws": "allow",
                                                                              "/w/ws/**": "allow"}}
    from cognitive_harness2 import upstream_egress
    assert not any(upstream_egress._is_secret_name(k) for k in env)         # the runner would refuse otherwise


def test_the_default_model_is_the_channels(world, monkeypatch):
    s, ex, runner = make_substrate(world, monkeypatch, Session())
    assert ex.model_name == "ollama/gemma4:12b"
    assert dataclasses.replace(ex, model="x/y").model_name == "x/y"


def test_the_budgeted_runner_clips_even_a_long_request_to_the_remaining_wall(world, monkeypatch):
    sess = Session()
    s, _, _ = make_substrate(world, monkeypatch, sess)
    world.mono.t += 2400                                       # 300s of the sweep are left
    s.execute(TASK, install(world), ["r1"], 3600)
    assert sess.plans[0].spec.timeout_s == pytest.approx(300.0)


# ── the model gate around a solver session ─────────────────────────────────────────

def test_a_solver_session_holds_the_model_gate_and_a_busy_gate_leaves_error_json(world, monkeypatch):
    from cognitive_harness2.model_gate import ModelGate
    gate = ModelGate()
    held = []

    def write(ws):
        held.append(gate.locked())
        (ws / "output" / "answer.txt").write_text(GOOD)
    s = Session(write=write)
    substrate, ex, _ = make_substrate(world, monkeypatch, s, gate=gate, gate_wait_s=0.2)
    d = substrate.execute(TASK, install(world), ["g1"], 60)[0]
    assert held == [True] and not gate.locked() and (d / "metrics.json").is_file()
    with gate.hold(5, label="someone else"):
        d2 = substrate.execute(TASK, install(world), ["g2"], 60)[0]
    assert not (d2 / "metrics.json").exists() and "gate" in json.loads((d2 / "error.json").read_text())["error"].lower()
    assert len(s.plans) == 1 and not gate.locked()


def test_without_a_gate_nothing_changes(world, monkeypatch):
    s = Session(write=lambda ws: (ws / "output" / "answer.txt").write_text(GOOD))
    substrate, ex, _ = make_substrate(world, monkeypatch, s)
    assert ex.gate is None
    assert (substrate.execute(TASK, install(world), ["n1"], 60)[0] / "metrics.json").is_file()


def test_metrics_carry_the_error_events_and_the_proxy_facts_of_a_session_that_failed(world, monkeypatch):
    """The first real stage-1 run left `rc=1` and a bare egress line; the cause (an `error` event) and the number of
    requests that reached the model are now in the metrics."""
    stdout = '{"type":"error","timestamp":1,"sessionID":"","error":{"type":"unknown","message":"UnexpectedStatus: 403"}}\n'
    log = ("ACCESS decision=deny host=127.0.0.1 port=34175 reason=not-allow-listed",)
    sess = Session(stdout=stdout, exit_code=1, egress_log=log)
    s, _, _ = make_substrate(world, monkeypatch, sess)
    (d,) = s.execute(TASK, install(world), ["bad"], 60)
    m = json.loads((d / "metrics.json").read_text())
    assert m["errors"] == ["UnexpectedStatus: 403"] and m["exit_code"] == 1 and m["delivered"] is False
    assert m["model_requests"] == 0 and m["denied_hosts"] == ["127.0.0.1:34175"] and m["egress"] == list(log)
    ok = Session(write=lambda ws: (ws / "output" / "answer.txt").write_text(GOOD),
                 egress_log=("ACCESS decision=allow host=harness2-proxy.internal port=1 reason=unix-route",) * 2)
    s2, _, _ = make_substrate(world, monkeypatch, ok)
    (d2,) = s2.execute(TASK, install(world), ["good"], 60)
    m2 = json.loads((d2 / "metrics.json").read_text())
    assert m2["errors"] == [] and m2["model_requests"] == 2 and m2["denied_hosts"] == []
