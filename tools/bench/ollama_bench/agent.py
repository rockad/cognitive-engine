"""Mini-agent tasks and the host-side agent loop.

The loop sends the conversation to the model, executes the tool calls it returns inside a throwaway directory
(see sandbox.py) and feeds the results back, for at most `max_steps` steps and `task_timeout` seconds.
"""
from __future__ import annotations

import json
import tempfile
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .client import ChatResult, is_malformed_tool_call
from .sandbox import Sandbox
from .scoring import MAX_STEPS, NUM_CTX, REQUEST_TIMEOUT_S, TASK_TIMEOUT_S
from .tools import agent_tools

AGENT_SYSTEM = (
    "You are an autonomous coding agent working in a sandboxed workspace directory (the current directory). "
    "Paths are relative to the workspace root; absolute paths and '..' are refused.\n"
    "Work step by step with the tools: list_dir to look around, read_file to read, write_file to create files "
    "(missing directories are created), edit_file for exact replacements in an existing file.\n"
    "Only use the provided tools; there is no shell.\n"
    "Prefer the smallest change that does the task. When the task is complete, reply with one short sentence "
    "and stop calling tools.\n"
)

Grader = Callable[[Path, Sandbox], tuple[bool, str | None, str]]   # (passed, category, detail)
ChatFn = Callable[[list[dict[str, Any]], list[dict[str, Any]], float], ChatResult]


@dataclass
class Task:
    id: str
    kind: str                      # sum-signed | edit-prompt
    system: str
    prompt: str
    files: dict[str, str]
    grade: Grader
    meta: dict[str, Any] = field(default_factory=dict)


# ── edit-prompt ───────────────────────────────────────────────────────────────────────────

def _lines(text: str) -> list[str]:
    return text.rstrip("\n").split("\n")


def expected_insert_index(orig: str, section: str | None) -> int:
    """Index (in the original's lines) at which the new last bullet belongs."""
    lines = _lines(orig)
    if section is None:
        return len(lines)
    start = next((i for i, ln in enumerate(lines) if ln == f"## {section}"), None)
    if start is None:
        raise ValueError(f"section {section!r} not found in the original")
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("#"):
            end = i
            break
    last = max(i for i in range(start + 1, end) if lines[i].strip())
    return last + 1


def check_edit(orig: str, new: str, rule: str, section: str | None) -> tuple[bool, str]:
    """New line present exactly once and in place, every other line unchanged byte for byte, no new frontmatter."""
    if not orig.startswith("---") and new.startswith("---"):
        return False, "frontmatter was added"
    new_lines = _lines(new)
    orig_lines = _lines(orig)
    n = new_lines.count(rule)
    if n != 1:
        return False, f"the new line is present {n} times (want exactly once)"
    idx = new_lines.index(rule)
    rest = new_lines[:idx] + new_lines[idx + 1:]
    if rest != orig_lines:
        diff = next((i for i, (a, b) in enumerate(zip(rest, orig_lines)) if a != b), min(len(rest), len(orig_lines)))
        return False, f"other lines changed (first difference at line {diff + 1}; {len(rest)} vs {len(orig_lines)} lines)"
    want = expected_insert_index(orig, section)
    if idx != want:
        return False, f"new line at index {idx}, expected {want}"
    return True, "ok"


@dataclass(frozen=True)
class EditSpec:
    id: str
    path: str
    original: str
    rule: str
    section: str | None


EDIT_SPECS: list[EditSpec] = [
    EditSpec("edit-prompt/bullets-at-end", "prompts/reviewer.md",
             "# Reviewer prompt\n\nYou review pull requests for a small Python service.\n\n## Rules\n\n"
             "- Be concise.\n- Quote the line you comment on.\n- Never approve a change without reading the tests.\n",
             "- Flag any new dependency.", "Rules"),
    EditSpec("edit-prompt/frontmatter", "agents/triage.md",
             "---\nname: triage\ndescription: Sorts incoming issues\nmodel: small\n---\n\n"
             "You triage GitHub issues.\n\n## Rules\n\n- Label every issue.\n"
             "- Ask for a reproduction when a bug report lacks one.\n",
             "- Never close an issue without a comment.", "Rules"),
    EditSpec("edit-prompt/section-in-middle", "prompts/planner.md",
             "# Planner\n\nBreak the task into steps.\n\n## Rules\n\n- One step per bullet.\n"
             "- Keep steps under 15 words.\n- Order steps by dependency.\n\n## Output format\n\n"
             "Return a numbered list.\n",
             "- Mark risky steps with (!).", "Rules"),
    EditSpec("edit-prompt/whitespace-traps", "prompts/writer.md",
             "# Writer prompt\n\nStyle notes for the docs writer.  \n\n## Rules\n\n- Use active voice.\n"
             "\t- Prefer \"use\" over \"utilise\".\n- Keep paragraphs short.   \n- Spell out acronyms on first use.\n",
             "- Link to the glossary for domain terms.", "Rules"),
]


def make_edit_task(spec: EditSpec) -> Task:
    prompt = (f"# TASK {spec.id}\n\nIn `{spec.path}`, add the rule `{spec.rule}` as the last bullet of the Rules "
              "list. Change nothing else in the file: every other line must stay exactly as it is.\n")

    def grade(root: Path, sb: Sandbox) -> tuple[bool, str | None, str]:
        p = root / spec.path
        new = p.read_bytes().decode("utf-8", "replace") if p.is_file() and not p.is_symlink() else None
        if new is None or new == spec.original:
            if any(w != spec.path for w in sb.written):
                return False, "wrong_file", f"wrote {sorted(set(sb.written))}, not {spec.path}"
            return False, None, "the file was not changed"
        ok, why = check_edit(spec.original, new, spec.rule, spec.section)
        return ok, (None if ok else "edit_violation"), why

    return Task(spec.id, "edit-prompt", AGENT_SYSTEM, prompt, {spec.path: spec.original}, grade,
                {"path": spec.path})


# ── sum-signed ────────────────────────────────────────────────────────────────────────────

def make_sum_tasks() -> list[Task]:
    from cognitive_harness2 import sum_signed as ss

    tasks: list[Task] = []
    for tid, inst in ss.INSTANCES.items():
        def grade(root: Path, sb: Sandbox, _inst=inst) -> tuple[bool, str | None, str]:
            p = root / ss.ANSWER_PATH
            raw = p.read_bytes() if p.is_file() and not p.is_symlink() else None
            verdict = ss.grade_answer(raw, _inst.expected_total)
            if verdict["all_pass"]:
                return True, None, "ok"
            if raw is None:
                if sb.written:
                    return False, "wrong_file", f"wrote {sorted(set(sb.written))}, not {ss.ANSWER_PATH}"
                return False, None, "no answer file"
            if verdict["n_passed"] < 2:
                return False, "bad_format", f"answer {raw[:80]!r} is not a single 'Total: X.XX' line"
            return False, "wrong_arithmetic", f"answer {raw[:40]!r}, expected 'Total: {_inst.expected_total}'"

        tasks.append(Task(f"sum-signed/{tid}", "sum-signed", AGENT_SYSTEM + ss.BASE_SYSTEM_PROMPT,
                          ss.task_prompt_text(tid), {"input.txt": inst.input_text}, grade,
                          {"expected": inst.expected_total}))
    return tasks


def build_tasks() -> list[Task]:
    """Interleaved sum, edit, sum, edit, ...: a truncated phase is still representative."""
    sums = make_sum_tasks()
    edits = [make_edit_task(s) for s in EDIT_SPECS]
    out: list[Task] = []
    for i in range(max(len(sums), len(edits))):
        if i < len(sums):
            out.append(sums[i])
        if i < len(edits):
            out.append(edits[i])
    return out


# ── the loop ──────────────────────────────────────────────────────────────────────────────

def _assistant_message(res: ChatResult) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant", "content": res.content}
    calls = (res.message.get("tool_calls") or [])
    if calls:
        msg["tool_calls"] = [{"function": {"name": (c.get("function") or {}).get("name"),
                                           "arguments": (c.get("function") or {}).get("arguments") or {}}}
                             for c in calls]
    return msg                                # thinking is deliberately not sent back


def run_agent_task(task: Task, chat: ChatFn, *, max_steps: int = MAX_STEPS, task_timeout: float = TASK_TIMEOUT_S,
                   request_timeout: float = REQUEST_TIMEOUT_S, clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    tools = agent_tools()
    steps: list[dict[str, Any]] = []
    outcome_cats: Counter[str] = Counter()
    seen: Counter[str] = Counter()
    stop = "finished"                      # finished | timeout | error | malformed | loops | max_steps
    err = None
    t0 = clock()
    with tempfile.TemporaryDirectory(prefix="bench-agent-") as tmp:
        root = Path(tmp)
        for rel, text in task.files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        sb = Sandbox(root)
        messages: list[dict[str, Any]] = [{"role": "system", "content": task.system},
                                          {"role": "user", "content": task.prompt}]
        stop = "max_steps"
        for _ in range(max_steps):
            remaining = task_timeout - (clock() - t0)
            if remaining <= 1.0:
                stop = "timeout"
                break
            res = chat(messages, tools, min(request_timeout, remaining))
            step: dict[str, Any] = {"stats": res.stats, "ok": res.ok, "raw": res.raw, "error": res.error,
                                    "tool_results": []}
            steps.append(step)
            if not res.ok:
                stop = "timeout" if res.timed_out else ("malformed" if is_malformed_tool_call(res) else "error")
                err = res.error
                break
            calls = res.tool_calls
            step["n_calls"] = len(calls)
            if not calls:
                stop = "finished"
                break
            messages.append(_assistant_message(res))
            looped = False
            for c in calls:
                out = sb.execute(c["name"] or "", c["arguments"])
                step["tool_results"].append({"name": c["name"], "ok": out.ok, "category": out.category,
                                             "text": out.text[:300]})
                if not out.ok and out.category:
                    outcome_cats[out.category] += 1
                messages.append({"role": "tool", "tool_name": c["name"], "content": out.text})
                key = json.dumps([c["name"], c["arguments"]], sort_keys=True, default=str)
                seen[key] += 1
                if seen[key] >= 3:
                    looped = True
            if looped:
                stop = "loops"
                break
        else:
            stop = "max_steps"
        wall = clock() - t0
        passed, grade_cat, detail = task.grade(root, sb)
        written = list(sb.written)
        escapes = sb.escape_attempts

    category = None if passed else classify_failure(stop, grade_cat, steps, outcome_cats)
    stats = [s["stats"] for s in steps]
    return {
        "task_id": task.id, "kind": task.kind, "passed": passed, "category": category, "detail": detail,
        "stop": stop, "error": err, "steps_used": len(steps), "wall_s": round(wall, 3),
        "prompt_tokens": sum(s.get("prompt_eval_count") or 0 for s in stats),
        "gen_tokens": sum(s.get("eval_count") or 0 for s in stats),
        "thinking_chars": sum(s.get("thinking_chars") or 0 for s in stats),
        "tool_outcomes": dict(outcome_cats), "escape_attempts": escapes, "written": written,
        "final_text": (steps[-1]["raw"] or {}).get("message", {}).get("content", "")[:300] if steps and steps[-1]["raw"] else "",
        "steps": steps,
    }


def classify_failure(stop: str, grade_cat: str | None, steps: list[dict[str, Any]],
                     outcome_cats: Counter[str]) -> str:
    """The one failure category of a failed task (see PREREGISTRATION.md for the order)."""
    if stop == "timeout":
        return "timeout"
    if stop == "error":
        return "error"
    if stop == "malformed":
        return "malformed_args"
    if stop == "loops":
        return "loops"
    last = steps[-1] if steps else None
    if last and last.get("ok") and (last["stats"].get("done_reason") == "length") and not last.get("n_calls"):
        return "truncation"
    if any((s["stats"].get("prompt_eval_count") or 0) >= NUM_CTX - 32 for s in steps if s.get("ok")):
        return "context"
    if grade_cat:
        return grade_cat
    total_calls = sum(s.get("n_calls") or 0 for s in steps)
    if total_calls == 0:
        return "no_tool_call"
    if outcome_cats.get("bad_args") or outcome_cats.get("unavailable"):
        return "malformed_args"
    if stop == "max_steps":
        return "max_steps"
    return "gave_up"
