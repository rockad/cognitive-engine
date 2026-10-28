"""DockerRunner against the REAL rootless Docker daemon on this host.

These run real containers. They do not skip: if the daemon is not rootless or the pinned
image is missing, the fixture raises and the tests fail, which is the correct signal that
the sandbox is unusable. See `require_rootless_docker` for the gate they all pass through.
"""
from __future__ import annotations

import subprocess
import time

import pytest

from cognitive_harness2 import sandbox
from cognitive_harness2.engine import Engine, Refused, Target
from cognitive_harness2.experience import ExperienceStore
from cognitive_harness2.quota import QuotaVerdict
from cognitive_harness2.sandbox import DockerRunner, SandboxSpec, SandboxUnavailable, require_rootless_docker

pytestmark = pytest.mark.requires_rootless_docker


@pytest.fixture(scope="module")
def runner() -> DockerRunner:
    return DockerRunner(require_rootless_docker())


def sh(runner, script, *, files=None, timeout_s=60):
    return runner.run(SandboxSpec(command=["sh", "-c", script], files=files or {}, timeout_s=timeout_s))


def sandbox_containers() -> list[str]:
    p = subprocess.run(["docker", "ps", "-a", "-q", "--filter", "label=cognitive-harness2.sandbox=1"],
                       capture_output=True, text=True, timeout=30)
    return p.stdout.split()


def test_the_host_daemon_is_rootless():
    out = subprocess.run(["docker", "info", "--format", "{{json .SecurityOptions}}"], capture_output=True,
                         text=True, timeout=30).stdout
    assert "name=rootless" in out
    assert require_rootless_docker().endswith("docker")


def test_stdout_stderr_and_exit_code_come_back(runner):
    r = sh(runner, "echo out; echo err >&2; exit 3")
    assert (r.exit_code, r.stdout, r.stderr, r.timed_out) == (3, "out\n", "err\n", False)


def test_the_candidate_is_staged_and_readable(runner):
    r = sh(runner, "cat /candidate/prompt; pwd", files={"prompt": "be kind"})
    assert r.exit_code == 0 and r.stdout == "be kind/candidate\n"


def test_the_candidate_cannot_be_modified_or_added_to(runner):
    r = sh(runner, "echo x > /candidate/prompt; echo x > /candidate/new", files={"prompt": "orig"})
    assert r.exit_code != 0 and "ead-only" in r.stderr
    assert sh(runner, "cat /candidate/prompt", files={"prompt": "orig"}).stdout == "orig"
    mount = sh(runner, "awk '$2 == \"/candidate\" {print $4}' /proc/mounts", files={"prompt": "orig"}).stdout
    assert mount.split(",")[0] == "ro"


def test_the_root_filesystem_is_read_only_and_only_tmpfs_scratch_is_writable(runner):
    # Not just file permissions: the root mount itself must be `ro` (the user is `nobody` anyway).
    root = sh(runner, "awk '$2 == \"/\" {print $4}' /proc/mounts").stdout
    assert root.split(",")[0] == "ro"
    assert sh(runner, "touch /etc/pwned").exit_code != 0
    assert sh(runner, "touch /usr/pwned").exit_code != 0
    r = sh(runner, "echo ok > /tmp/scratch && cat /tmp/scratch && mount | grep ' /tmp ' | grep -c tmpfs")
    assert r.exit_code == 0 and r.stdout.split() == ["ok", "1"]


def test_scratch_is_not_executable(runner):
    r = sh(runner, "cp /bin/true /tmp/t && /tmp/t")
    assert r.exit_code != 0


def test_the_network_is_really_unreachable(runner):
    only_lo = sh(runner, "tail -n +3 /proc/net/dev | cut -d: -f1 | tr -d ' '")
    assert only_lo.stdout.split() == ["lo"]
    for target in ("http://1.1.1.1", "http://example.com"):
        r = sh(runner, f"wget -T 3 -q -O- {target}", timeout_s=30)
        assert r.exit_code != 0 and r.stdout == ""
    assert sh(runner, "nc -z -w 2 1.1.1.1 53").exit_code != 0


def test_privileges_are_dropped(runner):
    r = sh(runner, "id -u; grep -E '^(CapEff|CapBnd|NoNewPrivs):' /proc/self/status")
    uid, *rest = r.stdout.splitlines()
    assert uid == "65534"
    assert [line.split()[-1] for line in rest] == ["0000000000000000", "0000000000000000", "1"]
    assert sh(runner, "mount -t tmpfs none /tmp").exit_code != 0


def test_resource_limits_are_applied(runner):
    r = sh(runner, "cat /sys/fs/cgroup/pids.max /sys/fs/cgroup/memory.max /sys/fs/cgroup/memory.swap.max")
    assert r.exit_code == 0
    assert r.stdout.split() == ["128", str(256 * 1024 * 1024), "0"]


def test_a_fork_bomb_is_contained_by_the_pids_limit():
    r = DockerRunner(require_rootless_docker(), pids_limit=16)
    out = sh(r, "for i in $(seq 1 200); do sleep 20 & done; echo reached-the-end")
    assert "can't fork" in out.stdout + out.stderr and "reached-the-end" not in out.stdout
    assert out.exit_code != 0


def test_neither_the_home_directory_nor_the_docker_socket_nor_host_env_is_visible(runner, monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS2_CANARY_SECRET", "s3cret")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-canary")
    r = sh(runner, "ls /home; ls /run /var/run 2>&1; find / -name 'docker.sock' 2>/dev/null; env")
    assert "s3cret" not in r.stdout and "sk-canary" not in r.stdout
    assert "docker.sock" not in r.stdout and "rockad" not in r.stdout
    assert "HOME=/tmp" in r.stdout


def test_a_timeout_kills_the_container_and_leaves_none_behind(runner):
    before = set(sandbox_containers())
    t0 = time.monotonic()
    r = runner.run(SandboxSpec(command=["sh", "-c", "echo started; sleep 300"], timeout_s=3))
    elapsed = time.monotonic() - t0
    assert r.timed_out and r.exit_code is None and r.stderr == "timed out"
    assert "started" in r.stdout
    assert elapsed < 30, f"the container outlived its timeout: {elapsed:.1f}s"
    assert set(sandbox_containers()) == before


def test_no_container_or_staging_directory_is_left_after_a_normal_run(runner):
    before = set(sandbox_containers())
    sh(runner, "true", files={"prompt": "x"})
    assert set(sandbox_containers()) == before


def test_output_is_capped_so_a_noisy_candidate_cannot_exhaust_memory():
    r = DockerRunner(require_rootless_docker(), max_output_bytes=1000)
    out = sh(r, "head -c 3000000 /dev/zero | tr '\\0' x")
    assert out.exit_code == 0 and len(out.stdout) < 1200 and "output truncated" in out.stdout


# ── fail closed with a daemon that cannot be used ────────────────────────────────

def test_a_stopped_or_unreachable_daemon_fails_closed(monkeypatch):
    # An invalid DOCKER_HOST stands in for a stopped daemon; the real one is left alone.
    monkeypatch.setenv("DOCKER_HOST", "unix:///nonexistent/docker.sock")
    with pytest.raises(SandboxUnavailable, match="unreachable"):
        require_rootless_docker()


def test_the_default_runner_refuses_when_the_daemon_is_unreachable(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "unix:///nonexistent/docker.sock")
    monkeypatch.setattr(sandbox.shutil, "which", lambda n: None if n == "sbx" else "/usr/bin/docker")
    with pytest.raises(SandboxUnavailable, match="fallback is unusable"):
        sandbox.default_runner(lambda n: None if n == "sbx" else "/usr/bin/docker")
    assert sandbox.sandbox_status(lambda n: None if n == "sbx" else "/usr/bin/docker").startswith("unavailable")


def test_an_existing_runner_refuses_to_run_once_the_daemon_is_gone(runner, monkeypatch):
    before = set(sandbox_containers())
    monkeypatch.setenv("DOCKER_HOST", "unix:///nonexistent/docker.sock")
    with pytest.raises(SandboxUnavailable):
        runner.run(SandboxSpec(command=["echo", "must not run"], files={"prompt": "x"}))
    monkeypatch.undo()
    assert set(sandbox_containers()) == before


def test_a_tcp_docker_host_is_refused_without_contacting_it(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "tcp://127.0.0.1:1")
    t0 = time.monotonic()
    with pytest.raises(SandboxUnavailable, match="local unix socket"):
        require_rootless_docker()
    assert time.monotonic() - t0 < 10


# ── through the engine ───────────────────────────────────────────────────────────

class Gen:
    async def generate(self, prompt, *, model):
        return "mutated prompt text"


async def q():
    return QuotaVerdict(True, "ok")


async def test_the_engine_probes_a_candidate_in_a_real_container(tmp_path, monkeypatch):
    # No sbx on this host; make that explicit so the test exercises the Docker fallback.
    monkeypatch.setattr(sandbox.shutil, "which", lambda n: None if n == "sbx" else "/usr/bin/docker")
    eng = Engine(generator=Gen(), sandbox_factory=sandbox.default_runner, store=ExperienceStore(tmp_path),
                 quota_check=q, base_url="http://127.0.0.1:11434/v1", model="m")
    run_id = (await eng.propose(task="t", targets=[Target("prompt", "orig")], n=1, zdr="strict",
                                classification="unstated", dry_run=False))["run_id"]
    out = eng.probe(run_id=run_id, command=["cat", "/candidate/prompt"], timeout_s=60, allow_worktree_diff=False)
    assert out["mode"] == "sandbox"
    (res,) = out["results"]
    assert res["exit_code"] == 0 and res["stdout"] == "mutated prompt text"


async def test_the_engine_fails_closed_on_an_unreachable_daemon_even_with_the_diff_opt_out_unset(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "unix:///nonexistent/docker.sock")
    which = lambda n: None if n == "sbx" else "/usr/bin/docker"  # noqa: E731
    eng = Engine(generator=Gen(), sandbox_factory=lambda: sandbox.default_runner(which),
                 store=ExperienceStore(tmp_path), quota_check=q, base_url="http://127.0.0.1:11434/v1", model="m")
    run_id = (await eng.propose(task="t", targets=[Target("prompt", "orig")], n=1, zdr="strict",
                                classification="unstated", dry_run=False))["run_id"]
    with pytest.raises(Refused, match="fail closed|unusable"):
        eng.probe(run_id=run_id, command=["echo", "x"], timeout_s=5, allow_worktree_diff=False)


def test_status_reports_rootless_docker_when_sbx_is_absent():
    assert sandbox.sandbox_status(lambda n: None if n == "sbx" else "/usr/bin/docker") == "rootless-docker"
