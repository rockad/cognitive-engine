"""First-contact probe: the REAL opencode binary in a contained stage-1 solver session against a STUB model.

No model, no ollama: the "model" is `StubModel`, an OpenAI-compatible server on THIS host's loopback that answers from a
script and logs what it is asked (paths, header NAMES, the model field, whether tools were offered, whether the
workspace's AGENTS.md reached the prompt). The session is the one stage 1 builds: `stage1.solver_argv` and
`stage1.solver_env_builder`, `TMP_EXEC`, the same workspace layout as `CognitiveSubstrate._run_one` (AGENTS.md, input.txt,
an output/ directory), the same hardening, the same host bridge and egress proxy. Only the model is a stub.

    uv run python packages/harness2/tests/real_cli_probe.py --scenario text
    uv run python packages/harness2/tests/real_cli_probe.py --scenario write --config api

Everything synthetic: the prompt is the sum-signed task, the workspace holds a made-up AGENTS.md and input.txt.
"""
from __future__ import annotations

import argparse
import dataclasses
import http.server
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from cognitive_harness2 import channels, contained_proposer as cp, image, sandbox, session_runner as sr, stage1, sum_signed, upstream_egress

OPENCODE = Path(os.environ.get("COGNITIVE_HARNESS2_OPENCODE", str(Path.home() / ".opencode" / "bin" / "opencode")))
ENABLE_ENV = "COGNITIVE_HARNESS2_REAL_CLI"
MODEL_ID = "gemma4:12b"
TASK = "household/sum-1"
AGENTS_MARKER = "H2-PROBE-AGENTS-MARKER-7731"
AGENTS_MD = f"# Harness\n\n{AGENTS_MARKER}\n\nCheck the sign of every amount before adding it.\n"


def enabled() -> bool:
    return OPENCODE.is_file() and os.environ.get(ENABLE_ENV) == "1"


# ── the stub model ────────────────────────────────────────────────────────────────

def _sse(obj: Any) -> bytes:
    return b"data: " + json.dumps(obj, separators=(",", ":")).encode() + b"\n\n"


def _pick(tool: Mapping[str, Any], *needles: str) -> str | None:
    props = (((tool.get("function") or {}).get("parameters") or {}).get("properties") or {})
    for n in needles:
        for k in props:
            if n in k.lower():
                return k
    return None


def _tool_by_name(tools: list[dict], name: str) -> dict | None:
    return next((t for t in tools if (t.get("function") or {}).get("name") == name), None)


def tool_args(tool: dict, canonical: Mapping[str, str]) -> dict[str, str]:
    """Build a tool call's arguments from canonical keys (path, content, command) using the tool's OWN property names."""
    out: dict[str, str] = {}
    for key, value in canonical.items():
        prop = _pick(tool, *{"path": ("path", "file"), "content": ("content", "text"), "command": ("command", "cmd"), "pattern": ("pattern",)}[key])
        if prop:
            out[prop] = value
    return out


class StubModel:
    """`turns`: what the model says on its 1st, 2nd, ... request of a session, each `{"text": s}` or
    `{"tool": name, "args": {path|content|command: s}}`. After the last turn it repeats the last text turn (or "STUB-DONE").
    A turn counts as taken once the request carries that many `tool` messages (a tool result is how a round ends)."""

    def __init__(self, turns: list[dict], *, model_id: str = MODEL_ID, stream_models_list: bool = True):
        self.turns, self.model_id = turns, model_id
        self.requests: list[dict] = []
        self.last_body: dict | None = None
        self.bodies: list[dict] = []
        self.chat_status: int | None = None        # when set, every chat completion fails with this status
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _send(self, status: int, body: bytes, ctype: str = "application/json") -> None:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:           # noqa: N802
                outer._log(self, None)
                if self.path.rstrip("/").endswith("/models"):
                    self._send(200, json.dumps({"object": "list", "data": [{"id": outer.model_id, "object": "model",
                                                                            "created": 0, "owned_by": "stub"}]}).encode())
                elif self.path.startswith("/api/tags"):
                    self._send(200, json.dumps({"models": [{"name": outer.model_id}]}).encode())
                else:
                    self._send(404, b'{"error":"not found"}')

            def do_POST(self) -> None:          # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    body = {}
                outer._log(self, body)
                if not self.path.rstrip("/").endswith("/chat/completions"):
                    self._send(404, b'{"error":"not found"}')
                    return
                if outer.chat_status:
                    self._send(outer.chat_status, json.dumps({"error": {"message": f"model \"{outer.model_id}\" not found, try pulling it first" if outer.chat_status == 404 else "stub failure",
                                                                         "type": "stub_error", "code": outer.chat_status}}).encode())
                    return
                turn = outer._turn(body)
                if body.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    for chunk in outer._stream_chunks(turn, body):
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    self.close_connection = True
                else:
                    self._send(200, json.dumps(outer._completion(turn, body)).encode())

            def log_message(self, *a: Any) -> None:
                pass
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]

    # -- logging --------------------------------------------------------------------
    def _log(self, h: http.server.BaseHTTPRequestHandler, body: dict | None) -> None:
        rec: dict[str, Any] = {"method": h.command, "path": h.path, "header_names": sorted({k.lower() for k in h.headers.keys()})}
        if body is not None:
            msgs = body.get("messages") or []
            tools = body.get("tools") or []
            text = json.dumps(body)
            rec.update(model=body.get("model"), stream=body.get("stream"), body_keys=sorted(body),
                       n_messages=len(msgs), roles=[m.get("role") for m in msgs],
                       tool_names=[(t.get("function") or {}).get("name") for t in tools], n_tools=len(tools),
                       tool_choice=body.get("tool_choice"), has_agents_marker=AGENTS_MARKER in text,
                       has_task_text="Total:" in text and "input.txt" in text, bytes=len(text),
                       max_tokens=body.get("max_tokens") or body.get("max_completion_tokens"),
                       temperature=body.get("temperature"), stream_options=body.get("stream_options"))
            sysmsgs = [m for m in msgs if m.get("role") == "system"]
            rec["system_chars"] = sum(len(m.get("content") or "") if isinstance(m.get("content"), str) else len(json.dumps(m.get("content"))) for m in sysmsgs)
            self.last_body = body
            self.bodies.append(body)
        self.requests.append(rec)

    # -- scripting ------------------------------------------------------------------
    def _turn(self, body: dict) -> dict:
        n_tool = sum(1 for m in body.get("messages") or [] if m.get("role") == "tool")
        if n_tool < len(self.turns):
            return self.turns[n_tool]
        return {"text": "STUB-DONE"}

    def _tool_call(self, turn: dict, body: dict) -> dict | None:
        if "tool" not in turn:
            return None
        tools = body.get("tools") or []
        tool = _tool_by_name(tools, turn["tool"])
        if tool is None:
            return None            # the model was not offered that tool: answer in text instead
        return {"id": f"call_{abs(hash(json.dumps(turn, sort_keys=True))) % 10**8}", "type": "function",
                "function": {"name": turn["tool"], "arguments": json.dumps(tool_args(tool, turn["args"]))}}

    def _completion(self, turn: dict, body: dict) -> dict:
        call = self._tool_call(turn, body)
        msg: dict[str, Any] = {"role": "assistant", "content": None if call else turn.get("text", "STUB-NO-SUCH-TOOL")}
        if call:
            msg["tool_calls"] = [call]
        return {"id": "chatcmpl-stub", "object": "chat.completion", "created": int(time.time()), "model": self.model_id,
                "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if call else "stop"}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}}

    def _stream_chunks(self, turn: dict, body: dict):
        call = self._tool_call(turn, body)
        base = {"id": "chatcmpl-stub", "object": "chat.completion.chunk", "created": int(time.time()), "model": self.model_id}
        yield _sse({**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]})
        if call:
            fn = call["function"]
            yield _sse({**base, "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": call["id"], "type": "function",
                                                                                   "function": {"name": fn["name"], "arguments": ""}}]},
                                             "finish_reason": None}]})
            yield _sse({**base, "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": fn["arguments"]}}]},
                                             "finish_reason": None}]})
            finish = "tool_calls"
        else:
            text = turn.get("text", "STUB-NO-SUCH-TOOL")
            for i in range(0, len(text), 8):
                yield _sse({**base, "choices": [{"index": 0, "delta": {"content": text[i:i + 8]}, "finish_reason": None}]})
            finish = "stop"
        yield _sse({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
        yield _sse({**base, "choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}})
        yield b"data: [DONE]\n\n"

    def __enter__(self) -> "StubModel":
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *a: Any) -> None:
        self._server.shutdown()
        self._server.server_close()


# ── provider-block variants for OPENCODE_CONFIG_CONTENT ───────────────────────────

def _cfg(provider: str, model_key: str, block: dict) -> str:
    return json.dumps({"provider": {provider: block}, "autoupdate": False, "share": "disabled"}, separators=(",", ":"))


def config_variants(model: str, base_url: str) -> dict[str, str]:
    provider, _, key = model.partition("/")
    models = {key: {"name": key}}
    return {
        "current": cp.opencode_config(model, base_url),        # npm + name + options.baseURL
        "baseurl-only": _cfg(provider, key, {"options": {"baseURL": base_url}, "models": models}),
        "name+baseurl": _cfg(provider, key, {"name": provider, "options": {"baseURL": base_url}, "models": models}),
        "npm+baseurl": _cfg(provider, key, {"npm": "@ai-sdk/openai-compatible", "options": {"baseURL": base_url}, "models": models}),
        "api": _cfg(provider, key, {"npm": "@ai-sdk/openai-compatible", "name": provider, "api": base_url, "models": models}),
        "api-only": _cfg(provider, key, {"api": base_url, "models": models}),
    }


# ── one session ───────────────────────────────────────────────────────────────────

@dataclasses.dataclass
class Probe:
    result: sr.SessionResult
    events: list[dict]
    stub: list[dict]
    bodies: list[dict]
    workspace_files: dict[str, str]          # relative path -> first 400 chars (text) of what the session left
    output_files: dict[str, str]
    wall_s: float
    argv: tuple[str, ...]
    env_names: list[str]


def run_session(verified: sandbox.VerifiedDocker, img: image.ImageRef, workdir: Path, stub: StubModel, *,
                config: str = "current", tmp_exec: bool | None = None, extra_env: Mapping[str, str] | None = None,
                timeout_s: float = 90.0, prompt: str | None = None, agents_md: str | None = AGENTS_MD,
                env_edit: Callable[[dict[str, str]], None] | None = None,
                config_merge: Mapping[str, Any] | None = None,
                argv_edit: Callable[[tuple[str, ...]], tuple[str, ...]] | None = None,
                extra_files: Mapping[str, str] | None = None) -> Probe:
    """Run the real opencode once, as a stage-1 SOLVER session, against `stub`. Never touches a real model."""
    ws_root = workdir / "workspaces"
    ws_root.mkdir(parents=True, exist_ok=True)
    os.chmod(workdir, 0o755)
    profile = stage1.selftest_profile(ws_root, stub.port)
    ch = profile.channels[stage1.CHANNEL]
    runner = sr.ProposerSessionRunner(verified, img, ch, zdr="strict", classification="restricted", role="solver",
                                      workspace_roots=profile.workspace_roots)
    ws = Path(tempfile.mkdtemp(prefix="h2ws-probe-", dir=ws_root)).resolve()
    ws.chmod(0o1777)
    task = sum_signed.solver_task(TASK)
    if agents_md:
        (ws / "AGENTS.md").write_text(agents_md)
        (ws / "AGENTS.md").chmod(0o644)
    for name, body in task.files.items():
        (ws / name).write_text(body)
        (ws / name).chmod(0o644)
    (ws / "output").mkdir()
    (ws / "output").chmod(0o1777)
    for name, body in (extra_files or {}).items():
        (ws / name).write_text(body)
        (ws / name).chmod(0o644)
    cli_in = sr.container_cli_path(str(OPENCODE))
    job = stage1.SolverJob(TASK, "probe", prompt or task.prompt, str(ws), ch.model, cli_in)
    argv = stage1.solver_argv(job)
    if argv_edit:
        argv = argv_edit(argv)
    env = dict(stage1.solver_env_builder(runner)(job))
    if config != "current":
        env["OPENCODE_CONFIG_CONTENT"] = config_variants(ch.model, runner.local_endpoint + stage1.API_PATH)[config]
    env.update(extra_env or {})
    if config_merge:
        merged = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        merged.update(config_merge)
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(merged, separators=(",", ":"))
    if env_edit:
        env_edit(env)
    spec = sr.SessionSpec(argv=argv, workspace=str(ws), cli=str(OPENCODE), timeout_s=timeout_s, env=env,
                          tmp_exec=stage1.TMP_EXEC["opencode"] if tmp_exec is None else tmp_exec,
                          harvest=cp.DEFAULT_HARVEST)
    t0 = time.monotonic()
    res = runner.run(spec)
    wall = time.monotonic() - t0
    events = []
    for line in res.stdout.splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            events.append({"_unparsed": line[:200]})

    def snap(root: Path) -> dict[str, str]:
        out: dict[str, str] = {}
        for p in sorted(root.rglob("*")):
            if p.is_file() and not p.is_symlink():
                out[str(p.relative_to(root))] = p.read_bytes()[:400].decode("utf-8", "replace")
        return out
    files, outputs = snap(ws), snap(ws / "output")
    shutil.rmtree(ws, ignore_errors=True)
    return Probe(res, events, list(stub.requests), list(stub.bodies), files, outputs, wall, argv, sorted(env))


# ── command line ──────────────────────────────────────────────────────────────────

SCENARIOS: dict[str, list[dict]] = {
    "text": [],
    "shell": [{"tool": "shell", "args": {"command": "id -u; pwd; ls -la; du -sk /home/agent /tmp 2>/dev/null; "
                                                  "find /home/agent /tmp -maxdepth 4 2>/dev/null | head -60; "
                                                  "awk '$2==\"/\"||$2==\"/tmp\"||$2==\"/home/agent\"{print $2,$4}' /proc/mounts; env | grep -i -E 'proxy|xdg|opencode' | sed 's/=.*//'"}}],
    "tools": [{"tool": "read", "args": {"path": "input.txt"}}, {"tool": "glob", "args": {"pattern": "*.txt"}},
              {"tool": "grep", "args": {"pattern": "Total|373", "path": "."}},
              {"tool": "write", "args": {"path": "output/answer.txt", "content": sum_signed.instance(TASK).expected_line + "\n"}},
              {"tool": "edit", "args": {"path": "output/answer.txt", "content": "x"}}],
    "write": [{"tool": "write", "args": {"path": "output/answer.txt", "content": sum_signed.instance(TASK).expected_line + "\n"}}],
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="text", choices=sorted(SCENARIOS))
    ap.add_argument("--config", default="current", choices=sorted(config_variants("a/b", "http://x").keys()))
    ap.add_argument("--no-tmp-exec", action="store_true")
    ap.add_argument("--timeout", type=float, default=90.0)
    ap.add_argument("--chat-status", type=int, help="the stub fails every chat completion with this HTTP status")
    ap.add_argument("--merge", help="JSON object merged into the top level of OPENCODE_CONFIG_CONTENT")
    a = ap.parse_args()
    verified = image.gate_for_build()
    img = image.resolve_image(verified)
    work = Path(tempfile.mkdtemp(prefix="h2-realcli-", dir=os.environ.get("H2_PROBE_TMP")))
    with StubModel(SCENARIOS[a.scenario]) as stub:
        stub.chat_status = a.chat_status
        p = run_session(verified, img, work, stub, config=a.config, tmp_exec=False if a.no_tmp_exec else None, timeout_s=a.timeout,
                        config_merge=json.loads(a.merge) if a.merge else None)
    out = {"exit_code": p.result.exit_code, "timed_out": p.result.timed_out, "wall_s": round(p.wall_s, 2),
           "stderr": p.result.stderr[-1500:], "events": p.events, "egress": list(p.result.egress_log),
           "stub": p.stub, "tools": {(t.get("function") or {}).get("name"): (t.get("function") or {}).get("parameters") for b in p.bodies[-1:] for t in b.get("tools") or []}, "workspace_files": p.workspace_files, "output_files": p.output_files,
           "harvest": None if p.result.harvest is None else {"files": list(p.result.harvest.files),
                                                              "findings": [(f.path, f.rule) for f in p.result.harvest.findings]}}
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
