"""An optional per-process run id that tags every container, network and runtime directory this process creates.

Why: the rootless Docker daemon and the runtime directory are shared by everything on the host. A leak check that
compares the WHOLE daemon (or every `h2s-*` directory) before and after a test fails whenever a second test process,
or a real stage-1 run, is working at the same moment. A process that sets `COGNITIVE_HARNESS2_RUN_ID` makes its
resources carry that id, so a check can look at "mine" only.

Production behaviour is unchanged: with the variable unset, `labels()` is empty and `tag()` is empty, so no argv, label
or directory name differs from before. The id is for isolation, never for security: nothing trusts it.
"""
from __future__ import annotations

import os
import re

ENV = "COGNITIVE_HARNESS2_RUN_ID"
LABEL = "cognitive-harness2.run"
_VALID = re.compile(r"[0-9a-f]{8,32}")


def current() -> str | None:
    """This process's run id, or None. A set but malformed value is an error, not a silent fallback to 'unscoped'."""
    value = os.environ.get(ENV)
    if not value:
        return None
    if not _VALID.fullmatch(value):
        raise ValueError(f"{ENV} must be 8 to 32 lowercase hex characters, got {value!r}")
    return value


def labels() -> list[str]:
    """`--label` arguments for `docker create/run/network create`; empty when unscoped."""
    rid = current()
    return ["--label", f"{LABEL}={rid}"] if rid else []


def label_filter() -> str | None:
    """The `label=...` value for `docker ps/network ls --filter`, or None when unscoped."""
    rid = current()
    return f"label={LABEL}={rid}" if rid else None


def dir_prefix(base: str) -> str:
    """`h2s-` becomes `h2s-<id>-` when scoped. (`mkdtemp` appends its own random part.)"""
    rid = current()
    return f"{base}{rid}-" if rid else base
