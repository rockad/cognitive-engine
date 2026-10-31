"""The task sets stage 1 can run, by the word `--task-set` takes.

    easy   `sum-signed`        five clean signed amounts; the base harness already passes it (stage 1, run #4)
    hard   `sum-signed-hard`   twelve amounts in mixed ledger notations; the base harness does not say how to read them

The default is `easy`, so a command written before the hard set existed means exactly what it did.
"""
from __future__ import annotations

from collections.abc import Mapping

from . import sum_signed, sum_signed_hard
from .sum_signed import TaskSet

DEFAULT = "easy"
TASK_SETS: Mapping[str, TaskSet] = {"easy": sum_signed.EASY, "hard": sum_signed_hard.TASKSET}
NAMES = tuple(TASK_SETS)


def get(name: str) -> TaskSet:
    """The task set called `name`; ValueError (naming the choices) for anything else."""
    try:
        return TASK_SETS[name]
    except (KeyError, TypeError):
        raise ValueError(f"unknown task set {str(name)[:40]!r}; choose one of: {', '.join(NAMES)}") from None
