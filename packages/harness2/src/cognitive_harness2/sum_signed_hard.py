"""`sum-signed-hard` (stage 1b): the same task as `sum-signed`, on a ledger that is written the way real ones are.

Why it exists. On `sum-signed` (five clean amounts like `-12.50`) the base harness already scores 3 of 3, so every
proposer stage keeps the base and the improvement loop (propose, harvest, accepted edit, candidate rollout, selection)
never runs with an ACCEPTED edit. This family gives System 2 something to improve: twelve amounts per instance in the
mixed notations of a ledger exported from several systems, and a base harness that does not say how to read them.

The input. `input.txt` holds twelve lines, one amount each, drawn from twelve notations, every one present in every
instance (their order is shuffled by the instance's own hash):

    -12.50   plain               -1,234.56   thousands separator, negative      (45.00)    parentheses mean negative
    $45.10   currency            ($1,234.50) parentheses, currency, thousands    -$12.30    currency after the sign
    12.3     one decimal         7           no decimals                         +0.5       explicit plus
    45.10␠␠  trailing spaces     130.05      plain positive                      (and CRLF line endings on some instances)

The output contract is `sum-signed`'s, unchanged: `Total: X.XX` as the one line of `output/answer.txt`, two decimals,
a minus sign for a negative total, no sign on zero. So `grade_answer` is shared, and only the PARSING of the input
differs. The reading rules (`FORMAT_RULES`) are deliberately NOT in the base harness or in the task prompt; a proposer
has to find them in the failing trajectories and write them into `systemprompt.md`.

Two implementations of ONE verdict, as in `sum_signed`: `VERIFY_SH` (busybox `sh` + `awk`, in a `DockerRunner`; the
authority) parses the input itself, so its total is independent of the Python side, and `grade_answer` with
`expected_total_from_input` (Python) does the same. A shared case table in the tests holds both to identical verdicts.

The grammar of one input line, after trimming spaces, tabs and CRs at both ends (an empty line is skipped; any other
line that does not match makes the input unusable, which is the judge failing, not a zero):

    line   := "(" body ")"  |  [+-] body  |  body          a parenthesised amount takes no sign inside
    body   := "$"? int ("." digit digit?)?
    int    := digit+  |  digit{1,3} ("," digit{3})+        ASCII digits only; thousands groups of exactly three

`naive_total` is what a model that ignores those rules would write (it reads each line as awk's `+0` does: the leading
number only), so a test can prove every instance defeats it.

Duck typed and import-light like `sum_signed`: nothing here imports upstream.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Mapping

from . import sum_signed as ss
from .sum_signed import TaskSet, format_cents

SPEC_NAME = "sum-signed-hard"
DOMAINS = ss.DOMAINS
TASKS_PER_DOMAIN = 3
AMOUNTS_PER_TASK = 12
CRLF = "\r\n"
DEFAULT_TASK = "household/hard-1"

PROMPT_FIELDS: Mapping[str, str] = {
    "work": "add up a list of signed ledger amounts exported from several systems and report the total",
    "domain_word": "ledger",
    "deliverable_word": "answer file",
}

# The WEAK base harness: a bookkeeper with no word about notations, signs, symbols, separators or rounding. The
# guardrail line is real (and protected: c2 is not editable), so the staged-protected-file path is exercised too.
BASE_SYSTEM_PROMPT = (
    "You are a bookkeeper working in a small workspace.\n"
    "Read the input file, add up the amounts and write the total to the answer file named in the task.\n"
)
BASE_GUARDRAILS = "Write only inside the output/ directory and never change input.txt.\n"

# What a good proposer would add to systemprompt.md (the selftest's scripted proposer adds exactly this; the fake
# opencode script carries a copy of it, which a test pins to this text).
FORMAT_RULES = (
    "Reading the input. The amounts come from several systems, one per line, and every line is a signed amount in its\n"
    "own notation:\n"
    "- Parentheses mean a negative amount: (45.00) is -45.00, and ($1,234.50) is -1234.50.\n"
    "- A currency symbol is not part of the number: $12.30 is 12.30 and -$12.30 is -12.30.\n"
    "- A comma is a thousands separator, not a decimal point: 1,234.56 is 1234.56.\n"
    "- A leading + is just a plus sign, 12.3 is 12.30 and 7 is 7.00.\n"
    "- Ignore spaces, tabs and carriage returns at the ends of a line (the file may use CRLF line endings).\n"
    "Convert every line to whole cents first, add the cents as integers, then write the total with exactly two\n"
    "decimals. A negative total keeps its minus sign; a zero total has no sign.\n"
)

# ── the reference parser (Python), mirroring the awk in VERIFY_SH ────────────────────────────

_BODY = re.compile(r"\$?(?P<int>[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.(?P<frac>[0-9]{1,2}))?")
_TRIM = " \t\r"


def parse_amount(line: str) -> int | None:
    """One input line as signed whole cents, or None if it is not an amount in the grammar (the caller skips a
    blank line before calling)."""
    s = line.strip(_TRIM)
    neg = False
    if s.startswith("("):
        if len(s) < 3 or not s.endswith(")"):
            return None
        neg, s = True, s[1:-1]
    elif s.startswith("-"):
        neg, s = True, s[1:]
    elif s.startswith("+"):
        s = s[1:]
    m = _BODY.fullmatch(s)
    if m is None:
        return None
    cents = int(m["int"].replace(",", "")) * 100 + int((m["frac"] or "").ljust(2, "0") or "0")
    return -cents if neg else cents


def parse_input(text: str) -> list[int]:
    """Every amount of an input, in cents. Raises ValueError for an unusable input (a bad line, or no amount)."""
    out: list[int] = []
    for n, line in enumerate(text.split("\n"), 1):
        if not line.strip(_TRIM):
            continue
        c = parse_amount(line)
        if c is None:
            raise ValueError(f"line {n} is not an amount")
        out.append(c)
    if not out:
        raise ValueError("the input holds no amount")
    return out


def expected_total_from_input(text: str) -> str:
    """The canonical total of an input, from the reference parser."""
    return format_cents(sum(parse_input(text)))


def _leading_number_cents(line: str) -> int:
    """The line as awk's `+0` reads it: the longest numeric prefix (after the sign), in cents, rounded."""
    s = line.strip(_TRIM)
    neg = s.startswith("-")
    if s[:1] in "+-":
        s = s[1:]
    m = re.match(r"[0-9]+(?:\.[0-9]*)?", s)
    value = float(m.group(0)) if m else 0.0
    cents = int(value * 100 + 0.5)
    return -cents if neg else cents


def naive_total(text: str) -> str:
    """The total of a reader that ignores parentheses, currency symbols and thousands separators (what the weak base
    harness invites): each line is its leading number, with a leading minus honoured."""
    return format_cents(sum(_leading_number_cents(ln) for ln in text.split("\n") if ln.strip(_TRIM)))


# ── the verifier: busybox sh + awk ───────────────────────────────────────────────────────────

VERIFY_SH = r'''#!/bin/sh
# sum-signed-hard verifier. busybox sh + awk only. Reads ./input.txt and ./output/answer.txt, prints one JSON line.
# Exit 0 with the JSON; exit 3 (no JSON) if input.txt itself is unusable, which is the judge failing, not a zero.
set -u
F=output/answer.txt
c1=0
if [ -f "$F" ] && [ ! -L "$F" ]; then
  size=$(wc -c < "$F")
  if [ "$size" -le 4096 ] && [ "$(tr -d '\000' < "$F" | wc -c)" = "$size" ]; then c1=1; fi
fi
[ -f input.txt ] || exit 3
if [ "$c1" = 1 ]; then ANS=$F; else ANS=/dev/null; fi
awk -v c1="$c1" '
# one trimmed, non-empty input line -> PCENTS (signed whole cents); 0 if the line is not an amount
function parse(s,    neg, dot, ip, fp) {
  neg = 0
  if (substr(s, 1, 1) == "(") {
    if (length(s) < 3 || substr(s, length(s), 1) != ")") return 0
    neg = 1; s = substr(s, 2, length(s) - 2)
  } else if (substr(s, 1, 1) == "-") { neg = 1; s = substr(s, 2) }
  else if (substr(s, 1, 1) == "+") s = substr(s, 2)
  if (substr(s, 1, 1) == "$") s = substr(s, 2)
  dot = index(s, ".")
  if (dot) { ip = substr(s, 1, dot - 1); fp = substr(s, dot + 1) } else { ip = s; fp = "" }
  if (dot && fp !~ /^[0-9][0-9]?$/) return 0
  if (ip ~ /^[0-9]+$/) { }
  else if (ip ~ /^[0-9][0-9]?[0-9]?(,[0-9][0-9][0-9])+$/) gsub(/,/, "", ip)
  else return 0
  if (length(fp) == 1) fp = fp "0"; else if (fp == "") fp = "00"
  PCENTS = ip * 100 + fp
  if (neg) PCENTS = -PCENTS
  return 1
}
FNR == NR {
  line = $0
  gsub(/^[ \t\r]+/, "", line); gsub(/[ \t\r]+$/, "", line)
  if (line == "") next
  if (!parse(line)) { bad = 1; next }
  total += PCENTS
  n++
  next
}
{ ans = ans $0 "\n" }
END {
  if (bad || n == 0) exit 3
  a = total < 0 ? -total : total
  ip = int(a / 100); fp = a - ip * 100
  want = sprintf("Total: %s%.0f.%02d", (total < 0 ? "-" : ""), ip, fp)
  gsub(/^[ \t\r\n]+/, "", ans); gsub(/[ \t\r\n]+$/, "", ans)
  c2 = (c1 == 1 && index(ans, "\n") == 0 && ans ~ /^Total: -?[0-9]+\.[0-9][0-9]$/) ? 1 : 0
  c3 = (c2 == 1 && ans == want) ? 1 : 0
  p = c1 + c2 + c3
  printf "{\"n_passed\":%d,\"n_criteria\":3,\"all_pass\":%s}\n", p, (p == 3 ? "true" : "false")
}' input.txt "$ANS"
'''

# ── the instances ────────────────────────────────────────────────────────────────────────────

# One of each notation per instance; the order they appear in is shuffled by the instance's hash.
STYLES = ("plain", "plain-positive", "thousands-negative", "thousands-positive", "paren", "paren-currency-thousands",
          "negative-currency", "currency", "one-decimal", "integer", "plus", "trailing-space")


@dataclass(frozen=True)
class HardInstance:
    task_id: str                       # "<domain>/hard-<n>"
    domain: str
    lines: tuple[str, ...]             # as written, without the line ending
    eol: str                           # "\n" or "\r\n", for every line
    expected_total: str                # canonical, from the reference parser

    @property
    def input_text(self) -> str:
        return "".join(line + self.eol for line in self.lines)

    @property
    def expected_line(self) -> str:
        return f"Total: {self.expected_total}"


def _h(task_id: str, what: str, i: int = 0) -> int:
    d = hashlib.sha256(f"{SPEC_NAME}/{task_id}/{what}/{i}".encode()).digest()
    return int.from_bytes(d[:8], "big")


def _grouped(cents: int) -> str:
    """Whole cents (positive) as `1,234.56`."""
    return f"{cents // 100:,}.{cents % 100:02d}"


def _written(task_id: str, style: str, i: int) -> tuple[str, int]:
    """(the line as written, the signed cents it stands for) for one notation. The cents come from the generator's own
    intent, not from the parser: a test compares the two."""
    h = _h(task_id, style, i)
    flip = -1 if (h >> 40) & 1 else 1
    small = 1 + h % 50_000                          # 0.01 .. 500.00
    big = 100_000 + h % 900_000                     # 1,000.00 .. 9,999.99
    if style == "plain":
        c = -small
        return format_cents(c), c
    if style == "plain-positive":
        return format_cents(small), small
    if style == "thousands-negative":
        return "-" + _grouped(big), -big
    if style == "thousands-positive":
        return _grouped(big), big
    if style == "paren":
        return f"({format_cents(small)})", -small
    if style == "paren-currency-thousands":
        return f"(${_grouped(big)})", -big
    if style == "negative-currency":
        return f"-${format_cents(small)}", -small
    if style == "currency":
        return f"${format_cents(small)}", small
    if style == "one-decimal":
        c = (1 + h % 4_999) * 10
        c += 10 if c % 100 == 0 else 0              # a real second digit would be 0: keep it a one-decimal amount
        text = f"{c // 100}.{c // 10 % 10}"
        return ("-" + text, -c) if flip < 0 else (text, c)
    if style == "integer":
        c = (1 + h % 499) * 100
        return (f"-{c // 100}", -c) if flip < 0 else (str(c // 100), c)
    if style == "plus":
        if (h >> 41) & 1:                           # `+0.5`: one decimal
            c = (1 + h % 99) * 10
            c += 10 if c % 100 == 0 else 0
            return f"+{c // 100}.{c // 10 % 10}", c
        return f"+{format_cents(small)}", small
    if style == "trailing-space":
        c = flip * small
        return format_cents(c) + ("  " if (h >> 42) & 1 else " \t"), c
    raise AssertionError(style)


def generate(task_id: str) -> tuple[tuple[str, ...], str, tuple[int, ...]]:
    """(lines, line ending, the signed cents each line was written for) of one instance, a pure function of its id."""
    order = sorted(range(len(STYLES)), key=lambda i: _h(task_id, "order", i))
    pairs = [_written(task_id, STYLES[i], i) for i in order]
    eol = CRLF if _h(task_id, "eol") % 2 else "\n"
    return tuple(p[0] for p in pairs), eol, tuple(p[1] for p in pairs)


def _build_instances() -> dict[str, HardInstance]:
    out: dict[str, HardInstance] = {}
    for domain in DOMAINS:
        for n in range(1, TASKS_PER_DOMAIN + 1):
            tid = f"{domain}/hard-{n}"
            lines, eol, _cents = generate(tid)
            text = "".join(line + eol for line in lines)
            out[tid] = HardInstance(tid, domain, lines, eol, expected_total_from_input(text))
    return out


INSTANCES: Mapping[str, HardInstance] = _build_instances()


def instance(task_id: str) -> HardInstance:
    try:
        return INSTANCES[task_id]
    except (KeyError, TypeError):
        raise ValueError(f"unknown sum-signed-hard task {str(task_id)[:60]!r}") from None


def task_prompt_text(task_id: str) -> str:
    """The task as the solver receives it: the output contract, no reading rules, no expected value, no hint of the check."""
    instance(task_id)
    return (
        f"# TASK {task_id}\n\n"
        "## Instructions\n"
        "`input.txt` in the working directory lists twelve signed ledger amounts, one per line. They were copied from "
        "several source systems, so the lines are not all written the same way. Credits are positive and debits are "
        "negative. Add all twelve and write the signed total with exactly two "
        "decimals to `output/answer.txt` as one line in this form:\n\n"
        "    Total: 123.45\n\n"
        "A negative total keeps its minus sign (`Total: -7.05`). Write nothing else in the file.\n\n"
        "## Deliverables\n- `output/answer.txt`\n"
    )


TASKSET = TaskSet(
    name="hard", spec_name=SPEC_NAME, instances=INSTANCES, prompt_text=task_prompt_text,
    base_files={"systemprompt.md": BASE_SYSTEM_PROMPT, "guardrails.md": BASE_GUARDRAILS},
    verify_sh=VERIFY_SH, prompt_fields=PROMPT_FIELDS, default_task=DEFAULT_TASK, enforce_slot_table=True,
    description="sum-signed-hard: 12 amounts per instance in mixed notations, a base harness that does not say how to read them")


def solver_task(task_ref: str):
    """The prompt and the input files a solver session gets, as `substrate.SolverTask` (the `task_loader`)."""
    return TASKSET.solver_task(task_ref)


def spec_fields(root) -> dict:
    """Keyword arguments for upstream's `BenchSpec` (see `sum_signed.spec_fields`); only the name differs."""
    return {**ss.spec_fields(root), "name": SPEC_NAME}
