"""First contact with the REAL opencode v2.0.25 binary, against a STUB model. Skipped unless the binary exists AND
COGNITIVE_HARNESS2_REAL_CLI=1, so the normal suite stays hermetic and starts nothing it did not build.

    COGNITIVE_HARNESS2_REAL_CLI=1 uv run pytest -q packages/harness2/tests/test_real_cli.py

No model and no ollama is ever called: the "model" is `real_cli_probe.StubModel` on this host's loopback, reached the way
stage 1 reaches ollama (host bridge, proxy container, `http://harness2-proxy.internal:<port>/v1`). The session is the one stage 1
builds (`stage1.solver_argv`, `stage1.solver_env_builder`, `TMP_EXEC`, the same hardening and workspace layout). What these
tests pin is what only a real binary can show; what only a real gemma can show (quality, speed, context truncation) is NOT
here. Findings are written up in the README ("First-contact findings (stub-model probe)").
"""
from __future__ import annotations

import json
import re

import pytest
from h2_upstream import isolated_upstream
from real_cli_probe import AGENTS_MARKER, MODEL_ID, OPENCODE, SCENARIOS, TASK, StubModel, enabled, run_session

from cognitive_harness2 import contained_proposer as cp, session_runner as sr, sum_signed

pytestmark = [
    pytest.mark.real_cli, pytest.mark.requires_rootless_docker,
    pytest.mark.skipif(not enabled(), reason=f"the real opencode probe needs {OPENCODE} and COGNITIVE_HARNESS2_REAL_CLI=1"),
]

TOOLS_OFFERED = {"edit", "read", "shell", "skill", "write", "execute"}     # 12 minus the six permission-denied ones


def text_of(events):
    return [e["part"]["text"] for e in events if e.get("type") == "text"]


def go(verified, proposer_image, tmp_path, turns, **kw):
    with StubModel(turns) as stub:
        stub.chat_status = kw.pop("chat_status", None)
        probe = run_session(verified, proposer_image, tmp_path, stub, **kw)
    # the stub enforces ollama's Host/Origin guard (ollama_guard): a session whose request real Ollama would answer with an
    # empty 403 (the second real stage-1 run: Host `harness2-proxy.internal`) fails here, whatever else the test looks at
    assert stub.rejected == [], stub.rejected
    return probe, stub


# ── the provider block ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("variant,works", [("current", True), ("npm+baseurl", True), ("api", True),
                                           ("baseurl-only", False), ("name+baseurl", False), ("api-only", False)])
def test_which_provider_blocks_the_real_binary_accepts(verified, proposer_image, tmp_path, variant, works):
    p, stub = go(verified, proposer_image, tmp_path, [], config=variant)
    posts = [r for r in p.stub if r["method"] == "POST"]
    if works:
        assert p.result.exit_code == 0 and text_of(p.events) == ["STUB-DONE"] and posts and posts[0]["model"] == MODEL_ID
    else:      # without `npm` opencode has no route: it never contacts the model, and says why
        assert p.result.exit_code == 1 and not posts
        err = [e for e in p.events if e.get("type") == "error"]
        assert err and err[0]["error"]["type"] == "provider.no-route" and "Unsupported package" in err[0]["error"]["message"]


# ── a text reply ──────────────────────────────────────────────────────────────────

def test_a_text_reply_round_trip_through_bridge_and_proxy(verified, proposer_image, tmp_path):
    p, stub = go(verified, proposer_image, tmp_path, [{"text": "THE-STUB-SAYS-HI"}])
    r = p.result
    assert r.exit_code == 0 and not r.timed_out and p.wall_s < 60
    assert [e["type"] for e in p.events] == ["text"] and text_of(p.events) == ["THE-STUB-SAYS-HI"]
    assert {"type", "timestamp", "sessionID", "part"} <= set(p.events[0]) and p.events[0]["part"]["type"] == "text"
    # the model was reached, through the proxy only: every egress line is the proxy alias (an allow), nothing denied
    assert r.egress_log and all(re.fullmatch(r"ACCESS decision=allow host=harness2-proxy\.internal port=\d+ reason=unix-route", x)
                                for x in r.egress_log), r.egress_log
    summary = sr.egress_summary(r.egress_log)
    assert summary["model_requests"] == 2 and summary["egress_denied"] == 0 and summary["denied_hosts"] == []   # GET + POST
    # what opencode asked the model: GET /api/tags once, then ONE chat request (the --title word skips title generation)
    assert [(x["method"], x["path"]) for x in p.stub] == [("GET", "/api/tags"), ("POST", "/v1/chat/completions")]
    chat = p.stub[1]
    assert chat["model"] == MODEL_ID and chat["stream"] is True and chat["stream_options"] == {"include_usage": True}
    assert "authorization" not in chat["header_names"]                              # no credential is sent or needed
    assert set(chat["tool_names"]) == TOOLS_OFFERED and chat["n_tools"] == 6
    assert chat["has_agents_marker"] is True and chat["has_task_text"] is True      # AGENTS.md reached the prompt
    assert chat["bytes"] < 20_000 and chat["max_tokens"] == 32000
    # upstream's own parser reads this stream (the proposer path)
    with isolated_upstream(tmp_path / "up") as up:
        text, err = up.proposer._opencode_stdout(r.stdout)
        assert (text, err) == ("THE-STUB-SAYS-HI", "")


def test_a_non_streaming_stub_still_works(verified, proposer_image, tmp_path):
    """opencode streams, so the non-streaming branch of the stub is never used; this pins that it is not needed."""
    p, stub = go(verified, proposer_image, tmp_path, [])
    assert all(b.get("stream") is True for b in stub.bodies)


# ── tools: the model's file write lands in the workspace and in the harvest ─────────

def test_a_write_tool_call_creates_output_answer_txt_in_the_workspace_and_the_verifier_accepts_it(verified, proposer_image, tmp_path):
    p, stub = go(verified, proposer_image, tmp_path, SCENARIOS["write"])
    assert p.result.exit_code == 0
    types = [e["type"] for e in p.events]
    assert types == ["step_start", "tool_use", "step_finish", "text"]
    tool = p.events[1]["part"]
    assert tool["tool"] == "write" and tool["state"]["status"] == "completed" and tool["state"]["input"]["path"] == "output/answer.txt"
    assert p.events[2]["part"]["reason"] == "tool-calls" and p.events[2]["part"]["cost"] == 0
    assert "output/answer.txt" in p.result.harvest.files and not p.result.harvest.findings
    raw = p.output_files["answer.txt"].encode()
    inst = sum_signed.instance(TASK)
    assert sum_signed.grade_answer(raw, inst.expected_total)["all_pass"] is True
    assert set(p.workspace_files) == {"AGENTS.md", "input.txt", "output/answer.txt"}      # nothing else is left behind
    # the second request carries the tool result back
    assert [x["roles"] for x in p.stub if x["method"] == "POST"] == [["system", "user"], ["system", "user", "assistant", "tool"]]


def test_the_dead_tools_are_not_offered_and_the_ones_that_are_work(verified, proposer_image, tmp_path):
    p, _ = go(verified, proposer_image, tmp_path, [{"tool": "read", "args": {"path": "input.txt"}}])
    assert p.events[1]["part"]["state"]["status"] == "completed" and "373.25" in p.events[1]["part"]["state"]["output"]
    offered = {t for x in p.stub for t in x.get("tool_names") or []}
    assert offered == TOOLS_OFFERED and not offered & {"grep", "glob", "webfetch", "websearch", "question", "subagent"}


# ── the container: what it writes, where, how big, and whether noexec is enough ──────

def test_opencode_runs_on_a_read_only_root_and_noexec_tmpfs_and_writes_only_where_expected(verified, proposer_image, tmp_path):
    p, _ = go(verified, proposer_image, tmp_path, SCENARIOS["shell"])
    assert p.result.exit_code == 0
    out = p.events[1]["part"]["state"]["output"]
    assert out.splitlines()[0] == "65534"
    assert re.search(r"^/tmp rw,[^ ]*noexec", out, re.M) and re.search(r"^/home/agent rw,[^ ]*noexec", out, re.M)    # TMP_EXEC False
    assert re.search(r"^/ ro,", out, re.M)
    sizes = dict((m[1], int(m[0])) for m in re.findall(r"^(\d+)\t(/\S+)$", out, re.M))
    assert sizes["/home/agent"] < 4096 and sizes["/tmp"] < 64 * 1024                       # KiB: HOME < 4 MiB, /tmp < 64 MiB
    paths = set(re.findall(r"^(/(?:home/agent|tmp)\S*)$", out, re.M))
    assert "/home/agent/.local/share/opencode/opencode.db" in paths and "/tmp/opencode" in paths
    assert not [x for x in paths if x.startswith("/home/agent") and "/opencode" not in x
                and x not in {"/home/agent", "/home/agent/.cache", "/home/agent/.config", "/home/agent/.local",
                              "/home/agent/.local/state", "/home/agent/.local/share"}]


# ── failures: a clear error event and exit code 1 ──────────────────────────────────

def test_a_model_the_server_does_not_have_fails_fast_with_the_servers_message(verified, proposer_image, tmp_path):
    p, _ = go(verified, proposer_image, tmp_path, [], chat_status=404)
    assert p.result.exit_code == 1 and p.wall_s < 30
    err = [e for e in p.events if e.get("type") == "error"]
    assert err and err[0]["error"]["status"] == 404 and "not found" in err[0]["error"]["message"]
    # the proposer parser keeps only the old `error.data.message` shape: it returns no error text for a v2 stream
    with isolated_upstream(tmp_path / "up") as up:
        assert up.proposer._opencode_stdout(p.result.stdout)[1] == ""


# ── the proposer's shape: MISSION.md, the old opencode.json, the answer in output/answer.md ─────────────────────────

def test_a_proposer_style_session_with_the_upstream_workspace_files_and_prompt(verified, proposer_image, tmp_path):
    """upstream's workspace: MISSION.md plus `opencode.json` (`agent.build.steps`, a v1 key). The prompt asks for the
    answer both as the final message and as output/answer.md."""
    turns = [{"tool": "read", "args": {"path": "MISSION.md"}},
             {"tool": "write", "args": {"path": "output/answer.md", "content": "<notes>stub proposer</notes>\n"}},
             {"text": "<notes>stub proposer</notes>"}]
    p, _ = go(verified, proposer_image, tmp_path, turns, prompt=cp.OPENCODE_PROMPT, agents_md=None,
              extra_files={"MISSION.md": "# mission\nsynthetic\n", "opencode.json": json.dumps({"agent": {"build": {"steps": 120}}})})
    assert p.result.exit_code == 0, (p.result.stderr, p.events[-1:])
    assert [e["part"]["tool"] for e in p.events if e["type"] == "tool_use"] == ["read", "write"]
    assert p.output_files == {"answer.md": "<notes>stub proposer</notes>\n"} and "output/answer.md" in p.result.harvest.files
    assert set(p.workspace_files) == {"MISSION.md", "opencode.json", "input.txt", "output/answer.md"}
    with isolated_upstream(tmp_path / "up") as up:
        assert up.proposer._opencode_stdout(p.result.stdout) == ("<notes>stub proposer</notes>", "")
        assert up.proposer._opencode_cost(p.result.stdout) == 0.0


def test_the_trajectory_renderer_understands_the_real_event_shapes(verified, proposer_image, tmp_path):
    """`trajectory.render_events` feeds the next proposer stage. It must render the real v2 tool events, not drop them."""
    from cognitive_harness2 import trajectory
    p, _ = go(verified, proposer_image, tmp_path, [{"tool": "read", "args": {"path": "input.txt"}}, *SCENARIOS["write"]])
    f = tmp_path / "stream.jsonl"
    f.write_text(p.result.stdout)
    text = trajectory.render_events(f, obs_trunc=400, max_chars=4000)
    assert trajectory.count_events(p.result.stdout) == len(p.events)
    print(text)
    assert "read" in text and "373.25" in text and "write" in text and "Created file successfully" in text, text
