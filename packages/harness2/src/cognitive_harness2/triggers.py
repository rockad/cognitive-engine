"""When probing may start (RFC 004 §2.3).

Probing is failure-driven and decoupled from System 1 routing. It starts on exactly:

  * a failed verification  (`jev_judge` FAIL, or a score below 0.50)
  * retry exhaustion       (3 or more consecutive failures on the same task)
  * an explicit request    (`/probe <task>` or `engine probe`)

`routing_confidence` is accepted and deliberately IGNORED. Routing uncertainty used to
trigger probes, which fired on tier ambiguity that says nothing about task failure;
taking the argument keeps callers honest about it while making the decoupling testable.
"""
from __future__ import annotations

from dataclasses import dataclass

SCORE_THRESHOLD = 0.50
RETRY_LIMIT = 3


@dataclass(frozen=True)
class Decision:
    probe: bool
    reason: str


def should_probe(
    *,
    judge_passed: bool | None = None,
    judge_score: float | None = None,
    consecutive_failures: int = 0,
    on_demand: bool = False,
    routing_confidence: float | None = None,  # noqa: ARG001 - ignored on purpose, see module docstring
) -> Decision:
    if on_demand:
        return Decision(True, "explicit on-demand request")
    if judge_passed is False:
        return Decision(True, "verification failed (jev_judge FAIL)")
    if judge_score is not None and judge_score < SCORE_THRESHOLD:
        return Decision(True, f"verification score {judge_score:.2f} is below {SCORE_THRESHOLD:.2f}")
    if consecutive_failures >= RETRY_LIMIT:
        return Decision(True, f"retries exhausted ({consecutive_failures} >= {RETRY_LIMIT})")
    return Decision(False, "no failure, retry exhaustion or explicit request")
