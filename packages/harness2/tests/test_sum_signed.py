"""`sum-signed`: the task set, the verifier in two implementations, and the duck-typed adapter (slice 3b WS-E).

The verdict is computed twice, by `VERIFY_SH` (busybox sh + awk, in a real rootless container) and by
`grade_answer` (Python). Every case below runs through both, and both must equal the hand-written expectation.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from cognitive_harness2 import sandbox, sum_signed as ss
from cognitive_harness2.substrate import StagedInstall

FIRST = "household/sum-1"


def dec_total(amounts) -> str:
    """An independent computation of the expected total (Decimal, not the module's cents arithmetic)."""
    t = sum((Decimal(a) for a in amounts), Decimal(0))
    return "0.00" if t == 0 else f"{t:.2f}"


# ── the task set ──────────────────────────────────────────────────────────────────

def test_there_are_three_domains_of_two_tasks_in_a_stable_order():
    assert list(ss.INSTANCES) == [f"{d}/sum-{n}" for d in ("household", "retail", "payroll") for n in (1, 2)]
    assert {i.domain for i in ss.INSTANCES.values()} == {"household", "retail", "payroll"}


def test_instances_are_deterministic_and_well_formed():
    again = ss._build_instances()
    assert again == dict(ss.INSTANCES)
    for tid, inst in ss.INSTANCES.items():
        assert len(inst.amounts) == 5 and all(re.fullmatch(r"-?[0-9]+\.[0-9]{2}", a) for a in inst.amounts)
        assert inst.expected_total == dec_total(inst.amounts), tid
        assert any(a.startswith("-") for a in inst.amounts) and any(not a.startswith("-") for a in inst.amounts)
        assert "0.00" not in inst.amounts
        positives = sum((Decimal(a) for a in inst.amounts if Decimal(a) > 0), Decimal(0))
        assert Decimal(inst.expected_total) != positives, f"{tid}: a positives-only sum must be wrong"
        assert inst.input_text == "".join(a + "\n" for a in inst.amounts)


def test_the_instances_are_pinned():
    # sha256-derived, so they cannot drift with the Python version; a change here is a change of the task.
    got = {tid: (inst.amounts, inst.expected_total) for tid, inst in ss.INSTANCES.items()}
    assert got["household/sum-1"] == (("373.25", "-47.63", "-358.53", "-25.41", "-101.98"), "-160.30")
    assert got["household/sum-2"] == (("242.42", "-196.67", "98.90", "-287.94", "-106.75"), "-250.04")
    assert got["payroll/sum-1"] == (("247.44", "-0.72", "-89.70", "-20.40", "108.55"), "245.17")
    assert hashlib.sha256(json.dumps(got, sort_keys=True).encode()).hexdigest()[:16] == "9684d2bd7fed2679"


def test_an_unknown_task_is_refused():
    for bad in ("household/sum-9", "../etc", "", None, 3):
        with pytest.raises(ValueError):
            ss.instance(bad)


def test_the_prompt_names_the_file_and_the_form_and_never_the_answer():
    for tid, inst in ss.INSTANCES.items():
        p = ss.task_prompt_text(tid)
        assert "input.txt" in p and "output/answer.txt" in p and "Total: " in p
        assert inst.expected_total not in p and "verify" not in p.lower()
        assert not any(a in p for a in inst.amounts)


def test_expected_total_is_exact_cents():
    assert ss.expected_total(["0.10", "0.20"]) == "0.30"
    assert ss.expected_total(["5.00", "-5.00"]) == "0.00"
    assert ss.expected_total(["1.00", "-8.05"]) == "-7.05"
    assert ss.expected_total(["-0.01"]) == "-0.01"
    with pytest.raises(ValueError):
        ss.expected_total(["1.5"])


# ── cases run through BOTH implementations ───────────────────────────────────────────

MIXED = ("100.25", "-20.10", "5.00", "-0.05", "12.50")          # 97.60
ZERO = ("5.00", "-5.00", "0.01", "-0.01")                       # 0.00
NEG = ("1.00", "-8.05")                                         # -7.05
BIG = ("9999999999.99", "9999999999.99", "-0.01")               # 19999999999.97, past 32-bit integers
SMALL = ("-0.01",)

# (id, amounts, answer template or None for "no file", expected n_passed). {E} is the expected total.
CASES = [
    ("exact", MIXED, b"Total: {E}\n", 3),
    ("no-newline", MIXED, b"Total: {E}", 3),
    ("crlf", MIXED, b"Total: {E}\r\n", 3),
    ("padding", MIXED, b"\n\n  \t Total: {E}  \t\r\n\n\n", 3),
    ("negative", NEG, b"Total: {E}\n", 3),
    ("zero", ZERO, b"Total: {E}\n", 3),
    ("big", BIG, b"Total: {E}\n", 3),
    ("small-negative", SMALL, b"Total: {E}\n", 3),
    ("missing-file", MIXED, None, 0),
    ("empty-file", MIXED, b"", 1),
    ("blank-only", MIXED, b" \n\t\r\n", 1),
    ("extra-text-after", MIXED, b"Total: {E} USD\n", 1),
    ("extra-text-before", MIXED, b"The Total: {E}\n", 1),
    ("two-lines", MIXED, b"Total: {E}\nTotal: {E}\n", 1),
    ("second-line-junk", MIXED, b"Total: {E}\nthanks\n", 1),
    ("no-space", MIXED, b"Total:{E}\n", 1),
    ("two-spaces", MIXED, b"Total:  {E}\n", 1),
    ("tab-separator", MIXED, b"Total:\t{E}\n", 1),
    ("lower-case", MIXED, b"total: {E}\n", 1),
    ("bare-number", MIXED, b"{E}\n", 1),
    ("plus-sign", MIXED, b"Total: +{E}\n", 1),
    ("thousands-comma", BIG, b"Total: 19,999,999,999.97\n", 1),
    ("one-decimal", MIXED, b"Total: 97.6\n", 1),
    ("three-decimals", MIXED, b"Total: 97.600\n", 1),
    ("no-decimals", MIXED, b"Total: 98\n", 1),
    ("exponent", MIXED, b"Total: 9.76e1\n", 1),
    ("rounded-down", MIXED, b"Total: 97.59\n", 2),
    ("rounded-up", MIXED, b"Total: 97.61\n", 2),
    ("wrong-sign", NEG, b"Total: 7.05\n", 2),
    ("positives-only", MIXED, b"Total: 117.75\n", 2),
    ("leading-zeros", MIXED, b"Total: 0097.60\n", 2),
    ("zero-wrong-sign", ZERO, b"Total: -0.00\n", 2),
    ("zero-leading-zeros", ZERO, b"Total: 00.00\n", 2),
    ("huge-number", MIXED, b"Total: 99999999999999999999999999999999999999.99\n", 2),
    ("huge-digits-matching-prefix", BIG, b"Total: 19999999999.970000000000000000001\n", 1),
    ("arabic-indic-digits", MIXED, "Total: ٩٧.٦٠\n".encode(), 1),
    ("fullwidth-digits", MIXED, "Total: ９７.６０\n".encode(), 1),
    ("unicode-minus", NEG, "Total: −7.05\n".encode(), 1),
    ("bom-prefix", MIXED, b"\xef\xbb\xbfTotal: {E}\n", 1),
    ("nbsp-suffix", MIXED, b"Total: {E}\xc2\xa0\n", 1),
    ("form-feed-suffix", MIXED, b"Total: {E}\x0c\n", 1),
    ("invalid-utf8", MIXED, b"Total: {E}\xff\n", 1),
    ("nul-byte", MIXED, b"Total: {E}\n\x00", 0),
    ("nul-inside", MIXED, b"Total: {E}\x00 junk\n", 0),
    ("oversize", MIXED, b"Total: {E}\n" + b" " * 5000, 0),
    ("just-under-the-cap", MIXED, b"Total: {E}\n" + b" " * 4070, 3),
]


def build(case):
    cid, amounts, template, n = case
    e = dec_total(amounts)
    assert e == ss.expected_total(amounts)
    raw = None if template is None else template.replace(b"{E}", e.encode())
    return raw, e, "".join(a + "\n" for a in amounts), n


@pytest.fixture(scope="module")
def runner(verified):
    return sandbox.DockerRunner(verified.path, docker_host=verified.host)


@pytest.fixture(scope="module")
def container_verdicts(runner):
    def one(case):
        raw, _e, input_text, _n = build(case)
        return case[0], ss.verify_in_container(runner, FIRST, raw, input_text=input_text)
    with ThreadPoolExecutor(max_workers=4) as pool:
        return dict(pool.map(one, CASES))


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_python_verdict_matches_the_hand_written_expectation(case):
    raw, e, _in, n = build(case)
    got = ss.grade_answer(raw, e)
    assert got == {"n_passed": n, "n_criteria": 3, "all_pass": n == 3}


@pytest.mark.requires_rootless_docker
@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_container_verdict_matches_the_python_verdict_on_the_same_case(case, container_verdicts):
    raw, e, _in, n = build(case)
    assert container_verdicts[case[0]] == ss.grade_answer(raw, e) == {"n_passed": n, "n_criteria": 3, "all_pass": n == 3}


@pytest.mark.requires_rootless_docker
def test_the_container_verifier_on_the_real_instances(runner):
    for tid, inst in ss.INSTANCES.items():
        right = inst.expected_line.encode() + b"\n"
        assert ss.verify_in_container(runner, tid, right) == {"n_passed": 3, "n_criteria": 3, "all_pass": True}
        naive = sum((Decimal(a) for a in inst.amounts if Decimal(a) > 0), Decimal(0))
        wrong = f"Total: {naive:.2f}\n".encode()
        assert ss.verify_in_container(runner, tid, wrong) == ss.grade_answer(wrong, inst.expected_total)
        assert ss.verify_in_container(runner, tid, wrong)["all_pass"] is False


@pytest.mark.requires_rootless_docker
def test_the_container_verifier_gives_no_verdict_for_an_unusable_input(runner):
    for bad in ("", "abc\n", "1.5\n", "1.00\nbad\n"):
        with pytest.raises(RuntimeError, match="verifier container failed"):
            ss.verify_in_container(runner, FIRST, b"Total: 1.00\n", input_text=bad)


@pytest.mark.requires_rootless_docker
def test_the_verifier_runs_under_the_hardened_runner_with_no_network(verified):
    # the script is plain busybox: it does not need (and cannot use) a network or a writable root
    argv = sandbox.DockerRunner(verified.path, docker_host=verified.host)._argv(
        sandbox.SandboxSpec(command=("sh", "verify.sh")), Path("/x"), "n", verified.host)
    assert "--network" in argv and argv[argv.index("--network") + 1] == "none"
    assert "--read-only" in argv


def test_the_verifier_script_is_posix_sh_with_no_external_dependency():
    assert ss.VERIFY_SH.startswith("#!/bin/sh\n")
    for banned in ("python", "bash", "curl", "wget", "nc ", "/dev/tcp"):
        assert banned not in ss.VERIFY_SH
    assert set(re.findall(r"\b(?:awk|tr|wc|sh)\b", ss.VERIFY_SH)) <= {"awk", "tr", "wc", "sh"}


def test_parse_verdict_is_strict():
    ok = '{"n_passed":2,"n_criteria":3,"all_pass":false}\n'
    assert ss.parse_verdict(ok) == {"n_passed": 2, "n_criteria": 3, "all_pass": False}
    for bad in ("", "x", "[]", '{"n_passed":4,"n_criteria":3,"all_pass":false}', '{"n_passed":3,"n_criteria":3}',
                '{"n_passed":true,"n_criteria":3,"all_pass":false}', '{"n_passed":1,"n_criteria":2,"all_pass":false}',
                '{"n_passed":3,"n_criteria":3,"all_pass":"yes"}',
                '{"n_passed":3,"n_criteria":3,"all_pass":true,"extra":1}'):
        with pytest.raises(ValueError):
            ss.parse_verdict(bad)


# ── the adapter (duck typed) ─────────────────────────────────────────────────────────

ALWAYS_ON = ("systemprompt.md", "guardrails.md", "LongTermMEMORY.md", "tool_descriptions/tool_guidance.md")


class FakeSubstrate:
    """The four things the adapter uses of a substrate, recording calls."""
    name = "cognitive"

    def __init__(self, results_root: Path, *, write_metrics=True, answer: bytes | None = b"Total: 0.00\n"):
        self.results_root, self.write_metrics, self.answer = results_root, write_metrics, answer
        self.executed: list = []
        self.released: list = []
        self.materialised: list = []

    def surface(self):
        return SimpleNamespace(name="opencode", always_on=ALWAYS_ON)

    def materialise(self, tree_dir, spec, ctx):
        self.materialised.append(Path(tree_dir))
        return StagedInstall(Path(tree_dir), "fp", {"install_mode": spec.install_mode})

    def execute(self, task_ref, install, run_ids, timeout):
        self.executed.append((task_ref, install, list(run_ids), timeout))
        for rid in run_ids:
            d = self.results_root / rid
            d.mkdir(parents=True, exist_ok=True)
            if self.answer is not None:
                (d / "output").mkdir(exist_ok=True)
                (d / "output" / "answer.txt").write_bytes(self.answer)
            if self.write_metrics:
                (d / "metrics.json").write_text("{}")
        return [self.results_root / r for r in run_ids]

    def release(self, install):
        self.released.append(install)

    def read_trajectory(self, stream_path, *, obs_trunc, max_chars, args_trunc=400):
        return f"traj:{Path(stream_path).name}:{obs_trunc}:{max_chars}"


@pytest.fixture
def adapter(tmp_path):
    results = tmp_path / "results"
    sub = FakeSubstrate(results)
    spec = SimpleNamespace(name=ss.SPEC_NAME, install_mode="workspace_agents_md", obs_trunc=50_000)
    return ss.SumSignedAdapter(tmp_path / "root", spec, sub, results_root=results, timeout=77)


def test_the_adapter_writes_sources_and_a_base_harness(adapter):
    for tid, inst in ss.INSTANCES.items():
        assert (adapter.task_files(tid) / "input.txt").read_text() == inst.input_text
    base = adapter.base_harness()
    assert (base / "systemprompt.md").read_text() == ss.BASE_SYSTEM_PROMPT
    assert all((base / n).is_file() for n in ALWAYS_ON)
    assert adapter.tasks() == list(ss.INSTANCES) and adapter.tasks("val") == list(ss.INSTANCES)
    assert adapter.domain_of(FIRST) == "household"
    assert adapter.task_prompt(FIRST) == ss.task_prompt_text(FIRST)
    with pytest.raises(ValueError):
        adapter.task_files("nope/sum-1")


def test_validate_tree_lists_every_problem_in_prose(adapter):
    adapter.validate_tree({"systemprompt.md": "Add them up.\nWrite to output/answer.txt.\n"})
    with pytest.raises(ValueError) as e:
        adapter.validate_tree({"systemprompt.md": "  \n", "guardrails.md": "Save it in answer.txt\nok\n"})
    msg = str(e.value)
    assert "systemprompt.md is missing or empty" in msg and "guardrails.md:1" in msg
    with pytest.raises(ValueError):
        adapter.validate_tree({"systemprompt.md": 3})  # type: ignore[dict-item]


def test_execute_runs_the_base_harness_and_marks_the_run_complete(adapter):
    d = adapter.execute(FIRST, None, "run-a")
    sub = adapter.substrate
    assert d == adapter.results_root / "run-a"
    task, install, ids, timeout = sub.executed[0]
    assert (task, ids, timeout) == (FIRST, ["run-a"], 77)
    assert install.extras == {"harness": "base"} and install.fingerprint == "base"
    assert install.staging_dir == adapter.base_harness()
    assert sub.released == [] and sub.materialised == []
    mark = json.loads((d / ".adapt_complete").read_text())
    assert mark["run_id"] == "run-a" and mark["harness"] == "base" and mark["version"] == 1
    assert mark["output_fingerprint"] == ss.fingerprint(d / "output")
    assert adapter.delivered(d) and adapter.artifacts_intact(d)


def test_execute_is_idempotent_by_run_id_and_rerolls_a_changed_output(adapter):
    d = adapter.execute(FIRST, None, "run-a")
    assert adapter.execute(FIRST, None, "run-a") == d and len(adapter.substrate.executed) == 1
    (d / "output" / "answer.txt").write_text("tampered")
    adapter.execute(FIRST, None, "run-a")
    assert len(adapter.substrate.executed) == 2


def test_execute_stages_and_releases_a_modified_harness(adapter, tmp_path):
    h = tmp_path / "h"
    h.mkdir()
    (h / "systemprompt.md").write_text("x\n")
    d = adapter.execute(FIRST, h, "run-b", timeout=30)
    sub = adapter.substrate
    assert sub.materialised == [h]
    _task, install, _ids, timeout = sub.executed[0]
    assert timeout == 30 and install.extras["harness"] == "staged"
    assert sub.released == [install]
    assert json.loads((d / ".adapt_complete").read_text())["harness"] == ss.fingerprint(h)


def test_a_run_without_metrics_is_not_marked_and_says_why(adapter):
    adapter.substrate.write_metrics = False
    adapter.results_root.mkdir(parents=True)

    orig = adapter.substrate.execute

    def execute(task_ref, install, run_ids, timeout):
        out = orig(task_ref, install, run_ids, timeout)
        (adapter.results_root / run_ids[0] / "error.json").write_text(json.dumps({"error": "out of budget"}))
        return out
    adapter.substrate.execute = execute
    with pytest.raises(RuntimeError, match="did not produce metrics.json.*out of budget"):
        adapter.execute(FIRST, None, "run-c")
    assert not (adapter.results_root / "run-c" / ".adapt_complete").exists()


def test_a_substrate_error_releases_the_staged_install_and_propagates(adapter, tmp_path):
    h = tmp_path / "h2"
    h.mkdir()
    (h / "systemprompt.md").write_text("x\n")

    def boom(*a, **k):
        raise PermissionError("no grant")
    adapter.substrate.execute = boom
    with pytest.raises(PermissionError):
        adapter.execute(FIRST, h, "run-d")
    assert len(adapter.substrate.released) == 1


def test_execute_refuses_an_unknown_task_before_touching_the_substrate(adapter):
    with pytest.raises(ValueError):
        adapter.execute("nope", None, "r")
    assert adapter.substrate.executed == []


def test_safe_rmtree_stays_inside_the_results_root(adapter, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(ValueError, match="not inside the results root"):
        adapter._safe_rmtree(outside)
    with pytest.raises(ValueError):
        adapter._safe_rmtree(adapter.results_root)
    assert outside.exists()


def test_trajectory_reads_the_stream_through_the_substrate_uncapped(adapter):
    d = adapter.execute(FIRST, None, "run-t")
    assert adapter.trajectory(d, obs_trunc=1234).startswith("(no trajectory recorded for ")
    (d / "opencode_session.jsonl").write_text("{}\n")
    assert adapter.trajectory(d, obs_trunc=1234) == f"traj:opencode_session.jsonl:1234:{10**9}"
    assert adapter.trajectory(tmp := d / "nothing", obs_trunc=5).startswith("(no trajectory recorded for ")
    assert tmp.name in adapter.trajectory(tmp, obs_trunc=5)


def test_deliverables_are_the_contents_of_output(adapter):
    d = adapter.execute(FIRST, None, "run-e")
    assert adapter.deliverable_files(d) == {"answer.txt": "Total: 0.00\n"}
    assert adapter.deliverable_text(d) == "----- FILE: answer.txt -----\nTotal: 0.00"
    empty = adapter.results_root / "empty"
    empty.mkdir()
    assert adapter.deliverable_text(empty) == "(this run produced NO deliverables)"
    assert not adapter.delivered(empty)


def test_score_is_pure_code_over_the_run_dir(adapter, tmp_path):
    def run_dir(name, answer=None, *, at="output/answer.txt"):
        d = tmp_path / name
        d.mkdir()
        if answer is not None:
            p = d / at
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(answer)
        return d

    inst = ss.instance(FIRST)
    assert adapter.score(run_dir("ok", inst.expected_line.encode()), FIRST) == {"n_passed": 3, "n_criteria": 3, "all_pass": True}
    assert adapter.score(run_dir("none"), FIRST) == {"n_passed": 0, "n_criteria": 3, "all_pass": False}
    # an answer written outside output/ is no answer
    outside = run_dir("outside", inst.expected_line.encode(), at="answer.txt")
    assert adapter.score(outside, FIRST)["n_passed"] == 0
    elsewhere = run_dir("elsewhere", inst.expected_line.encode(), at="workspace/answer.txt")
    assert adapter.score(elsewhere, FIRST)["n_passed"] == 0
    # a symlink in place of the file is no answer either
    d = run_dir("link")
    (d / "output").mkdir()
    real = tmp_path / "real.txt"
    real.write_text(inst.expected_line)
    os.symlink(real, d / "output" / "answer.txt")
    assert adapter.score(d, FIRST)["n_passed"] == 0
    # a judge that cannot judge says None, never a zero
    assert adapter.score(run_dir("x", b"Total: 1.00"), "no/such-task") is None


def test_score_never_starts_a_process(adapter, tmp_path, monkeypatch):
    def trap(*a, **k):
        raise AssertionError("score must not start a process")
    monkeypatch.setattr(subprocess, "run", trap)
    monkeypatch.setattr(subprocess, "Popen", trap)
    d = tmp_path / "r"
    (d / "output").mkdir(parents=True)
    (d / "output" / "answer.txt").write_text(ss.instance(FIRST).expected_line)
    assert adapter.score(d, FIRST)["all_pass"] is True


def test_container_score_needs_a_verifier(adapter, tmp_path):
    with pytest.raises(RuntimeError, match="no verifier runner"):
        adapter.container_score(tmp_path, FIRST)


@pytest.mark.requires_rootless_docker
def test_container_score_agrees_with_score_on_a_run_dir(tmp_path, runner):
    sub = FakeSubstrate(tmp_path / "res")
    spec = SimpleNamespace(name=ss.SPEC_NAME, install_mode="workspace_agents_md", obs_trunc=50_000)
    ad = ss.SumSignedAdapter(tmp_path / "root", spec, sub, results_root=tmp_path / "res", verifier=runner)
    inst = ss.instance(FIRST)
    for body in (inst.expected_line + "\n", "Total: 1.00\n", "junk"):
        d = tmp_path / f"d{abs(hash(body))}"
        (d / "output").mkdir(parents=True)
        (d / "output" / "answer.txt").write_text(body)
        assert ad.container_score(d, FIRST) == ad.score(d, FIRST)
    nothing = tmp_path / "nothing"
    nothing.mkdir()
    assert ad.container_score(nothing, FIRST) == ad.score(nothing, FIRST) == {"n_passed": 0, "n_criteria": 3, "all_pass": False}


# ── pinned to upstream (tests may import it; the module under test does not) ───────────

def test_the_local_runmark_copy_is_upstreams(tmp_path):
    from harness2.core import runmark as RM
    assert (ss.MARK, ss.MARK_VERSION) == (RM.MARK, RM.VERSION)
    tree = tmp_path / "t"
    (tree / "sub").mkdir(parents=True)
    (tree / "a.md").write_text("a")
    (tree / "sub" / "b.md").write_text("b")
    (tree / "sub" / "x.pyc").write_bytes(b"\0")
    (tree / ".DS_Store").write_text("junk")
    assert ss.fingerprint(tree) == RM.fingerprint(tree)
    assert ss.fingerprint(None) == RM.fingerprint(None) == "base"
    assert ss.fingerprint(tmp_path / "missing") == RM.fingerprint(tmp_path / "missing") == "absent"
    a, b = tmp_path / "a", tmp_path / "b"
    ss.mark_complete(a, run_id="r", harness_dir=tree, extra={"k": 1})
    RM.mark_complete(b, run_id="r", harness_dir=tree, extra={"k": 1})
    pa, pb = json.loads((a / ss.MARK).read_text()), json.loads((b / RM.MARK).read_text())
    assert pa.keys() == pb.keys() and {k: v for k, v in pa.items() if k != "at"} == {k: v for k, v in pb.items() if k != "at"}
    assert ss.is_complete(b, harness_dir=tree) and RM.is_complete(a, harness_dir=tree)
    assert ss.is_complete(b, harness_dir=None) is RM.is_complete(b, harness_dir=None) is False
    assert ss.read_mark(a) == pa and ss.read_mark(tmp_path / "none") is None


def test_the_adapter_satisfies_upstreams_benchmark_adapter_protocol(tmp_path):
    from harness2.core.spec import BenchmarkAdapter, BenchPrompts, BenchSpec, ChannelSpec
    from harness2.substrate.opencode import OPENCODE_SURFACE
    from cognitive_harness2.substrate import CognitiveSubstrate

    ch = ChannelSpec("opencode", "local/deterministic")
    spec = BenchSpec(**ss.spec_fields(tmp_path), prompts=BenchPrompts(**ss.PROMPT_FIELDS),
                     channels={"proposer": ch, "selector": ch})
    sub = CognitiveSubstrate(OPENCODE_SURFACE)
    ad = ss.SumSignedAdapter(tmp_path, spec, sub)
    assert isinstance(ad, BenchmarkAdapter)
    assert spec.install_mode == "workspace_agents_md" and spec.obs_trunc >= 10_000
    assert tuple(spec.leak_globs) == ("verify.sh", "expected*")
    # the base harness is a real opencode tree to upstream's own check
    from harness2.core import tree as HT
    assert HT.is_tree(OPENCODE_SURFACE, ad.base_harness())


def test_the_prompt_fields_pass_upstreams_own_wording_rule():
    from harness2.core.spec import BenchPrompts
    BenchPrompts(**ss.PROMPT_FIELDS)
    for bad in ("benchmark", "evaluation", "grader", "rubric", "score"):
        assert all(bad not in v.lower() for v in ss.PROMPT_FIELDS.values())


