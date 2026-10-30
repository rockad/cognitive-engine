"""The host-side tool executor for the mini-agent tasks.

Everything happens inside one throwaway directory. Absolute paths, `..` components, NUL bytes and symlinks that
resolve outside the directory are refused. There is NO bash and no network: `bash` and `todo` are not executable
here (the agent is only offered read_file, write_file, edit_file and list_dir).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .tools import AGENT_TOOL_NAMES, validate_args

MAX_READ_CHARS = 20_000
MAX_WRITE_BYTES = 200_000
MAX_LIST_ENTRIES = 500


@dataclass
class ToolOutcome:
    ok: bool
    text: str
    category: str | None = None     # None | bad_args | escape | not_found | not_unique | unavailable | io


class PathEscape(Exception):
    pass


class Sandbox:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.written: list[str] = []         # relative paths written or edited, in order
        self.escape_attempts = 0

    # ── path policy ────────────────────────────────────────────────────────────────
    def resolve(self, rel: Any) -> Path:
        if not isinstance(rel, str):
            raise PathEscape("path must be a string")
        if rel == "":
            rel = "."
        if "\0" in rel:
            raise PathEscape("path contains a NUL byte")
        if rel.startswith("/") or rel.startswith("\\") or (len(rel) > 1 and rel[1] == ":"):
            raise PathEscape(f"absolute paths are not allowed: {rel!r}; use a path relative to the workspace")
        if ".." in Path(rel.replace("\\", "/")).parts:
            raise PathEscape(f"paths containing '..' are not allowed: {rel!r}")
        candidate = (self.root / rel)
        real = Path(os.path.realpath(candidate))
        try:
            real.relative_to(self.root)
        except ValueError:
            raise PathEscape(f"path resolves outside the workspace: {rel!r}") from None
        return candidate

    # ── dispatch ───────────────────────────────────────────────────────────────────
    def execute(self, name: str, args: Any) -> ToolOutcome:
        if name not in AGENT_TOOL_NAMES:
            return ToolOutcome(False, f"Error: tool {name!r} is not available in this workspace. "
                                      f"Available tools: {', '.join(AGENT_TOOL_NAMES)}.", "unavailable")
        problems = validate_args(name, args)
        if problems:
            return ToolOutcome(False, "Error: invalid arguments: " + "; ".join(problems), "bad_args")
        try:
            return getattr(self, "_" + name)(**args)
        except PathEscape as e:
            self.escape_attempts += 1
            return ToolOutcome(False, f"Error: {e}", "escape")
        except OSError as e:
            return ToolOutcome(False, f"Error: {e.strerror or e}", "io")

    # ── tools ──────────────────────────────────────────────────────────────────────
    def _read_file(self, path: str, offset: int | None = None, limit: int | None = None) -> ToolOutcome:
        p = self.resolve(path)
        if not p.exists():
            return ToolOutcome(False, f"Error: file not found: {path}", "not_found")
        if p.is_dir():
            return ToolOutcome(False, f"Error: {path} is a directory; use list_dir", "io")
        text = p.read_bytes().decode("utf-8", "replace")
        if offset is not None or limit is not None:
            lines = text.splitlines(keepends=True)
            start = max(0, (offset or 1) - 1)
            end = len(lines) if limit is None else start + max(0, limit)
            text = "".join(lines[start:end])
        if len(text) > MAX_READ_CHARS:
            text = text[:MAX_READ_CHARS] + "\n[truncated]"
        return ToolOutcome(True, text)

    def _write_file(self, path: str, content: str, mode: str = "overwrite") -> ToolOutcome:
        p = self.resolve(path)
        data = content.encode("utf-8")
        if len(data) > MAX_WRITE_BYTES:
            return ToolOutcome(False, "Error: content too large", "io")
        if p.is_dir():
            return ToolOutcome(False, f"Error: {path} is a directory", "io")
        p.parent.mkdir(parents=True, exist_ok=True)
        # re-check after mkdir: a symlinked parent could point outside
        self.resolve(path)
        with open(p, "ab" if mode == "append" else "wb") as f:
            f.write(data)
        self.written.append(Path(path).as_posix())
        verb = "appended" if mode == "append" else "wrote"
        return ToolOutcome(True, f"{verb} {len(data)} bytes to {path}")

    def _edit_file(self, path: str, old_string: str, new_string: str, replace_all: bool = False) -> ToolOutcome:
        p = self.resolve(path)
        if not p.is_file():
            return ToolOutcome(False, f"Error: file not found: {path}", "not_found")
        if old_string == "":
            return ToolOutcome(False, "Error: old_string must not be empty", "bad_args")
        text = p.read_bytes().decode("utf-8", "replace")
        n = text.count(old_string)
        if n == 0:
            return ToolOutcome(False, "Error: old_string not found in the file (it must match exactly, "
                                      "including whitespace)", "not_found")
        if n > 1 and not replace_all:
            return ToolOutcome(False, f"Error: old_string appears {n} times; add more context or set "
                                      "replace_all to true", "not_unique")
        new = text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1)
        p.write_bytes(new.encode("utf-8"))
        self.written.append(Path(path).as_posix())
        return ToolOutcome(True, f"edited {path}: replaced {n if replace_all else 1} occurrence(s)")

    def _list_dir(self, path: str, recursive: bool = False) -> ToolOutcome:
        p = self.resolve(path)
        if not p.exists():
            return ToolOutcome(False, f"Error: directory not found: {path}", "not_found")
        if not p.is_dir():
            return ToolOutcome(False, f"Error: {path} is not a directory", "io")
        entries: list[str] = []
        if recursive:
            for dirpath, dirnames, filenames in os.walk(p):
                dirnames.sort()
                base = Path(dirpath).relative_to(p)
                for d in dirnames:
                    entries.append((base / d).as_posix() + "/")
                for f in sorted(filenames):
                    entries.append((base / f).as_posix())
                if len(entries) >= MAX_LIST_ENTRIES:
                    break
        else:
            for c in sorted(p.iterdir()):
                entries.append(c.name + ("/" if c.is_dir() else ""))
        return ToolOutcome(True, "\n".join(entries[:MAX_LIST_ENTRIES]) or "(empty directory)")
