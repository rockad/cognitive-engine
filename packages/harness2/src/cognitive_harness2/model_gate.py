"""The one-slot model gate: at most ONE model call at a time, whoever makes it.

The local machine has a single GPU. A proposer session and a solver session both end in a model call, and
two at once only make each other slow and flaky. Both therefore take the same gate, `GATE`, around every
session:

    from cognitive_harness2.model_gate import GATE, GateTimeout
    with GATE.hold(timeout=600, label="proposer:p1"):
        run_the_session()

The gate is a plain one-slot semaphore, so it is safe from upstream's thread pool and from asyncio
(`async with GATE.ahold(...)` waits in a worker thread and never blocks the event loop).

  * Deadline-aware: `timeout=` (seconds from now) and `deadline=` (a `time.monotonic()` instant) both bound the
    wait; the earlier wins. Waiting past it raises `GateTimeout` (a `TimeoutError`) naming who holds the slot,
    instead of queueing forever behind a stuck session.
  * Not re-entrant: a thread that holds the gate and asks again waits for itself until the timeout. Take it
    once, at the outermost point where a model call is made, and never inside another `hold`.
  * Not fair: waiters are served in whatever order the OS wakes them.
  * Process-local: it serialises this process only; a second process on the same GPU is not seen.
"""
from __future__ import annotations

import asyncio
import contextlib
import math
import threading
import time
from collections.abc import AsyncIterator, Iterator


class GateTimeout(TimeoutError):
    """The gate was not free before the deadline."""


def _wait_seconds(timeout: float | None, deadline: float | None) -> float | None:
    """Seconds to wait from now (None: forever), the earlier of `timeout` and `deadline`."""
    limits: list[float] = []
    if timeout is not None:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise TypeError("timeout must be a number of seconds")
        if math.isnan(timeout) or timeout < 0:
            raise ValueError("timeout must be a non-negative number of seconds")
        limits.append(float(timeout))
    if deadline is not None:
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or math.isnan(deadline):
            raise TypeError("deadline must be a time.monotonic() instant")
        limits.append(max(0.0, float(deadline) - time.monotonic()))
    return min(limits) if limits else None


class ModelGate:
    def __init__(self) -> None:
        self._sem = threading.BoundedSemaphore(1)
        self._info = threading.Lock()
        self._holder: str | None = None

    def locked(self) -> bool:
        with self._info:
            return self._holder is not None

    def acquire(self, timeout: float | None = None, *, deadline: float | None = None, label: str = "") -> None:
        wait = _wait_seconds(timeout, deadline)
        ok = self._sem.acquire(blocking=False) if wait == 0 else self._sem.acquire(timeout=wait)
        if not ok:
            with self._info:
                who = self._holder
            raise GateTimeout(f"the model gate was not free in time (held by {who or 'someone'})")
        with self._info:
            self._holder = label or "unlabelled"

    def release(self) -> None:
        with self._info:
            self._holder = None
        try:
            self._sem.release()
        except ValueError:
            raise RuntimeError("the model gate was released without being acquired") from None

    @contextlib.contextmanager
    def hold(self, timeout: float | None = None, *, deadline: float | None = None, label: str = "") -> Iterator[None]:
        self.acquire(timeout, deadline=deadline, label=label)
        try:
            yield
        finally:
            self.release()

    async def aacquire(self, timeout: float | None = None, *, deadline: float | None = None, label: str = "") -> None:
        # The wait happens in a worker thread. If this task is cancelled the thread may still win the slot, so a
        # callback hands it straight back; the slot is never leaked.
        fut = asyncio.get_running_loop().run_in_executor(None, lambda: self.acquire(timeout, deadline=deadline, label=label))
        try:
            await asyncio.shield(fut)
        except asyncio.CancelledError:
            def give_back(f: asyncio.Future) -> None:
                if not f.cancelled() and f.exception() is None:
                    self.release()
            fut.add_done_callback(give_back)
            raise

    @contextlib.asynccontextmanager
    async def ahold(self, timeout: float | None = None, *, deadline: float | None = None,
                    label: str = "") -> AsyncIterator[None]:
        await self.aacquire(timeout, deadline=deadline, label=label)
        try:
            yield
        finally:
            self.release()


GATE = ModelGate()
