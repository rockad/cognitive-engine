"""One shared budget for a sweep's contained sessions, proposer and solver alike (slice 3b, WS-E).

`SweepBudget` is a thread-safe deadline plus a session counter:

  * `total_wall_s`   the wall clock for the whole sweep (from the channel profile's cap), counted from the moment
                     the budget is built, on a MONOTONIC clock (injectable, for tests)
  * `per_session_s`  the longest one session may run
  * `max_sessions`   proposer plus solver sessions together; optional `role_caps` bound each role on its own
                     (at k=1: 3 proposer sessions and 4 solver rollouts, 7 in all)

`reserve_session(role)` is the only way a session may start. It returns a `Reservation` whose `timeout_s` is
`min(per_session_s, time left)`, or raises `BudgetExhausted` when the deadline has passed (or is closer than
`min_session_s`), or a cap is reached. A session that started before the deadline therefore also ends by it: the
runner clips its timeout to the reservation. `release` records how the session ended, and refunds the slot only
when the session never started (`started=False`: a refusal before any container existed).

The budget holds no prompt, path or credential: `snapshot()` is plain numbers and short outcome words, safe to put
in an audit log. Nothing here starts anything or touches the clock except through the injected callable.
"""
from __future__ import annotations

import math
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

ROLES = ("proposer", "solver")
_OUTCOME = re.compile(r"[A-Za-z0-9_.:-]{1,40}")


class BudgetExhausted(RuntimeError):
    """No session may start. `reason` is one of `deadline`, `sessions`, `role-sessions`. Nothing was started."""

    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason


@dataclass(frozen=True)
class Reservation:
    """The right to start one session, for at most `timeout_s` seconds."""
    id: int
    role: str
    timeout_s: float
    _owner: int = 0          # identity of the budget that issued it

    def __float__(self) -> float:
        return float(self.timeout_s)


def _positive(v: Any, name: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return float(v)


def _count(v: Any, name: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return v


class SweepBudget:
    def __init__(self, total_wall_s: float, per_session_s: float, max_sessions: int, *,
                 role_caps: Mapping[str, int] | None = None, min_session_s: float = 1.0,
                 clock: Callable[[], float] = time.monotonic):
        if not callable(clock):
            raise TypeError("clock must be a callable returning monotonic seconds")
        self.total_wall_s = _positive(total_wall_s, "total_wall_s")
        self.per_session_s = _positive(per_session_s, "per_session_s")
        self.max_sessions = _count(max_sessions, "max_sessions")
        if isinstance(min_session_s, bool) or not isinstance(min_session_s, (int, float)) \
                or not math.isfinite(min_session_s) or min_session_s < 0:
            raise ValueError("min_session_s must be a finite number, zero or more")
        self.min_session_s = float(min_session_s)
        caps: dict[str, int] = {}
        if role_caps is not None:
            if not isinstance(role_caps, Mapping):
                raise ValueError("role_caps must map a role to a positive integer")
            for role, n in role_caps.items():
                if role not in ROLES:
                    raise ValueError(f"role_caps names an unknown role {str(role)[:30]!r}; roles are {ROLES}")
                caps[role] = _count(n, f"role_caps[{role}]")
        self.role_caps = caps
        self._clock = clock
        self._start = float(clock())
        self._deadline = self._start + self.total_wall_s
        self._lock = threading.Lock()
        self._next_id = 1
        self._sessions: dict[int, dict[str, Any]] = {}
        self._refusals: dict[str, int] = {}

    @classmethod
    def from_channel(cls, channel: Any, *, proposer_sessions: int, solver_sessions: int,
                     per_session_s: float = 600.0, min_session_s: float = 1.0,
                     clock: Callable[[], float] = time.monotonic) -> "SweepBudget":
        """Total wall from the declared channel's `caps.wall_s`; the session counts are the plan's P and R."""
        try:
            wall = channel.caps.wall_s
        except AttributeError:
            raise ValueError("channel must be a declared channels.Channel (it has no caps.wall_s)") from None
        p, r = _count(proposer_sessions, "proposer_sessions"), _count(solver_sessions, "solver_sessions")
        return cls(wall, per_session_s, p + r, role_caps={"proposer": p, "solver": r},
                   min_session_s=min_session_s, clock=clock)

    # ── the one gate ─────────────────────────────────────────────────────────────────

    def _counted(self, role: str | None = None) -> int:
        return sum(1 for s in self._sessions.values() if s["counted"] and (role is None or s["role"] == role))

    def _refuse(self, reason: str, detail: str) -> BudgetExhausted:
        self._refusals[reason] = self._refusals.get(reason, 0) + 1
        return BudgetExhausted(reason, detail)

    def reserve_session(self, role: str = "proposer") -> Reservation:
        if role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}, not {str(role)[:30]!r}")
        with self._lock:
            now = float(self._clock())
            remaining = self._deadline - now
            if remaining <= 0 or remaining < self.min_session_s:
                raise self._refuse("deadline", f"the sweep's wall-clock budget ({self.total_wall_s:g}s) is spent: "
                                               "no session may start")
            if self._counted() >= self.max_sessions:
                raise self._refuse("sessions", f"the sweep's session cap ({self.max_sessions}) is reached")
            cap = self.role_caps.get(role)
            if cap is not None and self._counted(role) >= cap:
                raise self._refuse("role-sessions", f"the {role} session cap ({cap}) is reached")
            sid = self._next_id
            self._next_id += 1
            timeout = min(self.per_session_s, remaining)
            self._sessions[sid] = {"id": sid, "role": role, "timeout_s": timeout, "reserved_at_s": now - self._start,
                                   "released_at_s": None, "duration_s": None, "outcome": None, "counted": True}
            return Reservation(sid, role, timeout, id(self))

    def release(self, reservation: Reservation, *, started: bool = True, outcome: str = "ok") -> None:
        """Record how a session ended. `started=False` (nothing was created) gives the slot back."""
        if not isinstance(reservation, Reservation) or reservation._owner != id(self):
            raise ValueError("not a reservation issued by this budget")
        if not isinstance(outcome, str) or not _OUTCOME.fullmatch(outcome):
            raise ValueError("outcome must be a short word (letters, digits, . _ : -)")
        with self._lock:
            rec = self._sessions.get(reservation.id)
            if rec is None:
                raise ValueError("not a reservation issued by this budget")
            if rec["released_at_s"] is not None:
                raise ValueError(f"reservation {reservation.id} was already released")
            now = float(self._clock()) - self._start
            rec["released_at_s"], rec["duration_s"] = now, max(0.0, now - rec["reserved_at_s"])
            rec["outcome"] = outcome
            if not started:
                rec["counted"] = False

    # ── audit ────────────────────────────────────────────────────────────────────────

    def remaining_s(self) -> float:
        with self._lock:
            return max(0.0, self._deadline - float(self._clock()))

    def snapshot(self) -> dict[str, Any]:
        """A copy of the whole state: numbers and outcome words only."""
        with self._lock:
            now = float(self._clock())
            remaining = max(0.0, self._deadline - now)
            counts = {r: self._counted(r) for r in ROLES}
            sessions = [{k: v for k, v in s.items() if k != "counted"} for s in self._sessions.values()]
            return {
                "total_wall_s": self.total_wall_s, "per_session_s": self.per_session_s,
                "max_sessions": self.max_sessions, "role_caps": dict(self.role_caps),
                "min_session_s": self.min_session_s,
                "elapsed_s": now - self._start, "remaining_s": remaining, "exhausted": remaining < max(self.min_session_s, 1e-9) or sum(counts.values()) >= self.max_sessions,
                "counts": {**counts, "total": sum(counts.values())},
                "active": sum(1 for s in self._sessions.values() if s["released_at_s"] is None),
                "finished": sum(1 for s in self._sessions.values() if s["released_at_s"] is not None and s["counted"]),
                "refusals": dict(self._refusals),
                "sessions": sessions,
            }
