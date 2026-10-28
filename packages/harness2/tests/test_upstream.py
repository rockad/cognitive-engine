"""Import smoke test for the pinned upstream google-research/harness2 (RFC 004 §2.1, §2.8).

This is NOT a test of upstream behaviour. It proves that the dependency resolves, that the
installed build is the commit pinned in pyproject.toml (so a lock drift fails loudly), and
that the integration surface this package plans to bind to still exists.
"""
from __future__ import annotations

import json
import tomllib
from importlib import metadata
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _pinned_rev() -> str:
    cfg = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    src = cfg["tool"]["uv"]["sources"]["harness2"]
    assert src["git"] == "https://github.com/google-research/harness2.git"
    return src["rev"]


def test_pin_is_a_full_commit_sha():
    rev = _pinned_rev()
    assert len(rev) == 40 and set(rev) <= set("0123456789abcdef")


def test_installed_upstream_is_the_pinned_commit():
    direct_url = json.loads(metadata.distribution("harness2").read_text("direct_url.json"))
    assert direct_url["vcs_info"]["vcs"] == "git"
    assert direct_url["vcs_info"]["commit_id"] == _pinned_rev()


def test_upstream_substrate_surface_imports():
    from harness2.core import ledger, proposer, recursion, tree
    from harness2.substrate import Install, RunRequest, Substrate

    for name in ("surface", "materialise", "execute", "read_trajectory", "preflight"):
        assert callable(getattr(Substrate, name)), name
    assert Install and RunRequest
    assert hasattr(proposer, "Proposer") and hasattr(ledger, "Ledger")
    assert callable(recursion.run_parallel) and callable(recursion.run_sequential)
    assert hasattr(tree, "SurfaceSpec")
