"""`trajectory.render_events` is a copy of upstream's renderer; these tests render the same streams through both."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from cognitive_harness2 import trajectory
from cognitive_harness2.substrate import CognitiveSubstrate


def ev(**kw) -> str:
    return json.dumps(kw, ensure_ascii=False)


def tool(name, status="completed", inp=None, out=None, err=None) -> str:
    state = {"status": status, "input": inp, "output": out}
    if err is not None:
        state["error"] = err
    return ev(type="tool_use", part={"type": "tool", "tool": name, "state": state})


STREAMS = {
    "empty": "",
    "garbage": "not json\n{broken\n[1,2,3]\n\n   \n",
    "text-and-thinking": "\n".join([
        ev(type="text", part={"type": "text", "text": "hello"}),
        ev(type="reasoning", part={"type": "reasoning", "text": "hmm"}),
        ev(type="text", part={"type": "text", "text": "   "}),
        ev(type="text", part={"type": "text", "content": "via content"}),
    ]),
    "tools": "\n".join([
        tool("bash", inp={"command": "cat input.txt"}, out="1.00\n-2.00\n"),
        tool("write", inp={"filePath": "output/answer.txt", "content": "Total: -1.00\n"}, out="ok"),
        tool("bash", status="error", inp={"command": "false"}, err="exit 1"),
        tool("read", inp=None, out=None),
        ev(type="tool_call", name="grep", input={"q": "x"}, output="y"),
        ev(type="tool-invocation", part={"type": "tool-invocation", "toolInvocation": {"toolName": "ls"},
                                         "args": {"p": "."}, "result": "a b"}),
    ]),
    "errors": "\n".join([
        ev(error={"data": {"message": "rate limited"}}), ev(error={"message": "plain"}), ev(error={"code": 7}),
        ev(error="a string, not a dict", type="text", part={"type": "text", "text": "after"}),
    ]),
    "unicode-and-nul": "\n".join([
        ev(type="text", part={"type": "text", "text": "Σ ٩٧ \u0000 nul"}),
        tool("bash", inp={"command": "echo é"}, out="é\u0000x"),
    ]),
    "long": "\n".join([
        ev(type="text", part={"type": "text", "text": "A" * 5000}),
        tool("bash", inp={"command": "B" * 3000}, out="C" * 9000),
    ]),
    "many": "\n".join(ev(type="text", part={"type": "text", "text": f"line {i} " + "z" * 50}) for i in range(300)),
}


@pytest.mark.parametrize("name", list(STREAMS))
@pytest.mark.parametrize("obs,maxc,args", [(50_000, 10**9, 400), (100, 10**9, 400), (50, 700, 40), (0, 10**9, 0), (5000, 120, 2000)])
def test_the_render_equals_upstreams_on_the_same_stream(tmp_path, name, obs, maxc, args):
    from harness2.substrate.opencode import render_events as upstream
    p = tmp_path / "s.jsonl"
    p.write_text(STREAMS[name], encoding="utf-8")
    assert trajectory.render_events(p, obs_trunc=obs, max_chars=maxc, args_trunc=args) == \
        upstream(p, obs_trunc=obs, max_chars=maxc, args_trunc=args)


def test_a_missing_file_and_invalid_utf8_are_handled_like_upstream(tmp_path):
    from harness2.substrate.opencode import render_events as upstream
    missing = tmp_path / "none.jsonl"
    assert trajectory.render_events(missing, obs_trunc=10, max_chars=10).startswith("(failed to read trajectory")
    assert trajectory.render_events(missing, obs_trunc=10, max_chars=10) == upstream(missing, obs_trunc=10, max_chars=10)
    p = tmp_path / "bad.jsonl"
    p.write_bytes(b'{"type":"text","part":{"type":"text","text":"a\xffb"}}\n')
    assert trajectory.render_events(p, obs_trunc=100, max_chars=100) == upstream(p, obs_trunc=100, max_chars=100)


def test_the_shared_grammar_markers(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(STREAMS["tools"])
    out = trajectory.render_events(p, obs_trunc=1000, max_chars=10**6)
    assert "[ACTION] bash({\"command\": \"cat input.txt\"})" in out and "[OBSERVATION] 1.00" in out
    assert "[OBSERVATION ERROR] exit 1" in out


def test_the_substrate_method_is_the_same_renderer(tmp_path):
    from harness2.substrate.opencode import OPENCODE_SURFACE
    p = tmp_path / "s.jsonl"
    p.write_text(STREAMS["tools"])
    assert CognitiveSubstrate(OPENCODE_SURFACE).read_trajectory(p, obs_trunc=300, max_chars=900) == \
        trajectory.render_events(p, obs_trunc=300, max_chars=900)


def test_count_events_counts_json_objects_only():
    assert trajectory.count_events("") == 0
    assert trajectory.count_events("text\n{broken\n[1]\n") == 0
    assert trajectory.count_events('{"a":1}\n  {"b":2}  \nx\n{"c"\n') == 2


def test_rendering_reads_only_the_one_file_and_does_not_follow_a_missing_path(tmp_path):
    assert "failed to read" in trajectory.render_events(tmp_path, obs_trunc=1, max_chars=1)   # a directory
