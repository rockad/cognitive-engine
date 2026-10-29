"""Shared test setup.

1. Makes the shared helper `h2_profiles` importable under pytest's importlib mode.
2. Fixtures for the contained-session tests (real rootless daemon; these never skip).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from cognitive_harness2 import image, sandbox  # noqa: E402  (after the sys.path tweak above)


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
