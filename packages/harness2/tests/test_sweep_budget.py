"""`SweepBudget`: one wall-clock deadline and one session counter shared by proposer and solver sessions."""
from __future__ import annotations

import json
import math
import threading

import pytest

from cognitive_harness2.sweep_budget import BudgetExhausted, Reservation, SweepBudget


class Clock:
    """A monotonic clock the test moves by hand."""

    def __init__(self, start: float = 1000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


def make(total=2700.0, per=600.0, n=7, clock=None, **kw) -> tuple[SweepBudget, Clock]:
    clock = clock or Clock()
    return SweepBudget(total, per, n, clock=clock, **kw), clock


# ── construction ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("kw", [
    dict(total_wall_s=0), dict(total_wall_s=-1), dict(total_wall_s=math.nan), dict(total_wall_s=math.inf),
    dict(total_wall_s=True), dict(total_wall_s="60"), dict(total_wall_s=None),
    dict(per_session_s=0), dict(per_session_s=-5), dict(per_session_s=math.inf), dict(per_session_s=False),
    dict(max_sessions=0), dict(max_sessions=-1), dict(max_sessions=1.5), dict(max_sessions=True), dict(max_sessions=None),
    dict(role_caps={"nobody": 1}), dict(role_caps={"solver": 0}), dict(role_caps={"solver": 1.5}), dict(role_caps=[1]),
    dict(min_session_s=-1), dict(min_session_s=math.nan),
])
def test_a_bad_cap_is_refused_at_construction(kw):
    base = dict(total_wall_s=100.0, per_session_s=10.0, max_sessions=3)
    with pytest.raises((ValueError, TypeError)):
        SweepBudget(**{**base, **kw})


def test_the_clock_must_be_callable():
    with pytest.raises(TypeError):
        SweepBudget(10, 5, 1, clock=3)  # type: ignore[arg-type]


# ── reserve / release ───────────────────────────────────────────────────────────────

def test_a_reservation_carries_the_timeout_and_the_role():
    b, _ = make()
    r = b.reserve_session("solver")
    assert isinstance(r, Reservation) and r.role == "solver" and r.timeout_s == 600.0 and r.id == 1
    assert float(r) == 600.0
    assert b.reserve_session().role == "proposer"


def test_the_timeout_is_the_smaller_of_the_per_session_cap_and_what_remains():
    b, clock = make(total=700.0, per=600.0)
    assert b.reserve_session("proposer").timeout_s == 600.0
    clock.advance(250)
    assert b.reserve_session("solver").timeout_s == 450.0
    clock.advance(448.0)
    assert b.reserve_session("solver").timeout_s == 2.0


def test_no_session_starts_after_the_deadline_or_in_the_last_instant():
    b, clock = make(total=100.0, per=60.0)
    clock.advance(99.5)                       # 0.5s left, below the 1s minimum
    with pytest.raises(BudgetExhausted) as e:
        b.reserve_session("solver")
    assert e.value.reason == "deadline"
    clock.advance(0.5)                        # exactly at the deadline
    with pytest.raises(BudgetExhausted):
        b.reserve_session("proposer")
    clock.advance(1000)
    with pytest.raises(BudgetExhausted):
        b.reserve_session("proposer")
    assert b.snapshot()["counts"]["total"] == 0 and b.snapshot()["refusals"] == {"deadline": 3}


def test_min_session_s_can_be_zero():
    b, clock = make(total=100.0, per=60.0, min_session_s=0.0)
    clock.advance(99.9)
    assert b.reserve_session().timeout_s == pytest.approx(0.1)
    clock.advance(0.1)
    with pytest.raises(BudgetExhausted):
        b.reserve_session()


def test_the_session_cap_counts_both_roles_together():
    b, _ = make(n=3)
    b.reserve_session("proposer")
    b.reserve_session("solver")
    b.reserve_session("solver")
    with pytest.raises(BudgetExhausted) as e:
        b.reserve_session("proposer")
    assert e.value.reason == "sessions"
    assert b.snapshot()["counts"] == {"proposer": 1, "solver": 2, "total": 3}


def test_role_caps_count_each_role_separately():
    b, _ = make(n=7, role_caps={"proposer": 3, "solver": 4})
    for _ in range(3):
        b.reserve_session("proposer")
    with pytest.raises(BudgetExhausted) as e:
        b.reserve_session("proposer")
    assert e.value.reason == "role-sessions" and "proposer" in str(e.value)
    for _ in range(4):
        b.reserve_session("solver")
    with pytest.raises(BudgetExhausted):
        b.reserve_session("solver")
    assert b.snapshot()["counts"] == {"proposer": 3, "solver": 4, "total": 7}


def test_the_total_cap_wins_over_role_caps_that_add_up_to_more():
    b, _ = make(n=2, role_caps={"proposer": 2, "solver": 2})
    b.reserve_session("proposer")
    b.reserve_session("solver")
    with pytest.raises(BudgetExhausted) as e:
        b.reserve_session("solver")
    assert e.value.reason == "sessions"


def test_an_unknown_role_is_refused_and_counts_nothing():
    b, _ = make()
    for bad in ("judge", "", None, 3):
        with pytest.raises(ValueError):
            b.reserve_session(bad)  # type: ignore[arg-type]
    assert b.snapshot()["counts"]["total"] == 0


def test_a_session_that_never_started_gives_its_slot_back():
    b, _ = make(n=1)
    r = b.reserve_session("solver")
    b.release(r, started=False, outcome="refused")
    assert b.snapshot()["counts"] == {"proposer": 0, "solver": 0, "total": 0}
    b.reserve_session("solver")                       # the slot is free again


def test_a_session_that_ran_keeps_its_slot_and_is_recorded():
    b, clock = make(n=1)
    r = b.reserve_session("solver")
    clock.advance(42)
    b.release(r, outcome="timed_out")
    with pytest.raises(BudgetExhausted):
        b.reserve_session("solver")
    s = b.snapshot()
    assert s["active"] == 0 and s["finished"] == 1
    assert s["sessions"][0] == {"id": 1, "role": "solver", "timeout_s": 600.0, "reserved_at_s": 0.0,
                                     "released_at_s": 42.0, "duration_s": 42.0, "outcome": "timed_out"}


def test_release_is_checked():
    b, _ = make()
    other, _ = make()
    r = b.reserve_session()
    with pytest.raises(ValueError):
        other.release(r)                              # not this budget's reservation
    with pytest.raises(ValueError):
        b.release("nope")                             # type: ignore[arg-type]
    b.release(r)
    with pytest.raises(ValueError, match="already released"):
        b.release(r)
    with pytest.raises(ValueError):
        b.release(b.reserve_session(), outcome="x" * 100)
    with pytest.raises(ValueError):
        b.release(b.reserve_session(), outcome="has space and\nnewline")


# ── snapshot ─────────────────────────────────────────────────────────────────────

def test_snapshot_is_json_serialisable_and_a_copy():
    b, clock = make(role_caps={"solver": 4})
    r = b.reserve_session("solver")
    clock.advance(10)
    s = b.snapshot()
    json.dumps(s)
    assert s["total_wall_s"] == 2700.0 and s["per_session_s"] == 600.0 and s["max_sessions"] == 7
    assert s["role_caps"] == {"solver": 4} and s["elapsed_s"] == 10.0 and s["remaining_s"] == 2690.0
    assert s["exhausted"] is False and s["active"] == 1 and s["sessions"][0]["outcome"] is None
    s["sessions"].clear()
    s["counts"]["total"] = 99
    assert b.snapshot()["sessions"] and b.snapshot()["counts"]["total"] == 1
    b.release(r)
    clock.advance(5000)
    s = b.snapshot()
    assert s["exhausted"] is True and s["remaining_s"] == 0.0


def test_the_snapshot_never_carries_a_prompt_or_a_path():
    b, _ = make()
    b.release(b.reserve_session("solver"), outcome="ok")
    assert set(b.snapshot()["sessions"][0]) == {"id", "role", "timeout_s", "reserved_at_s", "released_at_s",
                                                 "duration_s", "outcome"}


# ── threads ───────────────────────────────────────────────────────────────────────

def test_concurrent_reservations_never_exceed_the_session_cap():
    b, _ = make(n=10)
    ok: list[Reservation] = []
    refused: list[BudgetExhausted] = []
    barrier = threading.Barrier(40)

    def worker(i: int) -> None:
        barrier.wait()
        try:
            ok.append(b.reserve_session("solver" if i % 2 else "proposer"))
        except BudgetExhausted as e:
            refused.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(40)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(ok) == 10 and len(refused) == 30
    assert sorted(r.id for r in ok) == list(range(1, 11))
    assert b.snapshot()["counts"]["total"] == 10 and b.snapshot()["refusals"] == {"sessions": 30}


def test_concurrent_role_caps_hold_per_role():
    b, _ = make(n=100, role_caps={"proposer": 3, "solver": 4})
    got = {"proposer": 0, "solver": 0}
    lock = threading.Lock()
    barrier = threading.Barrier(40)

    def worker(i: int) -> None:
        role = "solver" if i % 2 else "proposer"
        barrier.wait()
        try:
            b.reserve_session(role)
        except BudgetExhausted:
            return
        with lock:
            got[role] += 1

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(40)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert got == {"proposer": 3, "solver": 4}


def test_concurrent_reserve_and_release_stay_consistent():
    b, _ = make(n=1000)
    barrier = threading.Barrier(16)

    def worker() -> None:
        barrier.wait()
        for i in range(25):
            r = b.reserve_session("solver")
            b.release(r, started=bool(i % 2), outcome="ok" if i % 2 else "refused")

    threads = [threading.Thread(target=worker) for _ in range(16)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    s = b.snapshot()
    assert s["active"] == 0 and s["counts"]["total"] == 16 * 12 and len(s["sessions"]) == 16 * 25


def test_a_snapshot_taken_during_reservations_is_always_consistent():
    b, _ = make(n=500)
    stop = threading.Event()
    bad: list[str] = []

    def snap() -> None:
        while not stop.is_set():
            s = b.snapshot()
            c = s["counts"]
            if c["total"] != c["proposer"] + c["solver"] or c["total"] > s["max_sessions"]:
                bad.append(str(s))

    t = threading.Thread(target=snap)
    t.start()
    for _ in range(300):
        b.reserve_session("solver")
    stop.set()
    t.join()
    assert not bad


# ── the real clock, and the channel ────────────────────────────────────────────────

def test_the_default_clock_is_monotonic_and_counts_down():
    b = SweepBudget(5.0, 5.0, 2)
    r1 = b.reserve_session()
    assert 0 < r1.timeout_s <= 5.0
    assert b.snapshot()["remaining_s"] <= 5.0


def test_from_channel_takes_the_wall_cap_from_the_profile_and_the_counts_from_the_plan():
    from h2_profiles import PROFILE
    ch = PROFILE.channels["local"]
    b = SweepBudget.from_channel(ch, proposer_sessions=3, solver_sessions=4, per_session_s=600.0, clock=Clock())
    s = b.snapshot()
    assert s["total_wall_s"] == ch.caps.wall_s == 3600.0 and s["max_sessions"] == 7
    assert s["role_caps"] == {"proposer": 3, "solver": 4} and s["per_session_s"] == 600.0
    with pytest.raises(ValueError):
        SweepBudget.from_channel(ch, proposer_sessions=0, solver_sessions=0)
    with pytest.raises(ValueError):
        SweepBudget.from_channel(object(), proposer_sessions=1, solver_sessions=1)  # type: ignore[arg-type]
