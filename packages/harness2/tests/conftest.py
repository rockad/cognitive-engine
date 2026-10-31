"""Shared test setup.

1. Makes the shared helper `h2_profiles` importable under pytest's importlib mode.
2. Fixtures for the contained-session tests (real rootless daemon; these never skip).
"""
from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from cognitive_harness2 import image, run_scope, sandbox  # noqa: E402  (after the sys.path tweak above)


@pytest.fixture(scope="session")
def verified() -> sandbox.VerifiedDocker:
    """The rootless gate, run against the proposer image's pinned base (present locally)."""
    return image.gate_for_build()


@pytest.fixture(scope="session")
def proposer_image(verified) -> image.ImageRef:
    """The image for the current sources. Built once if absent (the build is cached and uses the pinned socket)."""
    try:
        return image.resolve_image(verified)
    except image.ImageError:
        return image.build(verified)


@pytest.fixture(scope="session", autouse=True)
def run_id():
    """A unique id for THIS pytest process, exported as COGNITIVE_HARNESS2_RUN_ID. The code under test then adds it as
    the `cognitive-harness2.run` label and to the names of the runtime directories it creates, so a leak check can look
    at this run's resources only (see h2_isolation) and two test processes on one daemon cannot fail each other.
    Always fresh: an id inherited from the environment would defeat the point."""
    rid = secrets.token_hex(4)
    previous = os.environ.get(run_scope.ENV)
    os.environ[run_scope.ENV] = rid
    try:
        yield rid
    finally:
        if previous is None:
            os.environ.pop(run_scope.ENV, None)
        else:
            os.environ[run_scope.ENV] = previous


@pytest.fixture(autouse=True)
def _fresh_sandbox_selection_state():
    """The sandbox selection caches its (real) sbx probe result and warns once per process: both are per-process
    state a test must neither inherit nor leave behind."""
    sandbox.clear_sbx_cache()
    sandbox._reset_warnings()
    yield
    sandbox.clear_sbx_cache()
    sandbox._reset_warnings()
