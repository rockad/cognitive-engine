"""Trigger evaluation edge cases (pure): thresholds, identical failures, precedence."""
import inspect

import pytest

from cognitive_harness2 import triggers
from cognitive_harness2.triggers import should_probe


@pytest.mark.parametrize("n,fires", [(0, False), (1, False), (2, False), (3, True), (4, True), (1000, True)])
def test_consecutive_failures_below_at_above_the_limit(n, fires):
    d = should_probe(consecutive_failures=n)
    assert d.probe is fires
    assert d.trigger == (triggers.RETRIES_EXHAUSTED if fires else None)


@pytest.mark.parametrize("score,fires", [(0.0, True), (0.49, True), (0.4999, True), (0.50, False), (0.51, False), (1.0, False)])
def test_judge_score_boundary(score, fires):
    assert should_probe(judge_score=score).probe is fires


def test_repeated_identical_failure_counts_without_an_explicit_count():
    sigs = ["ImportError: x"] * 3
    d = should_probe(failure_signatures=sigs)
    assert d.probe and d.trigger == triggers.RETRIES_EXHAUSTED and "3 identical" in d.reason


def test_two_identical_failures_are_not_exhaustion():
    assert not should_probe(failure_signatures=["a", "a"]).probe


def test_only_the_trailing_identical_run_counts():
    assert not should_probe(failure_signatures=["a", "a", "b", "a", "a"]).probe
    assert should_probe(failure_signatures=["b", "a", "a", "a"]).probe


def test_distinct_failures_alone_do_not_count_as_identical():
    assert not should_probe(failure_signatures=["a", "b", "c", "d"]).probe


def test_explicit_count_and_signatures_take_the_larger():
    assert should_probe(consecutive_failures=3, failure_signatures=["a"]).probe
    assert should_probe(consecutive_failures=0, failure_signatures=["a"] * 5).probe


def test_there_is_no_stagnation_trigger():
    # RFC 004 §2.3 lists exactly: judge FAIL or score < 0.50, 3+ consecutive failures, explicit request.
    assert not hasattr(triggers, "STAGNATION") and not hasattr(triggers, "STAGNATION_LIMIT")
    assert "attempts_without_progress" not in inspect.signature(should_probe).parameters
    with pytest.raises(TypeError):
        should_probe(attempts_without_progress=9)  # type: ignore[call-arg]
    assert {triggers.EXPLICIT, triggers.JUDGE_FAILED, triggers.JUDGE_SCORE, triggers.RETRIES_EXHAUSTED} == {
        v for k, v in vars(triggers).items() if k.isupper() and isinstance(v, str)}


@pytest.mark.parametrize("kwargs,expected", [
    ({"on_demand": True, "judge_passed": False, "consecutive_failures": 9}, triggers.EXPLICIT),
    ({"judge_passed": False, "judge_score": 0.1, "consecutive_failures": 9}, triggers.JUDGE_FAILED),
    ({"judge_score": 0.1, "consecutive_failures": 9}, triggers.JUDGE_SCORE),
    ({"consecutive_failures": 3}, triggers.RETRIES_EXHAUSTED),
])
def test_precedence_names_the_first_rule_that_fires(kwargs, expected):
    assert should_probe(**kwargs).trigger == expected


def test_a_passing_judge_does_not_suppress_other_triggers():
    # A pass on one verification says nothing about three failed retries.
    assert should_probe(judge_passed=True, judge_score=0.9, consecutive_failures=3).probe


def test_routing_confidence_still_ignored():
    assert not should_probe(routing_confidence=0.0).probe
    assert should_probe(consecutive_failures=3, routing_confidence=1.0).probe


def test_slice_one_two_field_decision_still_constructs():
    d = triggers.Decision(True, "why")
    assert d.trigger is None
