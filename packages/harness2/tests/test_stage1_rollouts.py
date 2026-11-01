"""Stage 1c: `--rollouts N`, the session arithmetic, the profile cap, and the pass-rate report.

Upstream fixes R to 4k and rolls exactly one base per k (`schedule.run_task`: `labels = [base1..basek]`; `recursion.run_parallel`
adds inv, dom and agg: one rollout each at k=1). Nothing in `run_task` takes a rollout count, so the extra rollouts are made by
OUR runner, around upstream and after it, with `adapter.execute`. These tests hold the arithmetic, the refusals and the honest
reporting. No Docker and no model: the pure parts here, the end-to-end ones in test_stage1_hard.py (under the docker lock).
"""
from __future__ import annotations

import dataclasses
import io
import json
import shlex
import shutil
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest
from h2_upstream import isolated_upstream

from cognitive_harness2 import channels, sandbox, stage1
from cognitive_harness2.image import ImageRef
from cognitive_harness2.stage1 import (Approval, FakeOllama, Probes, Stage1Error, build_parser, run_plan, run_run)
from cognitive_harness2.substrate import ExecutionRefused
from cognitive_harness2.sweep_plan import SweepRefused

REPO = Path(__file__).resolve().parents[1]
EXAMPLE = REPO / "examples" / "harness2-channels.stage1.toml"
FAKE_SH = REPO / "src" / "cognitive_harness2" / "selftest_fake_opencode.sh"
VERIFIED = sandbox.VerifiedDocker("/usr/bin/docker", "unix:///run/user/1000/docker.sock")
IMAGE = ImageRef("cognitive-harness2-proposer:0123456789ab", "sha256:" + "5" * 64)


@pytest.fixture(scope="module")
def up(tmp_path_factory):
    with isolated_upstream(tmp_path_factory.mktemp("runs")) as u:
        yield u


class Rig:
    def __init__(self, tmp: Path, fake: FakeOllama, *, sessions: int = 7, wall_s: int = 2700):
        self.tmp, self.fake = tmp, fake
        self.ws = tmp / "workspaces"
        self.ws.mkdir()
        self.runs = tmp / "runs"
        (tmp / "bin").mkdir()
        self.cli = tmp / "bin" / "opencode"
        shutil.copyfile(FAKE_SH, self.cli)
        self.cli.chmod(0o755)
        text = (EXAMPLE.read_text().replace("/home/rockad/h2-workspaces", str(self.ws))
                .replace("127.0.0.1:11434", f"127.0.0.1:{fake.port}")
                .replace("sessions = 7 ", f"sessions = {sessions} ").replace("wall_s = 2700 ", f"wall_s = {wall_s} "))
        assert f"sessions = {sessions} " in text and f"wall_s = {wall_s} " in text
        self.profile = tmp / "channels.toml"
        self.profile.write_text(text)

    def probes(self) -> Probes:
        return Probes(gate=lambda: VERIFIED, resolve=lambda v: IMAGE, ollama=stage1.probe_ollama, environ={}, which=lambda n: None)

    def parse(self, cmd: str, *extra: str):
        return build_parser().parse_args([cmd, "--profile", str(self.profile), "--opencode", str(self.cli),
                                          "--runs-dir", str(self.runs), *extra])

    def settings(self, **kw) -> stage1.Settings:
        return stage1.Settings(profile=channels.load_profile({stage1.ENV_CHANNELS: str(self.profile)}), cli=str(self.cli),
                               runs_dir=self.runs, **kw)


@pytest.fixture
def rig(tmp_path):
    with FakeOllama() as fake:
        yield Rig(tmp_path, fake)


def rig_with(tmp_path, **kw):
    fake = FakeOllama()
    fake.__enter__()
    return Rig(tmp_path, fake, **kw), fake


def plan_out(rig, *extra: str) -> tuple[int, str]:
    out = io.StringIO()
    code = run_plan(rig.parse("plan", *extra), environ={}, probes=rig.probes(), out=out)
    return code, out.getvalue()


# ══ the arithmetic ════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("n,rollouts,sessions", [(1, 4, 7), (2, 6, 9), (4, 10, 13), (8, 18, 21)])
def test_the_session_arithmetic_is_p_plus_upstreams_r_plus_two_n_minus_one_extras(n, rollouts, sessions):
    rp = stage1.rollout_plan(n, upstream_p=3, upstream_r=4)
    assert (rp.proposer, rp.upstream_rollouts, rp.extra_base, rp.extra_final) == (3, 4, n - 1, n - 1)
    assert (rp.rollouts, rp.sessions) == (rollouts, sessions)
    assert rp.sessions == rp.proposer + rp.rollouts == 2 * n + 5


def test_the_arithmetic_is_computed_from_upstreams_own_ledger_formulas_not_typed(up):
    from harness2.core import ledger
    assert (stage1.EXPECTED_P, stage1.EXPECTED_R) == (ledger.expected_sessions(1), ledger.expected_rollouts(1))


def test_upstream_fixes_r_and_has_no_rollout_count_to_set(up):
    """The reason the extra rollouts live in OUR runner: run_task rolls one base per k and nothing else takes a count."""
    params = inspect.signature(up.schedule.run_task).parameters
    assert not any("roll" in p or p in ("n", "r", "reps", "repeats") for p in params)
    src = inspect.getsource(up.schedule.run_task)
    assert 'labels = [f"base{i}" for i in range(1, k + 1)]' in src


# ══ the flag ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("cmd", ["plan", "run", "selftest"])
def test_the_flag_is_an_int_that_defaults_to_one_and_refuses_nonsense(cmd, capsys):
    assert build_parser().parse_args([cmd]).rollouts == 1
    assert build_parser().parse_args([cmd, "--rollouts", "8"]).rollouts == 8
    for bad in ("abc", "2.5"):
        with pytest.raises(SystemExit):
            build_parser().parse_args([cmd, "--rollouts", bad])
    capsys.readouterr()


@pytest.mark.parametrize("bad", [0, -1, 65, True, 2.5, "8"])
def test_settings_refuse_a_rollout_count_that_is_not_a_small_positive_int(rig, bad):
    with pytest.raises(Stage1Error, match="--rollouts"):
        rig.settings(rollouts=bad)
    assert rig.settings(rollouts=8).rollouts == 8 and rig.settings().rollouts == 1


def test_settings_from_args_carry_the_count(rig):
    assert stage1._settings_from(rig.parse("plan", "--rollouts", "8"), {}).rollouts == 8
    assert stage1._settings_from(rig.parse("plan"), {}).rollouts == 1
    with pytest.raises(Stage1Error, match="--rollouts"):
        stage1._settings_from(rig.parse("plan", "--rollouts", "0"), {})


# ══ the profile cap, in plan and in run ═══════════════════════════════════════════════

def test_a_plan_over_the_profile_cap_is_refused_with_the_number_to_put_in_the_profile(rig, up):
    s = rig.settings(rollouts=8)
    pre = stage1.preflight(s, rig.probes())
    with pytest.raises(SweepRefused) as e:
        stage1.build_plan(s, pre, stage1.load_surface())
    text = " ".join(e.value.reasons)
    for needle in ("21 sessions", "P=3", "R=18", "--rollouts 8", "caps.sessions = 7", "at least 21", "lower --rollouts"):
        assert needle in text, (needle, text)


def test_a_plan_that_fits_the_cap_carries_the_extra_rollouts_into_the_plan_and_the_grant(tmp_path, up):
    rig, fake = rig_with(tmp_path, sessions=21, wall_s=5400)
    try:
        s = rig.settings(rollouts=8)
        pre = stage1.preflight(s, rig.probes())
        plan = stage1.build_plan(s, pre, stage1.load_surface())
        assert (plan.planned_sessions, plan.planned_rollouts, plan.max_wall_seconds) == (3, 18, 5400)
        g = stage1.make_grant(plan, sweep_id="stage1-x", approval=Approval("t", "tty", "p"), per_session_s=600)
        assert (g.max_proposer_sessions, g.max_solver_sessions, g.max_sessions) == (3, 18, 21)
        assert g.make_budget().snapshot()["counts"]["total"] == 0
        one = stage1.build_plan(rig.settings(), pre, stage1.load_surface())             # the default is upstream's own plan
        assert (one.planned_sessions, one.planned_rollouts) == (3, 4)
    finally:
        fake.__exit__(None, None, None)


def test_the_cap_is_exact_one_session_short_is_refused(tmp_path, up):
    rig, fake = rig_with(tmp_path, sessions=20, wall_s=5400)
    try:
        s = rig.settings(rollouts=8)
        with pytest.raises(SweepRefused, match="caps.sessions = 20"):
            stage1.build_plan(s, stage1.preflight(s, rig.probes()), stage1.load_surface())
    finally:
        fake.__exit__(None, None, None)


def test_plan_prints_the_session_count_the_worst_case_and_refuses_over_the_cap(rig, up):
    code, text = plan_out(rig, "--rollouts", "8")
    assert code == 2 and "NOT READY" in text and "caps.sessions = 7" in text and "at least 21" in text
    assert "21 sessions" in text and "worst case 12600 s for 21 sessions" in text       # still told, so the cause is visible
    assert "--approve" not in text


def test_plan_over_a_profile_with_room_is_ready_and_shows_the_arithmetic_and_the_flag(tmp_path, up):
    rig, fake = rig_with(tmp_path, sessions=21, wall_s=5400)
    try:
        code, text = plan_out(rig, "--rollouts", "8", "--task-set", "hard")
        assert code == 0 and "READY." in text, text
        for needle in ("sessions 21", "5400 s", "P=3", "R=18", "3 + 18 = 21 sessions", "base1..base8 (8)", "agg1 (8)",
                       "worst case 12600 s for 21 sessions", "MORE than the 5400 s wall", "rollouts         8"):
            assert needle in text, (needle, text)
        cmd = next(line.strip() for line in text.splitlines() if "stage1 run --approve" in line)
        assert build_parser().parse_args(shlex.split(cmd)[5:]).rollouts == 8
    finally:
        fake.__exit__(None, None, None)


def test_the_default_plan_and_approve_command_are_unchanged(rig, up):
    code, text = plan_out(rig)
    assert code == 0
    for needle in ("sessions 7", "P=3", "R=4", "worst case 4200 s for 7 sessions", "(base1, inv1, dom1, agg1)"):
        assert needle in text, needle
    cmd = next(line.strip() for line in text.splitlines() if "stage1 run --approve" in line)
    assert "--rollouts" not in cmd and build_parser().parse_args(shlex.split(cmd)[5:]).rollouts == 1


def test_run_refuses_an_over_cap_plan_before_anything_is_created(rig, up, capsys):
    class TTY(io.StringIO):
        def isatty(self):
            return True
    code = run_run(rig.parse("run", "--approve", "local-ollama", "--rollouts", "8"), environ={}, probes=rig.probes(),
                   stdin=TTY(), stdout=TTY(), tty=TTY(), run_task=lambda *a, **k: pytest.fail("a sweep started"))
    err = capsys.readouterr().err
    assert code == 2 and "caps.sessions = 7" in err and "at least 21" in err
    assert not rig.runs.exists() and not any(rig.ws.iterdir())


def test_a_grant_over_the_channels_session_cap_is_refused_at_run_time_too(rig, up):
    """Defence in depth: even a plan and grant that agree with each other may not exceed what the profile allows."""
    s = rig.settings()
    plan = stage1.build_plan(s, stage1.preflight(s, rig.probes()), stage1.load_surface())
    bigger = dataclasses.replace(plan, planned_rollouts=plan.planned_rollouts + 1)          # one session over the cap of 7
    g = stage1.make_grant(bigger, sweep_id="stage1-x", approval=Approval("t", "tty", "p"), per_session_s=600)
    ctx = SimpleNamespace(grant=g, plan=bigger, settings=s)
    with pytest.raises(ExecutionRefused, match="8 sessions.*caps.sessions = 7"):
        stage1.check_grant(ctx)
    ok = SimpleNamespace(grant=stage1.make_grant(plan, sweep_id="stage1-x", approval=Approval("t", "tty", "p"), per_session_s=600),
                         plan=plan, settings=s)
    stage1.check_grant(ok)                                                                   # exactly at the cap is fine


# ══ the extra rollouts, made by our runner around upstream ════════════════════════════

class FakeAdapter:
    """Records every execute; `fail` names labels that raise. Returns a path the caller did not need to exist."""

    def __init__(self, root: Path, spec_name="sum-signed-hard", fail=()):
        self.calls: list[tuple[str, Path | None, str]] = []
        self.spec, self.substrate = SimpleNamespace(name=spec_name), SimpleNamespace(name="cognitive")
        self.fail, self.root = set(fail), root

    def execute(self, task_id, harness_dir, run_id, *, timeout=None):
        self.calls.append((task_id, None if harness_dir is None else Path(harness_dir), run_id))
        if run_id.rsplit("/", 1)[-1] in self.fail:
            raise RuntimeError("a rollout died")
        return self.root / run_id.rsplit("/", 1)[-1]


def selection(**kw):
    sel = dict(ok=True, trivial=True, chosen_label="agg1", chosen_is_base=False, chosen_tree_is_base=False)
    sel.update(kw)
    return SimpleNamespace(selections={"agg": sel})


class Audit:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def emit(self, event, **fields):
        self.events.append((event, fields))


def extras(up, tmp_path, n, res=None, *, adapter=None, exhausted=lambda: False, make_dir=True, audit=None):
    ad = adapter or FakeAdapter(tmp_path)
    runs_root = tmp_path / "upstream-runs"
    task = "household/hard-1"
    ns = up.schedule.Namespace("sum-signed-hard", "cognitive", "agg", 1)
    if make_dir:
        (ns.task_dir(runs_root, task) / "harnesses" / "agg1").mkdir(parents=True)
    out = stage1.run_extra_rollouts(ad, res or selection(), up=up, runs_root=runs_root, task=task, k=1, n=n,
                                    audit=audit or Audit(), exhausted=exhausted)
    return ad, out, ns, runs_root


def test_n_of_one_makes_no_extra_rollout(up, tmp_path):
    ad, out, *_ = extras(up, tmp_path, 1)
    assert ad.calls == [] and out["base"] == [] and out["final"] == [] and out["skipped"] is None


def test_eight_makes_seven_extra_base_and_seven_extra_final_rollouts_interleaved_with_upstreams_run_ids(up, tmp_path):
    ad, out, ns, runs_root = extras(up, tmp_path, 8)
    labels = [rid.rsplit("/", 1)[-1] for _t, _h, rid in ad.calls]
    assert labels == [x for i in range(2, 9) for x in (f"base{i}", f"agg1.r{i}")]            # base2, agg1.r2, base3, agg1.r3, ...
    assert out["base"] == [f"base{i}" for i in range(2, 9)] and out["final"] == [f"agg1.r{i}" for i in range(2, 9)]
    assert all(rid.startswith(f"{ns}/household/hard-1/") for _t, _h, rid in ad.calls)       # upstream's own namespace
    base_dir = ns.task_dir(runs_root, "household/hard-1") / "harnesses" / "agg1"
    assert all(h is None for _t, h, rid in ad.calls if "base" in rid)
    assert all(h == base_dir for _t, h, rid in ad.calls if "agg1.r" in rid)                 # the SAME harness upstream delivered
    assert not {*labels} & {"base1", "inv1", "dom1", "agg1"}                                  # upstream's four are never redone
    assert len(ad.calls) == 14 and out["skipped"] is None


def test_the_extra_labels_are_safe_run_ids():
    from cognitive_harness2.sum_signed import flat_run_id
    assert flat_run_id("sum-signed-hard-cognitive-agg1/household/hard-1/agg1.r8").endswith("agg1.r8")


def _labels(ad):
    return [rid.rsplit("/", 1)[-1] for _t, _h, rid in ad.calls]


def test_with_no_adopted_edit_the_base_extras_still_run_and_only_the_finals_repeats_do_not(up, tmp_path):
    """Decided 2026-11-01: the baseline pass rate over N is a result in itself, so base2..baseN always run."""
    ad, out, *_ = extras(up, tmp_path, 8, selection(chosen_tree_is_base=True))
    assert _labels(ad) == [f"base{i}" for i in range(2, 9)] and all(h is None for _t, h, _r in ad.calls)
    assert out["base"] == [f"base{i}" for i in range(2, 9)] and out["final"] == []
    assert "no edited harness was adopted" in out["skipped"] and "base2..base8 run" in out["skipped"]
    assert "the final's repeats do not" in out["skipped"] and out["errors"] == []


@pytest.mark.parametrize("n", [2, 3, 8])
def test_the_number_of_base_extras_is_n_minus_one_adopted_or_not(up, tmp_path, n):
    adopted_ad, adopted, *_ = extras(up, tmp_path / "a", n)
    none_ad, none, *_ = extras(up, tmp_path / "b", n, selection(chosen_tree_is_base=True))
    assert len(adopted["base"]) == len(none["base"]) == n - 1
    assert len(adopted["final"]) == n - 1 and none["final"] == []
    assert len(adopted_ad.calls) == 2 * (n - 1) and len(none_ad.calls) == n - 1


@pytest.mark.parametrize("res,why", [
    (SimpleNamespace(selections={}), "selection"),
    (selection(ok=False), "selection"),
    (selection(chosen_label=""), "selection"),
])
def test_a_missing_or_failed_selection_runs_the_base_extras_only_and_says_why(up, tmp_path, res, why):
    ad, out, *_ = extras(up, tmp_path, 8, res)
    assert _labels(ad) == [f"base{i}" for i in range(2, 9)] and out["final"] == [] and why in out["skipped"]


def test_a_missing_final_harness_directory_runs_the_base_extras_and_not_the_finals_repeats(up, tmp_path):
    ad, out, *_ = extras(up, tmp_path, 8, make_dir=False)
    assert _labels(ad) == [f"base{i}" for i in range(2, 9)] and out["final"] == []
    assert "harness directory" in out["skipped"]


def test_the_audit_says_the_final_extras_were_skipped_and_the_base_extras_started(up, tmp_path):
    audit = Audit()
    extras(up, tmp_path, 3, selection(chosen_tree_is_base=True), audit=audit)
    kinds = [e for e, _f in audit.events]
    assert kinds == ["extra_rollouts_skipped", "extra_rollouts_start", "extra_rollouts_end"]
    assert audit.events[0][1]["base_extras_run"] is True and audit.events[1][1] == {"n": 3, "chosen": None, "planned": 2}


def test_the_session_cap_worst_case_still_counts_the_final_extras():
    """The cap check, the grant and the budget use the worst case: 2 (n - 1) extras, though a no-edit sweep runs n - 1."""
    rp = stage1.rollout_plan(8, upstream_p=3, upstream_r=4)
    assert rp.sessions == 21 and rp.extra_base == 7 and rp.extra_final == 7


class FakeBudget:
    def __init__(self, **snap):
        self.snap = {"remaining_s": 100.0, "exhausted": False, "role_caps": {"solver": 10}, "counts": {"solver": 3, "proposer": 3, "total": 6}, **snap}

    def snapshot(self):
        return self.snap


@pytest.mark.parametrize("snap,expect", [
    ({}, False),
    ({"remaining_s": 0.0}, True),
    ({"exhausted": True}, True),                                        # the sweep's total session cap or the wall clock
    ({"counts": {"solver": 10, "proposer": 3, "total": 13}}, True),     # the solver role cap, wall clock fine
    ({"counts": {"solver": 9, "proposer": 3, "total": 12}}, False),
    ({"role_caps": {}}, False),
])
def test_the_sweep_is_exhausted_by_the_wall_clock_or_by_a_session_cap(snap, expect):
    assert stage1.sweep_exhausted(FakeBudget(**snap)) is expect


def test_a_session_cap_stops_the_extras_as_a_stop_and_not_as_dead_rollouts(up, tmp_path):
    budget = FakeBudget()
    ad = FakeAdapter(tmp_path)
    real_execute = ad.execute

    def execute(*a, **k):
        out = real_execute(*a, **k)
        budget.snap["counts"]["solver"] += 1                              # each rollout takes a solver session
        return out
    ad.execute = execute
    _ad, out, *_ = extras(up, tmp_path, 8, adapter=ad, exhausted=lambda: stage1.sweep_exhausted(budget))
    assert len(ad.calls) == 7 and out["errors"] == [] and "budget" in out["skipped"] and "7 of 14" in out["skipped"]


def test_the_report_gives_the_baseline_rate_and_says_no_edited_harness_was_adopted():
    res = adopted_res()
    res.selections["agg"]["chosen_tree_is_base"] = True
    rolls = rolls_for([True] * 3 + [False] * 5, [False])
    v = stage1._selection_view(res, rolls)
    arms = stage1.arm_rates(rolls, "agg1")
    line = stage1.pass_rate_line({"pass_rates": {"final_label": "agg1", **arms}, "selection": v})
    assert line == "pass rate base 3/8 (38%); no edited harness adopted\n"
    sel = stage1.selection_line({"selection": v})
    assert sel == ("selection base-kept: final agg1 (the base harness was kept); pass rate base 3/8; no edited harness adopted\n")
    assert "agg1 " not in line                                                    # the final is the base: not an arm


def test_an_unusable_base_rollout_still_shows_in_the_baseline_line():
    res = adopted_res()
    res.selections["agg"]["chosen_tree_is_base"] = True
    rolls = rolls_for([True, True, None], [False])
    line = stage1.pass_rate_line({"pass_rates": {"final_label": "agg1", **stage1.arm_rates(rolls, "agg1")},
                                  "selection": stage1._selection_view(res, rolls)})
    assert line == "pass rate base 2/3 (67%, 1 unusable); no edited harness adopted\n"


def test_with_one_rollout_and_no_edit_the_old_lines_stay():
    res = adopted_res()
    res.selections["agg"]["chosen_tree_is_base"] = True
    rolls = rolls_for([False], [False])
    line = stage1.pass_rate_line({"pass_rates": {"final_label": "agg1", **stage1.arm_rates(rolls, "agg1")},
                                  "selection": stage1._selection_view(res, rolls)})
    assert line == "pass rate base 0/1 (0%), final agg1 0/1 (0%)  (one sample each: not a measurement)\n"


def test_the_plan_text_says_which_extras_are_certain_and_which_depend_on_an_adopted_edit(tmp_path, up):
    rig4, fake = rig_with(tmp_path, sessions=13, wall_s=5400)
    try:
        code, text = plan_out(rig4, "--rollouts", "4", "--task-set", "hard")
        assert code == 0, text
        assert "the 3 base repeats always run, the final's only if an edited harness is adopted" in text
        assert "13 sessions (profile cap 13), the worst case: 3 fewer if no edited harness is adopted" in text
    finally:
        fake.__exit__(None, None, None)


def test_the_default_plan_has_no_rollout_extras_to_explain(rig, up):
    code, text = plan_out(rig)
    assert code == 0 and "fewer if no edited" not in text and "always run" not in text


def test_a_rollout_that_dies_is_counted_and_the_rest_still_run(up, tmp_path):
    ad, out, *_ = extras(up, tmp_path, 4, adapter=FakeAdapter(tmp_path, fail={"base3"}))
    assert len(ad.calls) == 6 and out["errors"] == ["base3: RuntimeError: a rollout died"]


def test_an_exhausted_budget_stops_the_extras_and_says_so(up, tmp_path):
    seen = {"n": 0}

    def exhausted():
        seen["n"] += 1
        return seen["n"] > 3
    ad, out, *_ = extras(up, tmp_path, 8, exhausted=exhausted)
    assert len(ad.calls) == 3 and "budget" in out["skipped"] and "3 of 14" in out["skipped"]


def test_each_extra_is_announced_in_the_audit(up, tmp_path):
    audit = Audit()
    extras(up, tmp_path, 3, audit=audit)
    kinds = [e for e, _f in audit.events]
    assert kinds[0] == "extra_rollouts_start" and kinds[-1] == "extra_rollouts_end"
    assert audit.events[0][1] == {"n": 3, "chosen": "agg1", "planned": 4}


# ══ the report: per-rollout results and rates ═════════════════════════════════════════

def roll(label, passed, ok=True):
    return {"label": label, "run_id": f"ns/t/{label}", "ok": ok, "passed": passed}


def test_the_arms_are_base1_to_basen_and_the_delivered_final_with_its_extras_and_nothing_else():
    rolls = [roll("base1", False), roll("base2", True), roll("inv1", True), roll("dom1", False), roll("agg1", True),
             roll("agg1.r2", False), roll("agg1.r3", True)]
    arms = stage1.arm_rates(rolls, "agg1")
    assert arms["base"] == dict(labels=["base1", "base2"], n=2, passed=1, failed=1, unusable=0, rate=0.5)
    assert arms["final"] == dict(labels=["agg1", "agg1.r2", "agg1.r3"], n=3, passed=2, failed=1, unusable=0, rate=2 / 3)
    assert stage1.arm_of("inv1", "agg1") == "other" and stage1.arm_of("agg1.r9", "agg1") == "final"
    assert stage1.arm_of("agg10", "agg1") == "other" and stage1.arm_of("base12", "agg1") == "base"


def test_a_rollout_that_died_counts_in_n_as_not_passed_and_is_called_unusable():
    arms = stage1.arm_rates([roll("base1", True), roll("base2", None, ok=False), roll("base3", False)], None)
    assert arms["base"]["n"] == 3 and arms["base"]["passed"] == 1 and arms["base"]["unusable"] == 1 and arms["base"]["failed"] == 1
    assert arms["final"]["n"] == 0 and arms["final"]["rate"] is None


def test_fisher_exact_is_the_classic_tea_tasting_value_and_the_extremes():
    assert stage1.fisher_two_sided(3, 4, 1, 4) == pytest.approx(34 / 70)
    assert stage1.fisher_two_sided(8, 8, 0, 8) == pytest.approx(2 / 12870)
    assert stage1.fisher_two_sided(4, 8, 4, 8) == pytest.approx(1.0)
    assert stage1.fisher_two_sided(0, 8, 0, 8) == 1.0 and stage1.fisher_two_sided(8, 8, 8, 8) == 1.0


def adopted_res():
    return SimpleNamespace(selections={"agg": dict(ok=True, trivial=True, chosen_label="agg1", chosen_is_base=False,
                                                    chosen_tree_is_base=False, n_candidates=1, dropped=[])})


def rolls_for(base_passed, final_passed):
    out = [roll(f"base{i}", p) for i, p in enumerate(base_passed, 1)]
    out += [roll("inv1", True), roll("dom1", True)]
    out += [roll("agg1" if i == 1 else f"agg1.r{i}", p) for i, p in enumerate(final_passed, 1)]
    return out


def test_with_many_rollouts_the_verdict_is_a_rate_comparison_and_never_the_word_improved():
    v = stage1._selection_view(adopted_res(), rolls_for([True] * 4 + [False] * 4, [True] * 7 + [False]))
    assert v["verdict"] == "higher-rate" and v["base_rate"] == {"passed": 4, "n": 8} and v["chosen_rate"] == {"passed": 7, "n": 8}
    assert "improved" not in json.dumps(v) and v["fisher_p"] == pytest.approx(stage1.fisher_two_sided(7, 8, 4, 8))
    assert "not significant" in v["evidence"] and "claim nothing" in v["evidence"]
    lower = stage1._selection_view(adopted_res(), rolls_for([True] * 7 + [False], [True] * 4 + [False] * 4))
    same = stage1._selection_view(adopted_res(), rolls_for([True] * 4 + [False] * 4, [True] * 4 + [False] * 4))
    assert (lower["verdict"], same["verdict"]) == ("lower-rate", "same-rate")


def test_a_large_gap_is_reported_as_unlikely_to_be_chance_but_still_as_a_rate_not_a_win():
    v = stage1._selection_view(adopted_res(), rolls_for([False] * 8, [True] * 8))
    assert v["verdict"] == "higher-rate" and v["fisher_p"] < 0.001
    assert "unlikely to be chance" in v["evidence"] and "one task" in v["evidence"]


def test_with_one_sample_each_the_old_verdicts_stay_and_say_they_are_an_anecdote():
    v = stage1._selection_view(adopted_res(), rolls_for([False], [True]))
    assert v["verdict"] == "improved" and v["base_passed"] is False and v["chosen_passed"] is True
    assert "one sample each" in v["evidence"] and "not a measurement" in v["evidence"] and v["fisher_p"] is None


def test_a_base_kept_final_is_base_kept_whatever_the_rates():
    res = adopted_res()
    res.selections["agg"]["chosen_tree_is_base"] = True
    v = stage1._selection_view(res, rolls_for([True] * 8, [False]))
    assert v["verdict"] == "base-kept" and v["adopted"] is False


def test_an_arm_with_no_usable_rollout_is_unknown_not_a_zero():
    v = stage1._selection_view(adopted_res(), rolls_for([None] * 8, [True] * 8))
    assert v["verdict"] == "unknown"


def test_the_selection_line_for_many_rollouts_gives_both_rates_and_the_evidence():
    v = stage1._selection_view(adopted_res(), rolls_for([True] * 4 + [False] * 4, [True] * 7 + [False]))
    line = stage1.selection_line({"selection": v})
    assert line.startswith("selection higher-rate: final agg1 (an edited harness was adopted); pass rate base 4/8, agg1 7/8;")
    assert "not significant" in line and line.endswith("\n")


def test_the_pass_rate_line_names_n_passed_over_n_for_both_arms_and_is_there_for_one_sample_too():
    arms = stage1.arm_rates(rolls_for([True] * 4 + [False] * 4, [True] * 7 + [False]), "agg1")
    assert stage1.pass_rate_line({"pass_rates": {"final_label": "agg1", **arms}}) == (
        "pass rate base 4/8 (50%), final agg1 7/8 (88%)\n")
    one = stage1.arm_rates(rolls_for([False], [True]), "agg1")
    assert stage1.pass_rate_line({"pass_rates": {"final_label": "agg1", **one}}) == (
        "pass rate base 0/1 (0%), final agg1 1/1 (100%)  (one sample each: not a measurement)\n")
    died = stage1.arm_rates([roll("base1", True), roll("base2", None, ok=False)], "agg1")
    assert "1 unusable" in stage1.pass_rate_line({"pass_rates": {"final_label": "agg1", **died}})
    assert stage1.pass_rate_line({}) == ""


# ══ round 3: a run_task that FAILED (without raising) must not be followed by base extras ══════════════════════════════════

def with_status(status, **kw):
    res = selection(**kw)
    res.status = status
    return res


@pytest.mark.parametrize("status", ["base-failed", "failed", "exception", "running"])
def test_a_failed_run_task_gets_no_extra_rollouts_and_the_report_says_why(up, tmp_path, status):
    audit = Audit()
    ad, out, *_ = extras(up, tmp_path, 8, with_status(status), audit=audit)
    assert ad.calls == [] and out["base"] == [] and out["final"] == [] and out["errors"] == []
    assert status in out["skipped"] and "run_task" in out["skipped"] and "base2..base8" in out["skipped"]
    assert [e for e, _f in audit.events] == ["extra_rollouts_skipped"]                       # nothing started, so no start/end pair
    assert audit.events[0][1] == {"n": 8, "reason": out["skipped"], "base_extras_run": False}


@pytest.mark.parametrize("status", ["ok", "partial", "skipped"])
def test_a_usable_run_task_still_gets_its_base_extras(up, tmp_path, status):
    ad, out, *_ = extras(up, tmp_path, 3, with_status(status))
    assert _labels(ad) == ["base2", "agg1.r2", "base3", "agg1.r3"] and out["skipped"] is None


def test_a_result_with_no_status_at_all_keeps_the_old_behaviour(up, tmp_path):
    ad, out, *_ = extras(up, tmp_path, 3)                                                    # `selection()` has no `status`
    assert len(ad.calls) == 4 and out["skipped"] is None


def test_a_failed_run_task_with_n_of_one_is_still_silent(up, tmp_path):
    audit = Audit()
    ad, out, *_ = extras(up, tmp_path, 1, with_status("base-failed"), audit=audit)
    assert ad.calls == [] and out["skipped"] is None and audit.events == []
