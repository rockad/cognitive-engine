"""A scriptable fake of the few Ollama endpoints the benchmark uses (no real Ollama is needed by any test)."""
from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

Responder = Callable[[dict[str, Any], "FakeOllama"], Any]


def chat_response(content: str = "", tool_calls: list[tuple[str, dict[str, Any]]] | None = None, *,
                  thinking: str = "", prompt_tokens: int = 100, gen_tokens: int = 20, done_reason: str = "stop",
                  load_ns: int = 0) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant", "content": content}
    if thinking:
        msg["thinking"] = thinking
    if tool_calls:
        msg["tool_calls"] = [{"function": {"name": n, "arguments": a}} for n, a in tool_calls]
    return {"model": "fake", "message": msg, "done": True, "done_reason": done_reason,
            "total_duration": 2_000_000_000, "load_duration": load_ns, "prompt_eval_count": prompt_tokens,
            "prompt_eval_duration": 500_000_000, "eval_count": gen_tokens, "eval_duration": 1_000_000_000}


class FakeOllama:
    def __init__(self, responder: Responder, *, size: int = 3_000_000_000, size_vram: int | None = None, ps_suffix: str = ""):
        self.responder = responder
        self.size = size
        self.size_vram = size if size_vram is None else size_vram
        self.ps_suffix = ps_suffix
        self.loaded: list[str] = []
        self.requests: list[dict[str, Any]] = []
        self.unloads: list[str] = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def _send(self, code: int, payload: Any) -> None:
                data = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/api/tags":
                    self._send(200, {"models": [{"name": n, "size": 1} for n in ("m1", "m2")]})
                elif self.path == "/api/ps":
                    self._send(200, {"models": [{"name": n + fake.ps_suffix, "size": fake.size, "size_vram": fake.size_vram,
                                                 "context_length": 16384} for n in fake.loaded]})
                elif self.path == "/api/version":
                    self._send(200, {"version": "0.0-fake"})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                if self.path == "/api/generate":
                    if body.get("keep_alive") == 0 and body.get("model") in fake.loaded:
                        fake.loaded.remove(body["model"])
                        fake.unloads.append(body["model"])
                    self._send(200, {"done": True})
                    return
                if self.path == "/api/chat":
                    fake.requests.append(body)
                    out = fake.responder(body, fake)
                    if isinstance(out, tuple) and out[0] == "sleep":
                        time.sleep(out[1])
                        out = out[2]
                    if isinstance(out, tuple) and out[0] == "error":
                        self._send(out[1], {"error": out[2]})
                        return
                    m = body.get("model")
                    if m not in fake.loaded:
                        fake.loaded.append(m)
                    self._send(200, out)
                    return
                self._send(404, {"error": "not found"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self) -> "FakeOllama":
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
