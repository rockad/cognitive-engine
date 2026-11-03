"""A named exclusive slot shared between processes: the `BuildSlot` Protocol and its flock implementation.

`model_gate.GATE` serialises one process. A second process on the same machine (a Docker-backed test suite while a
stage-1 run holds the daemon, two agents on one GPU) is not seen by it. A `BuildSlot` is the cross-process version:

    from cognitive_harness2.coordination import FlockBuildSlot, SlotTimeout
    slots = FlockBuildSlot()
    with slots.hold("rootless-docker", timeout=600, label="suite:full"):
        run_the_docker_suite()

Scope: ONE resource kind, a named exclusive slot. File leases, mailboxes, task delegation and heartbeats are not
here; each would need its own contract and a named consumer first.

What the Protocol promises, and what only some implementations can add:

  * required: mutual exclusion among cooperating processes on one host; release when the holder dies;
    `timeout=` (seconds from now) and `deadline=` (a `time.monotonic()` instant), the earlier wins, expiry raises
    `SlotTimeout`; `SlotTimeout.holder` is the holder when it is known and `None` when not (never guessed).
  * optional: holder metadata; `ttl_hint` (a hint, never enforced by the flock implementation: a live holder keeps
    the flock for as long as it lives).
  * out of scope, callers must not rely on them: fairness or queue order, forced release of a hung live holder,
    heartbeat, cross-host.
  * not re-entrant: a thread that holds a slot and asks for it again waits for itself until the timeout. Take it
    once, at the outermost point. If `GATE` is needed too, take `GATE` first, then the slot, and release in
    reverse order.

Flock implementation:

  * The kernel call is `fcntl.flock`, never `fcntl.lockf`: that is what `flock(1)` uses, so a shell wrapper
    `flock <lockfile> pytest ...` and a Python `hold()` on the same file exclude each other.
  * The wait is a poll of `LOCK_EX | LOCK_NB` until the deadline: a blocking flock cannot honour a deadline.
  * Holder data goes to a sidecar `<lockfile>.holder` (JSON), written after the lock is held and removed before it
    is released. The lock file itself stays empty. The sidecar is best effort and untrusted: it is size-capped,
    strictly parsed and sanitised, and ignored when the lock is in fact free or its pid is dead on this host.
    A holder that is not a `FlockBuildSlot` (the shell wrapper) leaves no sidecar: the holder is then unknown.
  * A hung live holder blocks everyone until someone kills it; the error names the holder. Nothing is ever killed
    here. Lock files must be on a local filesystem (not SMB or NFS); nothing checks.
  * Nesting: `flock(1)` holds the lock on its own open file description, so a `hold()` of the same slot inside a
    command that the wrapper started would wait for its own parent. The wrapper therefore exports
    `CE_BUILDSLOT_HELD_<SLOT>=1` (see `marker_name`) and `hold()` then returns without taking the lock. This is a
    COOPERATION device inside one process tree, not enforcement: any process that sets the variable skips the
    lock. Only the exact value `1` counts, and only for that slot.
"""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import math
import os
import re
import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from .model_gate import _wait_seconds

__all__ = ["DEFAULT_PATHS", "BuildSlot", "FlockBuildSlot", "HolderInfo", "SlotTimeout", "marker_name"]

MARKER_PREFIX = "CE_BUILDSLOT_HELD_"
_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_SIDECAR_MAX_BYTES = 4096
_LABEL_MAX = 120
_HOST_MAX = 255
# the slots whose lock file is shared with something outside Python (a shell wrapper); every other name is private
DEFAULT_PATHS: Mapping[str, str] = {"rootless-docker": "~/.agents/cognitive-engine-docker-tests.lock"}


@dataclass(frozen=True)
class HolderInfo:
    """Who holds a slot. Every field is optional: an implementation fills what it can."""
    label: str | None = None
    pid: int | None = None
    host: str | None = None
    acquired_at: float | None = None       # unix time
    ttl_hint_s: float | None = None        # what the holder said, not a promise

    def describe(self) -> str:
        parts = [self.label or "unlabelled"]
        if self.pid is not None:
            parts.append(f"pid {self.pid}")
        if self.host:
            parts.append(f"on {self.host}")
        return " ".join(parts)


class SlotTimeout(TimeoutError):
    """The slot was not free before the deadline. `holder` is who holds it, or None when that is not known."""

    def __init__(self, slot: str, holder: HolderInfo | None):
        who = holder.describe() if holder is not None else "someone, unknown"
        super().__init__(f"slot {slot!r} was not free in time (held by {who})")
        self.slot, self.holder = slot, holder


@runtime_checkable
class BuildSlot(Protocol):
    def hold(self, name: str, *, timeout: float | None = None, deadline: float | None = None,
             label: str | None = None, ttl_hint: float | None = None) -> AbstractContextManager[None]: ...

    def ahold(self, name: str, *, timeout: float | None = None, deadline: float | None = None,
              label: str | None = None, ttl_hint: float | None = None) -> contextlib.AbstractAsyncContextManager[None]: ...

    def holder(self, name: str) -> HolderInfo | None: ...


def marker_name(slot: str) -> str:
    """The environment variable a wrapper exports to say "this process tree already holds `slot`": the slot name
    upper-cased with every `-` replaced by `_` (`rootless-docker` gives
    `CE_BUILDSLOT_HELD_ROOTLESS_DOCKER`). A slot name may contain only lower-case letters, digits and `-`, so no two names share a
    marker or a lock file (names with `_` or `.` were refused for exactly that reason: `a-b` and `a_b` would have had two lock
    files and one marker, and a wrapper holding one would have made `hold()` of the other skip a lock someone else can take)."""
    return MARKER_PREFIX + re.sub(r"[^A-Za-z0-9]", "_", _checked(slot)).upper()


def _checked(name: str) -> str:
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError("a slot name is 1 to 64 characters: lower-case letters, digits and '-', starting with a letter or digit")
    return name


def _clean(text: object, limit: int) -> str | None:
    """`text` as one printable-ASCII token line (everything else dropped), cut to `limit`; None if nothing is left."""
    if not isinstance(text, str):
        return None
    out = "".join(c for c in text if " " <= c <= "~")[:limit].strip()
    return out or None


def _number(value: object, *, positive: bool) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value) if (value > 0 if positive else value >= 0) else None


class _Cancelled(Exception):
    """A waiter in a worker thread was told to stop (its task was cancelled)."""


class _Held:
    """One acquired slot: the open, locked file and the sidecar to clean up."""

    def __init__(self, fd: int, sidecar: Path, pid: int) -> None:
        self.fd, self.sidecar, self.pid = fd, sidecar, pid
        self._done = False

    def release(self) -> None:
        if self._done:
            return
        self._done = True
        try:
            # first: once the lock is dropped the next holder may already have written its own sidecar
            with contextlib.suppress(OSError):
                if _read_sidecar_pid(self.sidecar) == self.pid:
                    self.sidecar.unlink()
        finally:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)


def _read_sidecar_pid(path: Path) -> int | None:
    info = _parse_sidecar(path)
    return None if info is None else info.pid


def _parse_sidecar(path: Path) -> HolderInfo | None:
    try:
        with open(path, "rb") as f:
            raw = f.read(_SIDECAR_MAX_BYTES + 1)
    except OSError:
        return None
    if len(raw) > _SIDECAR_MAX_BYTES:
        return None
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        return None
    if not isinstance(obj, dict):
        return None
    pid = _number(obj.get("pid"), positive=True)
    info = HolderInfo(label=_clean(obj.get("label"), _LABEL_MAX), pid=None if pid is None or pid != int(pid) else int(pid),
                      host=_clean(obj.get("host"), _HOST_MAX), acquired_at=_number(obj.get("acquired_at"), positive=True),
                      ttl_hint_s=_number(obj.get("ttl_hint_s"), positive=True))
    return info if info != HolderInfo() else None            # nothing usable in it: unknown, not an empty holder


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class FlockBuildSlot:
    """`BuildSlot` over `fcntl.flock` lock files, cross-process on one host, no daemon and no port.

    `paths` maps a slot name to its lock file; a name not in it uses `<state_dir>/<name>.lock` (default
    `$XDG_STATE_HOME/cognitive-engine/slots`, created 0700). `rootless-docker` defaults to the lock file the shell
    wrapper uses, so wrapper and Python exclude each other."""

    def __init__(self, paths: Mapping[str, str | os.PathLike[str]] | None = None, *, state_dir: str | os.PathLike[str] | None = None,
                 poll_s: float = 0.05, host: str | None = None) -> None:
        if not (isinstance(poll_s, (int, float)) and not isinstance(poll_s, bool) and 0 < poll_s <= 5):
            raise ValueError("poll_s must be a number of seconds, more than 0 and at most 5")
        self._paths = {_checked(k): Path(v).expanduser() for k, v in {**DEFAULT_PATHS, **(paths or {})}.items()}
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
        self._state_dir = Path(state_dir).expanduser() if state_dir is not None else base / "cognitive-engine" / "slots"
        self._poll_s = float(poll_s)
        self._host = host if host is not None else socket.gethostname()

    # ── paths ─────────────────────────────────────────────────────────────────────

    def lock_path(self, name: str) -> Path:
        name = _checked(name)
        if name in self._paths:
            return self._paths[name]
        self._state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        return self._state_dir / f"{name}.lock"

    @staticmethod
    def sidecar_path(lock: Path) -> Path:
        return lock.with_name(lock.name + ".holder")

    # ── holder ────────────────────────────────────────────────────────────────────

    def holder(self, name: str) -> HolderInfo | None:
        """Who holds `name`, best effort and possibly stale, or None when not known (including: free). Only a
        sidecar written by a `FlockBuildSlot` is read, and only while the lock really is held and, for a holder on
        this host, its pid is alive. To check that the lock is held it tries the lock once without waiting and
        gives it straight back."""
        lock = self.lock_path(name)
        info = _parse_sidecar(self.sidecar_path(lock))
        if info is None:
            return None
        if info.host == self._host and info.pid is not None and not _pid_alive(info.pid):
            return None
        try:
            fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        except OSError:
            return info
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return info
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)       # nobody held it: the sidecar is a leftover
            return None
        finally:
            os.close(fd)

    # ── acquire and release ───────────────────────────────────────────────────────

    def _acquire(self, name: str, wait: float | None, label: str | None, ttl_hint: float | None,
                 cancel: threading.Event | None = None) -> _Held | None:
        """The held slot, or None when the nesting marker says this process tree already holds it."""
        name = _checked(name)
        if os.environ.get(marker_name(name)) == "1":
            return None
        lock = self.lock_path(name)
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        end = None if wait is None else time.monotonic() + wait
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    pass
                if cancel is not None and cancel.is_set():
                    raise _Cancelled
                left = None if end is None else end - time.monotonic()
                if left is not None and left <= 0:
                    raise SlotTimeout(name, self.holder(name))
                time.sleep(self._poll_s if left is None else min(self._poll_s, left))
        except BaseException:
            os.close(fd)
            raise
        held = _Held(fd, self.sidecar_path(lock), os.getpid())
        self._write_sidecar(held, label, ttl_hint)
        return held

    def _write_sidecar(self, held: _Held, label: str | None, ttl_hint: float | None) -> None:
        """Best effort: a hold without a sidecar is a hold whose holder is unknown, never a failed hold."""
        body = {"label": _clean(label, _LABEL_MAX), "pid": held.pid, "host": self._host, "acquired_at": time.time(),
                "ttl_hint_s": _number(ttl_hint, positive=True)}
        tmp = held.sidecar.with_name(f"{held.sidecar.name}.{held.pid}.{threading.get_ident()}.tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(body, f)
            os.replace(tmp, held.sidecar)
        except OSError:
            with contextlib.suppress(OSError):
                tmp.unlink()

    @contextlib.contextmanager
    def hold(self, name: str, *, timeout: float | None = None, deadline: float | None = None,
             label: str | None = None, ttl_hint: float | None = None) -> Iterator[None]:
        held = self._acquire(name, _wait_seconds(timeout, deadline), label, ttl_hint)
        try:
            yield
        finally:
            if held is not None:
                held.release()

    @contextlib.asynccontextmanager
    async def ahold(self, name: str, *, timeout: float | None = None, deadline: float | None = None,
                    label: str | None = None, ttl_hint: float | None = None) -> AsyncIterator[None]:
        """`hold` for asyncio: the wait runs in a worker thread and never blocks the event loop. If this task is
        cancelled while waiting, the worker stops polling and a slot it already won is handed straight back."""
        wait = _wait_seconds(timeout, deadline)
        cancel = threading.Event()
        fut = asyncio.get_running_loop().run_in_executor(None, lambda: self._acquire(name, wait, label, ttl_hint, cancel))
        try:
            held = await asyncio.shield(fut)
        except asyncio.CancelledError:
            cancel.set()

            def give_back(f: asyncio.Future) -> None:
                if not f.cancelled() and f.exception() is None and f.result() is not None:
                    f.result().release()
            fut.add_done_callback(give_back)
            raise
        try:
            yield
        finally:
            if held is not None:
                held.release()
