"""Per-run leak checks for the tests that use the REAL rootless daemon (a plain module, imported by name).

The daemon and the runtime directory are shared by every test process on the host. Every pytest process gets its
own run id (`conftest.run_id`, exported as COGNITIVE_HARNESS2_RUN_ID); the code under test adds it as the
`cognitive-harness2.run` label and to the names of the runtime directories it creates (see `cognitive_harness2.run_scope`).
These helpers look at THIS run's resources only, so another test process, or a real stage-1 sweep, working at the same
moment cannot fail a leak check, and a resource of this run that outlives its test still does.
"""
from __future__ import annotations

import os
import subprocess

from cognitive_harness2 import run_scope, sandbox
from cognitive_harness2 import session_runner as sr


def run_id() -> str:
    rid = run_scope.current()
    assert rid, f"{run_scope.ENV} is not set: the conftest run_id fixture did not run"
    return rid


def docker(verified, *args, timeout=60) -> subprocess.CompletedProcess:
    return subprocess.run([verified.path, "-H", verified.host, *args], capture_output=True, text=True, timeout=timeout,
                          env=sandbox._pinned_env(), stdin=subprocess.DEVNULL)


def _ids(verified, label_filter: str) -> list[str]:
    out = docker(verified, "ps", "-a", "-q", "--filter", label_filter).stdout.split()
    out += docker(verified, "network", "ls", "-q", "--filter", label_filter).stdout.split()
    return out


def run_resources(verified) -> set[str]:
    """Container and network ids carrying THIS run's label (sessions, proxies, reclaimers, sandbox containers)."""
    return set(_ids(verified, f"label={run_scope.LABEL}={run_id()}"))


def run_containers(verified) -> set[str]:
    return set(docker(verified, "ps", "-a", "-q", "--filter", f"label={run_scope.LABEL}={run_id()}").stdout.split())


def run_runtime_dirs() -> list[str]:
    """Session and bridge directories this run created in the runtime base (their names carry the run id)."""
    prefixes = (run_scope.dir_prefix("h2s-"), run_scope.dir_prefix("h2bridge-"))
    return sorted(n for n in os.listdir(sr._runtime_base_dir()) if n.startswith(prefixes))


def snapshot(verified) -> tuple[frozenset[str], tuple[str, ...]]:
    return frozenset(run_resources(verified)), tuple(run_runtime_dirs())


def assert_nothing_left(verified, before) -> None:
    """THIS run has no container, network or runtime directory that it did not have at `before`."""
    now = snapshot(verified)
    assert now == before, (f"this run (id {run_id()}) left resources behind: containers/networks "
                           f"{sorted(now[0] - before[0])}, runtime directories {sorted(set(now[1]) - set(before[1]))}")


def foreign_harness2_resources(verified) -> list[str]:
    """Harness2 containers and networks on the daemon that do NOT carry this run's id: someone else's run, a real
    stage-1 sweep, a test process from before this change, or a stale leftover. Used only to decide that a
    daemon-wide check cannot be trusted right now."""
    mine = run_id()
    found: list[str] = []
    for key in (sr.LABEL, "cognitive-harness2.sandbox"):
        for cmd, what in ((["ps", "-a"], "container"), (["network", "ls"], "network")):
            fmt = '{{.ID}} {{.Label "' + run_scope.LABEL + '"}}'
            p = docker(verified, *cmd, "--filter", f"label={key}", "--format", fmt)
            for line in p.stdout.splitlines():
                ident, _, rid = line.partition(" ")
                if ident and rid.strip() != mine:
                    found.append(f"{what} {ident} (run {rid.strip() or 'none'})")
    return found
