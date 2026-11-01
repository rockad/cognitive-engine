"""Teardown of a contained session cannot be aborted half way by an interrupt (F5).

A second Ctrl-C during teardown used to abort the remaining cleanup (docker calls of up to 60 s, a 120 s reclaim, sleeps),
so a network or an `h2s-` scratch directory could leak without a `SessionCleanupError`. Here a fake runner (no Docker, no
subprocess, no network) raises `KeyboardInterrupt` in each teardown step and a real SIGINT is delivered during teardown: the
cleanup still completes, and the first interrupt is re-raised at the end.
"""
from __future__ import annotations

import io
import os
import signal
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from cognitive_harness2 import sandbox, session_runner as sr, upstream_egress
from cognitive_harness2.egress_proxy import READY_LINE
from cognitive_harness2.image import ImageRef
from cognitive_harness2.session_runner import ProposerSessionRunner, SessionLimits, SessionSpec
from test_session_runner import PROFILE

V = sandbox.VerifiedDocker("/usr/bin/docker", "unix:///run/user/1000/docker.sock")
IMG = ImageRef("cognitive-harness2-proposer:0123456789ab", "sha256:" + "5" * 64)
OK = upstream_egress.Decision(True, ("approved",))


class FakeProc:
    """The `docker run` of the session: finishes at once, says nothing."""

    def __init__(self, *a, **k):
        self.stdin, self.stdout, self.stderr = None, io.BytesIO(b"done\n"), io.BytesIO(b"")

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return 0

    def kill(self):
        pass


class FakeRunner(ProposerSessionRunner):
    """Every docker call is recorded and answered; `interrupts` maps a step key to how many times it raises KeyboardInterrupt."""

    def __init__(self, *a, interrupts=None, **k):
        super().__init__(*a, **k)
        self.calls: list[list[str]] = []
        self.interrupts = dict(interrupts or {})
        self.seen_handlers: list[tuple[str, object, object]] = []
        self.hook = None

    @staticmethod
    def key(args) -> str:
        return f"{args[0]} {args[1]}" if args[0] == "network" else args[0]

    def _docker(self, args, *, timeout=60):
        args = list(args)
        k = self.key(args)
        self.calls.append(args)
        self.seen_handlers.append((k, signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)))
        if self.hook:
            self.hook(k, args)
        if self.interrupts.get(k, 0) > 0:
            self.interrupts[k] -= 1
            raise KeyboardInterrupt
        out = ""
        if args[0] == "logs":
            out = READY_LINE + "\n"
        elif args[0] == "inspect":
            out = "true"
        return subprocess.CompletedProcess(args, 0, out, "")

    def count(self, k: str) -> int:
        return sum(self.key(a) == k for a in self.calls)


@pytest.fixture
def world(tmp_path, monkeypatch):
    cli = tmp_path / "bin" / "fake-cli"
    cli.parent.mkdir()
    cli.write_text("#!/bin/sh\nexit 0\n")
    cli.chmod(0o755)
    ws = tmp_path / "ws"
    ws.mkdir()
    runtime = tmp_path / "run"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(sr.subprocess, "Popen", FakeProc)
    return SimpleNamespace(cli=str(cli), ws=str(ws), runtime=runtime)


def make(world, **kw):
    r = FakeRunner(V, IMG, PROFILE.channels["claude"], zdr="off", classification="open", proposer_decision=OK, **kw)
    spec = SessionSpec(argv=(sr.container_cli_path(world.cli), "run"), workspace=world.ws, cli=world.cli, timeout_s=60,
                       limits=SessionLimits(), harvest=None, reclaim_workspace=True)
    return r, r.authorize(spec)


def scratch(world) -> list[str]:
    return [p.name for p in world.runtime.iterdir() if p.name.startswith("h2s-")]


def assert_fully_cleaned(r, world, *, networks=2):
    assert r.count("kill") >= 2 and r.count("rm") >= 3                    # session + proxy killed and removed, and the reclaim box
    assert r.count("network rm") >= networks and r.count("run") >= 1      # every network removed, the workspace reclaimed
    assert scratch(world) == []                                           # no `h2s-` directory left


def test_an_uninterrupted_session_cleans_everything_and_returns_its_result(world):
    r, plan = make(world)
    res = r._execute(plan)
    assert res.exit_code == 0 and "done" in res.stdout and res.leftovers == ()
    assert_fully_cleaned(r, world)


@pytest.mark.parametrize("step", ["kill", "rm", "network rm", "run"])
def test_a_keyboard_interrupt_in_any_teardown_step_does_not_stop_the_rest_and_is_reraised_last(world, step):
    """`run` here is the workspace reclaim; `kill`, `rm` and `network rm` are the container and network teardown."""
    r, plan = make(world, interrupts={step: 1})
    with pytest.raises(KeyboardInterrupt):
        r._execute(plan)
    assert_fully_cleaned(r, world)


@pytest.mark.parametrize("step", ["kill", "rm", "network rm", "run"])
def test_a_step_that_is_interrupted_every_time_is_still_followed_by_all_the_others(world, step):
    r, plan = make(world, interrupts={step: 100})
    with pytest.raises(KeyboardInterrupt):
        r._execute(plan)
    for other in ("kill", "rm", "network rm", "run"):
        assert r.count(other) >= 1, (step, other, r.calls)
    assert scratch(world) == [] or step == "never"       # the scratch directory is removed whichever docker step is interrupted


def test_interrupts_in_every_teardown_step_at_once_still_complete_the_cleanup(world):
    r, plan = make(world, interrupts={"kill": 1, "rm": 2, "network rm": 1, "run": 1})
    with pytest.raises(KeyboardInterrupt):
        r._execute(plan)
    assert_fully_cleaned(r, world)


def test_an_interrupt_during_the_session_runs_the_whole_teardown_even_with_a_second_interrupt_in_it(world):
    r, plan = make(world, interrupts={"inspect": 1, "kill": 1, "network rm": 1})       # the first lands in _wait_ready
    with pytest.raises(KeyboardInterrupt):
        r._execute(plan)
    assert r.count("kill") >= 2 and r.count("network rm") >= 2 and scratch(world) == []


def test_the_first_interrupt_is_the_one_reraised(world):
    first, second = KeyboardInterrupt("first"), KeyboardInterrupt("second")
    r, plan = make(world)
    boxes = iter([first, second])

    def hook(k, args):
        if k in ("kill", "network rm"):
            raise next(boxes, second)
    r.hook = hook
    with pytest.raises(KeyboardInterrupt) as ei:
        r._execute(plan)
    assert ei.value is first


def test_sigint_and_sigterm_are_ignored_during_teardown_and_restored_after(world):
    sentinel_int, sentinel_term = (lambda *a: None), (lambda *a: None)
    old_int, old_term = signal.signal(signal.SIGINT, sentinel_int), signal.signal(signal.SIGTERM, sentinel_term)
    try:
        r, plan = make(world)
        r._execute(plan)
        in_teardown = [(k, i, t) for k, i, t in r.seen_handlers if k in ("kill", "network rm", "run")]
        assert in_teardown and all(i == signal.SIG_IGN and t == signal.SIG_IGN for _, i, t in in_teardown)
        before = [(k, i, t) for k, i, t in r.seen_handlers if k in ("network create", "create", "start")]
        assert before and all(i is sentinel_int and t is sentinel_term for _, i, t in before)      # not ignored while it runs
        assert signal.getsignal(signal.SIGINT) is sentinel_int and signal.getsignal(signal.SIGTERM) is sentinel_term
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)


def test_handlers_are_restored_when_the_teardown_itself_raises_something_else(world):
    sentinel = (lambda *a: None)
    old = signal.signal(signal.SIGINT, sentinel)
    try:
        r, plan = make(world)

        def hook(k, args):
            if k == "network rm":
                raise RuntimeError("boom")
        r.hook = hook
        try:
            r._execute(plan)
        except RuntimeError:
            pass
        assert signal.getsignal(signal.SIGINT) is sentinel
    finally:
        signal.signal(signal.SIGINT, old)


def test_a_real_sigint_during_teardown_does_not_abort_it(world):
    r, plan = make(world)

    def hook(k, args):
        if k == "kill":
            os.kill(os.getpid(), signal.SIGINT)            # the second Ctrl-C
    r.hook = hook
    res = r._execute(plan)                                   # no KeyboardInterrupt: it was ignored
    assert res.exit_code == 0
    assert_fully_cleaned(r, world)


def test_in_a_worker_thread_the_signal_handlers_are_left_alone(world, monkeypatch):
    calls = []
    real = signal.signal
    monkeypatch.setattr(sr.signal, "signal", lambda *a: (calls.append(a), real(*a))[1])
    r, plan = make(world)
    out = {}

    def work():
        try:
            out["res"] = r._execute(plan)
        except BaseException as e:   # noqa: BLE001
            out["err"] = e
    t = threading.Thread(target=work)
    t.start()
    t.join(30)
    assert "err" not in out and out["res"].exit_code == 0 and calls == []
    assert_fully_cleaned(r, world)


def test_the_scratch_directory_is_made_inside_the_guarded_block(world, monkeypatch):
    """`tempfile.mkdtemp` used to run before the `try`: whatever it raised skipped the whole cleanup."""
    r, plan = make(world)
    monkeypatch.setattr(sr.tempfile, "mkdtemp", lambda **kw: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        r._execute(plan)
    assert r.count("network create") == 0 and r.count("kill") >= 2 and r.count("rm") >= 2       # teardown still ran, on the names
    assert r.count("create") == 0 and scratch(world) == []


def test_mkdtemp_failing_with_an_oserror_is_still_raised_as_that_error(world, monkeypatch):
    r, plan = make(world)
    monkeypatch.setattr(sr.tempfile, "mkdtemp", lambda **kw: (_ for _ in ()).throw(OSError(28, "No space left")))
    with pytest.raises(OSError, match="No space"):
        r._execute(plan)
    assert r.count("network create") == 0


def test_teardown_called_on_its_own_reraises_the_first_interrupt_after_finishing(world):
    r, plan = make(world, interrupts={"kill": 1})
    names = sr.Names("abc123def456")
    base = Path(world.runtime) / "h2s-x"
    base.mkdir()
    import contextlib
    with pytest.raises(KeyboardInterrupt):
        r._teardown(names, base, ["n1", "n2"], contextlib.ExitStack())
    assert r.count("kill") == 3 and r.count("network rm") == 2 and not base.exists()       # the interrupted kill was retried


# ══ the window between entering `finally:` and the shield being in place ═════════════════════════════════════════════════
# An interrupt that lands after the body has ended but before SIGINT and SIGTERM are ignored (the `finally:` entry, the
# `_shielded_from_interrupts()` call, the first `signal.signal`) used to skip the whole teardown with no SessionCleanupError.
# The teardown now resumes: the interrupt is remembered (re-raised last, like any other) and the teardown runs again, shielded.


def _interrupt_entering_the_shield(how, times=1):
    """A replacement for `_shielded_from_interrupts` whose first `times` entries are interrupted before the real shield is up."""
    real = sr._shielded_from_interrupts
    left = [times]

    @__import__("contextlib").contextmanager
    def shield():
        if left[0] > 0:
            left[0] -= 1
            how()
        with real():
            yield
    return shield


def _raise_ki():
    raise KeyboardInterrupt("in the window")


def _deliver_sigint():
    os.kill(os.getpid(), signal.SIGINT)
    for _ in range(3):                      # the handler runs between bytecodes of this thread
        pass


@pytest.mark.parametrize("how", [_raise_ki, _deliver_sigint], ids=["raised", "real-sigint"])
@pytest.mark.parametrize("times", [1, 2, 3])
def test_an_interrupt_before_the_shield_is_up_does_not_skip_the_teardown(world, monkeypatch, how, times):
    r, plan = make(world)
    monkeypatch.setattr(sr, "_shielded_from_interrupts", _interrupt_entering_the_shield(how, times))
    with pytest.raises(KeyboardInterrupt):
        r._execute(plan)                                   # the interrupt is not swallowed ...
    assert_fully_cleaned(r, world)                         # ... and the whole teardown, reclaim and leftover check still ran


def test_the_interrupt_in_the_window_is_the_one_reraised_and_no_cleanup_error_is_raised(world, monkeypatch):
    first = KeyboardInterrupt("first")
    r, plan = make(world)

    def how():
        raise first
    monkeypatch.setattr(sr, "_shielded_from_interrupts", _interrupt_entering_the_shield(how))
    with pytest.raises(KeyboardInterrupt) as ei:
        r._execute(plan)
    assert ei.value is first and not isinstance(ei.value, sr.SessionCleanupError)


def test_an_interrupt_between_the_two_handler_installs_leaves_the_old_handlers_in_place(world, monkeypatch):
    sentinel_int, sentinel_term = (lambda *a: None), (lambda *a: None)
    old_int, old_term = signal.signal(signal.SIGINT, sentinel_int), signal.signal(signal.SIGTERM, sentinel_term)
    try:
        real = signal.signal
        seen = []

        def flaky(sig, handler):
            seen.append((sig, handler))
            out = real(sig, handler)
            if len(seen) == 1 and handler == signal.SIG_IGN:
                raise KeyboardInterrupt("right after the first install")       # SIGINT is already ignored, SIGTERM not yet
            return out
        monkeypatch.setattr(sr.signal, "signal", flaky)
        r, plan = make(world)
        with pytest.raises(KeyboardInterrupt):
            r._execute(plan)
        assert signal.getsignal(signal.SIGINT) is sentinel_int and signal.getsignal(signal.SIGTERM) is sentinel_term
        assert_fully_cleaned(r, world)
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)

