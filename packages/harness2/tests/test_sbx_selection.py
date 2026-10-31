"""Which sandbox runs candidate code: `COGNITIVE_HARNESS2_SANDBOX` is a tri-state and sbx is the default when usable.

  * unset / `auto`: `SbxRunner` when the installed sbx is a VERIFIED version and a cheap, read-only, time-bounded
    probe passes (kvm, daemon, deny-all policy, and the two host-reach settings off); otherwise rootless Docker,
    LOUDLY (a structured reason in the report, one WARNING per process) when sbx is present but not selected;
  * `docker`: rootless Docker, sbx never consulted (no process, no probe, not even `which`);
  * `sbx`: `SbxRunner` or `SandboxUnavailable`, never Docker;
  * anything else: `SandboxUnavailable`.

The invariant that survived the change: `sbx` on PATH alone never selects it. Nothing here boots a microVM; the
fake-sbx tests start only the read-only probe commands of a fake script. The one real-sbx test (`requires_sbx`) runs
read-only commands and is skipped when sbx is absent.
"""
from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from cognitive_harness2 import sandbox
from cognitive_harness2.sandbox import (
    ENV_ALLOW_HOST_SETTINGS,
    ENV_SANDBOX,
    VERIFIED_SBX_VERSIONS,
    DockerRunner,
    SandboxUnavailable,
    SbxProbes,
    SbxRunner,
    VerifiedDocker,
)

HERE = Path(__file__).parent
FAKE_DIR = HERE / "fixtures" / "fake-sbx"
DENY_ALL = HERE / "fixtures" / "sbx_policy_ls_deny_all.json"
SETTINGS_DEFAULTS = HERE / "fixtures" / "sbx_settings_ls_defaults.json"  # captured from a real v0.50.1 host
SOCK = "unix:///run/user/1000/docker.sock"
VERSION = "0.50.1"
OFF = {"ssh.agentForwardingEnabled": False, "browser.enabled": False}
WARN_LOGGER = sandbox.__name__


def probes(*, daemon=True, kvm=True, policy=True, version=VERSION, settings=None, raises=None, calls=None) -> SbxProbes:
    """Fake probes. `raises` maps a probe name to an exception instance or class it raises instead of answering."""
    raises = raises or {}

    def make(name, value):
        def f(*a):
            if calls is not None:
                calls.append(name)
            if name in raises:
                raise raises[name]
            return value
        return f
    return SbxProbes(
        kvm_problem=make("kvm", None if kvm else "/dev/kvm: permission denied"),
        daemon_reachable=make("daemon", daemon),
        version=make("version", version),
        policy_problem=make("policy", None if policy else "global network policy not initialised"),
        risky_settings=make("settings", OFF if settings is None else settings))


class Docker:
    """A stand-in for `verify_rootless_docker` that counts how often it is asked."""
    def __init__(self, monkeypatch, ok: bool = True):
        self.ok, self.asked = ok, 0
        monkeypatch.setattr(sandbox, "verify_rootless_docker", self)

    def __call__(self, which=None, **k):
        self.asked += 1
        if not self.ok:
            raise SandboxUnavailable("daemon is not rootless")
        return VerifiedDocker("/usr/bin/docker", SOCK)


@pytest.fixture
def docker(monkeypatch):
    return Docker(monkeypatch)


def which_with(sbx: bool):
    return lambda n: ("/usr/bin/docker" if n == "docker" else "/usr/bin/sbx" if sbx else None)


def warnings(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == WARN_LOGGER and r.levelno == logging.WARNING]


@pytest.fixture(autouse=True)
def _capture(caplog):
    caplog.set_level(logging.WARNING, logger=WARN_LOGGER)


# ── auto (the default): sbx when verified AND usable ─────────────────────────────────

@pytest.mark.parametrize("env", [{}, {ENV_SANDBOX: ""}, {ENV_SANDBOX: "auto"}, {ENV_SANDBOX: " AUTO "}])
def test_auto_selects_sbx_when_verified_and_usable(docker, caplog, env):
    r = sandbox.default_runner(which_with(True), environ=env, probes=probes())
    assert isinstance(r, SbxRunner) and r.verified == VERIFIED_SBX_VERSIONS
    rep = sandbox.sandbox_report(which_with(True), environ=env, probes=probes())
    assert (rep["sandbox"], rep["runner"], rep["mode"], rep["fallback_reason"]) == ("sbx", "sbx", "auto", None)
    assert rep["sbx"]["state"] == "usable-and-selected" and rep["sbx"]["reasons"] == [] and rep["sbx"]["codes"] == []
    assert rep["sbx"]["settings"] == OFF and rep["sbx"]["version"] == VERSION
    assert docker.asked == 0 and warnings(caplog) == []  # Docker is not even verified when sbx is chosen


def test_the_runner_gets_the_environment_and_the_verified_set(docker):
    env = {"XDG_STATE_HOME": "/x"}
    r = sandbox.default_runner(which_with(True), environ=env, probes=probes(), verified=frozenset({VERSION}))
    assert isinstance(r, SbxRunner) and r.verified == frozenset({VERSION}) and r.environ == env


def test_the_shipped_verified_set_is_exactly_the_live_verified_version():
    """v0.50.1 was verified end to end (tests/test_sbx_runner.py::test_live_*). Adding a version is the act of
    verifying it: run that test against it first."""
    assert VERIFIED_SBX_VERSIONS == frozenset({"0.50.1"})


def test_the_module_constant_is_what_default_runner_consults_when_not_overridden(docker, monkeypatch):
    monkeypatch.setattr(sandbox, "VERIFIED_SBX_VERSIONS", frozenset())
    assert isinstance(sandbox.default_runner(which_with(True), environ={}, probes=probes()), DockerRunner)
    monkeypatch.setattr(sandbox, "VERIFIED_SBX_VERSIONS", frozenset({VERSION}))
    assert isinstance(sandbox.default_runner(which_with(True), environ={}, probes=probes()), SbxRunner)


# every way a PRESENT sbx can fail to be selected: (id, probe kwargs, code, sbx asked, ask-order of the probes)
FALLBACKS = [
    ("daemon down", dict(daemon=False), "daemon-down", ["kvm", "daemon"]),
    ("kvm unusable", dict(kvm=False), "kvm-unusable", ["kvm", "daemon", "version", "policy", "settings"]),
    ("unverified version", dict(version="0.51.0"), "unverified-version", ["kvm", "daemon", "version"]),
    ("unreadable version", dict(version=None), "unreadable-version", ["kvm", "daemon", "version"]),
    ("policy not deny-all", dict(policy=False), "policy-not-deny-all", ["kvm", "daemon", "version", "policy", "settings"]),
    ("probe timeout", dict(raises={"version": subprocess.TimeoutExpired(["sbx"], 5)}), "probe-timeout",
     ["kvm", "daemon", "version"]),
    ("settings probe timeout", dict(raises={"settings": subprocess.TimeoutExpired(["sbx"], 5)}), "probe-timeout",
     ["kvm", "daemon", "version", "policy", "settings"]),
    ("probe error", dict(raises={"version": OSError("exec format error")}), "probe-error", ["kvm", "daemon", "version"]),
    ("daemon probe error", dict(raises={"daemon": RuntimeError("boom")}), "probe-error", ["kvm", "daemon"]),
    ("settings probe error", dict(raises={"settings": RuntimeError("sbx settings ls failed")}), "probe-error",
     ["kvm", "daemon", "version", "policy", "settings"]),
    ("ssh agent forwarding on", dict(settings={**OFF, "ssh.agentForwardingEnabled": True}), "unsafe-setting",
     ["kvm", "daemon", "version", "policy", "settings"]),
    ("browser on", dict(settings={**OFF, "browser.enabled": True}), "unsafe-setting",
     ["kvm", "daemon", "version", "policy", "settings"]),
    ("both on (the defaults of a fresh install)", dict(settings={k: True for k in OFF}), "unsafe-setting",
     ["kvm", "daemon", "version", "policy", "settings"]),
    ("a setting missing from the answer", dict(settings={"browser.enabled": False}), "unreadable-setting",
     ["kvm", "daemon", "version", "policy", "settings"]),
]


@pytest.mark.parametrize("why,kw,code,asked", FALLBACKS, ids=[f[0] for f in FALLBACKS])
def test_auto_falls_back_to_docker_with_a_structured_reason_and_one_warning(docker, caplog, why, kw, code, asked):
    calls: list[str] = []
    r = sandbox.default_runner(which_with(True), environ={}, probes=probes(calls=calls, **kw))
    assert isinstance(r, DockerRunner) and not isinstance(r, SbxRunner) and r.docker_host == SOCK
    assert calls == asked
    rep = sandbox.sandbox_report(which_with(True), environ={}, probes=probes(**kw))
    assert (rep["sandbox"], rep["runner"], rep["mode"]) == ("rootless-docker", "rootless-docker", "auto")
    assert rep["fallback_reason"]["code"] == code and rep["fallback_reason"]["loud"] is True
    assert rep["fallback_reason"]["detail"] and code in rep["sbx"]["codes"]
    assert rep["sbx"]["state"] == "present-but-unusable"
    (w,) = warnings(caplog)  # exactly one line, naming the runner and why
    assert "rootless Docker" in w.getMessage() and code in w.getMessage() and rep["sbx"]["reasons"][0] in w.getMessage()


def test_the_unsafe_setting_message_says_how_to_turn_them_off_and_the_override(docker):
    rep = sandbox.sandbox_report(which_with(True), environ={}, probes=probes(settings={k: True for k in OFF}))
    msg = rep["fallback_reason"]["detail"]
    assert "ssh.agentForwardingEnabled=true" in msg and "browser.enabled=true" in msg
    assert "sbx settings set" in msg and ENV_ALLOW_HOST_SETTINGS + "=1" in msg
    assert rep["sbx"]["settings"] == {k: True for k in OFF}


def test_the_documented_override_accepts_the_two_settings_and_nothing_else(docker):
    on = probes(settings={k: True for k in OFF})
    assert isinstance(sandbox.default_runner(which_with(True), environ={ENV_ALLOW_HOST_SETTINGS: "1"}, probes=on), SbxRunner)
    for wrong in ("true", "yes", "0", ""):  # only the exact value `1`
        assert isinstance(sandbox.default_runner(which_with(True), environ={ENV_ALLOW_HOST_SETTINGS: wrong}, probes=on),
                          DockerRunner)
    # every other check still applies with the override set
    for kw in (dict(daemon=False), dict(version="0.51.0"), dict(policy=False), dict(kvm=False)):
        assert isinstance(sandbox.default_runner(which_with(True), environ={ENV_ALLOW_HOST_SETTINGS: "1"},
                                                 probes=probes(settings={k: True for k in OFF}, **kw)), DockerRunner)


def test_the_verified_set_being_empty_asks_sbx_nothing(docker, caplog):
    calls: list[str] = []
    r = sandbox.default_runner(which_with(True), environ={}, probes=probes(calls=calls), verified=frozenset())
    assert isinstance(r, DockerRunner) and calls == []
    rep = sandbox.sandbox_report(which_with(True), environ={}, probes=probes(), verified=frozenset())
    assert rep["fallback_reason"]["code"] == "no-verified-version" and "verified set is empty" in rep["sbx"]["reasons"][0]
    assert len(warnings(caplog)) == 1


def test_sbx_not_installed_is_the_quiet_normal_case(docker, caplog):
    calls: list[str] = []
    r = sandbox.default_runner(which_with(False), environ={}, probes=probes(calls=calls))
    assert isinstance(r, DockerRunner) and calls == [] and warnings(caplog) == []
    rep = sandbox.sandbox_report(which_with(False), environ={}, probes=probes())
    assert (rep["runner"], rep["sbx"]["state"], rep["sbx"]["path"]) == ("rootless-docker", "absent", None)
    assert rep["fallback_reason"] == {"code": "not-installed", "detail": "`sbx` is not on PATH", "loud": False}


# ── PATH presence alone never selects sbx ────────────────────────────────────────────

@pytest.mark.parametrize("kw,verified", [
    (dict(), frozenset()),                          # a perfect probe, but nothing is verified
    (dict(version="0.49.9"), None),                 # a passing probe on an unverified version
    (dict(version="1.0.0"), None),
    (dict(daemon=False), None),                     # a verified version nobody can reach
    (dict(raises={"version": OSError("nope")}), None),
])
def test_path_presence_alone_never_selects_sbx(docker, kw, verified):
    r = sandbox.default_runner(which_with(True), environ={}, probes=probes(**kw), verified=verified)
    assert isinstance(r, DockerRunner) and not isinstance(r, SbxRunner)


def test_real_probes_that_cannot_pass_never_select_sbx(docker, monkeypatch):
    """No injected probes: sbx is a bare name on PATH, the real probe machinery runs and cannot succeed."""
    monkeypatch.setattr(sandbox, "_run_readonly", lambda *a, **k: (_ for _ in ()).throw(OSError("not a real sbx")))
    monkeypatch.setattr(sandbox.os.path, "exists", lambda p: True if p == "/dev/kvm" else os.path.lexists(p))
    r = sandbox.default_runner(lambda n: f"/x/{n}", environ={"XDG_STATE_HOME": "/nonexistent/state"})
    assert isinstance(r, DockerRunner) and not isinstance(r, SbxRunner)


# ── docker: sbx is never consulted ───────────────────────────────────────────────────

@pytest.mark.parametrize("value", ["docker", "DOCKER", " rootless-docker "])
def test_docker_never_touches_sbx(docker, caplog, monkeypatch, value):
    asked: list[str] = []
    calls: list[str] = []

    def which(n):
        asked.append(n)
        return which_with(True)(n)

    def boom(*a, **k):
        raise AssertionError("an sbx probe was started")
    monkeypatch.setattr(sandbox, "real_sbx_probes", boom)
    monkeypatch.setattr(sandbox, "_run_readonly", boom)
    env = {ENV_SANDBOX: value}
    r = sandbox.default_runner(which, environ=env, probes=probes(calls=calls))
    assert isinstance(r, DockerRunner) and not isinstance(r, SbxRunner)
    assert "sbx" not in asked and calls == []           # not looked up on PATH, no probe asked
    assert sandbox.sandbox_status(which, environ=env, probes=probes(calls=calls)) == "rootless-docker"
    rep = sandbox.sandbox_report(which, environ=env)    # and with no injected probes either
    assert (rep["runner"], rep["mode"], rep["sbx"]["state"]) == ("rootless-docker", "docker", "not-consulted")
    assert rep["fallback_reason"]["code"] == "forced-docker" and rep["fallback_reason"]["loud"] is False
    assert "sbx" not in asked and calls == [] and warnings(caplog) == []


def test_docker_unusable_under_docker_fails_closed_and_still_never_uses_sbx(monkeypatch):
    Docker(monkeypatch, ok=False)
    calls: list[str] = []
    with pytest.raises(SandboxUnavailable, match=f"{ENV_SANDBOX}=docker.*unusable.*not rootless"):
        sandbox.default_runner(which_with(True), environ={ENV_SANDBOX: "docker"}, probes=probes(calls=calls))
    assert calls == []


# ── sbx: forced, fails closed, never falls back ──────────────────────────────────────

def test_forced_sbx_selects_sbx_when_usable_and_never_verifies_docker(docker):
    r = sandbox.default_runner(which_with(True), environ={ENV_SANDBOX: "sbx"}, probes=probes())
    assert isinstance(r, SbxRunner) and docker.asked == 0
    rep = sandbox.sandbox_report(which_with(True), environ={ENV_SANDBOX: "sbx"}, probes=probes())
    assert (rep["runner"], rep["mode"], rep["fallback_reason"]) == ("sbx", "sbx", None)


@pytest.mark.parametrize("why,kw,code,asked", FALLBACKS, ids=[f[0] for f in FALLBACKS])
def test_forced_sbx_fails_closed_in_every_non_usable_case(docker, caplog, why, kw, code, asked):
    env = {ENV_SANDBOX: "sbx"}
    with pytest.raises(SandboxUnavailable, match="never falls back to Docker") as e:
        sandbox.default_runner(which_with(True), environ=env, probes=probes(**kw))
    assert ENV_SANDBOX in str(e.value) and sandbox.sandbox_status(
        which_with(True), environ=env, probes=probes(**kw)).startswith("unavailable (fail closed)")
    assert docker.asked == 0, "Docker was consulted although sbx was forced"
    assert code in sandbox.sandbox_report(which_with(True), environ=env, probes=probes(**kw))["sbx"]["codes"]
    assert warnings(caplog) == []  # a refusal is an error, not a warning about a fallback


def test_forced_sbx_without_sbx_installed_fails_closed(docker):
    with pytest.raises(SandboxUnavailable, match="`sbx` is not on PATH.*never falls back"):
        sandbox.default_runner(which_with(False), environ={ENV_SANDBOX: "sbx"}, probes=probes())
    assert docker.asked == 0


def test_forced_sbx_with_an_empty_verified_set_fails_closed(docker):
    with pytest.raises(SandboxUnavailable, match="verified set"):
        sandbox.default_runner(which_with(True), environ={ENV_SANDBOX: "sbx"}, probes=probes(), verified=frozenset())


# ── bad values fail closed ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("value", ["sbxx", "microvm", "host", "none", "1", "true", "auto,docker", "rootful"])
def test_an_unrecognised_setting_fails_closed_rather_than_being_ignored(docker, value):
    calls: list[str] = []
    for sbx in (True, False):
        with pytest.raises(SandboxUnavailable, match=ENV_SANDBOX):
            sandbox.default_runner(which_with(sbx), environ={ENV_SANDBOX: value}, probes=probes(calls=calls))
        assert sandbox.sandbox_status(which_with(sbx), environ={ENV_SANDBOX: value},
                                      probes=probes(calls=calls)).startswith("unavailable (fail closed)")
    assert calls == [] and docker.asked == 0  # nothing is probed and nothing falls back


# ── the warning: once per process, and only for the loud case ─────────────────────────

def test_the_warning_is_emitted_once_however_often_the_runner_is_built(docker, caplog):
    for _ in range(5):
        sandbox.default_runner(which_with(True), environ={}, probes=probes(daemon=False))
        sandbox.sandbox_report(which_with(True), environ={}, probes=probes(daemon=False))
    assert len(warnings(caplog)) == 1


def test_a_different_reason_is_a_new_fact_and_warns_again_once(docker, caplog):
    for _ in range(3):
        sandbox.default_runner(which_with(True), environ={}, probes=probes(daemon=False))
        sandbox.default_runner(which_with(True), environ={}, probes=probes(version="0.51.0"))
    assert len(warnings(caplog)) == 2


def test_no_warning_when_sbx_is_chosen_absent_or_docker_was_asked_for(docker, caplog):
    sandbox.default_runner(which_with(True), environ={}, probes=probes())
    sandbox.default_runner(which_with(False), environ={}, probes=probes())
    sandbox.default_runner(which_with(True), environ={ENV_SANDBOX: "docker"}, probes=probes(daemon=False))
    assert warnings(caplog) == []


def test_the_warning_goes_to_stderr_without_any_logging_setup(docker, capfd):
    """Python's last-resort handler prints WARNING and above to stderr when no handler is configured."""
    root = logging.getLogger()
    saved, root.handlers = root.handlers, []
    try:
        sandbox.default_runner(which_with(True), environ={}, probes=probes(daemon=False))
    finally:
        root.handlers = saved
    assert "rootless Docker" in capfd.readouterr().err


# ── the report ───────────────────────────────────────────────────────────────────────

def test_the_report_keeps_the_existing_string_and_adds_runner_mode_reason_and_one_closed_dict(docker):
    out = sandbox.sandbox_report(which_with(True), environ={}, probes=probes(daemon=False))
    assert set(out) == {"sandbox", "runner", "mode", "fallback_reason", "sbx"} and out["sandbox"] == "rootless-docker"
    assert set(out["sbx"]) == {"state", "path", "version", "mode", "verified_versions", "reasons", "codes", "settings"}
    assert out["sbx"]["verified_versions"] == ["0.50.1"] and out["sbx"]["mode"] == "auto"
    assert set(out["fallback_reason"]) == {"code", "detail", "loud"}


def test_the_report_when_nothing_is_usable_says_so_and_names_both(monkeypatch):
    Docker(monkeypatch, ok=False)
    out = sandbox.sandbox_report(which_with(True), environ={}, probes=probes(daemon=False))
    assert out["runner"] == "unavailable" and out["sandbox"].startswith("unavailable (fail closed)")
    assert "daemon is not reachable" in out["sandbox"] and "not rootless" in out["sandbox"]
    assert out["fallback_reason"]["code"] == "daemon-down"


def test_both_runtimes_unusable_is_a_refusal_never_a_host_or_rootful_runner(monkeypatch):
    Docker(monkeypatch, ok=False)
    for sbx, env, pr in [(True, {}, probes(kvm=False)), (True, {}, probes(daemon=False)), (False, {}, probes()),
                         (True, {ENV_SANDBOX: "docker"}, probes())]:
        with pytest.raises(SandboxUnavailable):
            sandbox.default_runner(which_with(sbx), environ=env, probes=pr)


def test_old_messages_are_kept_when_no_docker_either(monkeypatch):
    Docker(monkeypatch, ok=False)
    with pytest.raises(SandboxUnavailable, match="`sbx`.*not installed.*Docker fallback.*not rootless"):
        sandbox.default_runner(which_with(False), environ={}, probes=probes())
    with pytest.raises(SandboxUnavailable, match="not selected.*daemon is not reachable.*Docker fallback.*not rootless"):
        sandbox.default_runner(which_with(True), environ={}, probes=probes(daemon=False))


# ── the probe is cached per process, bounded, and read-only ──────────────────────────

def test_the_real_probe_result_is_cached_per_process_and_expires(docker, monkeypatch):
    built: list[int] = []

    def fake_real(environ):
        built.append(1)
        return probes()
    monkeypatch.setattr(sandbox, "real_sbx_probes", fake_real)
    clock = [1000.0]
    monkeypatch.setattr(sandbox.time, "monotonic", lambda: clock[0])
    env = {"XDG_STATE_HOME": "/s"}
    for _ in range(4):
        assert isinstance(sandbox.default_runner(which_with(True), environ=env), SbxRunner)
        sandbox.sandbox_report(which_with(True), environ=env)
    assert len(built) == 1
    clock[0] += sandbox._SBX_PROBE_CACHE_TTL_S + 1
    sandbox.default_runner(which_with(True), environ=env)
    assert len(built) == 2
    sandbox.clear_sbx_cache()
    sandbox.default_runner(which_with(True), environ=env)
    assert len(built) == 3
    # a different state dir, mode or the override is a different question: not answered from the cache
    sandbox.default_runner(which_with(True), environ={"XDG_STATE_HOME": "/other"})
    sandbox.default_runner(which_with(True), environ={**env, ENV_ALLOW_HOST_SETTINGS: "1"})
    assert len(built) == 5


def test_injected_probes_are_never_cached(docker):
    calls: list[str] = []
    for _ in range(3):
        sandbox.default_runner(which_with(True), environ={}, probes=probes(calls=calls))
    assert calls.count("version") == 3


def test_a_hung_command_is_killed_and_raises_within_its_timeout():
    t0 = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        sandbox._run_readonly(["sleep", "30"], timeout=0.3)
    assert time.monotonic() - t0 < 8


def test_a_hung_child_that_holds_the_pipes_cannot_hang_the_probe_either():
    t0 = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        sandbox._run_readonly(["sh", "-c", "sleep 30 & sleep 30"], timeout=0.3)
    assert time.monotonic() - t0 < 8


# ── the real probe functions, against the fake sbx script (read-only commands only) ───

class FakeSbxHost:
    def __init__(self, root: Path, *, socket_up: bool = True):
        self.root = root
        self.state = root / "state"
        sd = self.state / "sandboxes" / "sandboxes" / "sandboxd"
        sd.mkdir(parents=True)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(sd / "sandboxd.sock"))
        self.sock.listen(8)
        if not socket_up:
            self.sock.close()
            (sd / "sandboxd.sock").unlink()
        (root / "fake").mkdir()
        self.env = {"PATH": f"{FAKE_DIR}:{os.environ['PATH']}", "XDG_STATE_HOME": str(self.state),
                    "FAKE_SBX_DIR": str(root / "fake"), "FAKE_SBX_POLICY_FILE": str(DENY_ALL),
                    "FAKE_SBX_VERSION": "v9.9.9"}

    def verbs(self) -> list[str]:
        f = self.root / "fake" / "calls"
        if not f.exists():
            return []
        out = [line.split("\x1f")[:-1] for line in f.read_text().splitlines()]
        return [c[0] if c[0] not in ("policy", "settings") else f"{c[0]} {c[1]}" for c in out]


@pytest.fixture
def fakehost():
    root = Path(tempfile.mkdtemp(prefix="h2sel"))  # AF_UNIX paths are short
    host = FakeSbxHost(root)
    try:
        yield host
    finally:
        host.sock.close()
        shutil.rmtree(root, ignore_errors=True)


def real_probes_of(host: FakeSbxHost, env: dict) -> SbxProbes:
    """The REAL probe functions, with only the /dev/kvm answer replaced (it is the machine's, not the fake's)."""
    return sandbox.real_sbx_probes({**host.env, **env})._replace(kvm_problem=lambda: None)


def inspect_fake(host, monkeypatch, docker_ok=True, **env):
    Docker(monkeypatch, ok=docker_ok)
    e = {**host.env, **env}
    fake = str(FAKE_DIR / "sbx")
    which = lambda n: fake if n == "sbx" else "/usr/bin/docker"  # noqa: E731
    return which, e, real_probes_of(host, env), frozenset({"9.9.9"})


def test_the_real_probes_select_sbx_and_ask_only_read_only_questions(fakehost, monkeypatch):
    which, env, pr, ver = inspect_fake(fakehost, monkeypatch)
    r = sandbox.default_runner(which, environ=env, probes=pr, verified=ver)
    assert isinstance(r, SbxRunner)
    assert fakehost.verbs() == ["version", "policy ls", "settings ls"]  # never create, run, rm, exec, daemon start


@pytest.mark.parametrize("fake_settings,codes", [("ssh", ["unsafe-setting"]), ("browser", ["unsafe-setting"]),
                                                  ("both", ["unsafe-setting"]), ("fail", ["probe-error"]),
                                                  ("garbage", ["probe-error"])])
def test_the_real_settings_probe_refuses_unsafe_or_unreadable_settings(fakehost, monkeypatch, fake_settings, codes):
    which, env, pr, ver = inspect_fake(fakehost, monkeypatch, FAKE_SBX_SETTINGS=fake_settings)
    rep = sandbox.sandbox_report(which, environ=env, probes=pr, verified=ver)
    assert rep["runner"] == "rootless-docker" and rep["sbx"]["codes"] == codes


def test_the_override_selects_sbx_through_the_real_probes(fakehost, monkeypatch):
    which, env, pr, ver = inspect_fake(fakehost, monkeypatch, FAKE_SBX_SETTINGS="both", **{ENV_ALLOW_HOST_SETTINGS: "1"})
    assert isinstance(sandbox.default_runner(which, environ=env, probes=pr, verified=ver), SbxRunner)


def test_a_hung_sbx_means_fallback_with_a_reason_not_a_hang(fakehost, monkeypatch):
    monkeypatch.setattr(sandbox, "_SBX_PROBE_TIMEOUT_S", 0.5)
    which, env, pr, ver = inspect_fake(fakehost, monkeypatch, FAKE_SBX_SETTINGS="hang")
    t0 = time.monotonic()
    rep = sandbox.sandbox_report(which, environ=env, probes=pr, verified=ver)
    assert time.monotonic() - t0 < 15
    assert rep["runner"] == "rootless-docker" and rep["sbx"]["codes"] == ["probe-timeout"]
    assert "timed out" in rep["sbx"]["reasons"][0]


def test_no_sbx_process_is_started_while_the_daemon_socket_does_not_answer(monkeypatch):
    """Asking a CLI while its daemon is down can start the daemon (`sbx settings` says so): never do it."""
    root = Path(tempfile.mkdtemp(prefix="h2sel"))
    try:
        host = FakeSbxHost(root, socket_up=False)
        which, env, pr, ver = inspect_fake(host, monkeypatch)
        rep = sandbox.sandbox_report(which, environ=env, probes=pr, verified=ver)
        assert rep["runner"] == "rootless-docker" and rep["sbx"]["codes"] == ["daemon-down"]
        assert host.verbs() == []
        with pytest.raises(SandboxUnavailable, match="daemon is not reachable"):
            sandbox.default_runner(which, environ={**env, ENV_SANDBOX: "sbx"}, probes=pr, verified=ver)
        assert host.verbs() == []
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_docker_mode_starts_no_sbx_process_at_all(fakehost, monkeypatch):
    which, env, pr, ver = inspect_fake(fakehost, monkeypatch, **{ENV_SANDBOX: "docker"})
    assert isinstance(sandbox.default_runner(which, environ=env, probes=pr, verified=ver), DockerRunner)
    sandbox.sandbox_report(which, environ=env, verified=ver)
    assert fakehost.verbs() == []


def test_the_probe_commands_run_without_the_hosts_ssh_agent_or_sbx_overrides(fakehost, monkeypatch):
    which, env, pr, ver = inspect_fake(fakehost, monkeypatch, SSH_AUTH_SOCK="/tmp/agent.sock",
                                       SBX_CRED_X="y", DOCKER_SANDBOXES_API="http://evil")
    sandbox.default_runner(which, environ=env, probes=pr, verified=ver)
    seen = (fakehost.root / "fake" / "env.last").read_text()
    assert "SSH_AUTH_SOCK" not in seen and "SBX_CRED_X" not in seen and "DOCKER_SANDBOXES_API" not in seen
    assert "FAKE_SBX_DIR=" in seen


# ── the settings parser, on a real capture ───────────────────────────────────────────

def test_the_parser_reads_a_real_v0_50_1_capture():
    got = sandbox.sbx_settings_observed(SETTINGS_DEFAULTS.read_text())
    assert got == {"ssh.agentForwardingEnabled": True, "browser.enabled": True}  # the defaults of a fresh install
    assert sandbox.sbx_unsafe_settings(got, {}) == ["ssh.agentForwardingEnabled", "browser.enabled"]
    assert sandbox.sbx_unsafe_settings(got, {ENV_ALLOW_HOST_SETTINGS: "1"}) == []
    assert sandbox.sbx_unsafe_settings(OFF, {}) == []


@pytest.mark.parametrize("text", ["not json", "{}", "[]", '[{"key":"browser.enabled","value":false}]',
                                  '[{"key":"browser.enabled","value":"false"},{"key":"ssh.agentForwardingEnabled","value":false}]',
                                  '[1, 2]'])
def test_the_parser_never_reads_a_missing_or_malformed_setting_as_off(text):
    with pytest.raises(ValueError):
        sandbox.sbx_settings_observed(text)


def test_an_unobserved_setting_counts_as_unsafe():
    assert sandbox.sbx_unsafe_settings({}, {}) == ["ssh.agentForwardingEnabled", "browser.enabled"]


# ── the real host, read-only ─────────────────────────────────────────────────────────

@pytest.mark.requires_sbx
@pytest.mark.skipif(shutil.which("sbx") is None, reason="sbx is not installed")
def test_real_sbx_version_probe_is_read_only_and_parses():
    v = sandbox.real_sbx_probes(os.environ).version(shutil.which("sbx"))
    assert v and v[0].isdigit()
    assert subprocess.run([shutil.which("sbx"), "version"], capture_output=True, text=True).returncode == 0


@pytest.mark.requires_sbx
@pytest.mark.skipif(shutil.which("sbx") is None, reason="sbx is not installed")
def test_this_hosts_real_selection_is_consistent_with_its_own_report(monkeypatch):
    """Whatever this host is, the choice and the report agree, and sbx is chosen only with both settings off (or the
    documented override). Starts no VM: only read-only commands, and only if the daemon already answers."""
    Docker(monkeypatch, ok=True)
    env = dict(os.environ)
    env.pop(ENV_SANDBOX, None)
    rep = sandbox.sandbox_report(environ=env)
    assert rep["runner"] in ("sbx", "rootless-docker") and rep["mode"] == "auto"
    if rep["runner"] == "sbx":
        assert rep["sbx"]["state"] == "usable-and-selected"
        assert host_ok(rep, env)
    else:
        assert rep["fallback_reason"] is not None and rep["sbx"]["codes"]


def host_ok(rep, env) -> bool:
    return sandbox.host_settings_allowed(env) or rep["sbx"]["settings"] == OFF


# ── the runner built carries the reason it is not sbx, so a result can say it ─────────

def test_a_docker_runner_built_because_sbx_is_unusable_carries_the_structured_reason(docker):
    r = sandbox.default_runner(which_with(True), environ={}, probes=probes(daemon=False))
    assert isinstance(r, DockerRunner)
    assert r.fallback_reason["code"] == "daemon-down" and r.fallback_reason["detail"] and r.fallback_reason["loud"]


def test_a_docker_runner_without_sbx_or_by_choice_says_why_but_quietly(docker):
    absent = sandbox.default_runner(which_with(False), environ={}).fallback_reason  # sbx simply absent: not a warning
    assert absent["code"] == "not-installed" and absent["loud"] is False
    forced = sandbox.default_runner(which_with(True), environ={ENV_SANDBOX: "docker"})
    assert forced.fallback_reason["code"] == "forced-docker" and forced.fallback_reason["loud"] is False
