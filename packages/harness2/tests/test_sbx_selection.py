"""`sbx` on PATH must never, by itself, change which sandbox runs candidate code.

`SbxRunner._argv` is an unverified guess at the `sbx` command line, so `default_runner` picks `SbxRunner` only when ALL of:
the user opted in (`COGNITIVE_HARNESS2_SANDBOX=sbx`), the installed version is in `VERIFIED_SBX_VERSIONS` (empty today),
and a read-only usability probe passes (daemon reachable, /dev/kvm accessible, global policy initialised). Anything else
is rootless Docker exactly as before, and with no usable Docker either, the existing fail-closed refusal.

No test here starts a real `sbx` process except the one marked `requires_sbx`, which runs `sbx version` and is skipped
when `sbx` is absent.
"""
from __future__ import annotations

import itertools
import shutil
import subprocess

import pytest

from cognitive_harness2 import sandbox
from cognitive_harness2.sandbox import (
    ENV_SANDBOX,
    VERIFIED_SBX_VERSIONS,
    DockerRunner,
    SandboxUnavailable,
    SbxProbes,
    SbxRunner,
    VerifiedDocker,
)

SOCK = "unix:///run/user/1000/docker.sock"
VERSION = "0.50.1"


def probes(*, daemon=True, kvm=True, policy=True, version=VERSION, calls=None) -> SbxProbes:
    def log(name, value):
        def f(*a):
            if calls is not None:
                calls.append(name)
            return value
        return f
    return SbxProbes(
        kvm_problem=log("kvm", None if kvm else "/dev/kvm: permission denied"),
        daemon_reachable=log("daemon", daemon),
        version=log("version", version),
        policy_problem=log("policy", None if policy else "global network policy not initialised"))


def fake_docker(monkeypatch, ok: bool):
    def verify(which=None, **k):
        if not ok:
            raise SandboxUnavailable("daemon is not rootless")
        return VerifiedDocker("/usr/bin/docker", SOCK)
    monkeypatch.setattr(sandbox, "verify_rootless_docker", verify)


def which_with(sbx: bool):
    return lambda n: ("/usr/bin/docker" if n == "docker" else "/usr/bin/sbx" if sbx else None)


# ── the whole table ──────────────────────────────────────────────────────────────

CASES = list(itertools.product([True, False], repeat=6))  # sbx, opted, verified, daemon, kvm, docker


@pytest.mark.parametrize("sbx,opted,verified,daemon,kvm,docker", CASES)
def test_selection_table(monkeypatch, sbx, opted, verified, daemon, kvm, docker):
    fake_docker(monkeypatch, docker)
    environ = {ENV_SANDBOX: "sbx"} if opted else {}
    ver = frozenset({VERSION}) if verified else frozenset()
    pr = probes(daemon=daemon, kvm=kvm)
    sbx_selected = sbx and opted and verified and daemon and kvm
    kw = dict(environ=environ, probes=pr, verified=ver)
    if sbx_selected:
        assert isinstance(sandbox.default_runner(which_with(sbx), **kw), SbxRunner)
        assert sandbox.sandbox_status(which_with(sbx), **kw) == "sbx"
    elif docker:
        r = sandbox.default_runner(which_with(sbx), **kw)
        assert isinstance(r, DockerRunner) and r.docker_host == SOCK
        assert sandbox.sandbox_status(which_with(sbx), **kw) == "rootless-docker"
    else:
        with pytest.raises(SandboxUnavailable, match="Docker fallback.*not rootless"):
            sandbox.default_runner(which_with(sbx), **kw)
        assert sandbox.sandbox_status(which_with(sbx), **kw).startswith("unavailable (fail closed)")
    rep = sandbox.sandbox_report(which_with(sbx), **kw)["sbx"]
    expected = ("absent" if not sbx else "usable-and-selected" if sbx_selected else "present-but-unusable")
    assert rep["state"] == expected
    if sbx and not sbx_selected:
        reasons = " | ".join(rep["reasons"])
        assert ("not opted in" in reasons) is (not opted)
        assert ("not in the verified set" in reasons) is (not verified)
        assert ("daemon is not reachable" in reasons) is (not daemon)
        assert ("permission denied" in reasons) is (not kvm)
    if sbx_selected:
        assert rep["reasons"] == []


# ── the old behaviour is gone ────────────────────────────────────────────────────

def test_sbx_on_path_alone_no_longer_selects_sbx(monkeypatch):
    """The previous behaviour (`which('sbx')` found => SbxRunner) must not come back."""
    fake_docker(monkeypatch, True)
    r = sandbox.default_runner(lambda n: f"/x/{n}", environ={}, probes=probes())
    assert isinstance(r, DockerRunner) and not isinstance(r, SbxRunner)


def test_sbx_on_path_alone_does_not_probe_sbx_or_touch_docker_differently(monkeypatch):
    fake_docker(monkeypatch, True)
    calls: list[str] = []
    sandbox.default_runner(lambda n: f"/x/{n}", environ={}, probes=probes(calls=calls))
    assert calls == []  # not opted in and nothing verified: no sbx process is started to find out more


def test_the_default_verified_set_is_empty():
    assert VERIFIED_SBX_VERSIONS == frozenset()


@pytest.mark.parametrize("setting", ["sbx", "SBX", " sbx "])
def test_sbx_cannot_be_returned_while_the_verified_set_is_empty(monkeypatch, setting):
    """Opted in, daemon up, kvm fine, policy fine, every version string: still Docker while nothing is verified."""
    fake_docker(monkeypatch, True)
    calls: list[str] = []
    r = sandbox.default_runner(which_with(True), environ={ENV_SANDBOX: setting}, probes=probes(calls=calls))
    assert isinstance(r, DockerRunner)
    assert calls == []
    rep = sandbox.sandbox_report(which_with(True), environ={ENV_SANDBOX: setting}, probes=probes())["sbx"]
    assert rep["state"] == "present-but-unusable" and any("verified set is empty" in x for x in rep["reasons"])


def test_the_module_constant_is_what_default_runner_consults_when_not_overridden(monkeypatch):
    fake_docker(monkeypatch, True)
    env = {ENV_SANDBOX: "sbx"}
    assert isinstance(sandbox.default_runner(which_with(True), environ=env, probes=probes()), DockerRunner)
    monkeypatch.setattr(sandbox, "VERIFIED_SBX_VERSIONS", frozenset({VERSION}))
    assert isinstance(sandbox.default_runner(which_with(True), environ=env, probes=probes()), SbxRunner)


# ── opt-in values ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value", [None, "", "auto", "AUTO", "docker", "rootless-docker"])
def test_default_and_auto_mean_rootless_docker_even_with_everything_verified(monkeypatch, value):
    fake_docker(monkeypatch, True)
    env = {} if value is None else {ENV_SANDBOX: value}
    r = sandbox.default_runner(which_with(True), environ=env, probes=probes(), verified=frozenset({VERSION}))
    assert isinstance(r, DockerRunner)


@pytest.mark.parametrize("value", ["sbxx", "microvm", "host", "none", "1"])
def test_an_unrecognised_setting_fails_closed_rather_than_being_ignored(monkeypatch, value):
    fake_docker(monkeypatch, True)
    with pytest.raises(SandboxUnavailable, match=ENV_SANDBOX):
        sandbox.default_runner(which_with(True), environ={ENV_SANDBOX: value}, probes=probes())
    assert sandbox.sandbox_status(which_with(True), environ={ENV_SANDBOX: value}).startswith("unavailable")


def test_a_version_outside_the_verified_set_is_unusable_and_named(monkeypatch):
    fake_docker(monkeypatch, True)
    env = {ENV_SANDBOX: "sbx"}
    rep = sandbox.sandbox_report(which_with(True), environ=env, probes=probes(version="0.51.0"),
                                 verified=frozenset({VERSION}))["sbx"]
    assert rep["state"] == "present-but-unusable" and rep["version"] == "0.51.0"
    assert any("0.51.0" in x and "not in the verified set" in x for x in rep["reasons"])
    assert isinstance(sandbox.default_runner(which_with(True), environ=env, probes=probes(version="0.51.0"),
                                             verified=frozenset({VERSION})), DockerRunner)


def test_a_policy_that_is_not_initialised_blocks_selection(monkeypatch):
    fake_docker(monkeypatch, True)
    env, ver = {ENV_SANDBOX: "sbx"}, frozenset({VERSION})
    assert isinstance(sandbox.default_runner(which_with(True), environ=env, probes=probes(policy=False),
                                             verified=ver), DockerRunner)
    rep = sandbox.sandbox_report(which_with(True), environ=env, probes=probes(policy=False), verified=ver)["sbx"]
    assert any("policy" in x for x in rep["reasons"])


def test_no_sbx_process_is_asked_anything_while_the_daemon_is_unreachable(monkeypatch):
    """Asking a CLI while its daemon is down can start the daemon; this module must never be what starts it."""
    fake_docker(monkeypatch, True)
    calls: list[str] = []
    rep = sandbox.sandbox_report(which_with(True), environ={ENV_SANDBOX: "sbx"}, verified=frozenset({VERSION}),
                                 probes=probes(daemon=False, calls=calls))["sbx"]
    assert "version" not in calls and "policy" not in calls
    assert any("daemon is not reachable" in x for x in rep["reasons"])


def test_a_probe_that_raises_is_unusable_not_a_crash(monkeypatch):
    fake_docker(monkeypatch, True)
    def boom(*a):
        raise OSError("exec format error")
    pr = probes()._replace(version=boom)
    rep = sandbox.sandbox_report(which_with(True), environ={ENV_SANDBOX: "sbx"}, probes=pr,
                                 verified=frozenset({VERSION}))["sbx"]
    assert rep["state"] == "present-but-unusable" and any("exec format error" in x for x in rep["reasons"])


# ── what is reported when sbx is absent, and the unchanged refusals ─────────────────

def test_absent_sbx_is_reported_absent_and_the_old_message_is_kept(monkeypatch):
    fake_docker(monkeypatch, False)
    with pytest.raises(SandboxUnavailable, match="`sbx`.*not installed.*Docker fallback.*not rootless"):
        sandbox.default_runner(lambda _: None, environ={}, probes=probes())
    assert sandbox.sandbox_report(lambda _: None, environ={}, probes=probes())["sbx"]["state"] == "absent"


def test_present_but_not_selected_and_no_docker_names_the_reasons(monkeypatch):
    fake_docker(monkeypatch, False)
    with pytest.raises(SandboxUnavailable, match="not selected.*not opted in.*Docker fallback.*not rootless"):
        sandbox.default_runner(which_with(True), environ={}, probes=probes())


def test_never_falls_back_to_the_host_or_rootful_docker(monkeypatch):
    """Both runtimes unusable: a refusal, not a runner. (Rootful docker is refused by verify_rootless_docker itself.)"""
    fake_docker(monkeypatch, False)
    ver = frozenset({VERSION})
    for sbx, env, pr in [(True, {ENV_SANDBOX: "sbx"}, probes(kvm=False)), (True, {}, probes()), (False, {}, probes())]:
        with pytest.raises(SandboxUnavailable):
            sandbox.default_runner(which_with(sbx), environ=env, probes=pr, verified=ver)


def test_the_status_report_keeps_the_existing_string_and_adds_one_closed_dict(monkeypatch):
    fake_docker(monkeypatch, True)
    out = sandbox.sandbox_report(which_with(True), environ={}, probes=probes())
    assert set(out) == {"sandbox", "sbx"} and out["sandbox"] == "rootless-docker"
    assert set(out["sbx"]) == {"state", "path", "version", "opted_in", "verified_versions", "reasons"}
    assert out["sbx"]["verified_versions"] == [] and out["sbx"]["opted_in"] is False


# ── the real host, read-only ─────────────────────────────────────────────────────

def test_this_hosts_real_state_selects_docker_not_sbx(monkeypatch):
    """Whatever is installed here, with no opt-in and nothing verified, the default is rootless Docker."""
    fake_docker(monkeypatch, True)
    r = sandbox.default_runner(environ={})
    assert isinstance(r, DockerRunner) and not isinstance(r, SbxRunner)


@pytest.mark.requires_sbx
@pytest.mark.skipif(shutil.which("sbx") is None, reason="sbx is not installed")
def test_real_sbx_version_probe_is_read_only_and_parses():
    v = sandbox.real_sbx_probes({}).version(shutil.which("sbx"))
    assert v and v[0].isdigit()
    assert subprocess.run([shutil.which("sbx"), "version"], capture_output=True, text=True).returncode == 0
