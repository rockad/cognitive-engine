"""Stage 1b: the improvement loop, exercised end to end with a SCRIPTED proposer on a task the base harness fails.

On the easy `sum-signed` task the base already passes, so every proposer stage keeps the base and no edit is ever
accepted. On `sum-signed-hard` the fake solver is a weak reader unless its AGENTS.md says how to read a ledger, and the
fake proposer plays one script per scenario:

  format-rules          adds the notation rules to systemprompt.md (a legal c1 edit): the edit is HARVESTED, passes the
                        slot table, runs as a candidate that passes where the base fails, and is ADOPTED by upstream's selection
  forbid-guardrails     edits guardrails.md (c2)            -> refused by the harvest, base kept
  forbid-executable     adds an executable file             -> refused by the harvest, base kept
  forbid-frontmatter    prepends a frontmatter block        -> refused by the slot table (check_mutation), base kept
  forbid-script         adds a plain script (c7)            -> refused by the slot table, base kept

The unit tests below need no Docker; the end-to-end tests run on the real rootless daemon and never skip.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from h2_upstream import isolated_upstream

from cognitive_harness2 import stage1, sum_signed as ss, sum_signed_hard as hd
from cognitive_harness2.stage1 import Audit, SweepOutcome, hard_selftest_problems, run_selftest, selftest_problems

SRC = Path(__file__).resolve().parents[1] / "src" / "cognitive_harness2"
FAKE_SH = SRC / "selftest_fake_opencode.sh"
TASK = "household/hard-1"


@pytest.fixture(scope="module")
def up(tmp_path_factory):
    with isolated_upstream(tmp_path_factory.mktemp("runs")) as u:
        yield u


@pytest.fixture(scope="module")
def surface():
    from harness2.substrate.opencode import OPENCODE_SURFACE
    return OPENCODE_SURFACE


# ══ the fake opencode ═════════════════════════════════════════════════════════════

def test_the_fake_proposers_rules_text_is_the_task_sets_rules_text_exactly():
    text = FAKE_SH.read_text()
    m = re.search(r"cat >> \"\$f\" <<'RULES'\n(.*?)\nRULES\n", text, re.S)
    assert m is not None and m.group(1) + "\n" == hd.FORMAT_RULES
    assert "parentheses" in hd.FORMAT_RULES.lower() and "parentheses" in m.group(1).lower()


def test_the_proposer_script_marker_is_there_exactly_once_and_every_scenario_is_handled_by_the_fake():
    text = FAKE_SH.read_text()
    assert text.count(stage1.PROPOSER_MARKER) == 1
    assert subprocess.run(["bash", "-n", str(FAKE_SH)]).returncode == 0
    for scenario in stage1.SCENARIOS:
        assert f"\n    {scenario})" in text, scenario
        set_text = stage1.selftest_cli_text(scenario)
        assert set_text.count(f"\nPROPOSER_SCRIPT={scenario}\n") == 1 and set_text.count("PROPOSER_SCRIPT=sign-check\n") == (
            1 if scenario == "sign-check" else 0)
    assert set(stage1.REFUSED_BY) | {"sign-check", "format-rules"} == set(stage1.SCENARIOS)
    assert set(stage1.REFUSAL_TEXT) == set(stage1.REFUSED_BY)
    assert set(stage1.HARD_SCENARIOS) == set(stage1.SCENARIOS) - {"sign-check"}
    with pytest.raises(stage1.Stage1Error, match="unknown selftest scenario"):
        stage1.selftest_cli_text("rm-rf")


def test_selftest_cli_text_is_loud_when_the_marker_is_missing(monkeypatch, tmp_path):
    broken = tmp_path / "opencode"
    broken.write_text("#!/bin/bash\necho hi\n")
    monkeypatch.setattr(stage1, "FAKE_CLI", broken)
    with pytest.raises(stage1.Stage1Error, match="no single 'PROPOSER_SCRIPT=sign-check' line"):
        stage1.selftest_cli_text("format-rules")


def shell_fns() -> str:
    """The fake's `money` and `cents_of` functions, cut out of the script so they run on their own."""
    text = FAKE_SH.read_text()
    money = re.search(r"^money\(\).*$", text, re.M).group(0)
    cents = re.search(r"^cents_of\(\) \{.*?\"\$2\"; \}$", text, re.S | re.M).group(0)
    return money + "\n" + cents + "\n"


def fake_total(rules: int, input_text: str, tmp_path: Path) -> str:
    f = tmp_path / "input.txt"
    f.write_bytes(input_text.encode())
    p = subprocess.run(["bash", "-c", f'{shell_fns()}\nmoney "$(cents_of {rules} {f})"'], capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


def test_the_fake_solver_reads_like_the_weak_reader_without_the_rules_and_like_the_reference_parser_with_them(tmp_path):
    for tid, inst in hd.INSTANCES.items():
        assert fake_total(1, inst.input_text, tmp_path) == inst.expected_total, tid
        assert fake_total(0, inst.input_text, tmp_path) == hd.naive_total(inst.input_text), tid
        assert hd.naive_total(inst.input_text) != inst.expected_total
    # the easy task: both readers agree, so the easy selftest cannot change
    for tid, inst in ss.INSTANCES.items():
        assert fake_total(1, inst.input_text, tmp_path) == fake_total(0, inst.input_text, tmp_path) == inst.expected_total, tid


# ══ upstream's selection at k=1, pinned ═══════════════════════════════════════════

def test_upstream_k1_selection_delivers_the_single_final_and_never_compares_it_with_the_base(tmp_path):
    from harness2.core import selection
    from harness2.core.spec import RunView

    class A:
        spec = SimpleNamespace(state_deliverable=False, obs_trunc=50_000)

        def deliverable_text(self, run_dir):
            return "Total: 1.00"

    def view(label, delivered=True):
        return RunView(label, f"id-{label}", tmp_path / label, ok=True, delivered=delivered)

    rollouts = {"base1": view("base1"), "inv1": view("inv1"), "dom1": view("dom1"), "agg1": view("agg1")}
    pool = selection.selection_pool("agg", 1, rollouts, [rollouts["base1"]])
    assert list(pool) == ["agg1"]                                              # the base and the stage candidates are not in it
    sel = selection.trivial_selection("t", 1, "agg", pool, adapter=A(), work_dir=tmp_path / "w")
    assert (sel.ok, sel.trivial, sel.chosen_label, sel.chosen_is_base) == (True, True, "agg1", False)
    assert "without a selector session" in sel.why
    # the only way it is not delivered: the rollout produced nothing (no score is ever read)
    empty = selection.trivial_selection("t", 1, "agg", {"agg1": view("agg1", delivered=False)}, adapter=A(), work_dir=tmp_path / "w2")
    assert empty.ok is False and empty.dropped == ["agg1"] and "no candidate produced an artifact" in empty.error


def test_selection_view_says_what_was_adopted_and_what_the_runners_own_verdicts_say_of_it():
    def res(**sel):
        base = dict(ok=True, trivial=True, chosen_label="agg1", chosen_is_base=False, chosen_tree_is_base=False,
                    n_candidates=1, dropped=[], harness_fingerprint="fp")
        return SimpleNamespace(selections={"agg": {**base, **sel}})

    def rolls(base, chosen):
        return [{"label": "base1", "passed": base}, {"label": "agg1", "passed": chosen}]
    v = stage1._selection_view
    assert v(res(), rolls(False, True))["verdict"] == "improved" and v(res(), rolls(False, True))["adopted"] is True
    assert v(res(), rolls(True, False))["verdict"] == "regressed" and v(res(), rolls(True, False))["adopted"] is True
    assert v(res(), rolls(True, True))["verdict"] == "unchanged" and v(res(), rolls(False, False))["verdict"] == "unchanged"
    assert v(res(), rolls(None, True))["verdict"] == "unknown"
    kept = v(res(chosen_tree_is_base=True), rolls(False, False))
    assert kept["adopted"] is False and kept["verdict"] == "base-kept"
    assert v(res(ok=False, chosen_label=""), rolls(False, False))["adopted"] is False
    assert v(SimpleNamespace(selections={}), []) is None and v(SimpleNamespace(), []) is None
    line = stage1.selection_line({"selection": v(res(), rolls(False, True))})
    assert line.startswith("selection improved: final agg1 (an edited harness was adopted); base1 failed, agg1 passed")
    assert "base harness was kept" in stage1.selection_line({"selection": kept}) and stage1.selection_line({}) == ""


# ══ the evidence read from the run directory ══════════════════════════════════════

def make_run_dir(tmp_path: Path, edits: dict[str, dict[str, str]], checks: list[dict] | None = None) -> Path:
    run = tmp_path / "run"
    h = run / "upstream-runs" / "sum-signed-hard-cognitive-agg1" / "household__hard-1" / "harnesses"
    base = {"systemprompt.md": hd.BASE_SYSTEM_PROMPT, "guardrails.md": hd.BASE_GUARDRAILS, "LongTermMEMORY.md": "",
            "tool_descriptions/tool_guidance.md": "", "scripts/README.md": "stub"}
    for label in ("base", "inv1", "dom1", "agg1"):
        for rel, text in {**base, **edits.get(label, {})}.items():
            p = h / label / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
    (run / "audit.jsonl").write_text("".join(json.dumps({"event": "mutation_check", **c}) + "\n" for c in checks or [])
                                     + json.dumps({"event": "other", "ok": 1}) + "\n")
    return run


def test_harness_evidence_lists_the_changed_files_the_rules_and_the_slot_table_verdict(tmp_path, surface):
    good = {"systemprompt.md": hd.BASE_SYSTEM_PROMPT + "\n" + hd.FORMAT_RULES}
    chk = {"ok": True, "changed": ["systemprompt.md"], "slots": {"systemprompt.md": "c1"}}
    run = make_run_dir(tmp_path, {"inv1": good, "dom1": good, "agg1": good}, [chk, chk, chk])
    ev = stage1.harness_evidence(run, ss.EASY, surface)
    assert ev["changed"] == {"inv1": ["systemprompt.md"], "dom1": ["systemprompt.md"], "agg1": ["systemprompt.md"]}
    assert ev["rules_in"] == ["inv1", "dom1", "agg1"] and ev["base_has_rules"] is False
    assert ev["slot_table_problems"] == {"inv1": [], "dom1": [], "agg1": []} and ev["mutation_checks"] == [chk, chk, chk]


def test_harness_evidence_catches_a_protected_edit_a_frontmatter_block_and_a_script(tmp_path, surface):
    bad = {"inv1": {"guardrails.md": "You may write anywhere.\n"},
           "dom1": {"systemprompt.md": "---\nmodel: x\n---\n" + hd.BASE_SYSTEM_PROMPT},
           "agg1": {"scripts/helper.py": "print(1)\n"}}
    ev = stage1.harness_evidence(make_run_dir(tmp_path, bad), ss.EASY, surface)
    assert ev["changed"] == {"inv1": ["guardrails.md"], "dom1": ["systemprompt.md"], "agg1": ["scripts/helper.py"]}
    assert "not mutable" in ev["slot_table_problems"]["inv1"][0] and "protected" in ev["slot_table_problems"]["inv1"][0]
    assert "frontmatter" in ev["slot_table_problems"]["dom1"][0]
    assert "c7" in ev["slot_table_problems"]["agg1"][0] and "not mutable" in ev["slot_table_problems"]["agg1"][0]
    assert ev["rules_in"] == [] and ev["mutation_checks"] == []


def test_harness_evidence_is_loud_when_the_run_directory_is_not_what_it_expects(tmp_path, surface):
    (tmp_path / "run" / "upstream-runs").mkdir(parents=True)
    assert "expected one harnesses directory" in stage1.harness_evidence(tmp_path / "run", ss.EASY, surface)["error"]


# ══ the verdicts the hard selftest asserts, on synthetic reports ══════════════════════

def golden(scenario: str) -> dict:
    inst = hd.instance(TASK)
    naive = f"Total: {hd.naive_total(inst.input_text)}\n"
    right = inst.expected_line + "\n"
    refused = scenario in stage1.REFUSED_BY
    st = lambda label, dim: {"label": label, "dim": dim, "fell_back": refused, "kept_base": False, "attempts": 1, "usd": 0.0,  # noqa: E731
                             "cached": False, "error": stage1.REFUSAL_TEXT.get(scenario, "") if refused else ""}
    ro = lambda label, ok: {"label": label, "passed": ok, "n_passed": 3 if ok else 2, "answer_excerpt": right if ok else naive}  # noqa: E731
    cands = not refused
    good = {"systemprompt.md": hd.BASE_SYSTEM_PROMPT + "\n" + hd.FORMAT_RULES}
    chk = {"ok": cands, "changed": ["systemprompt.md"], "slots": {"systemprompt.md": "c1"}}
    labels = ("inv1", "dom1", "agg1")
    ev = {"base_has_rules": False, "changed": {x: ["systemprompt.md"] if cands else [] for x in labels},
          "rules_in": list(labels) if cands else [], "slot_table_problems": {x: [] for x in labels},
          "mutation_checks": ([chk] * 3 if (cands or stage1.REFUSED_BY.get(scenario) == "slot-table") else [])}
    sel = {"ok": True, "trivial": True, "chosen_label": "agg1", "chosen_is_base": False, "chosen_tree_is_base": not cands,
           "adopted": cands, "verdict": "improved" if cands else "base-kept", "base_passed": False, "chosen_passed": cands}
    return {"task": TASK, "task_set": "hard", "_scenario": scenario,
            "stages": [st("inv1", "INV"), st("dom1", "DOM"), st("agg1", "AGG")],
            "rollouts": [ro("base1", False), ro("inv1", cands), ro("dom1", cands), ro("agg1", cands)],
            "selection": sel, "_harness": ev}


@pytest.mark.parametrize("scenario", stage1.HARD_SCENARIOS)
def test_the_golden_report_of_each_scenario_passes(scenario):
    assert hard_selftest_problems(golden(scenario)) == []


def mutated(scenario: str, edit) -> list[str]:
    r = golden(scenario)
    edit(r)
    return hard_selftest_problems(r)


@pytest.mark.parametrize("name,edit,needle", [
    ("base passes", lambda r: r["rollouts"][0].update(passed=True, n_passed=3), "base1 must fail"),
    ("base wrong answer", lambda r: r["rollouts"][0].update(answer_excerpt="Total: 0.00\n"), "weak reader"),
    ("stage fell back", lambda r: r["stages"][2].update(fell_back=True), "not accepted as a changed harness"),
    ("stage kept base", lambda r: r["stages"][0].update(kept_base=True), "not accepted as a changed harness"),
    ("edit not harvested", lambda r: r["_harness"]["changed"].update(agg1=[]), "agg1: the harvested harness changed []"),
    ("edit touched more", lambda r: r["_harness"]["changed"].update(dom1=["systemprompt.md", "guardrails.md"]), "dom1: the harvested"),
    ("rules missing", lambda r: r["_harness"].update(rules_in=["inv1", "dom1"]), "agg1: the notation rules are not"),
    ("slot table refuses", lambda r: r["_harness"]["slot_table_problems"].update(inv1=["x: no"]), "inv1: the slot table refuses"),
    ("base already has rules", lambda r: r["_harness"].update(base_has_rules=True), "already holds the notation rules"),
    ("no mutation check audited", lambda r: r["_harness"].update(mutation_checks=[]), "three accepted mutation_check events"),
    ("a refused check", lambda r: r["_harness"]["mutation_checks"][1].update(ok=False), "three accepted mutation_check events"),
    ("candidate fails", lambda r: r["rollouts"][3].update(passed=False, n_passed=2), "candidate agg1 should pass"),
    ("not adopted", lambda r: r["selection"].update(adopted=False, verdict="base-kept"), "should adopt the edited agg1"),
    ("chosen is base", lambda r: r["selection"].update(chosen_is_base=True), "should adopt the edited agg1"),
    ("no selection", lambda r: r.update(selection=None), "should adopt the edited agg1"),
    ("wrong stages", lambda r: r["stages"].pop(), "unexpected stages"),
])
def test_the_adopt_scenario_notices_each_way_the_loop_could_silently_not_work(name, edit, needle):
    problems = mutated("format-rules", edit)
    assert any(needle in p for p in problems), (name, problems)


@pytest.mark.parametrize("scenario", sorted(stage1.REFUSED_BY))
def test_a_refusal_scenario_notices_an_edit_that_got_through(scenario):
    assert any("should have been refused" in p for p in mutated(scenario, lambda r: r["stages"][1].update(fell_back=False)))
    assert any("left a different harness" in p for p in mutated(scenario, lambda r: r["_harness"]["changed"].update(agg1=["systemprompt.md"])))
    assert any("should fail this task" in p for p in mutated(scenario, lambda r: r["rollouts"][2].update(passed=True)))
    assert any("should deliver a base-equal agg1" in p for p in mutated(scenario, lambda r: r["selection"].update(adopted=True, chosen_tree_is_base=False)))
    # the stage was refused for the WRONG reason
    assert any("should have been refused" in p for p in mutated(scenario, lambda r: r["stages"][0].update(error="boom")))


def test_the_layer_that_refuses_is_checked():
    harvest = "forbid-guardrails"
    assert any("harvest should refuse before" in p for p in mutated(
        harvest, lambda r: r["_harness"].update(mutation_checks=[{"ok": False, "changed": [], "slots": {}}])))
    assert any("three refused mutation_check events" in p for p in mutated(
        "forbid-frontmatter", lambda r: r["_harness"].update(mutation_checks=[])))
    assert any("three refused mutation_check events" in p for p in mutated(
        "forbid-script", lambda r: r["_harness"]["mutation_checks"][0].update(ok=True)))


def test_an_unknown_scenario_or_task_is_a_problem_not_a_pass():
    assert any("no expectations" in p for p in mutated("format-rules", lambda r: r.update(_scenario="sign-check")))
    assert hard_selftest_problems({**golden("format-rules"), "task": "household/sum-1"}) == ["the hard selftest ran an unknown task 'household/sum-1'"]


# ── stage 1c: the same verdicts at --rollouts N, and the proposer hint ──

def golden_n(scenario: str, n: int, *, hint: bool = True) -> dict:
    """`golden`, as a sweep at `--rollouts n` would report it (the base repeats always run; the final's repeats only in the adopt scenario)."""
    r = golden(scenario)
    inst = hd.instance(TASK)
    naive, right = f"Total: {hd.naive_total(inst.input_text)}\n", inst.expected_line + "\n"
    adopt = scenario == "format-rules"
    eb = [f"base{i}" for i in range(2, n + 1)]
    ef = [f"agg1.r{i}" for i in range(2, n + 1)] if adopt else []
    r["rollouts"] += [{"label": x, "passed": False, "n_passed": 2, "answer_excerpt": naive} for x in eb]
    r["rollouts"] += [{"label": x, "passed": True, "n_passed": 3, "answer_excerpt": right} for x in ef]
    r["rollouts_plan"] = {"n": n, "extra_base_run": eb, "extra_final_run": ef, "extra_errors": [],
                          "extra_skipped": None if adopt or n == 1 else "no edited harness was adopted (the final agg1 is the base): "
                                                                         f"nothing to compare it with: base2..base{n} run, the final's repeats do not"}
    r["pass_rates"] = {"final_label": "agg1", "base": {"passed": 0, "n": n}, "final": {"passed": n if adopt else 0, "n": n if adopt else 1}}
    if adopt and n > 1:
        r["selection"].update(verdict="higher-rate", fisher_p=stage1.fisher_two_sided(n, n, 0, n))
    sha = stage1.proposer_hint.digest("ENVIRONMENT RULES x\n")
    r["proposer_hint"] = {"on": hint, "source": "--proposer-hint", "sha": sha if hint else None}
    r["_proposer_audit"] = [{"label": f"{x}.a1", "hint": hint, "hint_sha": sha if hint else None, "block_matches_sha": hint,
                             "system_starts_with_block": hint} for x in ("inv1", "dom1", "agg1")]
    return r


@pytest.mark.parametrize("scenario", stage1.HARD_SCENARIOS)
@pytest.mark.parametrize("n", [1, 8])
def test_the_golden_report_of_each_scenario_passes_at_one_and_at_eight_rollouts(scenario, n):
    assert hard_selftest_problems(golden_n(scenario, n)) == []
    assert stage1.hint_selftest_problems(golden_n(scenario, n)) == [] and stage1.hint_selftest_problems(golden_n(scenario, n, hint=False)) == []


@pytest.mark.parametrize("name,edit,needle", [
    ("an extra base passes", lambda r: r["rollouts"][4].update(passed=True, n_passed=3), "base2 must fail"),
    ("an extra final fails", lambda r: r["rollouts"][-1].update(passed=False, n_passed=2), "extra final rollout agg1.r8 should pass"),
    ("an extra is missing", lambda r: (r["rollouts"].pop(), r["rollouts_plan"]["extra_final_run"].pop()), "expected 7 extra base and 7 extra final"),
    ("an extra errored", lambda r: r["rollouts_plan"].update(extra_errors=["base3: died"]), "expected 7 extra base and 7 extra final"),
    ("an unplanned rollout", lambda r: r["rollouts"].append({"label": "base9"}), "unexpected stages"),
    ("wrong rates", lambda r: r["pass_rates"]["final"].update(passed=7), "pass rates should be base 0/8 and final 8/8"),
    ("the word improved", lambda r: r["selection"].update(verdict="improved"), "judged higher-rate"),
    ("no significance", lambda r: r["selection"].update(fisher_p=0.5), "significant rate gap"),
])
def test_the_eight_rollout_adopt_scenario_notices_each_way_the_measurement_could_be_wrong(name, edit, needle):
    r = golden_n("format-rules", 8)
    edit(r)
    assert any(needle in p for p in hard_selftest_problems(r)), (name, hard_selftest_problems(r))


def test_a_refusal_scenario_at_eight_rollouts_runs_the_base_extras_not_the_finals_and_must_say_why():
    r = golden_n("forbid-script", 8)
    assert r["rollouts_plan"]["extra_base_run"] == [f"base{i}" for i in range(2, 9)] and r["rollouts_plan"]["extra_final_run"] == []
    r["rollouts_plan"]["extra_skipped"] = None
    assert any("base repeats should run" in p for p in hard_selftest_problems(r))                  # it must say why
    r = golden_n("forbid-script", 8)
    r["rollouts_plan"]["extra_final_run"] = ["agg1.r2"]                                             # a repeat of the final ran
    r["rollouts"].append({"label": "agg1.r2", "passed": False, "n_passed": 2})
    assert any("base repeats should run" in p for p in hard_selftest_problems(r))
    r = golden_n("forbid-script", 8)
    r["rollouts_plan"]["extra_base_run"].pop()                                                      # a base repeat is missing
    r["rollouts"].pop()
    assert any("base repeats should run" in p for p in hard_selftest_problems(r))
    r = golden_n("forbid-script", 8)
    r["pass_rates"]["base"]["n"] = 1                                                                # measured once, as before
    assert any("pass rate of the base should be 0/8" in p for p in hard_selftest_problems(r))


def test_selftest_problems_counts_the_extra_rollouts_as_sessions_that_must_have_run():
    base = {"task_set": "easy", "sessions": {"counts": {"proposer": 3, "solver": 18}}, "ledger": {"P": 3, "R": 4}, "rollouts": [{}] * 18,
            "rollouts_passed": 18, "stages": [{"fell_back": False}] * 3, "teardown": {"scratch_left": False, "docker_leftovers": [], "gate_locked": False},
            "sessions_summary": {"started": 21, "reached_model": 21},
            "rollouts_plan": {"n": 8, "extra_base_run": [f"base{i}" for i in range(2, 9)], "extra_final_run": [f"agg1.r{i}" for i in range(2, 9)]}}
    assert selftest_problems(SweepOutcome(base, 0)) == []
    short = {**base, "rollouts": [{}] * 17, "sessions": {"counts": {"proposer": 3, "solver": 17}}}
    problems = selftest_problems(SweepOutcome(short, 0))
    assert any("17 rollouts ran, expected 18" in p for p in problems) and any("expected 3 and 18" in p for p in problems)
    assert any("extra rollouts failed" in p for p in selftest_problems(SweepOutcome(
        {**base, "rollouts_plan": {**base["rollouts_plan"], "extra_errors": ["base2: died"]}}, 0)))


@pytest.mark.parametrize("name,edit,needle", [
    ("hint missing from the audit", lambda r: [a.update(hint=False) for a in r["_proposer_audit"]], "the audit says hint=False but the sweep ran with hint=True"),
    ("wrong hash", lambda r: r["_proposer_audit"][1].update(hint_sha="0" * 12), "does not hold the block, its hash"),
    ("block not in the system text", lambda r: r["_proposer_audit"][2].update(system_starts_with_block=False), "does not hold the block, its hash"),
    ("a stage is missing", lambda r: r["_proposer_audit"].pop(), "expected one session each for inv1, dom1 and agg1"),
])
def test_the_hint_check_notices_an_audit_that_does_not_match_the_flag(name, edit, needle):
    r = golden_n("format-rules", 1)
    edit(r)
    assert any(needle in p for p in stage1.hint_selftest_problems(r)), (name, stage1.hint_selftest_problems(r))


def test_the_hint_check_notices_a_block_in_an_audit_when_the_hint_was_off():
    r = golden_n("format-rules", 1, hint=False)
    r["_proposer_audit"][0].update(hint_sha="abcdef012345")
    assert any("hint is off but the audit records a block" in p for p in stage1.hint_selftest_problems(r))
    assert stage1.hint_selftest_problems({"proposer_hint": {"on": True}}) == []        # a report with no audit is not judged here


def test_the_selftest_detail_line_says_sessions_hint_and_rates():
    r = golden_n("format-rules", 8)
    r["sessions_summary"] = {"started": 21}
    r["rollouts_plan"]["sessions_planned"] = 21
    r["pass_rates"] = {"final_label": "agg1", **stage1.arm_rates(r["rollouts"], "agg1")}
    line = stage1.selftest_detail(r)
    assert line.startswith("  21 sessions (3 proposer + 18 rollouts; planned 21), proposer hint on, ")
    assert line.endswith("pass rate base 0/8 (0%), final agg1 8/8 (100%)")


def test_the_selftest_profile_gets_exactly_the_session_cap_the_arithmetic_needs(tmp_path):
    assert stage1.selftest_profile(tmp_path, 1).channels["local-ollama"].caps.sessions == 7
    assert stage1.selftest_profile(tmp_path, 1, sessions=21).channels["local-ollama"].caps.sessions == 21
    with pytest.raises(stage1.Stage1Error, match="--rollouts"):
        run_selftest(rollouts=0)


def test_selftest_problems_still_judges_the_easy_set_as_before_and_dispatches_the_hard_one():
    ok = {"sessions": {"counts": {"proposer": 3, "solver": 4}}, "ledger": {"P": 3, "R": 4}, "rollouts": [{}] * 4, "rollouts_passed": 4,
          "stages": [{"fell_back": False}] * 3, "teardown": {"scratch_left": False, "docker_leftovers": [], "gate_locked": False},
          "sessions_summary": {"started": 7, "reached_model": 7}}
    assert selftest_problems(SweepOutcome(ok, 0)) == []
    assert any("a proposer stage fell back" in p for p in selftest_problems(SweepOutcome({**ok, "stages": [{"fell_back": True}] * 3}, 0)))
    assert any("2 of 4 passed" in p for p in selftest_problems(SweepOutcome({**ok, "rollouts_passed": 2}, 0)))
    # a hard report is judged by the hard rules: three failed rollouts are fine only where the scenario says so
    hard = {**ok, "task_set": "hard", "rollouts_passed": 3, **{k: golden("format-rules")[k] for k in ("task", "_scenario", "stages", "rollouts", "selection", "_harness")}}
    assert selftest_problems(SweepOutcome(hard, 0)) == []
    hard["rollouts"][0].update(passed=True)
    assert any("base1 must fail" in p for p in selftest_problems(SweepOutcome(hard, 0)))


# ══ the audited adapter reports the slot table's verdict on every proposal ═════════════

class StubSubstrate:
    name = "cognitive"

    def __init__(self, surface):
        self._s = surface

    def surface(self):
        return self._s


def audited(tmp_path, surface, taskset):
    audit = Audit(tmp_path / "audit.jsonl")
    spec = SimpleNamespace(name=taskset.spec_name, install_mode="workspace_agents_md", obs_trunc=50_000)
    ad = stage1.AuditedAdapter(tmp_path / "root", spec, StubSubstrate(surface), results_root=tmp_path / "res", audit=audit,
                               taskset=taskset)
    return ad, audit


def rows(audit):
    audit.close()
    return [json.loads(x) for x in audit.path.read_text().splitlines()]


def test_the_audited_hard_adapter_audits_an_accepted_and_a_refused_proposal(tmp_path, surface):
    ad, audit = audited(tmp_path, surface, hd.TASKSET)
    tree = {n: (ad.base_harness() / n).read_text() for n in surface.always_on}
    ad.validate_tree({**tree, "systemprompt.md": tree["systemprompt.md"] + "\n" + hd.FORMAT_RULES})
    with pytest.raises(ValueError):
        ad.validate_tree({**tree, "systemprompt.md": "---\nmodel: SECRET-MODEL-LINE\n---\n" + tree["systemprompt.md"]})
    with pytest.raises(ValueError):
        ad.validate_tree({**tree, "guardrails.md": "x\n"})
    ev = [r for r in rows(audit) if r["event"] == "mutation_check"]
    assert [e["ok"] for e in ev] == [True, False, False]
    assert ev[0]["changed"] == ["systemprompt.md"] and ev[0]["slots"] == {"systemprompt.md": "c1"} and "refused" not in ev[0]
    assert "frontmatter" in ev[1]["refused"] and ev[2]["slots"] == {"guardrails.md": "c2"} and "protected" in ev[2]["refused"]
    assert "SECRET-MODEL-LINE" not in audit.path.read_text()


def test_the_audited_easy_adapter_audits_the_slot_table_too_f6(tmp_path, surface):
    """The slot table is enforced for every task set: the easy adapter audits each proposal as a `mutation_check` as well."""
    ad, audit = audited(tmp_path, surface, ss.EASY)
    tree = {n: (ad.base_harness() / n).read_text() for n in surface.always_on}
    ad.validate_tree({**tree, "systemprompt.md": tree["systemprompt.md"] + "\nCheck every sign.\n"})
    with pytest.raises(ValueError, match="slot c2"):
        ad.validate_tree({**tree, "guardrails.md": "anything\n"})
    with pytest.raises(ValueError, match="slot c7"):
        ad.validate_tree({**tree, "scripts/x.py": "print(1)\n"})
    with pytest.raises(ValueError, match="systemprompt.md is missing or empty"):
        ad.validate_tree({**tree, "systemprompt.md": ""})
    ev = [r for r in rows(audit) if r["event"] == "mutation_check"]
    assert [e["ok"] for e in ev] == [True, False, False, False]
    assert ev[2]["slots"] == {"scripts/x.py": "c7"}


# ══ the end-to-end selftests, on the real rootless daemon ═════════════════════════════

def run_hard(tmp_path, scenario, verified, proposer_image, up) -> SweepOutcome:
    base = tmp_path / "st"
    base.mkdir()
    base.chmod(0o755)
    return run_selftest(base=base, out=io.StringIO(), task_set="hard", scenario=scenario)


def common_clean(r):
    assert (r["ledger"]["P"], r["ledger"]["R"]) == (3, 4) and r["budget_identity"]["ok"]
    assert r["sessions"]["counts"] == {"proposer": 3, "solver": 4, "total": 7}
    assert (r["cli_sessions_started"], r["model_sessions_reached"]) == (7, 7)
    assert r["sessions_summary"]["exit_codes"] == {"0": 7} and r["sessions_summary"]["denied_hosts"] == {}
    assert r["approved_for_real_model"] is False and r["real_model_call"] is False and r["task_set"] == "hard"
    assert r["teardown"]["docker_leftovers"] == [] and r["teardown"]["scratch_left"] is False and r["teardown"]["gate_locked"] is False
    assert r["_fake_endpoint_rejected"] == [] and sorted(x["label"] for x in r["rollouts"]) == ["agg1", "base1", "dom1", "inv1"]


@pytest.mark.requires_rootless_docker
def test_a_scripted_proposer_edit_is_harvested_checked_run_as_a_candidate_and_adopted(tmp_path, verified, proposer_image, up):
    outcome = run_hard(tmp_path, "format-rules", verified, proposer_image, up)
    r = outcome.report
    assert outcome.exit_code == 0 and selftest_problems(outcome) == [], r
    common_clean(r)
    inst = hd.instance(TASK)
    by = {x["label"]: x for x in r["rollouts"]}
    # the base fails, the weak reader's way: the right shape and the wrong total
    assert (by["base1"]["passed"], by["base1"]["n_passed"]) == (False, 2)
    assert by["base1"]["answer_excerpt"].strip() == f"Total: {hd.naive_total(inst.input_text)}" != inst.expected_line
    # every candidate, given the rules, passes with the exact expected line
    for label in ("inv1", "dom1", "agg1"):
        assert (by[label]["passed"], by[label]["n_passed"]) == (True, 3), label
        assert by[label]["answer_excerpt"].strip() == inst.expected_line
    assert r["rollouts_passed"] == 3 and r["rollouts_failed"] == 1
    # the stages were accepted with a changed harness
    assert [(s["label"], s["fell_back"], s["kept_base"], s["error"]) for s in r["stages"]] == [
        ("inv1", False, False, ""), ("dom1", False, False, ""), ("agg1", False, False, "")]
    # harvested: the harnesses on disk differ from the base in systemprompt.md only, and carry the rules
    ev = r["_harness"]
    assert ev["changed"] == {x: ["systemprompt.md"] for x in ("inv1", "dom1", "agg1")} and ev["base_has_rules"] is False
    assert ev["rules_in"] == ["inv1", "dom1", "agg1"] and ev["slot_table_problems"] == {"inv1": [], "dom1": [], "agg1": []}
    # checked: the pipeline audited an accepted slot-table verdict for each of the three proposals
    assert ev["mutation_checks"] == [{"ok": True, "changed": ["systemprompt.md"], "slots": {"systemprompt.md": "c1"}}] * 3
    # adopted: upstream's trivial selection delivered agg1, and the harness it delivered is not the base
    sel = r["selection"]
    assert (sel["ok"], sel["trivial"], sel["chosen_label"], sel["chosen_is_base"], sel["chosen_tree_is_base"], sel["adopted"]) == (
        True, True, "agg1", False, False, True)
    assert (sel["base_passed"], sel["chosen_passed"], sel["verdict"]) == (False, True, "improved")
    run_dir = Path(r["_run_dir"])
    audit = [json.loads(x) for x in (run_dir / "audit.jsonl").read_text().splitlines()]
    assert [e["event"] for e in audit].count("mutation_check") == 3
    # the proposer's rules never reached the audit or the report (those hold events and numbers, not text)
    blob = (run_dir / "audit.jsonl").read_text() + (run_dir / "report.json").read_text()
    assert "Parentheses mean a negative" not in blob and "careful bookkeeper" not in blob
    # a harvested edit is a regular, non-executable file the container user wrote (the harvest would have refused anything else)
    candidate = next((run_dir / "upstream-runs").glob("*/*/harnesses/agg1/systemprompt.md"))
    assert hd.FORMAT_RULES.strip() in candidate.read_text() and candidate.stat().st_mode & 0o111 == 0


@pytest.mark.requires_rootless_docker
@pytest.mark.parametrize("scenario", sorted(stage1.REFUSED_BY))
def test_a_forbidden_proposer_edit_is_refused_and_the_base_is_kept(tmp_path, verified, proposer_image, up, scenario):
    outcome = run_hard(tmp_path, scenario, verified, proposer_image, up)
    r = outcome.report
    assert outcome.exit_code == 0 and selftest_problems(outcome) == [], r
    common_clean(r)
    layer, needle = stage1.REFUSED_BY[scenario], stage1.REFUSAL_TEXT[scenario]
    # every stage fell back to the base, and said why in the words of the layer that refused it
    assert all(s["fell_back"] and needle in s["error"] for s in r["stages"]), r["stages"]
    if layer == "harvest":
        assert all("nothing was copied back" in s["error"] for s in r["stages"])
    ev = r["_harness"]
    assert ev["changed"] == {"inv1": [], "dom1": [], "agg1": []} and ev["rules_in"] == []
    if layer == "harvest":
        assert ev["mutation_checks"] == []                                  # the harvest refused before the slot table was asked
    else:
        assert len(ev["mutation_checks"]) == 3 and all(c["ok"] is False for c in ev["mutation_checks"])
    # nothing improved: a base harness fails this task, so every rollout fails, and the base is what was delivered
    assert r["rollouts_passed"] == 0 and r["rollouts_failed"] == 4
    sel = r["selection"]
    assert (sel["chosen_label"], sel["chosen_is_base"], sel["chosen_tree_is_base"], sel["adopted"], sel["verdict"]) == (
        "agg1", False, True, False, "base-kept")
    # the forbidden file is nowhere in the harness the candidates ran with
    run_dir = Path(r["_run_dir"])
    for p in (run_dir / "upstream-runs").glob("*/*/harnesses/*/scripts/helper.py"):
        pytest.fail(f"a refused script reached a harness: {p}")
    for p in (run_dir / "upstream-runs").glob("*/*/harnesses/*/guardrails.md"):
        assert p.read_text() == hd.BASE_GUARDRAILS


@pytest.mark.requires_rootless_docker
def test_the_easy_selftest_is_unchanged_reports_what_selection_did_and_audits_the_slot_table(tmp_path, verified, proposer_image, up):
    base = tmp_path / "st"
    base.mkdir()
    base.chmod(0o755)
    outcome = run_selftest(base=base, out=io.StringIO())
    r = outcome.report
    assert selftest_problems(outcome) == [], r
    assert r["task_set"] == "easy" and r["task"] == "household/sum-1" and r["_scenario"] == "sign-check"
    assert all(x["passed"] is True for x in r["rollouts"]) and r["rollouts_passed"] == 4
    # the easy proposer edits systemprompt.md too, so an edited harness is adopted, but nothing improved: the base passed
    sel = r["selection"]
    assert (sel["adopted"], sel["verdict"], sel["chosen_label"]) == (True, "unchanged", "agg1")
    assert r["_harness"]["changed"] == {x: ["systemprompt.md"] for x in ("inv1", "dom1", "agg1")}
    # the slot table is enforced for every set (F6): each of the three proposals was audited, accepted
    assert [m["ok"] for m in r["_harness"]["mutation_checks"]] == [True, True, True]
    assert all(m["slots"] == {"systemprompt.md": "c1"} for m in r["_harness"]["mutation_checks"])


@pytest.mark.requires_rootless_docker
def test_the_selftest_subcommand_runs_the_hard_scenarios_and_says_what_each_proved(verified, proposer_image, up, capsys):
    code = stage1.main(["selftest", "--task-set", "hard", "--scenario", "format-rules"])
    out = capsys.readouterr()
    assert code == 0, out
    assert ("selftest[hard/format-rules] passed: rollouts 3/4 correct, selection improved (final agg1, edited harness adopted), "
            "teardown clean: True") in out.out


@pytest.mark.requires_rootless_docker
def test_eight_rollouts_and_the_hint_run_21_sessions_and_report_pass_rates_not_a_win(tmp_path, verified, proposer_image, up):
    """Stage 1c on the real daemon: 3 proposers + (4 upstream + 7 more base + 7 more final) = 21 sessions, all through the
    grant and budget; the hint reaches the three proposer audits and no solver; the report holds both pass rates."""
    base = tmp_path / "st"
    base.mkdir()
    base.chmod(0o755)
    outcome = run_selftest(base=base, out=io.StringIO(), task_set="hard", scenario="format-rules", rollouts=8, proposer_hint="on")
    r = outcome.report
    assert outcome.exit_code == 0 and selftest_problems(outcome) == [], r
    assert (r["ledger"]["P"], r["ledger"]["R"]) == (3, 4) and r["budget_identity"]["ok"]          # upstream's own ledger is unchanged
    assert r["sessions"]["counts"] == {"proposer": 3, "solver": 18, "total": 21}
    assert (r["cli_sessions_started"], r["model_sessions_reached"]) == (21, 21)
    assert r["grant"]["max_proposer_sessions"] == 3 and r["grant"]["max_solver_sessions"] == 18
    assert r["channel"]["caps"]["sessions"] == 21 and r["teardown"]["docker_leftovers"] == [] and r["teardown"]["scratch_left"] is False
    plan = r["rollouts_plan"]
    assert (plan["n"], plan["rollouts_planned"], plan["sessions_planned"]) == (8, 18, 21)
    assert plan["extra_base_run"] == [f"base{i}" for i in range(2, 9)] and plan["extra_final_run"] == [f"agg1.r{i}" for i in range(2, 9)]
    assert plan["extra_errors"] == [] and plan["extra_skipped"] is None
    # per-rollout pass/fail, and the arm each belongs to
    arms = {x["label"]: x["arm"] for x in r["rollouts"]}
    assert arms["inv1"] == arms["dom1"] == "other" and arms["base1"] == arms["base8"] == "base" and arms["agg1"] == arms["agg1.r8"] == "final"
    assert all(x["passed"] is (x["arm"] == "final" or x["label"] in ("inv1", "dom1")) for x in r["rollouts"])
    # the pass-rate summary and a verdict that is a rate, with the test that says whether to believe it
    pr = r["pass_rates"]
    assert (pr["final_label"], pr["base"]["passed"], pr["base"]["n"], pr["final"]["passed"], pr["final"]["n"]) == ("agg1", 0, 8, 8, 8)
    sel = r["selection"]
    assert sel["verdict"] == "higher-rate" and sel["base_rate"] == {"passed": 0, "n": 8} and sel["chosen_rate"] == {"passed": 8, "n": 8}
    assert sel["fisher_p"] == pytest.approx(2 / 12870) and "improved" not in sel["verdict"]
    assert stage1.pass_rate_line(r) == "pass rate base 0/8 (0%), final agg1 8/8 (100%)\n"
    assert stage1.selection_line(r).startswith("selection higher-rate: final agg1 (an edited harness was adopted); pass rate base 0/8, agg1 8/8;")
    # the hint: in the report and in each of the three proposer audits, never the solver's
    h = r["proposer_hint"]
    assert h["on"] is True and re.fullmatch(r"[0-9a-f]{12}", h["sha"])
    assert [(a["hint"], a["hint_sha"], a["block_matches_sha"], a["system_starts_with_block"]) for a in r["_proposer_audit"]] == [
        (True, h["sha"], True, True)] * 3
    run_dir = Path(r["_run_dir"])
    assert all("ENVIRONMENT RULES" not in p.read_text(errors="replace") for p in (run_dir / "results").rglob("*.md"))
    events = [json.loads(x) for x in (run_dir / "audit.jsonl").read_text().splitlines()]
    kinds = [e["event"] for e in events]
    assert kinds.count("session_start") == kinds.count("session_end") == 21 and kinds.count("rollout_verdict") == 18
    assert kinds.count("extra_rollouts_start") == kinds.count("extra_rollouts_end") == 1 and "session_refused" not in kinds
    blob = (run_dir / "audit.jsonl").read_text() + (run_dir / "report.json").read_text()
    assert "ENVIRONMENT RULES" not in blob and "Parentheses mean a negative" not in blob     # the block is in the proposer audit only


@pytest.mark.requires_rootless_docker
def test_with_no_edit_adopted_only_the_base_extras_run_and_a_hint_off_sweep_says_so(tmp_path, verified, proposer_image, up):
    base = tmp_path / "st"
    base.mkdir()
    base.chmod(0o755)
    outcome = run_selftest(base=base, out=(printed := io.StringIO()), task_set="hard", scenario="forbid-script", rollouts=8,
                           proposer_hint="off")
    r = outcome.report
    assert outcome.exit_code == 0 and selftest_problems(outcome) == [], r
    assert r["sessions"]["counts"] == {"proposer": 3, "solver": 11, "total": 14}                # 4 + the 7 base repeats: 14 of the 21 the plan allowed
    assert r["rollouts_plan"]["sessions_planned"] == 21 and "no edited harness was adopted" in r["rollouts_plan"]["extra_skipped"]
    assert r["proposer_hint"] == {"on": False, "source": "--proposer-hint", "sha": None}
    assert [a["hint"] for a in r["_proposer_audit"]] == [False] * 3
    assert r["selection"]["verdict"] == "base-kept" and r["pass_rates"]["base"]["n"] == 8
    assert r["rollouts_plan"]["extra_base_run"] == [f"base{i}" for i in range(2, 9)] and r["rollouts_plan"]["extra_final_run"] == []
    assert "extra rollouts: no edited harness was adopted" in printed.getvalue()
    assert "pass rate base 0/8 (0%); no edited harness adopted" in printed.getvalue()
    assert "pass rate base 0/1 (0%), final agg1 0/1 (0%)  (one sample each: not a measurement)" in printed.getvalue()


@pytest.mark.requires_rootless_docker
def test_the_selftest_subcommand_takes_rollouts_and_the_hint_and_prints_both(verified, proposer_image, up, capsys):
    code = stage1.main(["selftest", "--task-set", "hard", "--scenario", "format-rules", "--rollouts", "8", "--proposer-hint", "on"])
    out = capsys.readouterr()
    assert code == 0, out
    assert ("selftest[hard/format-rules] passed: rollouts 10/18 correct, selection higher-rate (final agg1, edited harness adopted), "
            "teardown clean: True") in out.out
    assert "21 sessions (3 proposer + 18 rollouts; planned 21), proposer hint on, " in out.out
    assert "pass rate base 0/8 (0%), final agg1 8/8 (100%)" in out.out


def test_the_selftest_subcommand_refuses_an_unknown_scenario_before_starting_anything(capsys):
    with pytest.raises(SystemExit):
        stage1.main(["selftest", "--task-set", "hard", "--scenario", "rm-rf"])
    assert "invalid choice" in capsys.readouterr().err
    with pytest.raises(stage1.Stage1Error, match="unknown selftest scenario"):
        run_selftest(task_set="hard", scenario="rm-rf")
    with pytest.raises(stage1.Stage1Error, match="unknown task set"):
        run_selftest(task_set="medium")
