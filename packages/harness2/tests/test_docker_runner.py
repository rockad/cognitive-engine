"""DockerRunner and its fail-closed gate, with no Docker daemon involved.

Everything here injects a fake `docker` so each refusal path is exercised deterministically.
The real rootless daemon is exercised in test_docker_integration.py.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import pytest

from cognitive_harness2 import sandbox
from cognitive_harness2.invariants import InvariantViolation
from cognitive_harness2.sandbox import (
    DEFAULT_IMAGE,
    DockerRunner,
    SandboxSpec,
    SandboxUnavailable,
    SbxRunner,
    require_rootless_docker,
)

ROOTLESS = ["name=seccomp,profile=builtin", "name=rootless", "name=cgroupns"]
ROOTFUL = ["name=seccomp,profile=builtin", "name=cgroupns"]
UNIX = {"DOCKER_HOST": "unix:///run/user/1000/docker.sock"}
which_docker = lambda _: "/usr/bin/docker"  # noqa: E731


def fake_docker(*, options=ROOTLESS, info_rc=0, info_out=None, info_err="", context="unix:///run/user/1000/docker.sock",
                image_rc=0, info_timeout=False, calls=None):
    """A stand-in for subprocess.run that answers the three probes the gate makes."""
    def run(argv, **kw):
        if calls is not None:
            calls.append(argv)
        sub = argv[1:]
        if sub[:2] == ["context", "inspect"]:
            return subprocess.CompletedProcess(argv, 0, context + "\n", "")
        if sub[0] == "info":
            if info_timeout:
                raise subprocess.TimeoutExpired(argv, 15)
            out = json.dumps(options) if info_out is None else info_out
            return subprocess.CompletedProcess(argv, info_rc, out, info_err)
        if sub[:2] == ["image", "inspect"]:
            return subprocess.CompletedProcess(argv, image_rc, "sha256:x\n", "")
        raise AssertionError(f"unexpected docker call {argv}")
    return run


def gate(**kw):
    env = kw.pop("environ", UNIX)
    which = kw.pop("which", which_docker)
    return require_rootless_docker(which, run=fake_docker(**kw), environ=env)


# ── the fail-closed gate ─────────────────────────────────────────────────────────

def test_a_rootless_daemon_passes():
    assert gate() == "/usr/bin/docker"


def test_no_docker_binary_fails_closed():
    with pytest.raises(SandboxUnavailable, match="not installed"):
        gate(which=lambda _: None)


def test_a_rootful_daemon_is_refused():
    with pytest.raises(SandboxUnavailable, match="rootful"):
        gate(options=ROOTFUL)


@pytest.mark.parametrize("out", ["null", "", "not json", '"name=rootless"', "{}", '["name=rootlessish"]'])
def test_only_a_security_options_list_naming_rootless_passes(out):
    # `docker info` can exit 0 with `null` for an unreachable TCP daemon, so rc is never trusted.
    with pytest.raises(SandboxUnavailable, match="name=rootless"):
        gate(info_out=out)


def test_an_unreachable_daemon_is_refused():
    with pytest.raises(SandboxUnavailable, match="unreachable.*no such file"):
        gate(info_rc=1, info_out="", info_err="failed to connect: no such file or directory")


def test_a_hung_daemon_is_refused_not_waited_on_forever():
    with pytest.raises(SandboxUnavailable, match="timed out"):
        gate(info_timeout=True)


def test_a_missing_pinned_image_is_refused_and_never_pulled():
    with pytest.raises(SandboxUnavailable, match="never pulled"):
        gate(image_rc=1)


@pytest.mark.parametrize("host", ["tcp://127.0.0.1:2375", "tcp://build.example:2376", "ssh://me@host", "bogus", ""])
def test_a_non_unix_endpoint_is_refused_before_any_daemon_call(host):
    calls: list = []
    with pytest.raises(SandboxUnavailable, match="local unix socket"):
        gate(environ={"DOCKER_HOST": host}, context=host, calls=calls)
    assert not any(c[1] == "info" for c in calls)


def test_the_active_context_is_used_when_docker_host_is_unset():
    assert gate(environ={}) == "/usr/bin/docker"
    with pytest.raises(SandboxUnavailable, match="local unix socket"):
        gate(environ={}, context="tcp://remote:2376")


def test_the_pinned_image_is_a_tag_plus_digest():
    assert DEFAULT_IMAGE.startswith("alpine:3@sha256:") and len(DEFAULT_IMAGE.split(":")[-1]) == 64


# ── runtime selection ────────────────────────────────────────────────────────────

def test_sbx_wins_when_installed(monkeypatch):
    monkeypatch.setattr(sandbox, "require_rootless_docker", lambda *a, **k: pytest.fail("docker must not be probed"))
    r = sandbox.default_runner(lambda n: f"/x/{n}")
    assert isinstance(r, SbxRunner)


def test_docker_is_the_fallback_when_sbx_is_absent(monkeypatch):
    monkeypatch.setattr(sandbox, "require_rootless_docker", lambda which=None, **k: "/usr/bin/docker")
    r = sandbox.default_runner(lambda n: None if n == "sbx" else f"/usr/bin/{n}")
    assert isinstance(r, DockerRunner) and r.docker_path == "/usr/bin/docker"


def test_neither_runtime_fails_closed_and_names_both(monkeypatch):
    def boom(*a, **k):
        raise SandboxUnavailable("daemon is not rootless")
    monkeypatch.setattr(sandbox, "require_rootless_docker", boom)
    with pytest.raises(SandboxUnavailable, match="`sbx`.*not installed.*Docker fallback.*not rootless"):
        sandbox.default_runner(lambda _: None)


def test_status_reports_the_runtime_or_the_refusal(monkeypatch):
    monkeypatch.setattr(sandbox, "require_rootless_docker", lambda *a, **k: "/usr/bin/docker")
    assert sandbox.sandbox_status(lambda n: None if n == "sbx" else "/d") == "rootless-docker"
    assert sandbox.sandbox_status(lambda n: "/x/sbx") == "sbx"
    monkeypatch.setattr(sandbox, "require_rootless_docker",
                        lambda *a, **k: (_ for _ in ()).throw(SandboxUnavailable("nope")))
    assert sandbox.sandbox_status(lambda _: None).startswith("unavailable (fail closed)")


# ── argv construction ────────────────────────────────────────────────────────────

def argv(command=("echo", "hi"), **kw):
    return DockerRunner("/usr/bin/docker", **kw)._argv(SandboxSpec(command=list(command)), Path("/tmp/stage"), "h2-x")


def pair(a, flag):
    return a[a.index(flag) + 1]


def test_argv_carries_every_required_hardening_flag():
    a = argv()
    assert a[:3] == ["/usr/bin/docker", "run", "--rm"]
    assert pair(a, "--network") == "none"
    assert "--read-only" in a
    assert pair(a, "--cap-drop") == "ALL"
    assert pair(a, "--security-opt") == "no-new-privileges"
    assert pair(a, "--pids-limit") == "128"
    assert pair(a, "--memory") == "256m" and pair(a, "--memory-swap") == "256m"
    assert pair(a, "--cpus") == "1"
    assert pair(a, "--user") == "65534:65534"
    assert pair(a, "--pull") == "never"
    assert pair(a, "--tmpfs").startswith("/tmp:rw,noexec,nosuid,size=")


def test_argv_stages_the_candidate_read_only_and_nothing_else():
    a = argv()
    mounts = [a[i + 1] for i, x in enumerate(a) if x == "--mount"]
    assert mounts == ["type=bind,src=/tmp/stage,dst=/candidate,readonly"]
    assert "-v" not in a and "--volume" not in a and "--volumes-from" not in a


def test_argv_never_exposes_the_home_directory_the_docker_socket_or_the_host_environment():
    text = " ".join(argv())
    assert str(Path.home()) not in text
    assert "docker.sock" not in text and "/var/run" not in text
    toks = argv()
    for banned in ("--privileged", "--env-file", "--pid", "--cap-add", "--device", "--userns", "--ipc", "--uts"):
        assert banned not in toks
    assert not [t for t in toks if t.startswith(("--net=", "--network=", "--pid=", "--cap-add=", "--privileged="))]
    assert "seccomp=unconfined" not in text and pair(toks, "--network") == "none"
    assert [x for x in toks if x == "-e"] == ["-e"] and "HOME=/tmp" in toks


def test_argv_puts_the_command_after_the_image_verbatim():
    a = argv(("sh", "-c", "--help; echo $X"))
    assert a[-4:] == [DEFAULT_IMAGE, "sh", "-c", "--help; echo $X"]


def test_limits_are_configurable_but_the_isolation_flags_are_not():
    a = argv(pids_limit=7, memory="64m", cpus="0.5")
    assert pair(a, "--pids-limit") == "7" and pair(a, "--memory") == "64m" and pair(a, "--cpus") == "0.5"
    assert pair(a, "--network") == "none" and "--read-only" in a and pair(a, "--cap-drop") == "ALL"


# ── staging ──────────────────────────────────────────────────────────────────────

def test_staging_writes_read_only_files_and_a_traversable_directory():
    r = DockerRunner("/usr/bin/docker")
    stage = r._stage(SandboxSpec(command=["x"], files={"prompt": "hello", "a/b.txt": "nested"}))
    try:
        assert (stage / "prompt").read_text() == "hello" and (stage / "a" / "b.txt").read_text() == "nested"
        assert (stage / "prompt").stat().st_mode & 0o777 == 0o444
        assert stage.stat().st_mode & 0o777 == 0o755
    finally:
        sandbox.shutil.rmtree(stage)


@pytest.mark.parametrize("name", ["../escape", "/etc/passwd", "a/../../b", "", "hooks/pre.sh", "my_egress_policy",
                                  "nul\0byte"])
def test_unsafe_or_protected_staged_names_are_refused_and_nothing_is_left_behind(name, tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    with pytest.raises((ValueError, InvariantViolation)):
        DockerRunner("/usr/bin/docker")._stage(SandboxSpec(command=["x"], files={name: "x"}))
    assert list(tmp_path.iterdir()) == []


def test_staging_under_the_home_directory_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(SandboxUnavailable, match="home directory"):
        DockerRunner("/usr/bin/docker")._stage(SandboxSpec(command=["x"], files={"prompt": "x"}))
    assert list(tmp_path.iterdir()) == []


# ── run() refuses before it executes anything ────────────────────────────────────

def test_run_rechecks_the_daemon_and_never_starts_a_container_when_it_is_not_rootless(monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("a container must not start"))
    monkeypatch.setattr(sandbox, "require_rootless_docker",
                        lambda *a, **k: (_ for _ in ()).throw(SandboxUnavailable("daemon went away")))
    with pytest.raises(SandboxUnavailable, match="daemon went away"):
        DockerRunner("/usr/bin/docker").run(SandboxSpec(command=["echo"], files={"prompt": "x"}))


def test_an_empty_command_is_rejected():
    with pytest.raises(ValueError, match="empty"):
        DockerRunner("/usr/bin/docker").run(SandboxSpec(command=[]))


async def test_a_runtime_that_vanishes_mid_probe_is_refused_not_degraded(tmp_path):
    from cognitive_harness2.engine import Engine, Refused, Target
    from cognitive_harness2.experience import ExperienceStore
    from cognitive_harness2.quota import QuotaVerdict

    class Gone:
        def run(self, spec):
            raise SandboxUnavailable("daemon went away (fail closed)")

    class Gen:
        async def generate(self, prompt, *, model):
            return "x"

    async def q():
        return QuotaVerdict(True, "ok")

    eng = Engine(generator=Gen(), sandbox_factory=Gone, store=ExperienceStore(tmp_path), quota_check=q,
                 base_url="http://127.0.0.1:11434/v1", model="m")
    run_id = (await eng.propose(task="t", targets=[Target("prompt", "o")], n=1, zdr="strict",
                                classification="unstated", dry_run=False))["run_id"]
    with pytest.raises(Refused, match="daemon went away"):
        eng.probe(run_id=run_id, command=["x"], timeout_s=5, allow_worktree_diff=True)
