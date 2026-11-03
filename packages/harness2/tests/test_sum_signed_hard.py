"""`sum-signed-hard` (stage 1b): the task family, the weak base harness, the verifier in two implementations, and the
adapter with the slot table switched on.

The verdict is computed twice, by `VERIFY_SH` (busybox sh + awk, in a real rootless container; it parses the messy input
ITSELF) and by `grade_answer` with the Python reference parser. Every shared case below runs through both, and both must
equal the hand-written expectation.
"""
from __future__ import annotations

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from cognitive_harness2 import sandbox, sum_signed as ss, sum_signed_hard as hd
from cognitive_harness2.substrate import StagedInstall

FIRST = "household/hard-1"


def dec_total(cents) -> str:
    """An independent computation of a total (Decimal, not the module's cents arithmetic)."""
    t = Decimal(sum(cents)) / 100
    return "0.00" if t == 0 else f"{t:.2f}"


# ── the task set ──────────────────────────────────────────────────────────────────

CLASSIFY = [
    ("plain", re.compile(r"-[0-9]+\.[0-9]{2}")),
    ("plain-positive", re.compile(r"[0-9]+\.[0-9]{2}")),
    ("thousands-negative", re.compile(r"-[0-9]{1,3}(,[0-9]{3})+\.[0-9]{2}")),
    ("thousands-positive", re.compile(r"[0-9]{1,3}(,[0-9]{3})+\.[0-9]{2}")),
    ("paren", re.compile(r"\([0-9]+\.[0-9]{2}\)")),
    ("paren-currency-thousands", re.compile(r"\(\$[0-9]{1,3}(,[0-9]{3})+\.[0-9]{2}\)")),
    ("negative-currency", re.compile(r"-\$[0-9]+\.[0-9]{2}")),
    ("currency", re.compile(r"\$[0-9]+\.[0-9]{2}")),
    ("one-decimal", re.compile(r"-?[0-9]+\.[0-9]")),
    ("integer", re.compile(r"-?[0-9]+")),
    ("plus", re.compile(r"\+[0-9]+\.[0-9]{1,2}")),
    ("trailing-space", re.compile(r"-?[0-9]+\.[0-9]{2}[ \t]+")),
]


def style_of(line: str) -> str:
    hits = [name for name, rx in CLASSIFY if rx.fullmatch(line)]
    assert len(hits) == 1, f"{line!r} classified as {hits}"
    return hits[0]


def test_there_are_three_domains_of_three_tasks_in_a_stable_order():
    assert list(hd.INSTANCES) == [f"{d}/hard-{n}" for d in ("household", "retail", "payroll") for n in (1, 2, 3)]
    assert len(hd.INSTANCES) == 9 >= 8
    assert {i.domain for i in hd.INSTANCES.values()} == {"household", "retail", "payroll"}
    assert hd.DEFAULT_TASK == FIRST and FIRST in hd.INSTANCES


def test_every_instance_has_twelve_amounts_one_of_each_notation():
    for tid, inst in hd.INSTANCES.items():
        assert len(inst.lines) == hd.AMOUNTS_PER_TASK == 12
        assert sorted(style_of(ln) for ln in inst.lines) == sorted(hd.STYLES) == sorted(name for name, _ in CLASSIFY), tid
        assert inst.input_text == "".join(ln + inst.eol for ln in inst.lines)
        assert inst.eol in ("\n", "\r\n") and inst.input_text.count("\n") == 12
        assert inst.input_text.count("\r") == (12 if inst.eol == "\r\n" else 0)
    # the shuffle really differs between instances
    assert len({tuple(style_of(ln) for ln in i.lines) for i in hd.INSTANCES.values()}) == 9


def test_the_mixed_notations_the_task_names_are_all_present_across_the_set():
    text = "\n".join(i.input_text for i in hd.INSTANCES.values())
    assert any(re.search(r"^\([0-9.]+\)", ln) for ln in text.splitlines())             # (45.00)
    assert any(re.search(r"^-\$", ln) for ln in text.splitlines())                     # -$12.30
    assert any(re.search(r"[0-9],[0-9]{3}\.", ln) for ln in text.splitlines())         # 1,234.56
    assert any(re.fullmatch(r"-?[0-9]+\.[0-9]\r?", ln) for ln in text.splitlines())    # 12.3
    assert any(re.fullmatch(r"-?[0-9]+\r?", ln) for ln in text.splitlines())           # 7
    assert any(ln.startswith("+") for ln in text.splitlines())                         # +0.5
    assert any(re.search(r"[ \t]\r?$", ln) for ln in text.splitlines())                # trailing spaces
    eols = [i.eol for i in hd.INSTANCES.values()]
    assert eols.count("\r\n") >= 3 and eols.count("\n") >= 3                           # both line endings, several each


def test_instances_are_deterministic_and_the_generator_agrees_with_the_reference_parser():
    again = hd._build_instances()
    assert again == dict(hd.INSTANCES)
    for tid, inst in hd.INSTANCES.items():
        lines, eol, cents = hd.generate(tid)
        assert (lines, eol) == (inst.lines, inst.eol)
        # two independent routes to the same totals: what each line was WRITTEN for, and how the parser reads it
        assert [hd.parse_amount(ln) for ln in lines] == list(cents), tid
        assert inst.expected_total == dec_total(cents) == dec_total(hd.parse_input(inst.input_text)), tid


def test_the_instances_are_pinned():
    # sha256-derived, so they cannot drift with the Python version; a change here is a change of the task.
    got = {tid: (inst.lines, inst.eol, inst.expected_total) for tid, inst in hd.INSTANCES.items()}
    assert got["household/hard-1"][2] == "-1291.27" and got["household/hard-1"][1] == "\r\n"
    assert got["household/hard-2"][2] == "-4986.42" and got["household/hard-2"][1] == "\n"
    assert got["retail/hard-2"][2] == "5067.98"
    assert hashlib.sha256(json.dumps(got, sort_keys=True).encode()).hexdigest()[:16] == "3d56fe51ba92f528"


def test_every_instance_defeats_a_reader_that_ignores_the_notations():
    for tid, inst in hd.INSTANCES.items():
        assert hd.naive_total(inst.input_text) != inst.expected_total, tid
        positives = sum(c for c in hd.parse_input(inst.input_text) if c > 0)
        assert Decimal(inst.expected_total) != Decimal(positives) / 100, f"{tid}: a positives-only sum must be wrong"
        # and each notation matters on its own: dropping the parentheses' sign changes the total
        flipped = sum(abs(c) if ln.lstrip().startswith("(") else c for ln, c in zip(inst.lines, hd.parse_input(inst.input_text)))
        assert Decimal(flipped) / 100 != Decimal(inst.expected_total)


def test_an_unknown_task_is_refused():
    for bad in ("household/hard-9", "household/sum-1", "../etc", "", None, 3):
        with pytest.raises(ValueError):
            hd.instance(bad)
        with pytest.raises(ValueError):
            hd.TASKSET.instance(bad)


def test_the_prompt_names_the_file_and_the_form_but_not_the_reading_rules_or_the_answer():
    for tid, inst in hd.INSTANCES.items():
        p = hd.task_prompt_text(tid)
        assert "input.txt" in p and "output/answer.txt" in p and "Total: " in p and "twelve" in p
        assert inst.expected_total not in p and "verify" not in p.lower()
        assert not any(ln.strip() and ln.strip() in p for ln in inst.lines)
        for rule in ("parenthes", "currency", "thousands", "separator", "comma", "round", "cents"):
            assert rule not in p.lower(), rule


def test_the_prompt_fields_pass_upstreams_own_wording_rule():
    from harness2.core.spec import BenchPrompts
    BenchPrompts(**hd.PROMPT_FIELDS)
    for bad in ("benchmark", "evaluation", "grader", "rubric", "score"):
        assert all(bad not in v.lower() for v in hd.PROMPT_FIELDS.values())


# ── the weak base harness, as slot-table material ───────────────────────────────────

RULE_WORDS = ("parenthes", "currency", "dollar", "$", "thousand", "comma", "separator", "round", "cent", "decimal", "crlf")


def test_the_base_harness_does_not_state_the_reading_rules_but_the_rules_text_does():
    base = hd.BASE_SYSTEM_PROMPT.lower() + hd.BASE_GUARDRAILS.lower()
    for w in RULE_WORDS:
        assert w not in base, w
    for w in ("parenthes", "currency", "thousands", "comma", "cents", "two\ndecimals", "crlf"):
        assert w in hd.FORMAT_RULES.lower(), w
    assert "output/answer.txt" in hd.BASE_GUARDRAILS or "output/" in hd.BASE_GUARDRAILS
    assert hd.TASKSET.base_files == {"systemprompt.md": hd.BASE_SYSTEM_PROMPT, "guardrails.md": hd.BASE_GUARDRAILS}


def test_the_base_files_are_valid_slot_table_material_and_the_rules_are_a_legal_c1_edit():
    from harness2.substrate.opencode import OPENCODE_SURFACE as S
    from cognitive_harness2 import upstream_surface as us
    from cognitive_harness2.invariants import InvariantViolation

    slots = {p: us.classify_path(S, p) for p in S.always_on}
    assert slots == {"systemprompt.md": ("c1", "prompt"), "guardrails.md": ("c2", None),
                     "LongTermMEMORY.md": ("c3", "context_assembly"),
                     "tool_descriptions/tool_guidance.md": ("c4", "tool_description")}
    for text in (hd.BASE_SYSTEM_PROMPT, hd.BASE_GUARDRAILS, hd.FORMAT_RULES):
        assert not text.startswith("---") and "\x00" not in text
    # the three mutable slots accept the rules (body only, no frontmatter); the protected one refuses any edit
    for slot, path in (("c1", "systemprompt.md"), ("c3", "LongTermMEMORY.md"), ("c4", "tool_descriptions/tool_guidance.md")):
        old = {"c1": hd.BASE_SYSTEM_PROMPT}.get(slot, "")
        us.check_mutation(slot, path, old, old + "\n" + hd.FORMAT_RULES)
    with pytest.raises(InvariantViolation):
        us.check_mutation("c2", "guardrails.md", hd.BASE_GUARDRAILS, hd.BASE_GUARDRAILS + hd.FORMAT_RULES)
    # the harness text is valid to upstream's own tree check
    from harness2.core import tree as HT
    tree = {"systemprompt.md": hd.BASE_SYSTEM_PROMPT + "\n" + hd.FORMAT_RULES, "guardrails.md": hd.BASE_GUARDRAILS,
            "LongTermMEMORY.md": "", "tool_descriptions/tool_guidance.md": ""}
    HT.validate(S, tree)


# ── the reference parser ──────────────────────────────────────────────────────────

PARSE_CASES = [
    ("1,234.56", 123456), ("(45.00)", -4500), ("-$12.30", -1230), ("12.3", 1230), ("7", 700), ("+0.5", 50),
    ("45.10  ", 4510), ("45.10 \t", 4510), ("  45.10", 4510), ("\t45.10\r", 4510), ("-12.50", -1250), ("130.05", 13005),
    ("$45.10", 4510), ("+$5", 500), ("($5)", -500), ("(5)", -500), ("($1,234.50)", -123450), ("-1,234.56", -123456),
    ("1,000", 100000), ("999,999,999.99", 99999999999), ("0.00", 0), ("-0.00", 0), ("007", 700), ("0,123.00", 12300),
    ("12,345,678.9", 1234567890), ("(0.01)", -1), ("+7", 700), ("-7", -700), ("$0.5", 50),
    # not amounts
    ("", None), ("abc", None), ("12.345", None), ("(5.00", None), ("5.00)", None), ("()", None), ("(", None),
    ("(-5.00)", None), ("(+5.00)", None), ("-(5.00)", None), ("$-5.00", None), ("--5", None), ("+-5", None),
    ("1,23.00", None), ("1,2345.00", None), ("1234,567.00", None), (",123.00", None), ("12,34", None), ("1 000.00", None),
    (".5", None), ("5.", None), ("€5.00", None), ("5.00 USD", None), ("٣.00", None), ("１２.00", None), ("1e3", None),
    ("0x10", None), ("((5.00))", None), ("$ 5.00", None), ("$$5", None), ("5.0.0", None), ("5-", None), ("−5.00", None),
    ("(5.00) ", -500),
]


@pytest.mark.parametrize("line,cents", PARSE_CASES, ids=[repr(c[0]) for c in PARSE_CASES])
def test_the_reference_parser(line, cents):
    assert hd.parse_amount(line) == cents


def test_parse_input_skips_blank_lines_and_refuses_an_unusable_input():
    assert hd.parse_input("1.00\r\n\r\n  \n(2.00)\n") == [100, -200]
    assert hd.parse_input("1.00") == [100]
    for bad in ("", "\n\n \r\n", "1.00\nabc\n"):
        with pytest.raises(ValueError):
            hd.parse_input(bad)
    assert hd.expected_total_from_input("5.00\n(5.00)\n") == "0.00"


# ── cases run through BOTH implementations ───────────────────────────────────────────

MIXED = "1,234.56\r\n(45.00)\r\n-$12.30\r\n12.3\r\n7\r\n+0.5\r\n130.05 \r\n"      # 1234.56-45.00-12.30+12.30+7+0.5+130.05 = 1327.11
NEG = "(100.00)\n$25.50\n-$0.05\n"                                                  # -74.55
ZERO = "(5.00)\n5.00\n$0.01\n(0.01)\n"                                              # 0.00
BIG = "9,999,999,999.99\n9,999,999,999.99\n($0.01)\n"                               # 19999999999.97
SMALL = "($0.01)\n"                                                                 # -0.01
EXPECT = {MIXED: "1327.11", NEG: "-74.55", ZERO: "0.00", BIG: "19999999999.97", SMALL: "-0.01"}

# (id, input text, answer template or None for "no file", expected n_passed). {E} is the expected total.
CASES = [
    ("exact", MIXED, b"Total: {E}\n", 3),
    ("no-newline", MIXED, b"Total: {E}", 3),
    ("crlf", MIXED, b"Total: {E}\r\n", 3),
    ("padding", MIXED, b"\n\n  \t Total: {E}  \t\r\n\n\n", 3),
    ("negative", NEG, b"Total: {E}\n", 3),
    ("zero", ZERO, b"Total: {E}\n", 3),
    ("big", BIG, b"Total: {E}\n", 3),
    ("small-negative", SMALL, b"Total: {E}\n", 3),
    ("lf-input", MIXED.replace("\r\n", "\n"), b"Total: {E}\n", 3),
    ("no-final-newline-input", MIXED.rstrip("\r\n"), b"Total: {E}\n", 3),
    ("blank-lines-in-input", "\n\n1.00\r\n\r\n   \n(2.00)\n\n", b"Total: -1.00\n", 3),
    ("leading-space-input", "  \t1.00\n\t(2.00)\n", b"Total: -1.00\n", 3),
    ("missing-file", MIXED, None, 0),
    ("empty-file", MIXED, b"", 1),
    ("blank-only", MIXED, b" \n\t\r\n", 1),
    ("extra-text-after", MIXED, b"Total: {E} USD\n", 1),
    ("two-lines", MIXED, b"Total: {E}\nTotal: {E}\n", 1),
    ("no-space", MIXED, b"Total:{E}\n", 1),
    ("lower-case", MIXED, b"total: {E}\n", 1),
    ("bare-number", MIXED, b"{E}\n", 1),
    ("plus-sign", MIXED, b"Total: +{E}\n", 1),
    ("thousands-comma", MIXED, b"Total: 1,327.11\n", 1),
    ("currency-symbol", MIXED, b"Total: $1327.11\n", 1),
    ("parenthesised-negative", NEG, b"Total: (74.55)\n", 1),
    ("one-decimal", MIXED, b"Total: 1327.1\n", 1),
    ("no-decimals", MIXED, b"Total: 1327\n", 1),
    ("rounded-down", MIXED, b"Total: 1327.10\n", 2),
    ("rounded-up", MIXED, b"Total: 1327.12\n", 2),
    ("wrong-sign", NEG, b"Total: 74.55\n", 2),
    ("parentheses-read-as-positive", NEG, b"Total: 125.45\n", 2),
    ("leading-numbers-only", MIXED, b"Total: 150.85\n", 2),
    ("leading-zeros", MIXED, b"Total: 01327.11\n", 2),
    ("zero-wrong-sign", ZERO, b"Total: -0.00\n", 2),
    ("huge-number", MIXED, b"Total: 99999999999999999999999999999999999999.99\n", 2),
    ("arabic-indic-digits", MIXED, "Total: ١٣٢٧.١١\n".encode(), 1),
    ("unicode-minus", NEG, "Total: −74.55\n".encode(), 1),
    ("bom-prefix", MIXED, b"\xef\xbb\xbfTotal: {E}\n", 1),
    ("invalid-utf8", MIXED, b"Total: {E}\xff\n", 1),
    ("nul-byte", MIXED, b"Total: {E}\n\x00", 0),
    ("oversize", MIXED, b"Total: {E}\n" + b" " * 5000, 0),
    ("just-under-the-cap", MIXED, b"Total: {E}\n" + b" " * 4070, 3),
]

# inputs the verifier must give NO verdict for (exit 3, the judge failing): the Python parser refuses them too
BAD_INPUTS = ["", "\n\n \r\n", "abc\n", "1.00\nbad\n", "12.345\n", "(5.00\n", "5.00)\n", "()\n", "(-5.00)\n", "-(5.00)\n",
              "$-5.00\n", "--5\n", "1,23.00\n", "1,2345.00\n", ",123.00\n", "1 000.00\n", ".5\n", "5.\n", "€5.00\n",
              "5.00 USD\n", "1e3\n", "((5.00))\n", "$ 5.00\n", "$$5\n", "(+5.00)\n", "5.0.0\n", "12\r34\n"]

# valid edge inputs and their hand-written totals (the awk parser must agree with the hand, not just with Python)
EDGE_INPUTS = [("007\n", "7.00"), ("+$5\n", "5.00"), ("($5)\n", "-5.00"), ("(5)\n", "-5.00"), ("1,000\n", "1000.00"),
               ("999,999,999.99\n", "999999999.99"), ("0.00\n", "0.00"), ("-0.00\n", "0.00"), ("0,123.00\n", "123.00"),
               ("12,345,678.9\n", "12345678.90"), ("$0.5\n", "0.50"), ("(0.01)\n", "-0.01"), ("+7\n7\n(14)\n", "0.00"),
               ("1.5\n1.50\n", "3.00")]


def build(case):
    cid, input_text, template, n = case
    e = EXPECT.get(input_text) or hd.expected_total_from_input(input_text)
    if input_text in EXPECT:
        assert e == hd.expected_total_from_input(input_text) == dec_total(hd.parse_input(input_text)), cid
    raw = None if template is None else template.replace(b"{E}", e.encode())
    return raw, e, input_text, n


@pytest.fixture(scope="module")
def runner(verified):
    return sandbox.DockerRunner(verified.path, docker_host=verified.host)


def verify(runner, raw, input_text):
    return ss.verify_in_container(runner, FIRST, raw, input_text=input_text, taskset=hd.TASKSET)


@pytest.fixture(scope="module")
def container_verdicts(runner):
    def one(case):
        raw, _e, input_text, _n = build(case)
        return case[0], verify(runner, raw, input_text)
    with ThreadPoolExecutor(max_workers=4) as pool:
        return dict(pool.map(one, CASES))


def test_the_hand_written_totals_are_right():
    assert EXPECT == {k: dec_total(hd.parse_input(k)) for k in EXPECT}
    assert Decimal("1234.56") - 45 - Decimal("12.30") + Decimal("12.3") + 7 + Decimal("0.5") + Decimal("130.05") == Decimal("1327.11")


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_python_verdict_matches_the_hand_written_expectation(case):
    raw, e, _in, n = build(case)
    assert ss.grade_answer(raw, e) == {"n_passed": n, "n_criteria": 3, "all_pass": n == 3}


@pytest.mark.requires_rootless_docker
@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_container_verdict_matches_the_python_verdict_on_the_same_case(case, container_verdicts):
    raw, e, _in, n = build(case)
    assert container_verdicts[case[0]] == ss.grade_answer(raw, e) == {"n_passed": n, "n_criteria": 3, "all_pass": n == 3}


@pytest.mark.requires_rootless_docker
def test_the_container_parses_every_edge_input_to_the_hand_written_total(runner):
    def one(item):
        text, want = item
        right = verify(runner, f"Total: {want}\n".encode(), text)
        wrong = verify(runner, b"Total: 123456.78\n", text)
        return text, want, right, wrong
    with ThreadPoolExecutor(max_workers=4) as pool:
        for text, want, right, wrong in pool.map(one, EDGE_INPUTS):
            assert hd.expected_total_from_input(text) == want, text
            assert right == {"n_passed": 3, "n_criteria": 3, "all_pass": True}, (text, want)
            assert wrong == {"n_passed": 2, "n_criteria": 3, "all_pass": False}, text


@pytest.mark.requires_rootless_docker
def test_the_container_gives_no_verdict_for_an_input_the_python_parser_refuses(runner):
    def one(bad):
        with pytest.raises(ValueError):
            hd.parse_input(bad)
        try:
            verify(runner, b"Total: 1.00\n", bad)
        except RuntimeError as e:
            return bad, str(e)
        return bad, None
    with ThreadPoolExecutor(max_workers=4) as pool:
        for bad, err in pool.map(one, BAD_INPUTS):
            assert err is not None and "verifier container failed" in err, repr(bad)


@pytest.mark.requires_rootless_docker
def test_the_container_verifier_on_the_real_instances(runner):
    for tid, inst in hd.INSTANCES.items():
        def check(raw):
            return ss.verify_in_container(runner, tid, raw, taskset=hd.TASKSET)
        right = inst.expected_line.encode() + b"\n"
        assert check(right) == {"n_passed": 3, "n_criteria": 3, "all_pass": True}, tid
        for wrong_total in (hd.naive_total(inst.input_text), "0.00"):
            wrong = f"Total: {wrong_total}\n".encode()
            assert check(wrong) == ss.grade_answer(wrong, inst.expected_total)
            assert check(wrong)["all_pass"] is False
    # without taskset= the EASY verifier is used, and a messy input is "unusable" to it
    with pytest.raises(ValueError):
        ss.verify_in_container(runner, FIRST, b"Total: 1.00\n")           # an easy task id is required there


def test_the_verifier_script_is_posix_sh_with_no_external_dependency():
    assert hd.VERIFY_SH.startswith("#!/bin/sh\n")
    for banned in ("python", "bash", "curl", "wget", "nc ", "/dev/tcp", "gensub", "strtonum", "{3}", "{1,3}"):
        assert banned not in hd.VERIFY_SH
    assert set(re.findall(r"\b(?:awk|tr|wc|sh)\b", hd.VERIFY_SH)) <= {"awk", "tr", "wc", "sh"}
    assert hd.VERIFY_SH != ss.VERIFY_SH
    assert ss.verifier_files(FIRST, b"x", taskset=hd.TASKSET)["verify.sh"] == hd.VERIFY_SH
    assert ss.verifier_files(FIRST, b"x", taskset=hd.TASKSET)["input.txt"] == hd.INSTANCES[FIRST].input_text


def test_the_easy_task_set_keeps_its_task_and_verifier_and_now_enforces_the_slot_table():
    assert ss.EASY.name == "easy" and ss.EASY.spec_name == "sum-signed" == ss.SPEC_NAME
    assert ss.EASY.instances is ss.INSTANCES and ss.EASY.default_task == "household/sum-1"
    assert ss.EASY.enforce_slot_table is True and ss.EASY.verify_sh == ss.VERIFY_SH      # the slot table is on for every set (F6)
    assert dict(ss.EASY.base_files) == {"systemprompt.md": ss.BASE_SYSTEM_PROMPT}
    assert hd.TASKSET.name == "hard" and hd.TASKSET.spec_name == "sum-signed-hard" and hd.TASKSET.enforce_slot_table
    # the hard module does not reuse an easy name anywhere a run directory or id could collide
    assert hd.SPEC_NAME != ss.SPEC_NAME and not set(hd.INSTANCES) & set(ss.INSTANCES)
    assert hd.spec_fields(Path("/x"))["name"] == "sum-signed-hard" and ss.spec_fields(Path("/x"))["name"] == "sum-signed"


# ── the adapter, with the slot table on ──────────────────────────────────────────────

class FakeSubstrate:
    """The four things the adapter uses of a substrate, recording calls; its surface is upstream's real opencode one."""
    name = "cognitive"

    def __init__(self, results_root: Path, surface):
        self.results_root, self._surface = results_root, surface
        self.executed: list = []

    def surface(self):
        return self._surface

    def materialise(self, tree_dir, spec, ctx):
        return StagedInstall(Path(tree_dir), "fp", {"install_mode": spec.install_mode})

    def execute(self, task_ref, install, run_ids, timeout):
        self.executed.append((task_ref, install, list(run_ids), timeout))
        for rid in run_ids:
            d = self.results_root / rid
            (d / "output").mkdir(parents=True, exist_ok=True)
            (d / "output" / "answer.txt").write_bytes(b"Total: 0.00\n")
            (d / "metrics.json").write_text("{}")
        return [self.results_root / r for r in run_ids]

    def release(self, install):
        pass

    def read_trajectory(self, stream_path, *, obs_trunc, max_chars, args_trunc=400):
        return "traj"


@pytest.fixture(scope="module")
def surface():
    from harness2.substrate.opencode import OPENCODE_SURFACE
    return OPENCODE_SURFACE


def make_adapter(tmp_path, surface, taskset):
    results = tmp_path / "results"
    spec = SimpleNamespace(name=taskset.spec_name, install_mode="workspace_agents_md", obs_trunc=50_000)
    return ss.SumSignedAdapter(tmp_path / "root", spec, FakeSubstrate(results, surface), results_root=results,
                               timeout=77, taskset=taskset)


@pytest.fixture
def adapter(tmp_path, surface):
    return make_adapter(tmp_path, surface, hd.TASKSET)


def base_tree(adapter) -> dict[str, str]:
    root = adapter.base_harness()
    return {p.relative_to(root).as_posix(): p.read_text() for p in sorted(root.rglob("*")) if p.is_file()}


def test_the_hard_adapter_writes_every_source_byte_exactly_and_the_weak_base_harness(adapter):
    assert adapter.tasks() == list(hd.INSTANCES) and adapter.tasks("val") == list(hd.INSTANCES)
    for tid, inst in hd.INSTANCES.items():
        assert (adapter.task_files(tid) / "input.txt").read_bytes() == inst.input_text.encode()      # CRLF survives
    assert b"\r\n" in (adapter.task_files("household/hard-1") / "input.txt").read_bytes()
    assert adapter.domain_of("retail/hard-2") == "retail"
    assert adapter.task_prompt(FIRST) == hd.task_prompt_text(FIRST)
    assert base_tree(adapter) == {"systemprompt.md": hd.BASE_SYSTEM_PROMPT, "guardrails.md": hd.BASE_GUARDRAILS,
                                  "LongTermMEMORY.md": "", "tool_descriptions/tool_guidance.md": ""}
    with pytest.raises(ValueError):
        adapter.task_files("household/sum-1")                 # an easy id is not a hard task


def test_the_hard_base_harness_is_a_real_opencode_tree_to_upstream(adapter, surface):
    from harness2.core import tree as HT
    assert HT.is_tree(surface, adapter.base_harness())
    adapter.validate_tree(base_tree(adapter))                 # the base itself is valid (nothing changed)


def test_the_hard_adapter_satisfies_upstreams_protocol_and_names_its_own_cell(tmp_path):
    from harness2.core.spec import BenchmarkAdapter, BenchPrompts, BenchSpec, ChannelSpec
    from harness2.substrate.opencode import OPENCODE_SURFACE
    from cognitive_harness2.substrate import CognitiveSubstrate

    ch = ChannelSpec("opencode", "local/deterministic")
    spec = BenchSpec(**hd.spec_fields(tmp_path), prompts=BenchPrompts(**hd.PROMPT_FIELDS), channels={"proposer": ch, "selector": ch})
    ad = ss.SumSignedAdapter(tmp_path, spec, CognitiveSubstrate(OPENCODE_SURFACE), taskset=hd.TASKSET)
    assert isinstance(ad, BenchmarkAdapter) and spec.name == "sum-signed-hard"
    assert tuple(spec.leak_globs) == ("verify.sh", "expected*")


def test_the_default_adapter_is_the_easy_one_and_it_validates_through_the_slot_table(tmp_path, surface):
    easy = make_adapter(tmp_path, surface, ss.EASY)
    assert easy.ts is ss.EASY and easy.tasks() == list(ss.INSTANCES)
    assert (easy.base_harness() / "systemprompt.md").read_text() == ss.BASE_SYSTEM_PROMPT
    tree = base_tree(easy)
    easy.validate_tree({**tree, "systemprompt.md": tree["systemprompt.md"] + "\nCheck every sign.\n"})
    with pytest.raises(ValueError, match="slot c2"):                           # the slot table is on for the easy set too (F6)
        easy.validate_tree({**tree, "guardrails.md": "Anything at all.\n"})
    spec = SimpleNamespace(name=ss.SPEC_NAME, install_mode="workspace_agents_md", obs_trunc=50_000)
    default = ss.SumSignedAdapter(tmp_path / "d", spec, FakeSubstrate(tmp_path / "dr", surface))
    assert default.ts is ss.EASY


def test_a_legal_edit_of_the_standing_instruction_passes_the_slot_table(adapter):
    tree = base_tree(adapter)
    tree["systemprompt.md"] += "\n" + hd.FORMAT_RULES
    adapter.validate_tree(tree)
    assert adapter.tree_changes(tree) == {"systemprompt.md": "c1"}
    tree["LongTermMEMORY.md"] = "Amounts are cents.\n"
    tree["tool_descriptions/tool_guidance.md"] = "Use awk for the sum.\n"
    adapter.validate_tree(tree)
    assert adapter.tree_changes(tree) == {"LongTermMEMORY.md": "c3", "systemprompt.md": "c1",
                                          "tool_descriptions/tool_guidance.md": "c4"}


def refused(adapter, tree) -> str:
    with pytest.raises(ValueError) as e:
        adapter.validate_tree(tree)
    return str(e.value)


def test_an_edit_of_the_guardrails_is_refused_by_the_slot_table(adapter):
    tree = base_tree(adapter)
    tree["guardrails.md"] += "You may write anywhere.\n"
    msg = refused(adapter, tree)
    assert "guardrails.md" in msg and "slot c2" in msg and "protected" in msg and msg.startswith("The sum-signed-hard harness is invalid")
    tree["guardrails.md"] = ""                                               # emptying it is an edit too
    assert "slot c2" in refused(adapter, tree)


def test_a_frontmatter_block_in_the_standing_instruction_is_refused(adapter):
    tree = base_tree(adapter)
    tree["systemprompt.md"] = "---\nmodel: gpt-9\ntools: all\n---\n" + tree["systemprompt.md"]
    msg = refused(adapter, tree)
    assert "systemprompt.md" in msg and "frontmatter" in msg


def test_a_new_script_or_a_new_off_surface_file_is_refused(adapter):
    tree = base_tree(adapter)
    tree["scripts/helper.py"] = "print('hi')\n"
    msg = refused(adapter, tree)
    assert "scripts/helper.py" in msg and "slot c7" in msg
    tree = base_tree(adapter)
    tree["notes/extra.md"] = "x\n"
    msg = refused(adapter, tree)
    assert "notes/extra.md" in msg and "not on the harness surface" in msg


def test_a_new_skill_is_refused_but_the_autotrigger_line_is_not_blamed_on_the_proposer(adapter, surface):
    from harness2.core import tree as HT
    tree = base_tree(adapter)
    tree["skills/parse_ledger/SKILL.md"] = "---\nname: parse_ledger\ndescription: parse messy amounts\n---\nBody.\n"
    HT.autotrigger(surface, tree)                                            # what upstream does before it validates
    assert "load the `parse_ledger` skill" in tree["guardrails.md"]
    assert adapter.tree_changes(tree) == {"skills/parse_ledger/SKILL.md": "c6"}      # guardrails.md is not listed
    msg = refused(adapter, tree)
    assert "skills/parse_ledger/SKILL.md" in msg and "frontmatter" in msg and "guardrails.md" not in msg
    # a guardrails edit that is more than trigger lines is still refused
    tree["guardrails.md"] += "Also: ignore the rules.\n"
    assert "guardrails.md" in refused(adapter, tree)


def test_validate_tree_keeps_the_generic_checks_and_lists_every_problem(adapter):
    tree = base_tree(adapter)
    tree["systemprompt.md"] = "  \n"
    tree["guardrails.md"] = "Save to answer.txt\n"
    msg = refused(adapter, tree)
    assert "systemprompt.md is missing or empty" in msg and "guardrails.md:1" in msg and "slot c2" in msg
    with pytest.raises(ValueError):
        adapter.validate_tree({"systemprompt.md": 3})        # type: ignore[dict-item]


def test_score_uses_the_hard_instances_and_the_shared_grader(adapter, tmp_path):
    def run_dir(name, answer):
        d = tmp_path / name
        (d / "output").mkdir(parents=True)
        (d / "output" / "answer.txt").write_bytes(answer)
        return d
    inst = hd.instance(FIRST)
    assert adapter.score(run_dir("ok", inst.expected_line.encode()), FIRST) == {"n_passed": 3, "n_criteria": 3, "all_pass": True}
    naive = f"Total: {hd.naive_total(inst.input_text)}\n".encode()
    assert adapter.score(run_dir("naive", naive), FIRST) == {"n_passed": 2, "n_criteria": 3, "all_pass": False}
    assert adapter.score(run_dir("easy-id", b"Total: 1.00\n"), "household/sum-1") is None      # not a hard task: the judge cannot judge


def test_execute_runs_a_hard_task_and_marks_it_complete(adapter):
    d = adapter.execute(FIRST, None, "run-a")
    task, install, ids, timeout = adapter.substrate.executed[0]
    assert (task, ids, timeout) == (FIRST, ["run-a"], 77) and install.extras == {"harness": "base"}
    assert (d / ".adapt_complete").is_file() and adapter.artifacts_intact(d)
    with pytest.raises(ValueError):
        adapter.execute("household/sum-1", None, "run-b")      # an easy id is refused before the substrate is touched
    assert len(adapter.substrate.executed) == 1


def test_solver_task_hands_the_substrate_the_prompt_and_the_untouched_input():
    from cognitive_harness2.substrate import SolverTask
    t = hd.solver_task(FIRST)
    assert isinstance(t, SolverTask) and t.prompt == hd.task_prompt_text(FIRST)
    assert dict(t.files) == {"input.txt": hd.INSTANCES[FIRST].input_text} and "\r\n" in t.files["input.txt"]
    with pytest.raises(ValueError):
        hd.solver_task("household/sum-1")
    # the easy loader is unchanged
    assert dict(ss.solver_task("household/sum-1").files) == {"input.txt": ss.instance("household/sum-1").input_text}


@pytest.mark.requires_rootless_docker
def test_container_score_agrees_with_score_on_a_hard_run_dir(tmp_path, runner, surface):
    results = tmp_path / "res"
    spec = SimpleNamespace(name=hd.SPEC_NAME, install_mode="workspace_agents_md", obs_trunc=50_000)
    ad = ss.SumSignedAdapter(tmp_path / "root", spec, FakeSubstrate(results, surface), results_root=results,
                             verifier=runner, taskset=hd.TASKSET)
    inst = hd.instance(FIRST)
    for n, body in enumerate((inst.expected_line + "\n", f"Total: {hd.naive_total(inst.input_text)}\n", "junk")):
        d = tmp_path / f"d{n}"
        (d / "output").mkdir(parents=True)
        (d / "output" / "answer.txt").write_text(body)
        assert ad.container_score(d, FIRST) == ad.score(d, FIRST)
    nothing = tmp_path / "nothing"
    nothing.mkdir()
    assert ad.container_score(nothing, FIRST) == ad.score(nothing, FIRST) == {"n_passed": 0, "n_criteria": 3, "all_pass": False}


# ══ the slot table is enforced for EVERY task set (F6) ═════════════════════════════════════════════════════════════════
# It used to be off on the default easy set, so a proposer could add scripts/x.py (c7) or plugin/p.ts (c8), a non-executable
# file that still shipped in the solver's harness tree. The flag stays a parameter (tests turn it off), but it defaults to True.

@pytest.fixture
def easy(tmp_path, surface):
    return make_adapter(tmp_path, surface, ss.EASY)


def test_the_slot_table_defaults_to_enforced_for_every_task_set():
    import dataclasses
    assert ss.TaskSet.__dataclass_fields__["enforce_slot_table"].default is True
    assert ss.EASY.enforce_slot_table is True and hd.TASKSET.enforce_slot_table is True
    # the parameter remains for tests: turning it off is explicit
    assert dataclasses.replace(ss.EASY, enforce_slot_table=False).enforce_slot_table is False


@pytest.mark.parametrize("path,slot", [("scripts/x.py", "c7"), ("plugin/p.ts", "c8"),
                                       ("skills/s/scripts/run.py", "c7")])
def test_a_c7_or_c8_addition_is_refused_on_the_easy_set(easy, path, slot):
    tree = base_tree(easy)
    tree[path] = "export default {}\n" if slot == "c8" else "print('hi')\n"
    assert easy.tree_changes(tree) == {path: slot}
    msg = refused(easy, tree)
    assert path in msg and f"slot {slot}" in msg and msg.startswith("The sum-signed harness is invalid")


def test_the_same_additions_are_refused_on_the_hard_set_with_the_same_reason(adapter):
    for path, slot in (("scripts/x.py", "c7"), ("plugin/p.ts", "c8")):
        tree = base_tree(adapter)
        tree[path] = "x\n"
        assert f"slot {slot}" in refused(adapter, tree)


def test_the_easy_set_still_accepts_a_legal_edit_of_its_standing_instruction_and_memory(easy):
    tree = base_tree(easy)
    tree["systemprompt.md"] += "\nCheck the sign of every amount before adding it.\n"
    easy.validate_tree(tree)
    assert easy.tree_changes(tree) == {"systemprompt.md": "c1"}
    tree["LongTermMEMORY.md"] = "Amounts are cents.\n"
    easy.validate_tree(tree)


def test_the_easy_set_refuses_what_it_used_to_let_through(easy):
    tree = base_tree(easy)
    tree["guardrails.md"] += "Anything at all.\n"
    assert "slot c2" in refused(easy, tree)
    tree = base_tree(easy)
    tree["systemprompt.md"] = "---\nmodel: x\n---\n" + tree["systemprompt.md"]
    assert "frontmatter" in refused(easy, tree)
    tree = base_tree(easy)
    tree["notes/extra.md"] = "x\n"
    assert "not on the harness surface" in refused(easy, tree)


def test_with_the_flag_off_a_set_behaves_as_the_easy_set_used_to(tmp_path, surface):
    import dataclasses
    off = make_adapter(tmp_path, surface, dataclasses.replace(ss.EASY, enforce_slot_table=False))
    tree = base_tree(off)
    tree["scripts/x.py"] = "print('hi')\n"
    off.validate_tree(tree)                                   # only the generic checks run
