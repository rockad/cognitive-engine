import pytest

from fake_ollama import FakeOllama, chat_response
from ollama_bench.client import Ollama, ChatResult, is_malformed_tool_call, response_stats


def test_response_stats():
    raw = chat_response("hi", thinking="x" * 400, prompt_tokens=1000, gen_tokens=50, load_ns=3_000_000_000)
    s = response_stats(raw, 4.2)
    assert s["prompt_eval_count"] == 1000 and s["eval_count"] == 50
    assert s["prompt_tps"] == pytest.approx(2000.0) and s["gen_tps"] == pytest.approx(50.0)
    assert s["thinking_chars"] == 400 and s["thinking_tokens_est"] == 100 and s["done_reason"] == "stop"
    assert s["load_duration"] == 3_000_000_000 and s["wall_s"] == 4.2


def test_chat_ok_options_and_think_pass_through():
    with FakeOllama(lambda b, f: chat_response("hello")) as f:
        c = Ollama(f.url)
        res = c.chat("m1", [{"role": "user", "content": "x"}], options={"num_ctx": 16384}, think=False)
        assert res.ok and res.content == "hello"
        body = f.requests[0]
        assert body["stream"] is False and body["think"] is False and body["options"]["num_ctx"] == 16384
        c.chat("m1", [{"role": "user", "content": "x"}])
        assert "think" not in f.requests[1]            # None leaves the model's default


def test_tool_calls_parsed():
    with FakeOllama(lambda b, f: chat_response("", [("read_file", {"path": "a"})])) as f:
        res = Ollama(f.url).chat("m1", [{"role": "user", "content": "x"}])
        assert res.tool_calls == [{"name": "read_file", "arguments": {"path": "a"}}]


def test_http_error_is_a_failed_result_not_an_exception():
    with FakeOllama(lambda b, f: ("error", 500, "llama runner process has terminated")) as f:
        res = Ollama(f.url).chat("m1", [{"role": "user", "content": "x"}])
        assert not res.ok and "terminated" in res.error and not res.timed_out
        assert not is_malformed_tool_call(res)


def test_malformed_tool_call_detected():
    r = ChatResult(False, 1.0, None, "HTTP 500: error parsing tool call: raw='{bad'")
    assert is_malformed_tool_call(r)
    assert not is_malformed_tool_call(ChatResult(True, 1.0, {}, None))


def test_timeout_is_reported():
    with FakeOllama(lambda b, f: ("sleep", 1.5, chat_response("late"))) as f:
        res = Ollama(f.url).chat("m1", [{"role": "user", "content": "x"}], timeout=0.4)
        assert not res.ok and res.timed_out


def test_connection_refused():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]            # closed again: nothing listens here
    res = Ollama(f"http://127.0.0.1:{port}").chat("m1", [{"role": "user", "content": "x"}], timeout=2)
    assert not res.ok and not res.timed_out


def test_ps_unload_and_confirm():
    with FakeOllama(lambda b, f: chat_response("x")) as f:
        c = Ollama(f.url)
        c.chat("m1", [{"role": "user", "content": "x"}])
        assert [m["name"] for m in c.ps()] == ["m1"]
        assert not c.wait_unloaded("m1", wait_s=0.2, poll_s=0.05)
        c.unload("m1")
        assert c.wait_unloaded("m1", wait_s=1) and c.wait_unloaded(None, wait_s=1)
        assert f.unloads == ["m1"]
