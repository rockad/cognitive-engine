"""Seam 4: a sweep PLAN, or a refusal. Nothing here starts a sweep (slice 3a).

`plan_sweep` is the last gate before a sweep could be started by a later, user-gated step. It
returns an immutable `SweepPlan` only when ALL of these hold, and otherwise raises `SweepRefused`
carrying every reason at once:

  * the three hard caps (sessions, wall-clock seconds, cost or tokens) are present, finite,
    positive and under a sanity ceiling: missing, zero, negative, NaN, infinite and "huge" are refused;
  * the plan fits its own session cap, by upstream's budget identity (P = 3k proposer sessions and
    R = 4k rollouts per task, `harness2.core.ledger`), plus whatever a ledger has already spent;
  * every candidate is on the mutable surface (`upstream_surface.check_surface`), with unique labels;
  * readiness has no known blocker (`False`); unknown (`None`) is allowed and recorded, as in `advice`;
    a quota verdict, when given, is consulted;
  * the Proposer decision from `upstream_egress` is an allowed `Decision` made for THIS channel and
    THIS classification, and the channel (named, and declared in the profile) is allowed under the
    zdr mode and the sweep's classification, re-checked here so a stale decision cannot be reused;
  * the sweep's classification is explicit (missing means restricted);
  * the caps are the MINIMUM of what was requested and what the channel's profile allows, and the
    plan is refused when it needs more than a channel cap.

The plan always has `requires_human_approval = True`; it cannot be constructed otherwise. The
estimated cost class is `advice.cost_class` over the Proposer's declared endpoint and model.

`max_sessions` bounds proposer (model) sessions. Rollouts are reported (`planned_rollouts`) but not
capped here; bounding execution is slice 3b's job.

The ledger formulas are read from upstream (`harness2.core.ledger`, stdlib-only, no side effects on
import). Without the `upstream` extra the plan is refused rather than guessed.
"""
from __future__ import annotations

import importlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType, SimpleNamespace
from typing import Any

from . import advice, channels, invariants
from .channels import ChannelProfile
from .engine import MAX_CANDIDATES
from .quota import QuotaVerdict
from .upstream_egress import Decision
from .upstream_surface import check_surface

COST_UNITS = ("usd", "tokens")

# Sanity ceilings: "effectively unbounded" is refused too. They are defined with the profile schema
# (channels.py) so a profile cap and a requested cap are held to the same bound.
MAX_SESSIONS_CEILING = channels.MAX_SESSIONS_CEILING
MAX_WALL_SECONDS_CEILING = channels.MAX_WALL_SECONDS_CEILING
MAX_USD_CEILING = channels.MAX_USD_CEILING
MAX_TOKENS_CEILING = channels.MAX_TOKENS_CEILING


class SweepRefused(RuntimeError):
    """The plan cannot be made. `reasons` lists every cause."""

    def __init__(self, reasons: Sequence[str]):
        self.reasons = tuple(reasons)
        super().__init__("; ".join(self.reasons))


@dataclass(frozen=True)
class CostCap:
    unit: str      # "usd" | "tokens"
    amount: float


@dataclass(frozen=True)
class SweepCandidate:
    label: str
    path: str      # the surface path the proposer would edit


@dataclass(frozen=True)
class SweepPlan:
    candidates: tuple[SweepCandidate, ...] = ()
    k: int = 0
    n_tasks: int = 0
    planned_sessions: int = 0
    planned_rollouts: int = 0
    max_sessions: int = 0
    max_wall_seconds: float = 0
    max_cost_or_tokens: CostCap = CostCap("usd", 0)
    cost_class: str = "none"
    readiness: Mapping[str, Any] = field(default_factory=dict)
    ready: bool | None = None
    warnings: tuple[str, ...] = ()
    proposer: Mapping[str, Any] = field(default_factory=dict)
    channel: str = ""
    classification: str = "restricted"
    channel_caps: Mapping[str, Any] = field(default_factory=dict)
    requires_human_approval: bool = field(default=True, init=False)

    def to_dict(self) -> dict:
        return {
            "candidates": [{"label": c.label, "path": c.path} for c in self.candidates],
            "k": self.k, "n_tasks": self.n_tasks,
            "planned_sessions": self.planned_sessions, "planned_rollouts": self.planned_rollouts,
            "max_sessions": self.max_sessions, "max_wall_seconds": self.max_wall_seconds,
            "max_cost_or_tokens": {"unit": self.max_cost_or_tokens.unit, "amount": self.max_cost_or_tokens.amount},
            "cost_class": self.cost_class, "readiness": {k: dict(v) for k, v in self.readiness.items()},
            "ready": self.ready, "warnings": list(self.warnings), "proposer": dict(self.proposer),
            "channel": self.channel, "classification": self.classification,
            "channel_caps": dict(self.channel_caps),
            "requires_human_approval": self.requires_human_approval,
        }


# ── validation helpers (each appends reasons, never raises) ───────────────────────

def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _positive_finite(v: Any) -> bool:
    if not _is_number(v):
        return False
    try:
        return math.isfinite(v) and v > 0
    except OverflowError:
        return False


def _cap_sessions(v: Any, out: list[str]) -> int | None:
    if v is None:
        out.append("max_sessions is missing: a hard session cap is required")
    elif not isinstance(v, int) or isinstance(v, bool) or v <= 0:
        out.append("max_sessions must be a positive integer (zero, negative, fractional and non-numeric are refused)")
    elif v > MAX_SESSIONS_CEILING:
        out.append(f"max_sessions is above the sanity ceiling {MAX_SESSIONS_CEILING} (effectively unbounded)")
    else:
        return v
    return None


def _cap_wall(v: Any, out: list[str]) -> float | None:
    if v is None:
        out.append("max_wall_seconds is missing: a hard wall-clock cap is required")
    elif not _positive_finite(v):
        out.append("max_wall_seconds must be a positive finite number (zero, negative, NaN, infinite and non-numeric are refused)")
    elif v > MAX_WALL_SECONDS_CEILING:
        out.append(f"max_wall_seconds is above the sanity ceiling {MAX_WALL_SECONDS_CEILING} (effectively unbounded)")
    else:
        return v
    return None


def _cap_cost(v: Any, out: list[str]) -> CostCap | None:
    if v is None:
        out.append("max_cost_or_tokens is missing: a hard cost or token cap is required")
    elif not isinstance(v, CostCap) or v.unit not in COST_UNITS:
        out.append(f"max_cost_or_tokens must be a CostCap naming its unit ({', '.join(COST_UNITS)})")
    elif not _positive_finite(v.amount):
        out.append("max_cost_or_tokens must be a positive finite amount (zero, negative, NaN, infinite and non-numeric are refused)")
    elif v.amount > (MAX_USD_CEILING if v.unit == "usd" else MAX_TOKENS_CEILING):
        out.append(f"max_cost_or_tokens is above the sanity ceiling for {v.unit} (effectively unbounded)")
    else:
        return v
    return None


def _count(v: Any, name: str, out: list[str]) -> int | None:
    if not isinstance(v, int) or isinstance(v, bool) or v < 1:
        out.append(f"{name} must be a positive integer")
        return None
    return v


def _check_candidates(surface: Any, cands: Any, out: list[str]) -> tuple[SweepCandidate, ...]:
    if not isinstance(cands, (list, tuple)) or not cands:
        out.append("candidates: at least one candidate is required")
        return ()
    if len(cands) > MAX_CANDIDATES:
        out.append(f"candidates: {len(cands)} is more than the engine's limit of {MAX_CANDIDATES}")
    labels: set[str] = set()
    paths: set[str] = set()
    good: list[SweepCandidate] = []
    for c in cands:
        if not isinstance(c, SweepCandidate) or not isinstance(c.label, str) or not c.label.strip() \
                or not isinstance(c.path, str):
            out.append("candidate must be a SweepCandidate with a non-empty string label and a string path")
            continue
        if c.label in labels or c.path in paths:
            out.append(f"candidate {c.label!r} repeats a label or a path")
            continue
        try:
            check_surface(surface, tree=[c.path])
        except invariants.InvariantViolation as e:
            out.append(f"candidate {c.label!r} is not on the mutable surface: {e}")
            continue
        labels.add(c.label)
        paths.add(c.path)
        good.append(c)
    return tuple(good)


def _check_readiness(readiness: Any, quota: Any, out: list[str]) -> dict | None:
    if not isinstance(readiness, Mapping) or not readiness:
        out.append("readiness is missing or empty: a readiness report (as advice.advise produces) is required")
        return None
    merged: dict[str, dict] = {}
    for name, v in readiness.items():
        if not isinstance(name, str) or not isinstance(v, Mapping) or "ok" not in v \
                or not (v["ok"] is True or v["ok"] is False or v["ok"] is None):
            out.append(f"readiness entry {name!r} is malformed: expected a mapping with ok True, False or None")
            return None
        merged[name] = dict(v)
    if quota is not None:
        if not isinstance(quota, QuotaVerdict):
            out.append("quota must be a QuotaVerdict (the result of the quota pre-flight)")
            return None
        merged["quota"] = {"ok": quota.ok, "status": "checked", "detail": quota.reason}
    for name, v in merged.items():
        if v["ok"] is False:
            out.append(f"readiness blocker, {name}: {v.get('detail', 'not ready')}")
    return merged


def _ledger_numbers(ledger: Any) -> tuple[int, float] | None:
    try:
        p, usd = ledger.P, ledger.usd
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(p, int) or isinstance(p, bool) or not _is_number(usd):
        return None
    return p, float(usd)


def _clip(requested: float | int | None, cap: float | int, name: str, notes: list[str]) -> float | int | None:
    """min(requested, channel cap); a request above the channel's cap is clipped and recorded."""
    if requested is None:
        return None
    if requested > cap:
        notes.append(f"{name} {requested:g} is above the channel's cap {cap:g}; the plan is capped at {cap:g}")
        return cap
    return requested


def plan_sweep(*, surface: Any, candidates: Any, k: Any, n_tasks: Any = 1, max_sessions: Any = None,
               max_wall_seconds: Any = None, max_cost_or_tokens: Any = None, proposer: Any,
               readiness: Any, profile: Any = None, channel: Any = None, classification: Any = None,
               zdr: Any = "strict", quota: Any = None, ledger: Any = None) -> SweepPlan:
    """The plan, or `SweepRefused`. Starts nothing: no session, no model call, no container.

    `profile` and `channel` name the declared channel the sweep would use; `classification` is the sweep's
    (the strictest of its sources; missing means restricted); `zdr` is the zdr mode. The proposer decision must
    have been made for the same channel, classification and zdr mode.
    """
    out: list[str] = []
    notes: list[str] = []

    sessions_cap = _cap_sessions(max_sessions, out)
    wall_cap = _cap_wall(max_wall_seconds, out)
    cost_cap = _cap_cost(max_cost_or_tokens, out)
    kk = _count(k, "k", out)
    tasks = _count(n_tasks, "n_tasks", out)

    # the channel: declared, allowed for this sweep, and the source of the hard caps
    cls = channels.effective_classification(classification)
    if classification is None:
        notes.append("no classification was given: the sweep is treated as restricted")
    ch = None
    if not isinstance(profile, ChannelProfile):
        out.append("profile must be the ChannelProfile loaded by channels.load_profile")
    elif not profile.declared or not isinstance(channel, str) or channel not in profile.channels:
        out.append(f"channel {ascii(str(channel)[:40])} is not declared in {profile.source}; {channels.HOWTO}")
    else:
        ch = profile.channels[channel]
        verdict = channels.decide(ch, zdr=zdr, classification=classification)
        if not verdict.allowed:
            out.extend(f"channel is not allowed for this sweep: {r}" for r in verdict.reasons)
    if ch is not None:
        sessions_cap = _clip(sessions_cap, ch.caps.sessions, "max_sessions", notes)  # type: ignore[assignment]
        wall_cap = _clip(wall_cap, ch.caps.wall_s, "max_wall_seconds", notes)  # type: ignore[assignment]
        if cost_cap is not None and cost_cap.unit == "usd":
            cost_cap = CostCap("usd", _clip(cost_cap.amount, ch.caps.cost_usd, "max_cost_or_tokens", notes))  # type: ignore[arg-type]

    try:
        check_surface(surface)
    except invariants.InvariantViolation as e:
        out.append(f"surface is not recognised: {e}")
        cands: tuple[SweepCandidate, ...] = ()
    else:
        cands = _check_candidates(surface, candidates, out)

    view: Mapping[str, Any] = {}
    warnings: tuple[str, ...] = ()
    cost_class = "none"
    if not isinstance(proposer, Decision):
        out.append("proposer must be the Decision returned by upstream_egress.check_proposer")
    elif not proposer.allowed:
        out.append("proposer decision is not allowed: " + " | ".join(proposer.reasons))
    elif not proposer.view.get("endpoint") or not proposer.view.get("model"):
        out.append("proposer decision carries no endpoint and model, so the cost class cannot be estimated")
    else:
        for key, want in (("channel", channel), ("classification", cls), ("zdr", zdr)):
            if proposer.view.get(key) != want:
                out.append(f"the proposer decision was made for {key} {proposer.view.get(key)!r}, "
                           f"not the sweep's {want!r}: ask again")
        view, warnings = proposer.view, proposer.warnings
        shim = SimpleNamespace(base_url=view["endpoint"], model=view["model"])
        cost_class = advice.cost_class(shim, True)

    merged = _check_readiness(readiness, quota, out)

    planned_sessions = planned_rollouts = 0
    if kk is not None and tasks is not None:
        try:
            upstream_ledger = importlib.import_module("harness2.core.ledger")
        except ImportError:
            out.append("the upstream extra is not installed, so the ledger budget cannot be computed "
                       "(run `uv sync --extra upstream`)")
        else:
            planned_sessions = upstream_ledger.expected_sessions(kk) * tasks
            planned_rollouts = upstream_ledger.expected_rollouts(kk) * tasks
            spent_p, spent_usd = 0, 0.0
            if ledger is not None:
                nums = _ledger_numbers(ledger)
                if nums is None:
                    out.append("ledger is unreadable: expected an integer .P and a numeric .usd")
                else:
                    spent_p, spent_usd = nums
            if sessions_cap is not None and spent_p + planned_sessions > sessions_cap:
                out.append(f"the plan needs {planned_sessions} proposer sessions (P = 3k per task) plus {spent_p} "
                           f"already in the ledger, over max_sessions {sessions_cap}"
                           + (" (the channel's cap)" if ch is not None and sessions_cap == ch.caps.sessions else ""))
            if cost_cap is not None and cost_cap.unit == "usd" and spent_usd >= cost_cap.amount:
                out.append(f"the ledger has already spent {spent_usd:g} usd, at or over max_cost_or_tokens "
                           f"{cost_cap.amount:g} usd")
            if ch is not None and spent_usd >= ch.caps.cost_usd:
                out.append(f"the ledger has already spent {spent_usd:g} usd, at or over channel {ch.name!r}'s "
                           f"cost_usd cap {ch.caps.cost_usd:g}")

    if out:
        raise SweepRefused(out)
    assert ch is not None
    assert sessions_cap is not None and wall_cap is not None and cost_cap is not None and merged is not None
    ready = True if all(v["ok"] is True for v in merged.values()) else None
    return SweepPlan(
        candidates=cands, k=kk, n_tasks=tasks, planned_sessions=planned_sessions,  # type: ignore[arg-type]
        planned_rollouts=planned_rollouts, max_sessions=sessions_cap, max_wall_seconds=wall_cap,
        max_cost_or_tokens=cost_cap, cost_class=cost_class, readiness=MappingProxyType(merged), ready=ready,
        warnings=tuple(warnings) + tuple(notes), proposer=MappingProxyType(dict(view)),
        channel=ch.name, classification=cls, channel_caps=MappingProxyType(ch.caps.to_dict()),
    )
