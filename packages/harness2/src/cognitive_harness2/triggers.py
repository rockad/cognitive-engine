"""When probing may start (RFC 004 §2.3).

Probing is failure-driven and decoupled from System 1 routing. It starts on exactly:

  * a failed verification  (`jev_judge` FAIL, or a score below 0.50)
  * retry exhaustion       (3 or more consecutive failures on the same task)
  * an explicit request    (`/probe <task>` or `engine probe`)

These are exactly the RFC 004 §2.3 triggers. There is no stagnation trigger: an earlier
extension ("3 or more attempts without progress") was dropped by decision, and the closed
`harness2_trigger` schema now rejects its old `attempts_without_progress` argument.

`routing_confidence` is accepted and deliberately IGNORED. Routing uncertainty used to
trigger probes, which fired on tier ambiguity that says nothing about task failure;
taking the argument keeps callers honest about it while making the decoupling testable.

This module only DECIDES. It starts nothing: the advisory hook and the `harness2_trigger`
pull tool both call it, and System 2 itself runs only when `harness2_propose` and
`harness2_probe` are called explicitly.

Slice 2 additions to the slice 1 function (`should_probe` keeps its old behaviour):
  * `Decision.trigger` names which rule fired (a stable machine-readable id);
  * `failure_signatures`: recent failure fingerprints, newest last. A trailing run of
    identical signatures counts as that many consecutive failures, so a caller that sends
    signatures and no count still reaches retry exhaustion.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

SCORE_THRESHOLD = 0.50
RETRY_LIMIT = 3

EXPLICIT = "explicit"
JUDGE_FAILED = "judge_failed"
JUDGE_SCORE = "judge_score"
RETRIES_EXHAUSTED = "retries_exhausted"


@dataclass(frozen=True)
class Decision:
    probe: bool
    reason: str
    trigger: str | None = None


def trailing_identical(signatures: Sequence[str]) -> int:
    """Length of the run of equal signatures at the end of the list."""
    if not signatures:
        return 0
    last, n = signatures[-1], 0
    for s in reversed(signatures):
        if s != last:
            break
        n += 1
    return n


def should_probe(
    *,
    judge_passed: bool | None = None,
    judge_score: float | None = None,
    consecutive_failures: int = 0,
    on_demand: bool = False,
    failure_signatures: Sequence[str] = (),
    routing_confidence: float | None = None,  # noqa: ARG001 - ignored on purpose, see module docstring
) -> Decision:
    if on_demand:
        return Decision(True, "explicit on-demand request", EXPLICIT)
    if judge_passed is False:
        return Decision(True, "verification failed (jev_judge FAIL)", JUDGE_FAILED)
    if judge_score is not None and judge_score < SCORE_THRESHOLD:
        return Decision(True, f"verification score {judge_score:.2f} is below {SCORE_THRESHOLD:.2f}", JUDGE_SCORE)
    repeated = trailing_identical(failure_signatures)
    failures = max(consecutive_failures, repeated)
    if failures >= RETRY_LIMIT:
        same = f", {repeated} identical in a row" if repeated >= RETRY_LIMIT else ""
        return Decision(True, f"retries exhausted ({failures} >= {RETRY_LIMIT}{same})", RETRIES_EXHAUSTED)
    return Decision(False, "no failure, retry exhaustion or explicit request")
