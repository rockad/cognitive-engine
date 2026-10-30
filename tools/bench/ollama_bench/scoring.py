"""The pre-registered scoring rules (see tools/bench/PREREGISTRATION.md). Committed before any result.

Pure functions only: no I/O, so the unit tests need no Ollama.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

WEIGHTS: dict[str, float] = {"agent": 0.50, "tools": 0.25, "speed": 0.15, "robustness": 0.10}
TIE_POINTS = 3.0
GATE_NAMES = ("load_failure", "crash", "gpu_split")
THINK_OFF_AGENT_MARGIN = 0.0     # A(think:false) >= A(default) - margin
THINK_OFF_TOOLS_MARGIN = 0.05    # T(think:false) >= T(default) - margin

# Run-wide constants (also in the pre-registration document).
TOTAL_BUDGET_S = 3600
REPORT_RESERVE_S = 300
PHASE_ORDER = ("perf", "tools_t0", "tools_t06", "agent_t0", "tools_nothink", "agent_nothink")
PHASE_CUM_FRACTION = {"perf": 0.10, "tools_t0": 0.35, "tools_t06": 0.55, "agent_t0": 0.80,
                      "tools_nothink": 0.88, "agent_nothink": 1.00}
NUM_CTX = 16384
NUM_PREDICT = {"tools": 2048, "agent": 4096, "perf": 160}
REQUEST_TIMEOUT_S = 240
TASK_TIMEOUT_S = 180
MAX_STEPS = 12
ERROR_STREAK_LIMIT = 3
LOAD_AVG_MAX = 4.0
AVAIL_RAM_MIN_GB = 3.0
NOISE_WAIT_MAX_S = 300
NOISE_POLL_S = 15


@dataclass
class ModelMetrics:
    name: str
    load_failure: bool = False
    crash: bool = False
    gpu_fraction: float | None = None        # size_vram / size at 16k; None when never measured
    agent_success: float = 0.0               # phase C
    tools_t0: float = 0.0                    # phase B
    tools_t06: float = 0.0                   # phase B'
    agent_median_s: float | None = None      # phase C median wall time per task
    agent_success_nothink: float | None = None
    tools_nothink: float | None = None
    agent_median_nothink_s: float | None = None
    incomplete: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


def gpu_fraction(ps_entry: dict[str, Any] | None) -> float | None:
    """Fraction of the loaded model that sits in VRAM, from one `/api/ps` entry."""
    if not ps_entry:
        return None
    size = ps_entry.get("size") or 0
    vram = ps_entry.get("size_vram") or 0
    if size <= 0:
        return None
    return min(1.0, vram / size)


def gate_failures(m: ModelMetrics) -> list[str]:
    out: list[str] = []
    if m.load_failure:
        out.append("load_failure")
    if m.crash:
        out.append("crash")
    if m.gpu_fraction is None or m.gpu_fraction < 1.0:
        out.append("gpu_split")
    return out


def median_or_none(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def speed_component(own_median: float | None, fastest_median: float | None, agent_success: float) -> float:
    if own_median is None or fastest_median is None or own_median <= 0 or agent_success <= 0:
        return 0.0
    return min(1.0, fastest_median / own_median)


def robustness_component(rate_t06: float, rate_t0: float) -> float:
    if rate_t0 <= 0:
        return 0.0
    return min(1.0, rate_t06 / rate_t0)


def score_all(models: list[ModelMetrics]) -> dict[str, dict[str, Any]]:
    """Per-model components, score and gates. `fastest_median` is taken over non-load-failures."""
    medians = [m.agent_median_s for m in models if not m.load_failure and m.agent_median_s]
    fastest = min(medians) if medians else None
    out: dict[str, dict[str, Any]] = {}
    for m in models:
        s = speed_component(m.agent_median_s, fastest, m.agent_success)
        r = robustness_component(m.tools_t06, m.tools_t0)
        comps = {"agent": m.agent_success, "tools": m.tools_t0, "speed": s, "robustness": r}
        score = 100.0 * sum(WEIGHTS[k] * v for k, v in comps.items())
        gates = gate_failures(m)
        out[m.name] = {"components": comps, "score": score, "gate_failures": gates, "eligible": not gates,
                       "agent_median_s": m.agent_median_s, "incomplete": m.incomplete}
    return out


def _pick(names: list[str], scored: dict[str, dict[str, Any]]) -> tuple[str | None, bool, list[str]]:
    """Best of `names` by score with the tie rule. Returns (winner, tied, tied_names)."""
    if not names:
        return None, False, []
    top = max(scored[n]["score"] for n in names)
    band = [n for n in names if scored[n]["score"] >= top - TIE_POINTS]

    def key(n: str) -> tuple[float, float]:
        med = scored[n]["agent_median_s"]
        return (med if med is not None else float("inf"), -scored[n]["score"])

    winner = min(band, key=key)
    return winner, len(band) > 1, sorted(band)


def recommend(scored: dict[str, dict[str, Any]]) -> dict[str, Any]:
    eligible = [n for n, v in scored.items() if v["eligible"]]
    if not eligible:
        best, _, _ = _pick(list(scored), scored)
        return {"recommended": None, "runner_up": None, "tied": False, "tied_names": [],
                "note": "no model passes the hard gates; none is called optimal",
                "best_by_score_among_gate_failures": best}
    winner, tied, band = _pick(eligible, scored)
    rest = [n for n in scored if n != winner]
    runner, _, _ = _pick(rest, scored)
    return {"recommended": winner, "runner_up": runner, "tied": tied, "tied_names": band, "note": ""}


def thinking_verdict(m: ModelMetrics) -> dict[str, Any]:
    """Should the recommendation include disabling thinking for this model?"""
    if m.agent_success_nothink is None or m.tools_nothink is None:
        return {"disable_thinking": None, "reason": "think:false variant not completed"}
    agent_ok = m.agent_success_nothink >= m.agent_success - THINK_OFF_AGENT_MARGIN
    tools_ok = m.tools_nothink >= m.tools_t0 - THINK_OFF_TOOLS_MARGIN
    faster = None
    if m.agent_median_s and m.agent_median_nothink_s:
        faster = m.agent_median_nothink_s < m.agent_median_s
    return {"disable_thinking": bool(agent_ok and tools_ok), "agent_ok": agent_ok, "tools_ok": tools_ok,
            "faster": faster,
            "reason": ("think:false matches default quality" if agent_ok and tools_ok
                       else "think:false loses quality; keep thinking on")}


def phase_deadline(model_start: float, model_budget_s: float, phase: str) -> float:
    return model_start + model_budget_s * PHASE_CUM_FRACTION[phase]
