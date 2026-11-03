import json
from collections import Counter

import pytest

from fake_ollama import FakeOllama, chat_response
from ollama_bench import agent as ag
from ollama_bench.client import ChatResult, Ollama, response_stats
from cognitive_harness2 import sum_signed as ss


def reply(content="", calls=None, **kw):
    raw = chat_response(content, calls, **kw)
    return ChatResult(True, 0.1, raw, None, False, response_stats(raw, 0.1))


def scripted(steps):
    it = iter(steps)

    def chat(messages, tools, timeout):
        return next(it)
    return chat


# ── check_edit ────────────────────────────────────────────────────────────────────────────

ORIG = ag.EDIT_SPECS[2].original      # bullets in the middle, followed by '## Output format'
RULE = ag.EDIT_SPECS[2].rule


def inserted(orig, line, after):
    lines = orig.rstrip("\n").split("\n")
    i = lines.index(after) + 1
    return "\n".join(lines[:i] + [line] + lines[i:]) + "\n"


def test_edit_ok_in_the_middle_section():
    new = inserted(ORIG, RULE, "- Order steps by dependency.")
    assert ag.check_edit(ORIG, new, RULE, "Rules") == (True, "ok")


def test_edit_wrong_position_duplicate_changed_line_frontmatter():
    at_end = ORIG.rstrip("\n") + "\n" + RULE + "\n"
    ok, why = ag.check_edit(ORIG, at_end, RULE, "Rules")
    assert not ok and "expected" in why
    ok, why = ag.check_edit(ORIG, inserted(at_end, RULE, "- Order steps by dependency."), RULE, "Rules")
    assert not ok and "2 times" in why
    changed = inserted(ORIG, RULE, "- Order steps by dependency.").replace("Keep steps under 15", "Keep steps under 12")
    ok, why = ag.check_edit(ORIG, changed, RULE, "Rules")
    assert not ok and "other lines changed" in why
    ok, why = ag.check_edit(ORIG, "---\nname: x\n---\n" + inserted(ORIG, RULE, "- Order steps by dependency."), RULE, "Rules")
    assert not ok and "frontmatter" in why
    ok, why = ag.check_edit(ORIG, ORIG, RULE, "Rules")
    assert not ok and "0 times" in why


def test_edit_whitespace_traps_are_byte_exact():
    spec = ag.EDIT_SPECS[3]
    good = spec.original.rstrip("\n") + "\n" + spec.rule + "\n"
    assert ag.check_edit(spec.original, good, spec.rule, spec.section)[0]
    stripped = good.replace("short.   \n", "short.\n").replace("\t- Prefer", "    - Prefer")
    assert not ag.check_edit(spec.original, stripped, spec.rule, spec.section)[0]


def test_frontmatter_file_keeps_its_frontmatter():
    spec = ag.EDIT_SPECS[1]
    good = spec.original.rstrip("\n") + "\n" + spec.rule + "\n"
    assert ag.check_edit(spec.original, good, spec.rule, spec.section)[0]


# ── tasks ─────────────────────────────────────────────────────────────────────────────────

def test_task_set_matches_the_spec():
    tasks = ag.build_tasks()
    kinds = Counter(t.kind for t in tasks)
    assert kinds == {"sum-signed": 6, "edit-prompt": 4}
    assert len({t.id for t in tasks}) == 10
    assert [t.kind for t in tasks[:4]] == ["sum-signed", "edit-prompt", "sum-signed", "edit-prompt"]
    for t in tasks:
        if t.kind == "sum-signed":
            assert "output/answer.txt" in t.prompt and "input.txt" in t.files
            assert t.meta["expected"] not in t.prompt          # the answer is never leaked


def solve_sum(task):
    total = ss.expected_total(task.files["input.txt"].split())
    return [reply("", [("read_file", {"path": "input.txt"})]),
            reply("", [("write_file", {"path": "output/answer.txt", "content": f"Total: {total}\n"})]),
            reply("Done.")]


def test_loop_solves_every_sum_task_with_a_cooperative_model():
    for t in [t for t in ag.build_tasks() if t.kind == "sum-signed"]:
        r = ag.run_agent_task(t, scripted(solve_sum(t)))
        assert r["passed"] and r["category"] is None and r["steps_used"] == 3, (t.id, r["detail"])


def test_loop_solves_every_edit_task_with_edit_file():
    for spec in ag.EDIT_SPECS:
        t = ag.make_edit_task(spec)
        last = spec.original.rstrip("\n").split("\n")
        anchor = last[ag.expected_insert_index(spec.original, spec.section) - 1]
        steps = [reply("", [("read_file", {"path": spec.path})]),
                 reply("", [("edit_file", {"path": spec.path, "old_string": anchor, "new_string": anchor + "\n" + spec.rule})]),
                 reply("Done.")]
        r = ag.run_agent_task(t, scripted(steps))
        assert r["passed"], (spec.id, r["detail"])


def test_wrong_arithmetic_bad_format_wrong_file_and_gave_up_are_categorised():
    t = ag.make_sum_tasks()[0]
    bad = [reply("", [("write_file", {"path": "output/answer.txt", "content": "Total: 0.01\n"})]), reply("Done.")]
    assert ag.run_agent_task(t, scripted(bad))["category"] == "wrong_arithmetic"
    fmt = [reply("", [("write_file", {"path": "output/answer.txt", "content": "The total is 5"})]), reply("Done.")]
    assert ag.run_agent_task(t, scripted(fmt))["category"] == "bad_format"
    wf = [reply("", [("write_file", {"path": "answer.txt", "content": "Total: 1.00\n"})]), reply("Done.")]
    assert ag.run_agent_task(t, scripted(wf))["category"] == "wrong_file"
    nothing = [reply("", [("read_file", {"path": "input.txt"})]), reply("I cannot do this.")]
    assert ag.run_agent_task(t, scripted(nothing))["category"] == "gave_up"
    no_call = [reply("Sure, the total is 5.")]
    assert ag.run_agent_task(t, scripted(no_call))["category"] == "no_tool_call"


def test_path_escape_is_refused_in_the_loop_and_nothing_is_written(tmp_path):
    t = ag.make_sum_tasks()[0]
    steps = [reply("", [("write_file", {"path": "../../escape.txt", "content": "x"})]),
             reply("", [("write_file", {"path": "/tmp/bench-escape.txt", "content": "x"})]),
             reply("I could not write there.")]
    r = ag.run_agent_task(t, scripted(steps))
    assert r["escape_attempts"] == 2 and r["written"] == [] and not r["passed"]
    assert r["steps"][0]["tool_results"][0]["category"] == "escape"
    import os
    assert not os.path.exists("/tmp/bench-escape.txt")


def test_malformed_args_category():
    t = ag.make_sum_tasks()[0]
    steps = [reply("", [("read_file", {"file": "input.txt"})]), reply("", [("bash", {"command": "cat input.txt"})]),
             reply("Giving up.")]
    r = ag.run_agent_task(t, scripted(steps))
    assert r["category"] == "malformed_args" and r["tool_outcomes"] == {"bad_args": 1, "unavailable": 1}


def test_loop_detection_stops_on_third_identical_call():
    t = ag.make_sum_tasks()[0]
    same = reply("", [("list_dir", {"path": "."})])
    r = ag.run_agent_task(t, scripted([same] * 10))
    assert r["category"] == "loops" and r["steps_used"] == 3


def test_max_steps():
    t = ag.make_sum_tasks()[0]
    steps = [reply("", [("list_dir", {"path": f"d{i}"})]) for i in range(20)]
    r = ag.run_agent_task(t, scripted(steps), max_steps=5)
    assert r["category"] == "max_steps" and r["steps_used"] == 5


def test_timeout_by_clock_and_by_request():
    t = ag.make_sum_tasks()[0]
    now = [0.0]

    def clock():
        return now[0]

    def chat(messages, tools, timeout):
        now[0] += 100.0
        return reply("", [("list_dir", {"path": f"x{now[0]}"})])
    r = ag.run_agent_task(t, chat, task_timeout=180, clock=clock)
    assert r["category"] == "timeout" and r["steps_used"] == 2
    to = ChatResult(False, 5.0, None, "timeout after 5s", True, {"wall_s": 5.0})
    assert ag.run_agent_task(t, scripted([to]))["category"] == "timeout"
    err = ChatResult(False, 5.0, None, "HTTP 500: boom", False, {"wall_s": 5.0})
    assert ag.run_agent_task(t, scripted([err]))["category"] == "error"
    mal = ChatResult(False, 1.0, None, "HTTP 500: error parsing tool call: raw='{'", False, {"wall_s": 1.0})
    assert ag.run_agent_task(t, scripted([mal]))["category"] == "malformed_args"


def test_truncation_and_context_categories():
    t = ag.make_sum_tasks()[0]
    r = ag.run_agent_task(t, scripted([reply("", [], thinking="x" * 100, done_reason="length")]))
    assert r["category"] == "truncation"
    big = [reply("", [("read_file", {"path": "input.txt"})], prompt_tokens=16380), reply("Done.", prompt_tokens=16380)]
    assert ag.run_agent_task(t, scripted(big))["category"] == "context"


def test_tool_results_and_not_thinking_are_fed_back():
    t = ag.make_sum_tasks()[0]
    seen = []

    def chat(messages, tools, timeout):
        seen.append(json.loads(json.dumps(messages)))
        return reply("", [("read_file", {"path": "input.txt"})], thinking="secret thoughts") if len(seen) == 1 else reply("Done.")
    ag.run_agent_task(t, chat)
    second = seen[1]
    assert second[-1]["role"] == "tool" and "\n" in second[-1]["content"]
    assert all("thinking" not in m for m in second)


def test_loop_over_http_with_a_fake_ollama():
    t = [x for x in ag.build_tasks() if x.kind == "sum-signed"][1]
    script = solve_sum(t)
    it = iter(r.raw for r in script)

    with FakeOllama(lambda b, f: next(it)) as f:
        c = Ollama(f.url)
        r = ag.run_agent_task(t, lambda m, tl, to: c.chat("m1", m, tools=tl, options={"num_ctx": 16384}, think=False, timeout=to))
        assert r["passed"] and r["prompt_tokens"] == 300
        assert [x["function"]["name"] for x in f.requests[0]["tools"]] == ["read_file", "write_file", "edit_file", "list_dir"]
        assert f.requests[0]["think"] is False
