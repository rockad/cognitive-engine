"""The approval gate against a REAL pseudo-terminal: what it stops and, stated plainly, what it cannot.

No Docker, no model, no network. The child runs `confirm_on_terminal` on a real controlling terminal; the test plays the
part of a process that drives that terminal (like python `pty.fork`, `script`, `expect` or `tmux send-keys`).
"""
from __future__ import annotations

import os
import pty
import re
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest

CHILD = Path(__file__).with_name("pty_confirm_child.py")
pytestmark = pytest.mark.skipif(not hasattr(pty, "fork"), reason="needs a pty")


def _drive(env: dict[str, str], *, type_back: bool = True, timeout: float = 20.0) -> tuple[int, str]:
    """Run the child under pty.fork(); read the prompt; type back what it shows (phrase and nonce). (exit code, output)"""
    pid, fd = pty.fork()
    if pid == 0:                                                       # the child: a fresh session, the pty as controlling tty
        os.execve(sys.executable, [sys.executable, "-I", str(CHILD)], env)
    out = b""
    typed = False
    deadline = time.monotonic() + timeout
    status = None
    while time.monotonic() < deadline:
        r, _, _ = select.select([fd], [], [], 0.2)
        if r:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                chunk = b""
            if not chunk:
                break
            out += chunk
            if type_back and not typed:
                m = re.search(rb"\n  (approve [^\r\n]+)\r?\n\r?\n> ", out)
                if m:
                    os.write(fd, m.group(1) + b"\n")
                    typed = True
    _, status = os.waitpid(pid, 0)
    os.close(fd)
    return os.waitstatus_to_exitcode(status), out.decode(errors="replace")


def _env(**extra: str) -> dict[str, str]:
    from cognitive_harness2 import stage1
    env = {k: v for k, v in os.environ.items() if not stage1.agent_markers({k: v})}
    env["TERM"] = "dumb"
    # the child runs `python -I` (no site, no cwd), so hand it the import path the test run itself uses
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    env.update(extra)
    return env


def test_a_driven_pty_without_agent_markers_still_approves_which_is_the_documented_residual_limit():
    """DOCUMENTED LIMIT, NOT HIDDEN: a same-user process that owns a pty can read the phrase and its nonce and type them back,
    so the gate returns True. This test asserts that on purpose; the README says the same. The human is the control."""
    code, out = _drive(_env())
    assert code == 0 and "APPROVED" in out, out


@pytest.mark.parametrize("marker", ["CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "ANTIGRAVITY_SESSION", "GEMINI_CLI", "AGY_HOME",
                                    "OPENCODE", "CODEX_SANDBOX"])
def test_the_same_driven_pty_with_an_agent_marker_in_the_environment_is_refused(marker):
    """Same pty, same driver, same typed-back phrase; one agent-harness marker in the environment is enough to be refused."""
    code, out = _drive(_env(**{marker: "1"}))
    assert code == 4 and "REFUSED" in out and marker in out and "APPROVED" not in out, out


def test_a_pty_session_that_types_the_phrase_without_the_nonce_is_not_approved():
    pid, fd = pty.fork()
    if pid == 0:
        os.execve(sys.executable, [sys.executable, "-I", str(CHILD)], _env())
    out = b""
    deadline = time.monotonic() + 20
    sent = False
    while time.monotonic() < deadline:
        r, _, _ = select.select([fd], [], [], 0.2)
        if not r:
            continue
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        out += chunk
        if not sent and b"> " in out:
            os.write(fd, b"approve local-ollama stage1-pty\n")                  # the old phrase, no nonce
            sent = True
    _, status = os.waitpid(pid, 0)
    os.close(fd)
    assert os.waitstatus_to_exitcode(status) == 3 and b"NOT-APPROVED" in out


def test_a_child_with_no_controlling_terminal_is_refused_even_if_its_stdin_is_a_pipe_with_the_phrase():
    p = subprocess.run([sys.executable, "-I", str(CHILD)], input="approve local-ollama stage1-pty\n", text=True,
                       capture_output=True, env=_env(), start_new_session=True, timeout=30)
    assert p.returncode == 4 or p.returncode == 3, (p.returncode, p.stdout, p.stderr)
    assert "APPROVED" not in p.stdout.replace("NOT-APPROVED", "")


@pytest.mark.parametrize("name", ["CODEX_HOME", "OPENCODE_CONFIG"])
def test_a_config_variable_exported_in_a_persons_own_shell_does_not_refuse_the_driven_pty(name):
    """The review's input: a person who exports CODEX_HOME or OPENCODE_CONFIG in their profile must still be able to approve."""
    code, out = _drive(_env(**{name: "/x"}))
    assert code == 0 and "APPROVED" in out and "REFUSED" not in out, out
