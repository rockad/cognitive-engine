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
    VerifiedDocker,
    require_rootless_docker,
    verify_rootless_docker,
)

ROOTLESS = ["name=seccomp,profile=builtin", "name=rootless", "name=cgroupns"]
ROOTFUL = ["name=seccomp,profile=builtin", "name=cgroupns"]
SOCK = "unix:///run/user/1000/docker.sock"
OTHER = "unix:///tmp/other-daemon.sock"
UNIX = {"DOCKER_HOST": SOCK}
which_docker = lambda _: "/usr/bin/docker"  # noqa: E731


def fake_docker(*, options=ROOTLESS, info_rc=0, info_out=None, info_err="", context="unix:///run/user/1000/docker.sock",
                image_rc=0, info_timeout=False, calls=None, contexts=None):
    """A stand-in for subprocess.run that answers the three probes the gate makes.

    `contexts`, when given, is a list of answers to successive `context inspect` calls (a context
    that changes under the gate's feet); the last one repeats. Every other call must be pinned
    with `-H <host>` and is recorded with the `-H <host>` stripped as `sub`.
    """
    asked = []

    def run(argv, **kw):
        if calls is not None:
            calls.append(argv)
        sub = argv[1:]
        if sub[:2] == ["context", "inspect"]:
            asked.append(1)
            answer = context if contexts is None else contexts[min(len(asked), len(contexts)) - 1]
            return subprocess.CompletedProcess(argv, 0, answer + "\n", "")
        if sub[0] != "-H":
            raise AssertionError(f"docker call not pinned to the verified host: {argv}")
        sub = sub[2:]
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
    assert not any("info" in c for c in calls)


def test_the_active_context_is_used_when_docker_host_is_unset():
    assert gate(environ={}) == "/usr/bin/docker"
    with pytest.raises(SandboxUnavailable, match="local unix socket"):
        gate(environ={}, context="tcp://remote:2376")


def test_the_pinned_image_is_a_tag_plus_digest():
    assert DEFAULT_IMAGE.startswith("alpine:3@sha256:") and len(DEFAULT_IMAGE.split(":")[-1]) == 64


# ── runtime selection ────────────────────────────────────────────────────────────

def test_sbx_on_path_no_longer_wins(monkeypatch):
    """Was `test_sbx_wins_when_installed`. Selection is now opt-in + verified + usable: tests/test_sbx_selection.py."""
    monkeypatch.setattr(sandbox, "verify_rootless_docker", lambda which=None, **k: VerifiedDocker("/usr/bin/docker", SOCK))
    r = sandbox.default_runner(lambda n: f"/x/{n}", environ={})
    assert isinstance(r, DockerRunner) and not isinstance(r, SbxRunner)


def test_docker_is_the_fallback_when_sbx_is_absent(monkeypatch):
    monkeypatch.setattr(sandbox, "verify_rootless_docker", lambda which=None, **k: VerifiedDocker("/usr/bin/docker", SOCK))
    r = sandbox.default_runner(lambda n: None if n == "sbx" else f"/usr/bin/{n}")
    assert isinstance(r, DockerRunner) and r.docker_path == "/usr/bin/docker"
    assert r.docker_host == SOCK  # the verified endpoint travels with the runner


def test_neither_runtime_fails_closed_and_names_both(monkeypatch):
    def boom(*a, **k):
        raise SandboxUnavailable("daemon is not rootless")
    monkeypatch.setattr(sandbox, "verify_rootless_docker", boom)
    with pytest.raises(SandboxUnavailable, match="`sbx`.*not installed.*Docker fallback.*not rootless"):
        sandbox.default_runner(lambda _: None)


def test_status_reports_the_runtime_or_the_refusal(monkeypatch):
    monkeypatch.setattr(sandbox, "verify_rootless_docker", lambda *a, **k: VerifiedDocker("/usr/bin/docker", SOCK))
    assert sandbox.sandbox_status(lambda n: None if n == "sbx" else "/d") == "rootless-docker"
    monkeypatch.setattr(sandbox, "verify_rootless_docker",
                        lambda *a, **k: (_ for _ in ()).throw(SandboxUnavailable("nope")))
    assert sandbox.sandbox_status(lambda _: None).startswith("unavailable (fail closed)")


# ── argv construction ────────────────────────────────────────────────────────────

def argv(command=("echo", "hi"), **kw):
    return DockerRunner("/usr/bin/docker", **kw)._argv(SandboxSpec(command=list(command)), Path("/tmp/stage"), "h2-x", SOCK)


def pair(a, flag):
    return a[a.index(flag) + 1]


def test_argv_carries_every_required_hardening_flag():
    a = argv()
    assert a[:5] == ["/usr/bin/docker", "-H", SOCK, "run", "--rm"]  # pinned to the verified daemon
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
    assert "/var/run" not in text
    assert text.count("docker.sock") == 1 and f"-H {SOCK} run" in text  # only as the pinned daemon endpoint
    assert "dst=/run" not in text and "src=/run" not in text   # never mounted into the container
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
    monkeypatch.setattr(sandbox, "verify_rootless_docker",
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


# ── TOCTOU: the verified daemon is pinned on every docker invocation ──────────────

def test_the_gate_returns_the_verified_endpoint_and_pins_its_own_probes_to_it():
    calls: list = []
    v = verify_rootless_docker(which_docker, run=fake_docker(calls=calls), environ=UNIX)
    assert v == VerifiedDocker("/usr/bin/docker", SOCK)
    assert [c[3] for c in calls] == ["info", "image"]
    assert all(c[:3] == ["/usr/bin/docker", "-H", SOCK] for c in calls)


def test_the_context_is_read_once_and_the_answer_it_gave_is_the_one_used_everywhere():
    calls: list = []
    v = verify_rootless_docker(which_docker, run=fake_docker(calls=calls, contexts=[SOCK, OTHER]), environ={})
    assert v.host == SOCK
    assert sum(c[1] == "context" for c in calls) == 1
    assert all(c[1:3] == ["-H", SOCK] for c in calls if c[1] != "context")
    assert not any(OTHER in c for c in calls)


def test_a_caller_supplied_host_skips_the_ambient_environment_and_context_entirely():
    calls: list = []
    v = verify_rootless_docker(which_docker, run=fake_docker(calls=calls), environ={"DOCKER_HOST": "tcp://evil:1"},
                               host=SOCK)
    assert v.host == SOCK and not any(c[1] == "context" for c in calls)


@pytest.mark.parametrize("host", ["tcp://127.0.0.1:2375", "ssh://me@h", "", "bogus", "unix:/x", "UNIX:///x"])
def test_a_pinned_non_unix_host_is_refused(host):
    with pytest.raises(SandboxUnavailable, match="local unix socket"):
        verify_rootless_docker(which_docker, run=fake_docker(), environ=UNIX, host=host)
    with pytest.raises(SandboxUnavailable, match="local unix socket"):
        DockerRunner("/usr/bin/docker", docker_host=host)


class Recorder:
    """Stands in for subprocess.run and subprocess.Popen; records (argv, env) for every docker call."""

    def __init__(self, monkeypatch, *, context=SOCK, hang_first=False):
        import io
        self.calls: list[tuple[list[str], dict | None]] = []
        self.hang_first = hang_first
        inner = fake_docker(context=context)

        def run(argv, **kw):
            self.calls.append((list(argv), kw.get("env")))
            verb = argv[argv.index("-H") + 2] if "-H" in argv else argv[1]
            if verb in ("kill", "rm"):
                return subprocess.CompletedProcess(argv, 0, b"", b"")
            return inner(argv, **kw)

        rec = self

        class Popen:
            def __init__(self, argv, **kw):
                rec.calls.append((list(argv), kw.get("env")))
                self.stdout, self.stderr, self.waits = io.BytesIO(b"out"), io.BytesIO(b""), 0

            def wait(self, timeout=None):
                self.waits += 1
                if rec.hang_first and self.waits == 1:
                    raise subprocess.TimeoutExpired("docker", timeout)
                return 0

            def poll(self):
                return 0

            def kill(self):
                pass

        monkeypatch.setattr(subprocess, "run", run)
        monkeypatch.setattr(subprocess, "Popen", Popen)

    def verbs(self):
        return [a[a.index("-H") + 2] if "-H" in a else a[1] for a, _ in self.calls]


def assert_every_call_is_pinned(rec, host):
    assert rec.calls
    for argv, env in rec.calls:
        if argv[1] == "context":
            continue
        assert argv[1:3] == ["-H", host], argv
        assert env is not None and "DOCKER_HOST" not in env and "DOCKER_CONTEXT" not in env, (argv, env)


def hostile(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "tcp://attacker.example:2375")
    monkeypatch.setenv("DOCKER_CONTEXT", "remote")


def test_every_docker_call_a_run_makes_names_the_pinned_daemon(monkeypatch):
    rec = Recorder(monkeypatch)
    hostile(monkeypatch)  # switched after the runner was verified and built
    res = DockerRunner("/usr/bin/docker", docker_host=SOCK).run(SandboxSpec(command=["echo"], files={"prompt": "x"}))
    assert res.exit_code == 0
    assert {"info", "image", "run", "kill", "rm"} <= set(rec.verbs())
    assert "context" not in rec.verbs()   # a pinned runner never consults the ambient context
    assert_every_call_is_pinned(rec, SOCK)
    assert not any("attacker.example" in " ".join(a) for a, _ in rec.calls)


DOCKER_VARS = {
    "DOCKER_HOST": "tcp://attacker.example:2375", "DOCKER_CONTEXT": "remote", "DOCKER_CONFIG": "",
    "DOCKER_CERT_PATH": "/tmp/certs", "DOCKER_TLS_VERIFY": "1", "DOCKER_API_VERSION": "1.40",
    "DOCKER_CONTENT_TRUST": "1", "DOCKER_DEFAULT_PLATFORM": "linux/s390x", "DOCKER_BUILDKIT": "0",
    "DOCKER_HIDE_LEGACY_COMMANDS": "1", "DOCKER_SOMETHING_NEW": "x",
}


def _docker_config_dir(tmp_path):
    d = tmp_path / "dockercfg"
    d.mkdir()
    (d / "config.json").write_text(json.dumps({
        "currentContext": "remote", "credsStore": "evil-helper",
        "proxies": {"default": {"httpProxy": "http://attacker.example:3128"}},
    }))
    return d


def test_no_docker_variable_reaches_any_docker_child(monkeypatch, tmp_path):
    """DOCKER_CONFIG (currentContext, credsStore, proxies) and every other DOCKER_* is dropped, not just HOST/CONTEXT."""
    rec = Recorder(monkeypatch)
    for k, v in DOCKER_VARS.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("DOCKER_CONFIG", str(_docker_config_dir(tmp_path)))
    monkeypatch.setenv("docker_lower", "x")   # env names are case-sensitive; strip look-alikes too
    monkeypatch.setenv("KEEP_ME", "1")
    DockerRunner("/usr/bin/docker", docker_host=SOCK).run(SandboxSpec(command=["echo"], files={"prompt": "x"}))
    assert {"info", "image", "run", "kill", "rm"} <= set(rec.verbs())
    for argv, env in rec.calls:
        assert env is not None, argv
        assert not [k for k in env if k.upper().startswith("DOCKER_")], (argv, sorted(k for k in env if "OCKER" in k.upper()))
        assert env["KEEP_ME"] == "1"   # only DOCKER_* goes
    assert "dockercfg" not in " ".join(" ".join(a) for a, _ in rec.calls)


def test_the_gate_probes_carry_no_docker_variable_either(monkeypatch, tmp_path):
    rec = Recorder(monkeypatch)
    for k, v in DOCKER_VARS.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("DOCKER_CONFIG", str(_docker_config_dir(tmp_path)))
    sandbox.verify_rootless_docker(which_docker, run=subprocess.run, environ={"DOCKER_HOST": SOCK})
    assert rec.calls
    for argv, env in rec.calls:
        assert env is not None and not [k for k in env if k.upper().startswith("DOCKER_")], argv


def test_the_kill_and_rm_after_a_timeout_are_pinned_too(monkeypatch):
    rec = Recorder(monkeypatch, hang_first=True)
    hostile(monkeypatch)
    res = DockerRunner("/usr/bin/docker", docker_host=SOCK).run(SandboxSpec(command=["sleep", "9"], timeout_s=1))
    assert res.timed_out
    assert rec.verbs().count("kill") >= 1 and rec.verbs().count("rm") >= 1
    assert_every_call_is_pinned(rec, SOCK)


def test_an_unpinned_runner_resolves_the_endpoint_once_per_run_then_pins_all_of_it(monkeypatch):
    rec = Recorder(monkeypatch)
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setenv("DOCKER_CONTEXT", "rootless")
    DockerRunner("/usr/bin/docker").run(SandboxSpec(command=["echo"], files={"prompt": "x"}))
    assert rec.verbs().count("context") == 1
    assert_every_call_is_pinned(rec, SOCK)


def test_a_pinned_runner_still_refuses_when_its_own_daemon_stops_being_rootless(monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("a container must not start"))
    monkeypatch.setattr(subprocess, "run", fake_docker(options=ROOTFUL))
    with pytest.raises(SandboxUnavailable, match="rootful"):
        DockerRunner("/usr/bin/docker", docker_host=SOCK).run(SandboxSpec(command=["echo"]))


def test_the_default_runner_carries_the_verified_host(monkeypatch):
    monkeypatch.setattr(sandbox, "verify_rootless_docker", lambda *a, **k: VerifiedDocker("/usr/bin/docker", SOCK))
    r = sandbox.default_runner(lambda n: None if n == "sbx" else "/usr/bin/docker")
    assert isinstance(r, DockerRunner) and r.docker_host == SOCK


# ── staging keys must already be clean relative paths ─────────────────────────────

CLEAN = ["payload.py", "a/b.txt", "deep/er/x_y-z.py", ".hidden", "a..b", "a.b/c.d", "Prompt.TXT", "ünï/ĉode.py",
         "has space/in name.txt"]

BAD_KEYS = {
    "leading ./": "./payload.py",
    "doubled slash": "a//b",
    "inner dot segment": "a/./b",
    "dot only": ".",
    "dotdot only": "..",
    "dotdot prefix": "../x",
    "dotdot inner": "a/../b",       # would normalise to a clean name; still refused as written
    "dotdot trailing": "a/..",
    "absolute": "/etc/passwd",
    "double slash absolute": "//x",
    "backslash": "a\\b",
    "backslash leading": "\\x",
    "empty": "",
    "NUL": "a\0b",
    "NUL trailing": "x\0",
    "trailing slash": "dir/",
    "trailing slash on a file": "payload.py/",
    "newline": "a\nb",
    "tab": "a\tb",
    "carriage return": "a\rb",
    "escape char": "a\x1bb",
    "DEL": "a\x7fb",
    "zero width space": "a\u200bb",
    "bidi override": "a\u202eb",
    "leading space": " payload.py",
    "trailing space": "payload.py ",
    "padded segment": "a/ b/c",
    "drive letter": "c:/x",
}


@pytest.mark.parametrize("key", sorted(BAD_KEYS.values()), ids=sorted(BAD_KEYS, key=lambda k: BAD_KEYS[k]))
def test_an_unclean_staging_key_is_refused_before_anything_is_staged(key, tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    spec = SandboxSpec(command=["x"], files={"fine.txt": "ok", key: "bad"})   # a good key must not be staged either
    with pytest.raises((ValueError, InvariantViolation)):
        DockerRunner("/usr/bin/docker", docker_host=SOCK)._stage(spec)
    assert list(tmp_path.iterdir()) == []                                     # no stage dir was even created


def test_only_the_exact_root_guardrails_file_name_may_be_staged_among_protected_names(tmp_path, monkeypatch):
    # The surface's own guardrails.md is stageable (the agent runs with its guardrails); no variant is.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    DockerRunner("/usr/bin/docker", docker_host=SOCK)._stage(SandboxSpec(command=["x"], files={"guardrails.md": "Never delete.\n"}))
    for variant in ("GUARDRAILS.md", "Guardrails.MD", "guardrails.md.", "guardrails.MD", "sub/guardrails.md",
                    "a/b/guardrails.md", "guardrails/rules.md", "guardrails.json", "guardrails.md.bak",
                    "guardrails․md", "ｇuardrails.md", "hooks/guardrails.md"):
        with pytest.raises((ValueError, InvariantViolation)):
            DockerRunner("/usr/bin/docker", docker_host=SOCK)._stage(SandboxSpec(command=["x"], files={variant: "x"}))


@pytest.mark.parametrize("key", sorted(BAD_KEYS.values()), ids=sorted(BAD_KEYS, key=lambda k: BAD_KEYS[k]))
def test_run_validates_keys_before_it_touches_docker(key, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("docker must not be contacted"))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("a container must not start"))
    monkeypatch.setattr(tempfile, "mkdtemp", lambda *a, **k: pytest.fail("nothing may be staged"))
    with pytest.raises((ValueError, InvariantViolation)):
        DockerRunner("/usr/bin/docker", docker_host=SOCK).run(SandboxSpec(command=["x"], files={key: "bad"}))


@pytest.mark.parametrize("key", CLEAN)
def test_clean_relative_keys_are_staged_under_exactly_the_name_given(key):
    r = DockerRunner("/usr/bin/docker", docker_host=SOCK)
    stage = r._stage(SandboxSpec(command=["x"], files={key: "content"}))
    try:
        assert (stage / key).read_text() == "content"   # not lower-cased, not normalised
    finally:
        sandbox.shutil.rmtree(stage)


@pytest.mark.parametrize("key", ["hooks", "pkg/guard/x.ts", "hooks/pre.sh", "my_egress_policy", "x/Hooks.JSON"])
def test_protected_keys_are_still_refused_and_nothing_is_left_behind(key, tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    with pytest.raises(InvariantViolation):
        DockerRunner("/usr/bin/docker", docker_host=SOCK)._stage(SandboxSpec(command=["x"], files={key: "x"}))
    assert list(tmp_path.iterdir()) == []


def test_a_non_string_key_is_refused():
    with pytest.raises(ValueError):
        DockerRunner("/usr/bin/docker", docker_host=SOCK)._stage(SandboxSpec(command=["x"], files={1: "x"}))  # type: ignore[dict-item]
