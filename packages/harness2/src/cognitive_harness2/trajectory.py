"""Render an OpenCode `--format json` event stream into the shared `[ACTION]/[OBSERVATION]` grammar.

Pure code: it reads one file and returns text. It is a copy of upstream's `render_events`
(`harness2.substrate.opencode`, pinned commit 1cfe254e), kept here because importing that module runs
`harness2.config`, which reads a `.env` into `os.environ`. A test renders the same streams through both and
requires equal text, so a drift in upstream's shape is caught the day the pin is bumped.

The stream is untrusted (it is a model session's stdout): a line that is not a JSON object is skipped, a value is
clipped to `obs_trunc`, tool arguments to `args_trunc`, and the whole text to `max_chars`.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

_TRUNC_FMT = "…(+{n} chars — render truncation)"


def _clip(s, n: int) -> str:
    s = "" if s is None else str(s)
    s = s.replace("\x00", "")
    if n <= 0 or len(s) <= n:
        return s
    return s[:n] + _TRUNC_FMT.format(n=len(s) - n)


def render_events(path: Path, *, obs_trunc: int, max_chars: int, args_trunc: int = 400) -> str:
    text_tags = {"text": "ASSISTANT", "reasoning": "THINKING"}
    args_cap = int(args_trunc)
    try:
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"(failed to read trajectory: {e})"
    lines: list[str] = []
    for ln in raw.splitlines():
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        try:
            ev = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        err = ev.get("error")
        if isinstance(err, dict):
            msg = (
                (err.get("data") or {}).get("message")
                or err.get("message")
                or json.dumps(err)[:300]
            )
            lines.append(f"[ERROR] {_clip(msg, 300)}")
            continue
        part = ev.get("part") if isinstance(ev.get("part"), dict) else ev
        ptype = part.get("type", ev.get("type"))
        if ptype in text_tags:
            txt = part.get("text") or part.get("content")
            if isinstance(txt, str) and txt.strip():
                lines.append(f"[{text_tags[ptype]}] {_clip(txt, obs_trunc)}")
        elif ptype in ("tool", "tool_use", "tool_call", "tool-invocation"):
            name = (
                part.get("tool")
                or ev.get("tool")
                or part.get("name")
                or (part.get("toolInvocation") or {}).get("toolName")
                or "tool"
            )
            state = part.get("state") if isinstance(part.get("state"), dict) else part
            status = state.get("status", "")
            tin = state.get("input") or part.get("input") or part.get("args")
            tout = state.get("output") or state.get("result") or part.get("output")
            args = json.dumps(tin, default=str, ensure_ascii=False) if tin is not None else ""
            lines.append(f"[ACTION] {name}({_clip(args, args_cap)})")
            if status == "error":
                lines.append(f"[OBSERVATION ERROR] {_clip(state.get('error'), obs_trunc)}")
            elif tout is not None:
                lines.append(f"[OBSERVATION] {_clip(tout, obs_trunc)}")
    rendered = "\n".join(lines).strip()
    if not rendered:
        return "(no parseable events in trajectory)"
    if len(rendered) > max_chars:
        dropped = len(rendered) - max_chars
        rendered = rendered[:max_chars] + "\n" + _TRUNC_FMT.format(n=dropped)
    return rendered


def count_events(text: str) -> int:
    """How many lines of `text` are JSON objects (what a live session stream is made of)."""
    n = 0
    for ln in text.splitlines():
        ln = ln.strip()
        if ln.startswith("{"):
            try:
                if isinstance(json.loads(ln), dict):
                    n += 1
            except json.JSONDecodeError:
                continue
    return n


_ESC = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b.", re.DOTALL)


def error_events(text: str, *, limit: int = 3, width: int = 200) -> list[str]:
    """The messages of the `error` events in a session stream, as short clean one-line strings.

    opencode v2.0.25 writes `{"type":"error","error":{"type":"provider.no-route","message":"..."}}`; older builds wrote
    `error.data.message`. Both are read. Upstream's own `_opencode_stdout` knows only the old shape, so for a v2 failure it
    reports "empty reply" and the real cause (`UnexpectedStatus: 403`, `Unsupported package ...`, `model ... not found`) is
    lost. The stream is untrusted: control characters and escape sequences are stripped, each message is cut to `width`."""
    out: list[str] = []
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        try:
            ev = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict) or ev.get("type") != "error":
            continue
        err = ev.get("error")
        if isinstance(err, dict):
            data = err.get("data") if isinstance(err.get("data"), dict) else {}
            msg = str(err.get("message") or data.get("message") or err.get("name") or "").strip()
            kind = str(err.get("type") or "").strip()
            if kind and kind != "unknown" and msg and not msg.startswith(kind):
                msg = f"{kind}: {msg}"
        else:
            msg = str(err or "").strip()
        msg = "".join(c for c in _ESC.sub("", msg) if c.isprintable()).strip()[:width].strip()
        if msg:
            out.append(msg)
            if len(out) >= limit:
                break
    return out
