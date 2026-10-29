"""`sum-signed`: a tiny deterministic task and its verifier, for the k=1 solver side (slice 3b, WS-E).

The task. `input.txt` holds five signed amounts, one per line (`-12.50`, `130.05`, ...). The solver writes
`Total: X.XX`, the signed sum to two decimals, to `output/answer.txt`. A naive "sum the positives" or a float
formatting slip gives a wrong answer, so the task has something to be wrong about while staying checkable in a
few lines of shell.

Two implementations of ONE verdict, kept in step by tests on the same cases:

  * `VERIFY_SH`   a busybox `sh` + `awk` script. It runs in `DockerRunner` (`--network none`, `--read-only`, the
                  default alpine image, no python) and prints one JSON object. This is the authority.
  * `grade_answer` the same rules in Python. `SumSignedAdapter.score` uses it, so scoring needs no container;
                  the tests run both on every case and require equal verdicts.

Three criteria, each pass or fail, `all_pass` when all three hold:

  1. the answer file exists as a regular file (not a link), is at most 4096 bytes and holds no NUL byte
  2. its content, trimmed of leading and trailing space, tab, CR and LF, is `Total: ` and a number written as
     optional `-`, one or more ASCII digits, `.`, exactly two ASCII digits, and nothing else (so no extra text,
     no second line, no `+`, no `,`, no unicode digits, no exponent)
  3. that line equals `Total: ` and the expected total in its one canonical spelling: no leading zeros, no
     sign on zero (`-0.00` is wrong, the total is `0.00`), and the digits exactly: a rounding difference or a
     huge number fails

The answer is judged as the text the verifier container receives: the bytes decoded as UTF-8 with undecodable
bytes replaced (such a file can never pass criterion 2 either way). The verifier does its arithmetic in whole
cents, so there is no float rounding to disagree about.

The adapter is DUCK TYPED. This module imports nothing from upstream (importing `harness2.substrate` or
`harness2.benchmarks` runs `harness2.config`, which reads a `.env` into `os.environ`). It satisfies upstream's
`BenchmarkAdapter` Protocol by shape; `spec` and `substrate` are handed in. Its `.adapt_complete` marker is
written by a local copy of upstream's `runmark` (`mark_complete`, `fingerprint`, ...) that a test pins to upstream.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

SPEC_NAME = "sum-signed"
ANSWER_PATH = "output/answer.txt"
MAX_ANSWER_BYTES = 4096
N_CRITERIA = 3
DOMAINS = ("household", "retail", "payroll")
TASKS_PER_DOMAIN = 2
AMOUNTS_PER_TASK = 5
_TRIM = " \t\r\n"
_LINE = re.compile(r"Total: -?[0-9]+\.[0-9][0-9]")
_AMOUNT = re.compile(r"[+-]?[0-9]+\.[0-9][0-9]")

# What the benchmark's BenchPrompts carries. BenchPrompts refuses the words benchmark, evaluation, grader,
# rubric and score anywhere in these (it describes the WORK, not its measurement); a test pins that.
PROMPT_FIELDS: Mapping[str, str] = {
    "work": "add up a short list of signed amounts (credits positive, debits negative) and report the total",
    "domain_word": "ledger",
    "deliverable_word": "answer file",
}

BASE_SYSTEM_PROMPT = (
    "You are a careful bookkeeper working in a small workspace.\n"
    "Read the input file, add the amounts exactly (cents, not floating point), and write the result to the\n"
    "answer file named in the task. Check the sign of every amount before you add it.\n"
)

# ── the verifier: busybox sh + awk, prints {"n_passed":N,"n_criteria":3,"all_pass":true|false} ─────────────

VERIFY_SH = r'''#!/bin/sh
# sum-signed verifier. busybox sh + awk only. Reads ./input.txt and ./output/answer.txt, prints one JSON line.
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
FNR == NR {
  line = $0
  gsub(/^[ \t\r]+/, "", line); gsub(/[ \t\r]+$/, "", line)
  if (line == "") next
  if (line !~ /^[+-]?[0-9]+\.[0-9][0-9]$/) { bad = 1; next }
  neg = 0
  if (substr(line, 1, 1) == "-") { neg = 1; line = substr(line, 2) } else if (substr(line, 1, 1) == "+") line = substr(line, 2)
  dot = index(line, ".")
  cents = substr(line, 1, dot - 1) * 100 + substr(line, dot + 1)
  total += neg ? -cents : cents
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


# ── the task instances ───────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Instance:
    task_id: str                       # "<domain>/<name>"
    domain: str
    amounts: tuple[str, ...]           # as written in input.txt: "-12.50", "130.05"
    expected_total: str                # canonical: "117.55", "-3.20", "0.00"

    @property
    def input_text(self) -> str:
        return "".join(a + "\n" for a in self.amounts)

    @property
    def expected_line(self) -> str:
        return f"Total: {self.expected_total}"


def _cents(amount: str) -> int:
    if not _AMOUNT.fullmatch(amount):
        raise ValueError(f"not an amount with two decimals: {amount!r}")
    d = Decimal(amount)
    return int(d * 100)


def format_cents(cents: int) -> str:
    """Whole cents as the one canonical spelling. Zero has no sign."""
    sign = "-" if cents < 0 else ""
    a = abs(cents)
    return f"{sign}{a // 100}.{a % 100:02d}"


def expected_total(amounts: tuple[str, ...] | list[str]) -> str:
    return format_cents(sum(_cents(a) for a in amounts))


def _amounts_for(task_id: str) -> tuple[str, ...]:
    """Five amounts from -500.00 to 499.99, a pure function of the task id (sha256), so nothing drifts."""
    digest = hashlib.sha256(f"{SPEC_NAME}/{task_id}".encode()).digest()
    out: list[str] = []
    for i in range(AMOUNTS_PER_TASK):
        cents = int.from_bytes(digest[2 * i:2 * i + 2], "big") % 100000 - 50000
        if i == 0:
            cents = abs(cents)             # at least one credit and one debit, so the sign matters
        elif i == 1:
            cents = -abs(cents)
        cents = cents or 1
        out.append(format_cents(cents))
    return tuple(out)


def _build_instances() -> dict[str, Instance]:
    out: dict[str, Instance] = {}
    for domain in DOMAINS:
        for n in range(1, TASKS_PER_DOMAIN + 1):
            tid = f"{domain}/sum-{n}"
            amounts = _amounts_for(tid)
            out[tid] = Instance(tid, domain, amounts, expected_total(amounts))
    return out


INSTANCES: Mapping[str, Instance] = _build_instances()


def instance(task_id: str) -> Instance:
    try:
        return INSTANCES[task_id]
    except (KeyError, TypeError):
        raise ValueError(f"unknown sum-signed task {str(task_id)[:60]!r}") from None


def task_prompt_text(task_id: str) -> str:
    """The task as the solver receives it: no expected value, no hint about how it is checked."""
    instance(task_id)
    return (
        f"# TASK {task_id}\n\n"
        "## Instructions\n"
        "`input.txt` in the working directory lists five signed amounts, one per line. Credits are positive and "
        "debits are negative. Add all five exactly, in cents, and write the signed total with exactly two "
        "decimals to `output/answer.txt` as one line in this form:\n\n"
        "    Total: 123.45\n\n"
        "A negative total keeps its minus sign (`Total: -7.05`). Write nothing else in the file.\n\n"
        "## Deliverables\n- `output/answer.txt`\n"
    )


# ── the verdict, in Python ───────────────────────────────────────────────────────────────────

def staged_text(raw: bytes) -> str:
    """The answer as the verifier container receives it (UTF-8, undecodable bytes replaced)."""
    return raw.decode("utf-8", "replace")


def grade_answer(raw: bytes | None, expected: str) -> dict[str, Any]:
    """The verdict `VERIFY_SH` gives, computed in Python. `raw` is the answer file's bytes, or None if absent."""
    c1 = c2 = c3 = False
    if raw is not None:
        text = staged_text(raw)
        c1 = len(text.encode("utf-8")) <= MAX_ANSWER_BYTES and "\0" not in text
        if c1:
            body = text.strip(_TRIM)
            c2 = _LINE.fullmatch(body) is not None
            c3 = c2 and body == f"Total: {expected}"
    n = int(c1) + int(c2) + int(c3)
    return {"n_passed": n, "n_criteria": N_CRITERIA, "all_pass": n == N_CRITERIA}


def verifier_files(task_id: str, raw: bytes | None, *, input_text: str | None = None) -> dict[str, str]:
    """What the verifier container is given: the script, the input and (if there is one) the answer.

    `input_text` replaces the task's input (used by the tests of the verifier itself, to reach totals such as
    zero or a negative one that no fixed instance has)."""
    files = {"verify.sh": VERIFY_SH, "input.txt": instance(task_id).input_text if input_text is None else input_text}
    if raw is not None:
        files[ANSWER_PATH] = staged_text(raw)
    return files


def parse_verdict(stdout: str) -> dict[str, Any]:
    try:
        got = json.loads(stdout)
    except ValueError:
        raise ValueError("the verifier printed no JSON verdict") from None
    ok = (isinstance(got, dict) and set(got) == {"n_passed", "n_criteria", "all_pass"}
          and isinstance(got["n_passed"], int) and not isinstance(got["n_passed"], bool)
          and got["n_criteria"] == N_CRITERIA and isinstance(got["all_pass"], bool)
          and 0 <= got["n_passed"] <= N_CRITERIA)
    if not ok:
        raise ValueError("the verifier's JSON verdict has an unexpected shape")
    return got


def verify_in_container(runner: Any, task_id: str, raw: bytes | None, *, timeout_s: int = 60,
                        input_text: str | None = None) -> dict[str, Any]:
    """Score an answer with `VERIFY_SH` in a `DockerRunner`-like `runner` (`.run(SandboxSpec) -> SandboxResult`)."""
    from .sandbox import SandboxSpec   # local: this module's pure parts import nothing that touches docker

    res = runner.run(SandboxSpec(command=("sh", "verify.sh"), timeout_s=timeout_s,
                                 files=verifier_files(task_id, raw, input_text=input_text)))
    if res.timed_out or res.exit_code != 0:
        raise RuntimeError(f"the verifier container failed (exit {res.exit_code}, timed_out={res.timed_out})")
    return parse_verdict(res.stdout)


# ── a local copy of upstream's runmark (harness2.core.runmark), pinned to it by a test ───────

MARK = ".adapt_complete"
MARK_VERSION = 1


def _ignored(p: Path) -> bool:
    return "__pycache__" in p.parts or p.suffix == ".pyc" or p.name == ".DS_Store"


def fingerprint(harness_dir: Path | None) -> str:
    if harness_dir is None:
        return "base"
    root = Path(harness_dir)
    if not root.is_dir():
        return "absent"
    h = hashlib.sha1()
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.name == MARK or _ignored(p):
            continue
        h.update(str(p.relative_to(root)).encode())
        h.update(b"\0")
        try:
            h.update(p.read_bytes())
        except OSError:
            h.update(b"<unreadable>")
        h.update(b"\0")
    return h.hexdigest()[:16]


def mark_complete(where: Path, *, run_id: str, harness_dir: Path | None = None, extra: dict | None = None) -> Path:
    d = Path(where)
    d.mkdir(parents=True, exist_ok=True)
    payload = {"version": MARK_VERSION, "run_id": run_id, "at": time.time(), "harness": fingerprint(harness_dir),
               **(extra or {})}
    tmp = d / f"{MARK}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    final = d / MARK
    os.replace(tmp, final)
    return final


def read_mark(where: Path) -> dict | None:
    try:
        return json.loads((Path(where) / MARK).read_text())
    except Exception:  # noqa: BLE001 - mirrors upstream: unreadable means no marker
        return None


def is_complete(where: Path, *, harness_dir: Path | None = None) -> bool:
    m = read_mark(where)
    if not m or m.get("version") != MARK_VERSION:
        return False
    return m.get("harness") == fingerprint(harness_dir)


# ── the adapter ──────────────────────────────────────────────────────────────────────────────

def spec_fields(root: Path) -> dict[str, Any]:
    """Keyword arguments for upstream's `BenchSpec` (the caller adds `prompts=BenchPrompts(**PROMPT_FIELDS)` and
    `channels`). Kept here as plain data so this module needs no upstream import."""
    return dict(name=SPEC_NAME, runs_root=Path(root) / "runs", install_mode="workspace_agents_md",
                domain_word="ledger", leak_globs=("verify.sh", "expected*"), obs_trunc=50_000)


class SumSignedAdapter:
    """Upstream's `BenchmarkAdapter`, by duck typing. `spec` is a BenchSpec, `substrate` a CognitiveSubstrate.

    `execute` hands the run to `substrate.execute(task_ref, install, [run_id], timeout)` (the one place a model
    session can start, and only with an ExecutionGrant), then checks that the rollout left a `metrics.json`
    and writes `.adapt_complete` itself, as upstream's lab adapter does.
    """

    def __init__(self, root: Path, spec: Any, substrate: Any, *, results_root: Path | None = None,
                 timeout: int = 600, verifier: Any = None,
                 mark: Callable[..., Path] = mark_complete, is_done: Callable[..., bool] = is_complete,
                 read_marker: Callable[[Path], dict | None] = read_mark, fp: Callable[..., str] = fingerprint):
        self.root = Path(root)
        self.spec, self.substrate = spec, substrate
        self.results_root = Path(results_root) if results_root is not None else self.root / "results"
        self.timeout = int(timeout)
        self._verifier = verifier           # optional DockerRunner: `container_score` uses it; `score` never does
        self._mark, self._is_done, self._read_mark, self._fp = mark, is_done, read_marker, fp
        for task_id, inst in INSTANCES.items():
            src = self.task_files(task_id)
            assert src is not None
            src.mkdir(parents=True, exist_ok=True)
            (src / "input.txt").write_text(inst.input_text, encoding="utf-8")
        self._write_base()

    # ── what the method asks ─────────────────────────────────────────────────────────
    def tasks(self, split: str = "all") -> list[str]:
        return list(INSTANCES)

    def domain_of(self, task_id: str) -> str:
        return instance(task_id).domain

    def task_prompt(self, task_id: str) -> str:
        return task_prompt_text(task_id)

    def task_files(self, task_id: str) -> Path | None:
        instance(task_id)
        return self.root / "sources" / task_id

    def base_harness(self) -> Path:
        return self.root / "base"

    def _write_base(self) -> None:
        surface = self.substrate.surface()
        base = self.base_harness()
        for name in surface.always_on:
            p = base / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(BASE_SYSTEM_PROMPT if name == "systemprompt.md" else "", encoding="utf-8")

    def validate_tree(self, tree: dict[str, str]) -> None:
        """Harness invariants for this task, as actionable prose. Every problem is listed."""
        problems: list[str] = []
        if not isinstance(tree, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in tree.items()):
            raise ValueError("the harness tree must map file names to text")
        if not tree.get("systemprompt.md", "").strip():
            problems.append("systemprompt.md is missing or empty: it is the solver's standing instruction")
        for name, text in tree.items():
            for ln, line in enumerate(text.splitlines(), 1):
                if "answer.txt" in line and "output/answer.txt" not in line:
                    problems.append(f"{name}:{ln} names answer.txt without its output/ directory: the answer file "
                                    "is always output/answer.txt")
        if problems:
            raise ValueError("The sum-signed harness is invalid:\n- " + "\n- ".join(problems))

    # ── running ──────────────────────────────────────────────────────────────────────
    def _run_dir(self, run_id: str) -> Path:
        return self.results_root / run_id

    def _safe_rmtree(self, d: Path) -> None:
        d = d.resolve()
        root = self.results_root.resolve()
        if d == root or root not in d.parents:
            raise ValueError(f"refusing to remove {str(d)!r}: it is not inside the results root")
        shutil.rmtree(d, ignore_errors=True)

    def _output_fingerprint(self, run_dir: Path) -> str | None:
        return self._fp(Path(run_dir) / "output") if self.delivered(run_dir) else None

    def artifacts_intact(self, run_dir: Path) -> bool:
        marker = self._read_mark(run_dir) or {}
        return ((Path(run_dir) / "metrics.json").is_file() and "output_fingerprint" in marker
                and marker["output_fingerprint"] == self._output_fingerprint(run_dir))

    def execute(self, task_id: str, harness_dir: Path | None, run_id: str, *, timeout: int | None = None) -> Path:
        instance(task_id)
        out = self._run_dir(run_id)
        if self._is_done(out, harness_dir=harness_dir) and self.artifacts_intact(out):
            return out
        if out.is_dir() and any(out.iterdir()):
            self._safe_rmtree(out)
        if harness_dir is None:
            install = self._base_install()
        else:
            staged = self.substrate.materialise(Path(harness_dir), self.spec, {})
            install = type(staged)(staged.staging_dir, staged.fingerprint, {**staged.extras, "harness": "staged"})
        try:
            self.substrate.execute(task_id, install, [run_id], int(timeout or self.timeout))
        finally:
            if install.extras.get("harness") == "staged":
                self.substrate.release(install)
        if not (out / "metrics.json").is_file():
            why = ""
            try:
                why = ": " + str(json.loads((out / "error.json").read_text())["error"])[:300]
            except (OSError, ValueError, KeyError, TypeError):
                pass
            raise RuntimeError(f"sum-signed rollout did not produce metrics.json: {out}{why}. The session stream had "
                               "no parseable events (dead chain, missing binary, refused or out of budget), so this "
                               "failure must not be cached")
        self._mark(out, run_id=run_id, harness_dir=harness_dir,
                   extra={"output_fingerprint": self._output_fingerprint(out)})
        return out

    def _base_install(self) -> Any:
        from .substrate import StagedInstall   # local: StagedInstall has upstream's Install fields
        return StagedInstall(self.base_harness(), "base", {"harness": "base"})

    # ── reading a finished run ───────────────────────────────────────────────────────
    def delivered(self, run_dir: Path) -> bool:
        output_dir = Path(run_dir) / "output"
        return output_dir.is_dir() and any(p.is_file() for p in output_dir.rglob("*"))

    def trajectory(self, run_dir: Path, *, obs_trunc: int) -> str:
        stream = Path(run_dir) / "opencode_session.jsonl"
        if not stream.is_file():
            return f"(no trajectory recorded for {Path(run_dir).name})"
        return self.substrate.read_trajectory(stream, obs_trunc=obs_trunc, max_chars=10**9)

    def deliverable_files(self, run_dir: Path) -> dict[str, str]:
        out: dict[str, str] = {}
        base = Path(run_dir) / "output"
        if base.is_dir():
            for p in sorted(base.rglob("*")):
                if p.is_file() and not p.is_symlink():
                    out[p.relative_to(base).as_posix()] = p.read_bytes().decode("utf-8", "replace")
        return out

    def deliverable_text(self, run_dir: Path) -> str:
        files = self.deliverable_files(run_dir)
        if not files:
            return "(this run produced NO deliverables)"
        return "\n\n".join(f"----- FILE: {name} -----\n{text.strip() or '(empty file)'}" for name, text in files.items())

    # ── judging ──────────────────────────────────────────────────────────────────────
    def _answer_bytes(self, run_dir: Path) -> bytes | None:
        p = Path(run_dir) / ANSWER_PATH
        try:
            if p.is_symlink() or not p.is_file():
                return None
            with open(p, "rb") as f:
                return f.read(MAX_ANSWER_BYTES * 4 + 1)
        except OSError:
            return None

    def score(self, run_dir: Path, task_id: str) -> dict | None:
        """Pure code: no container, no model. `None` only for an unknown task (the judge failing, never a zero)."""
        try:
            inst = instance(task_id)
        except ValueError:
            return None
        return grade_answer(self._answer_bytes(run_dir), inst.expected_total)

    def container_score(self, run_dir: Path, task_id: str) -> dict:
        """The same verdict from the verifier script in a container (needs `verifier=` a DockerRunner)."""
        if self._verifier is None:
            raise RuntimeError("no verifier runner was given to this adapter")
        return verify_in_container(self._verifier, task_id, self._answer_bytes(run_dir))
