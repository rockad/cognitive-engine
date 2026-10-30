"""A privately imported real upstream for tests (a plain module, imported by name like `h2_profiles`).

Other test modules import `harness2` directly, and `import_upstream` refuses a package someone imported first.
`isolated_upstream` stashes those modules, runs the verified import, and puts everything back afterwards, so a
test can bind to the real upstream without depending on test order and without leaking its overrides
(the attempt constants) into the modules other tests use.
"""
from __future__ import annotations

import contextlib
import sys
from importlib import metadata
from pathlib import Path

import pytest

from cognitive_harness2 import contained_proposer, upstream_import


def _modules(name: str) -> list[str]:
    return [m for m in sys.modules if m == name or m.startswith(name + ".")]


def upstream_installed() -> bool:
    try:
        metadata.distribution("harness2")
        return True
    except metadata.PackageNotFoundError:
        return False


@contextlib.contextmanager
def isolated_upstream(runs_dir: Path):
    if not upstream_installed():
        pytest.skip("OPTIONAL EXTRA ABSENT: the `upstream` extra is not installed; the real-upstream tests did NOT run")
    stashed = {m: sys.modules.pop(m) for m in _modules("harness2")}
    saved_cache = dict(upstream_import._CACHE)
    upstream_import._CACHE.clear()
    try:
        up = upstream_import.import_upstream(runs_dir=runs_dir)
        yield up
    finally:
        for m in _modules("harness2"):
            del sys.modules[m]
        sys.modules.update(stashed)
        upstream_import._CACHE.clear()
        upstream_import._CACHE.update(saved_cache)
        contained_proposer._CLASSES.clear()
