"""Candidate execution in local microVMs, failing closed (RFC 004 §2.2).

Candidate code runs ONLY inside a local Docker Sandbox driven by the standalone `sbx` CLI.
If `sbx` is not installed the prober refuses to run anything. The one fallback is an
explicit opt-in that writes candidates to a temp directory and returns a diff for static
inspection; it never executes code.

⚠️ `SbxRunner._argv` is the single place that knows the `sbx` command line, and it is
UNVERIFIED against a real `sbx` (none was installed while this was written). The
fail-closed behaviour does not depend on it and is fully tested; real isolation is an
open acceptance item until `sbx` exists on the host.
"""
from __future__ import annotations

import difflib
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


class SandboxUnavailable(RuntimeError):
    """No local microVM runtime: refuse to run candidate code."""


@dataclass(frozen=True)
class SandboxSpec:
    command: Sequence[str]
    files: Mapping[str, str] = field(default_factory=dict)
    timeout_s: int = 120


@dataclass(frozen=True)
class SandboxResult:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool = False


class SandboxRunner(Protocol):
    def run(self, spec: SandboxSpec) -> SandboxResult: ...


def require_sbx(which: Callable[[str], str | None] = shutil.which) -> str:
    path = which("sbx")
    if not path:
        raise SandboxUnavailable(
            "the `sbx` runtime is not installed, so candidate code will not be run (fail closed). "
            "Install sbx, or pass allow_worktree_diff to get static diffs only.")
    return path


class SbxRunner:
    def __init__(self, sbx_path: str):
        self.sbx_path = sbx_path

    def _argv(self, spec: SandboxSpec) -> list[str]:
        # UNVERIFIED against a real sbx: the subcommand and flags are assumptions.
        return [self.sbx_path, "run", "--", *spec.command]

    def run(self, spec: SandboxSpec) -> SandboxResult:
        try:
            p = subprocess.run(self._argv(spec), capture_output=True, text=True, timeout=spec.timeout_s)
        except subprocess.TimeoutExpired as e:
            return SandboxResult(None, (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or ""),
                                 "timed out", True)
        return SandboxResult(p.returncode, p.stdout, p.stderr)


def default_runner(which: Callable[[str], str | None] = shutil.which) -> SandboxRunner:
    return SbxRunner(require_sbx(which))


def static_diff(name: str, original: str, candidate: str, *, allow_worktree_diff: bool) -> str:
    """A unified diff computed in a temp directory. Never executes anything."""
    if not allow_worktree_diff:
        raise SandboxUnavailable("static diff fallback needs explicit opt-in (allow_worktree_diff)")
    with tempfile.TemporaryDirectory(prefix="harness2-diff-") as d:
        base = Path(d)
        (base / "a").write_text(original, encoding="utf-8")
        (base / "b").write_text(candidate, encoding="utf-8")
        a = (base / "a").read_text(encoding="utf-8").splitlines(keepends=True)
        b = (base / "b").read_text(encoding="utf-8").splitlines(keepends=True)
    return "".join(difflib.unified_diff(a, b, f"a/{name}", f"b/{name}"))
