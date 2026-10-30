"""A minimal Ollama HTTP client (stdlib only) and the per-response statistics the benchmark records.

Only these endpoints are used: /api/tags, /api/ps, /api/chat, /api/generate (keep_alive 0 to unload).
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

DEFAULT_HOST = "http://127.0.0.1:11434"


class OllamaError(Exception):
    """A request failed (HTTP error, connection error, bad JSON) or timed out."""

    def __init__(self, message: str, *, timeout: bool = False, status: int | None = None):
        super().__init__(message)
        self.timeout = timeout
        self.status = status


@dataclass
class ChatResult:
    ok: bool
    wall_s: float
    raw: dict[str, Any] | None = None
    error: str | None = None
    timed_out: bool = False
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def message(self) -> dict[str, Any]:
        return (self.raw or {}).get("message") or {}

    @property
    def tool_calls(self) -> list[dict[str, Any]]:
        calls = self.message.get("tool_calls") or []
        out = []
        for c in calls:
            fn = (c or {}).get("function") or {}
            args = fn.get("arguments")
            out.append({"name": fn.get("name"), "arguments": args})
        return out

    @property
    def content(self) -> str:
        return self.message.get("content") or ""

    @property
    def thinking(self) -> str:
        return self.message.get("thinking") or ""


def response_stats(raw: dict[str, Any] | None, wall_s: float) -> dict[str, Any]:
    """The numbers recorded from every response. Durations stay in nanoseconds as Ollama reports them."""
    raw = raw or {}
    msg = raw.get("message") or {}
    thinking = msg.get("thinking") or ""
    content = msg.get("content") or ""
    pe_n, pe_d = raw.get("prompt_eval_count"), raw.get("prompt_eval_duration")
    ev_n, ev_d = raw.get("eval_count"), raw.get("eval_duration")
    return {
        "wall_s": round(wall_s, 3),
        "total_duration": raw.get("total_duration"),
        "load_duration": raw.get("load_duration"),
        "prompt_eval_count": pe_n,
        "prompt_eval_duration": pe_d,
        "eval_count": ev_n,
        "eval_duration": ev_d,
        "prompt_tps": (pe_n / (pe_d / 1e9)) if pe_n and pe_d else None,
        "gen_tps": (ev_n / (ev_d / 1e9)) if ev_n and ev_d else None,
        "thinking_chars": len(thinking),
        "thinking_tokens_est": len(thinking) // 4,
        "content_chars": len(content),
        "done_reason": raw.get("done_reason"),
    }


class Ollama:
    def __init__(self, host: str = DEFAULT_HOST):
        self.host = host.rstrip("/")

    def _request(self, path: str, body: dict[str, Any] | None, timeout: float) -> dict[str, Any]:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.host + path, data=data,
                                     headers={"Content-Type": "application/json"},
                                     method="GET" if body is None else "POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            raise OllamaError(f"HTTP {e.code}: {detail}", status=e.code) from None
        except TimeoutError:
            raise OllamaError(f"timeout after {timeout:.0f}s", timeout=True) from None
        except urllib.error.URLError as e:
            if isinstance(e.reason, TimeoutError):
                raise OllamaError(f"timeout after {timeout:.0f}s", timeout=True) from None
            raise OllamaError(f"connection error: {e.reason}") from None
        except OSError as e:
            if "timed out" in str(e):
                raise OllamaError(f"timeout after {timeout:.0f}s", timeout=True) from None
            raise OllamaError(f"connection error: {e}") from None
        try:
            return json.loads(payload)
        except ValueError:
            raise OllamaError("response was not JSON") from None

    def tags(self, timeout: float = 10) -> list[dict[str, Any]]:
        return self._request("/api/tags", None, timeout).get("models") or []

    def ps(self, timeout: float = 10) -> list[dict[str, Any]]:
        return self._request("/api/ps", None, timeout).get("models") or []

    def unload(self, model: str, timeout: float = 60) -> None:
        self._request("/api/generate", {"model": model, "keep_alive": 0}, timeout)

    def wait_unloaded(self, model: str | None = None, wait_s: float = 30, poll_s: float = 0.5) -> bool:
        """True once /api/ps lists no model (or not `model`)."""
        deadline = time.monotonic() + wait_s
        while True:
            names = [m.get("name") or m.get("model") for m in self.ps()]
            if (model is None and not names) or (model is not None and model not in names):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(poll_s)

    def chat(self, model: str, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None,
             options: dict[str, Any] | None = None, think: bool | None = None, timeout: float = 240,
             keep_alive: str | int = "10m") -> ChatResult:
        body: dict[str, Any] = {"model": model, "messages": messages, "stream": False, "keep_alive": keep_alive}
        if tools:
            body["tools"] = tools
        if options:
            body["options"] = options
        if think is not None:                 # None leaves the model's default
            body["think"] = think
        t0 = time.monotonic()
        try:
            raw = self._request("/api/chat", body, timeout)
        except OllamaError as e:
            wall = time.monotonic() - t0
            return ChatResult(False, wall, None, str(e), e.timeout, {"wall_s": round(wall, 3)})
        wall = time.monotonic() - t0
        if isinstance(raw, dict) and raw.get("error"):
            return ChatResult(False, wall, raw, str(raw["error"]), False, {"wall_s": round(wall, 3)})
        return ChatResult(True, wall, raw, None, False, response_stats(raw, wall))


def is_malformed_tool_call(res: ChatResult) -> bool:
    """Ollama answers HTTP 500 'error parsing tool call' when the model emitted unparseable tool-call JSON.
    That is the MODEL's failure, not an infrastructure error."""
    return (not res.ok) and "error parsing tool call" in (res.error or "").lower()
