"""The per-run leak checks, proved against the REAL rootless daemon (never skipped).

A leak check is only worth having if it still fails for a resource of THIS run that outlives its test, and does not
fail for a resource of another run. Each test here makes such a resource on purpose, labelled or named the way the code
under test labels and names its own, checks the verdict, and removes it again, whatever the verdict was.
"""
from __future__ import annotations

import os
import secrets

import pytest

import h2_isolation as iso
from cognitive_harness2 import run_scope
from cognitive_harness2 import session_runner as sr

pytestmark = pytest.mark.requires_rootless_docker

FOREIGN = "ffffffff"


def _mine() -> list[str]:
    return ["--label", f"{run_scope.LABEL}={iso.run_id()}"]


def _foreign() -> list[str]:
    return ["--label", f"{run_scope.LABEL}={FOREIGN}"]


def _name(kind: str) -> str:
    return f"h2s-iso-{secrets.token_hex(4)}-{kind}"


def test_a_container_of_this_run_that_outlives_its_test_fails_the_check(verified, proposer_image):
    before = iso.snapshot(verified)
    name = _name("leak")
    try:
        p = iso.docker(verified, "create", "--name", name, "--label", f"{sr.LABEL}=leak", *_mine(), "--pull", "never",
                       proposer_image.id, "true")
        assert p.returncode == 0, p.stderr
        with pytest.raises(AssertionError, match=r"left resources behind.*containers/networks \['"):
            iso.assert_nothing_left(verified, before)
    finally:
        iso.docker(verified, "rm", "-f", name)
    iso.assert_nothing_left(verified, before)                                  # and it is clean again once removed


def test_a_network_of_this_run_that_outlives_its_test_fails_the_check(verified):
    before = iso.snapshot(verified)
    name = _name("net")
    try:
        p = iso.docker(verified, "network", "create", "--internal", "--label", f"{sr.LABEL}=leak", *_mine(), name)
        assert p.returncode == 0, p.stderr
        with pytest.raises(AssertionError, match="left resources behind"):
            iso.assert_nothing_left(verified, before)
    finally:
        iso.docker(verified, "network", "rm", name)
    iso.assert_nothing_left(verified, before)


@pytest.mark.parametrize("prefix", ["h2s-", "h2bridge-"])
def test_a_runtime_directory_of_this_run_that_outlives_its_test_fails_the_check(verified, prefix):
    before = iso.snapshot(verified)
    leaked = os.path.join(sr._runtime_base_dir(), f"{run_scope.dir_prefix(prefix)}leak{secrets.token_hex(3)}")
    os.mkdir(leaked)
    try:
        with pytest.raises(AssertionError, match="runtime directories"):
            iso.assert_nothing_left(verified, before)
    finally:
        os.rmdir(leaked)                                                      # empty, and named by us: removed by name
    iso.assert_nothing_left(verified, before)


def test_resources_and_directories_of_another_run_do_not_fail_this_runs_check(verified, proposer_image):
    """The whole point: a concurrent run's containers, networks and directories are not this run's leak."""
    before = iso.snapshot(verified)
    c, n = _name("fc"), _name("fn")
    d = os.path.join(sr._runtime_base_dir(), f"h2s-{FOREIGN}-other{secrets.token_hex(3)}")
    os.mkdir(d)
    try:
        assert iso.docker(verified, "create", "--name", c, "--label", f"{sr.LABEL}=other", *_foreign(), "--pull", "never",
                          proposer_image.id, "true").returncode == 0
        assert iso.docker(verified, "network", "create", "--internal", "--label", f"{sr.LABEL}=other", *_foreign(), n).returncode == 0
        iso.assert_nothing_left(verified, before)                              # must not raise
        assert any(FOREIGN in x for x in iso.foreign_harness2_resources(verified))   # and it is visible to the global check
    finally:
        iso.docker(verified, "rm", "-f", c)
        iso.docker(verified, "network", "rm", n)
        os.rmdir(d)
    assert not [x for x in iso.foreign_harness2_resources(verified) if FOREIGN in x]


def test_a_labelled_resource_with_no_run_id_is_not_this_runs_and_is_reported_as_foreign(verified, proposer_image):
    """Something from an unscoped producer (a real sweep, an old test process) is foreign to a scoped run."""
    c = _name("unscoped")
    try:
        assert iso.docker(verified, "create", "--name", c, "--label", f"{sr.LABEL}=real", "--pull", "never", proposer_image.id,
                          "true").returncode == 0
        iso.assert_nothing_left(verified, iso.snapshot(verified))
        assert any("run none" in x for x in iso.foreign_harness2_resources(verified))
        assert c not in [x for x in iso.run_containers(verified)]
    finally:
        iso.docker(verified, "rm", "-f", c)


def test_the_runtime_directories_of_a_run_are_found_by_the_run_id_in_their_names(verified):
    d = os.path.join(sr._runtime_base_dir(), f"{run_scope.dir_prefix('h2s-')}probe")
    os.mkdir(d)
    try:
        assert os.path.basename(d) in iso.run_runtime_dirs()
    finally:
        os.rmdir(d)
    assert os.path.basename(d) not in iso.run_runtime_dirs()
