"""The one-slot model gate: a single GPU serves one model call at a time, proposer and solver alike."""
from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from cognitive_harness2 import model_gate
from cognitive_harness2.model_gate import GATE, GateTimeout, ModelGate


def test_the_module_level_gate_is_one_shared_object():
    assert isinstance(GATE, ModelGate)
    import cognitive_harness2.model_gate as again
    assert again.GATE is GATE


def test_hold_is_exclusive_across_threads():
    gate = ModelGate()
    inside, peak, lock = 0, 0, threading.Lock()

    def work():
        nonlocal inside, peak
        with gate.hold(timeout=10):
            with lock:
                inside += 1
                peak = max(peak, inside)
            time.sleep(0.02)
            with lock:
                inside -= 1

    with ThreadPoolExecutor(8) as pool:
        for f in [pool.submit(work) for _ in range(16)]:
            f.result()
    assert peak == 1 and not gate.locked()


def test_a_waiter_times_out_with_a_gate_timeout_and_names_the_holder():
    gate = ModelGate()
    with gate.hold(label="proposer:p1"):
        t0 = time.monotonic()
        with pytest.raises(GateTimeout, match="proposer:p1"):
            with gate.hold(timeout=0.15, label="solver"):
                pytest.fail("must not enter")
        assert 0.1 <= time.monotonic() - t0 < 2
    assert not gate.locked()
    with gate.hold(timeout=0.1):       # still usable after a timed-out waiter
        assert gate.locked()


def test_a_deadline_in_the_past_does_not_wait_at_all():
    gate = ModelGate()
    with gate.hold():
        t0 = time.monotonic()
        with pytest.raises(GateTimeout):
            gate.acquire(deadline=time.monotonic() - 1)
        assert time.monotonic() - t0 < 0.1


def test_a_deadline_is_a_monotonic_instant_and_the_earlier_of_timeout_and_deadline_wins():
    gate = ModelGate()
    with gate.hold():
        t0 = time.monotonic()
        with pytest.raises(GateTimeout):
            gate.acquire(timeout=30, deadline=time.monotonic() + 0.1)
        assert time.monotonic() - t0 < 2
        t0 = time.monotonic()
        with pytest.raises(GateTimeout):
            gate.acquire(timeout=0.1, deadline=time.monotonic() + 30)
        assert time.monotonic() - t0 < 2


def test_a_free_gate_is_acquired_even_with_a_zero_timeout():
    gate = ModelGate()
    gate.acquire(timeout=0)
    gate.release()


def test_hold_releases_when_the_body_raises():
    gate = ModelGate()
    with pytest.raises(RuntimeError):
        with gate.hold():
            raise RuntimeError("boom")
    assert not gate.locked()


def test_release_without_acquire_is_an_error_not_a_second_slot():
    gate = ModelGate()
    with pytest.raises(RuntimeError):
        gate.release()
    with gate.hold():
        with pytest.raises(GateTimeout):
            gate.acquire(timeout=0.05)     # still exactly one slot


def test_negative_or_non_numeric_timeouts_are_refused():
    gate = ModelGate()
    for bad in (-1, "x", float("nan")):
        with pytest.raises((ValueError, TypeError)):
            gate.acquire(timeout=bad)


def test_the_async_hold_serialises_tasks_without_blocking_the_event_loop():
    async def main():
        gate = ModelGate()
        inside, peak, ticks = 0, 0, 0

        async def work():
            nonlocal inside, peak
            async with gate.ahold(timeout=10):
                inside += 1
                peak = max(peak, inside)
                await asyncio.sleep(0.02)
                inside -= 1

        async def ticker():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.005)

        t = asyncio.create_task(ticker())
        await asyncio.gather(*[work() for _ in range(6)])
        t.cancel()
        return peak, ticks, gate.locked()

    peak, ticks, locked = asyncio.run(main())
    assert peak == 1 and ticks > 5 and not locked        # the loop kept ticking while tasks waited


def test_the_async_hold_times_out_with_a_gate_timeout():
    async def main():
        gate = ModelGate()
        async with gate.ahold():
            with pytest.raises(GateTimeout):
                async with gate.ahold(timeout=0.1):
                    pytest.fail("must not enter")
        return gate.locked()

    assert asyncio.run(main()) is False


def test_a_cancelled_async_waiter_does_not_leak_the_slot():
    async def main():
        gate = ModelGate()
        holder = gate.acquire()
        waiter = asyncio.create_task(gate.aacquire(timeout=5))
        await asyncio.sleep(0.05)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        gate.release()
        await asyncio.sleep(0.2)               # the abandoned acquire finishes and hands the slot back
        return gate.locked()

    assert asyncio.run(main()) is False


def test_threads_and_async_share_the_slot():
    gate = ModelGate()
    started = threading.Event()
    release = threading.Event()

    def thread_holder():
        with gate.hold():
            started.set()
            release.wait(5)

    th = threading.Thread(target=thread_holder)
    th.start()
    started.wait(5)

    async def main():
        with pytest.raises(GateTimeout):
            async with gate.ahold(timeout=0.1):
                pytest.fail("must not enter")
        release.set()
        async with gate.ahold(timeout=5):
            return True

    assert asyncio.run(main()) is True
    th.join()
    assert not gate.locked()


def test_the_module_documents_its_one_hazard():
    assert "re-entrant" in (model_gate.__doc__ or "").lower() or "reentrant" in (model_gate.__doc__ or "").lower()
