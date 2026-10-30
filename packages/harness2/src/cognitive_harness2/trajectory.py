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
