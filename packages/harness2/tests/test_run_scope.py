"""The per-process run id (`run_scope`): present on everything a scoped process creates, absent when unset.

No Docker here. The real-daemon proof that the leak checks use it is in test_isolation.py.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from cognitive_harness2 import run_scope, sandbox, stage1
from cognitive_harness2 import session_runner as sr
from cognitive_harness2.host_bridge import HostBridge
from cognitive_harness2.sandbox import DockerRunner, SandboxSpec

SOCK = "unix:///run/user/1000/docker.sock"
RID = "0123abcd"


def test_the_conftest_gives_every_test_process_a_run_id(run_id):
    assert run_scope.current() == run_id and len(run_id) == 8
    assert os.environ[run_scope.ENV] == run_id


def test_unset_means_no_label_no_filter_and_unchanged_directory_names(monkeypatch):
    monkeypatch.delenv(run_scope.ENV, raising=False)
    assert run_scope.current() is None and run_scope.labels() == [] and run_scope.label_filter() is None
    assert run_scope.dir_prefix("h2s-") == "h2s-" and run_scope.dir_prefix("h2bridge-") == "h2bridge-"
    monkeypatch.setenv(run_scope.ENV, "")
    assert run_scope.current() is None


def test_set_means_a_label_a_filter_and_the_id_in_directory_names(monkeypatch):
    monkeypatch.setenv(run_scope.ENV, RID)
    assert run_scope.labels() == ["--label", f"cognitive-harness2.run={RID}"]
    assert run_scope.label_filter() == f"label=cognitive-harness2.run={RID}"
    assert run_scope.dir_prefix("h2s-") == f"h2s-{RID}-" and run_scope.dir_prefix("h2bridge-") == f"h2bridge-{RID}-"


@pytest.mark.parametrize("bad", ["short", "UPPERCASE1", "has space 1", "0123abcd,x=y", "g" * 8, "a" * 33, "../../etc/x"])
def test_a_malformed_id_is_an_error_not_a_silent_unscoped_run(monkeypatch, bad):
    monkeypatch.setenv(run_scope.ENV, bad)
    with pytest.raises(ValueError, match=run_scope.ENV):
        run_scope.current()


def _sandbox_argv() -> list[str]:
    return DockerRunner("/usr/bin/docker")._argv(SandboxSpec(command=["true"]), Path("/tmp/stage"), "h2-x", SOCK)


def test_a_sandbox_container_carries_the_run_label_only_when_scoped(monkeypatch):
    monkeypatch.setenv(run_scope.ENV, RID)
    argv = _sandbox_argv()
    assert argv[argv.index(f"cognitive-harness2.run={RID}") - 1] == "--label" and "cognitive-harness2.sandbox=1" in argv
    monkeypatch.delenv(run_scope.ENV)
    assert not [a for a in _sandbox_argv() if a.startswith("cognitive-harness2.run")]


def test_a_scoped_bridge_directory_name_carries_the_id_and_an_unscoped_one_does_not(monkeypatch):
    with tempfile.TemporaryDirectory(prefix="h2t-") as short:                  # a unix socket path is limited to ~100 bytes
        base = Path(short)
        monkeypatch.setenv(run_scope.ENV, RID)
        with HostBridge(base_dir=base) as b:
            assert b.directory.name.startswith(f"h2bridge-{RID}-")
        monkeypatch.delenv(run_scope.ENV)
        with HostBridge(base_dir=base) as b:
            assert b.directory.name.startswith("h2bridge-") and RID not in b.directory.name
        assert list(base.iterdir()) == []


def test_stage1_runtime_entries_are_this_runs_only_when_scoped(monkeypatch, tmp_path):
    monkeypatch.setattr(sr, "_runtime_base_dir", lambda: str(tmp_path))
    for n in (f"h2s-{RID}-aaaa", f"h2bridge-{RID}-bbbb", "h2s-ffffffff-cccc", "h2bridge-dddd", "h2s-eeee", "unrelated"):
        (tmp_path / n).mkdir()
    monkeypatch.setenv(run_scope.ENV, RID)
    assert stage1.runtime_entries() == {f"h2s-{RID}-aaaa", f"h2bridge-{RID}-bbbb"}
    monkeypatch.delenv(run_scope.ENV)                                     # a real sweep: every session directory, as before
    assert stage1.runtime_entries() == {f"h2s-{RID}-aaaa", f"h2bridge-{RID}-bbbb", "h2s-ffffffff-cccc", "h2bridge-dddd", "h2s-eeee"}
