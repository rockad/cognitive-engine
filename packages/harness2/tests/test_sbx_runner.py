"""`SbxRunner` against a FAKE `sbx` (tests/fixtures/fake-sbx/sbx), plus one opt-in real-sbx round trip.

The fake emulates only what the runner calls, with the outputs and exit codes observed on a real v0.50.1, and records
every call. These tests prove the runner's argv, its refusal paths (each one BEFORE anything is created), its
cleanup guarantee (`rm --force` in a `finally`, whatever failed) and its wall clock. They do NOT prove that a real
microVM behaves the same: that is the `requires_sbx_live` test, skipped unless COGNITIVE_HARNESS2_SBX_LIVE=1.
No Docker, no model, no network.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from cognitive_harness2 import sandbox
from cognitive_harness2.invariants import InvariantViolation
from cognitive_harness2.sandbox import SandboxSpec, SandboxUnavailable, SbxRunner

HERE = Path(__file__).parent
FAKE_DIR = HERE / "fixtures" / "fake-sbx"
DENY_ALL = HERE / "fixtures" / "sbx_policy_ls_deny_all.json"  # captured from a real v0.50.1 host
VERSION = "9.9.9"


class World:
    def __init__(self, root: Path, state: Path, sock: socket.socket):
        self.root, self.state, self.sock = root, state, sock
        self.fake = root / "fake"
        self.fake.mkdir()
        self.env = {"PATH": f"{FAKE_DIR}:{os.environ['PATH']}", "XDG_STATE_HOME": str(state),
                    "FAKE_SBX_DIR": str(self.fake), "FAKE_SBX_POLICY_FILE": str(DENY_ALL)}

    def runner(self, **kw) -> SbxRunner:
        kw.setdefault("verified", frozenset({VERSION}))
        env = {**self.env, **kw.pop("env", {})}
        return SbxRunner(str(FAKE_DIR / "sbx"), environ=env, **kw)

    def calls(self) -> list[list[str]]:
        f = self.fake / "calls"
        return [line.split("\x1f")[:-1] for line in f.read_text().splitlines()] if f.exists() else []

    def verbs(self) -> list[str]:
        return [c[0] if c[0] != "policy" else f"policy {c[1]}" for c in self.calls()]

    def listed(self) -> list[str]:
        return (self.fake / "sandboxes").read_text().split()


@pytest.fixture
def world():
    # AF_UNIX paths are short: a pytest tmp_path can exceed the 107-byte limit.
    root = Path(tempfile.mkdtemp(prefix="h2sbx"))
    state = root / "state"
    sd = state / "sandboxes" / "sandboxes" / "sandboxd"
    sd.mkdir(parents=True)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(sd / "sandboxd.sock"))
    try:
        yield World(root, state, s)
    finally:
        s.close()
        shutil.rmtree(root, ignore_errors=True)


def spec(*command, files=None, timeout_s=30):
    return SandboxSpec(command=list(command) or ["echo", "hi"], files=files or {}, timeout_s=timeout_s)


# ── the happy path and the exact command lines ─────────────────────────────────────

def test_the_whole_flow_and_every_argv(world):
    r = world.runner(memory="768m", cpus=2).run(spec("python3", "-c", "-x", "print(1)", files={"a.py": "x = 1\n", "d/b.py": "y = 2\n"}))
    assert r.exit_code == 0 and not r.timed_out
    calls = world.calls()
    assert world.verbs() == ["version", "policy ls", "create", "ls", *["policy check"] * 4,
                             "cp", "exec", "exec", "rm", "ls"]
    name = calls[2][3]
    assert name.startswith("harness2-") and len(name) == len("harness2-") + 12
    sbx = str(FAKE_DIR / "sbx")
    assert calls[0] == ["version"] and calls[1] == ["policy", "ls", "--json"]
    # NO workspace path after the agent: no host bind mount, ever.
    assert calls[2] == ["create", "shell", "--name", name, "--no-progress", "--cpus", "2", "--memory", "768m",
                        "--pull", "missing", "--skills", "off"]
    assert calls[3] == ["ls", "-q"]
    assert len(sandbox.SBX_DENIED_TARGETS) == 4
    for c, t in zip(calls[4:8], sandbox.SBX_DENIED_TARGETS):
        assert c == ["policy", "check", "network", "--sandbox", name, "--json", t]
    assert calls[8][0] == "cp" and calls[8][1].endswith("/candidate") and calls[8][2] == f"{name}:/tmp"
    assert calls[9] == ["exec", "--", name, "find", "/tmp/candidate", "-type", "f"]
    # `--` precedes the sandbox name, so a flag-looking candidate argument cannot be read by sbx.
    assert calls[10] == ["exec", "-w", "/tmp/candidate", "--", name, "python3", "-c", "-x", "print(1)"]
    assert calls[11] == ["rm", "--force", name] and calls[12] == ["ls", "-q"]
    assert not any(c[0] in ("run", "ssh", "attach") for c in calls) and sbx
    assert world.listed() == []  # nothing left behind


def test_no_files_means_no_cp_and_the_scratch_workdir(world):
    world.runner().run(spec("true"))
    assert "cp" not in world.verbs()
    assert [c for c in world.calls() if c[0] == "exec"] == [["exec", "-w", "/tmp", "--", world.calls()[2][3], "true"]]


def test_output_and_exit_code_come_back(world):
    r = world.runner(env={"FAKE_SBX_EXEC_RC": "3", "FAKE_SBX_EXEC_OUT": "out\n", "FAKE_SBX_EXEC_ERR": "err\n"}).run(spec())
    assert (r.exit_code, r.stdout, r.stderr, r.timed_out) == (3, "out\n", "err\n", False)


def test_exit_127_is_the_commands_own_result_not_a_refusal(world):
    r = world.runner(env={"FAKE_SBX_EXEC_RC": "127", "FAKE_SBX_EXEC_ERR": "no such command\n"}).run(spec("nope"))
    assert r.exit_code == 127 and "no such command" in r.stderr
    assert world.listed() == []


def test_output_is_capped(world):
    big = "x" * 5000
    r = world.runner(max_output_bytes=100, env={"FAKE_SBX_EXEC_OUT": big}).run(spec())
    assert r.stdout.startswith("x" * 100) and "output truncated" in r.stdout and len(r.stdout) < 300


def test_the_env_that_reaches_sbx_has_nothing_that_can_repoint_it(world):
    world.runner(env={"DOCKER_SANDBOXES_API": "http://evil", "SBX_CRED_ANTHROPIC_MODE": "x", "sbx_lower": "ok",
                      "ANTHROPIC_API_KEY": "kept-but-never-forwarded-to-the-vm"}).run(spec())
    seen = (world.fake / "env.last").read_text()
    assert "DOCKER_SANDBOXES_API" not in seen and "SBX_CRED_ANTHROPIC_MODE" not in seen
    assert "XDG_STATE_HOME=" in seen and "FAKE_SBX_DIR=" in seen


def test_no_environment_is_ever_passed_into_the_vm(world):
    world.runner(env={"SECRET_TOKEN": "s3cret"}).run(spec("env"))
    assert not any(a in ("-e", "--env", "--env-file") or "s3cret" in a for c in world.calls() for a in c)


# ── refusals: each one BEFORE a sandbox exists ─────────────────────────────────────

def never_created(world):
    assert "create" not in world.verbs() and world.listed() == []


def test_an_unverified_version_is_refused(world):
    with pytest.raises(SandboxUnavailable, match="not in the verified set"):
        world.runner(verified=frozenset({"1.0.0"})).run(spec())
    never_created(world)


def test_the_empty_verified_set_refuses_everything(world):
    with pytest.raises(SandboxUnavailable, match="verified set .*empty"):
        world.runner(verified=frozenset()).run(spec())
    never_created(world)


def test_the_module_default_verified_set_is_what_a_bare_runner_uses(world, monkeypatch):
    monkeypatch.setattr(sandbox, "VERIFIED_SBX_VERSIONS", frozenset())
    with pytest.raises(SandboxUnavailable, match="not in the verified set"):
        SbxRunner(str(FAKE_DIR / "sbx"), environ=world.env).run(spec())
    never_created(world)


def test_an_unreadable_version_is_refused(world):
    with pytest.raises(SandboxUnavailable, match="could not read the sbx version"):
        world.runner(env={"FAKE_SBX_VERSION": "nightly"}).run(spec())
    never_created(world)


def write_policy(world, rules) -> dict:
    p = world.root / "policy.json"
    p.write_text(json.dumps({"rules": rules}) if not isinstance(rules, str) else rules)
    return {"FAKE_SBX_POLICY_FILE": str(p)}


DENY = {"id": "default-deny-all", "resource_type": "network", "decision": "deny", "resources": ["**"],
        "status": "active", "actions": ["net:connect:tcp", "net:connect:udp"]}
ALLOW = {"id": "r1", "name": "allow-pypi", "resource_type": "network", "decision": "allow",
         "resources": ["pypi.org"], "status": "active", "actions": ["net:connect:tcp"]}


@pytest.mark.parametrize("rules,why", [
    ([DENY, ALLOW], "allow rule exists"),
    ([DENY, {**ALLOW, "status": "inactive"}], "allow rule exists"),   # an inactive allow is still a refusal
    ([], "no active network deny-all"),
    ([{**DENY, "status": "inactive"}], "no active network deny-all"),
    ([{**DENY, "resources": ["example.com"]}], "no active network deny-all"),
    ([{**DENY, "actions": ["net:connect:tcp"]}], "no active network deny-all"),
    ([DENY, {**DENY, "decision": "prompt"}], "unknown decision"),
    ([DENY, "oops"], "malformed rule"),
    ([DENY, {"id": "x"}], "without a resource_type"),
    ("not json at all", "did not return JSON"),
    ('{"nope": 1}', "no `rules` list"),
])
def test_a_policy_that_is_not_deny_all_is_refused(world, rules, why):
    with pytest.raises(SandboxUnavailable, match=why):
        world.runner(env=write_policy(world, rules)).run(spec())
    never_created(world)


def test_the_real_captured_policy_is_deny_all():
    assert sandbox.sbx_network_policy_problem(DENY_ALL.read_text()) is None
    assert sandbox.sbx_network_policy_problem(json.dumps({"rules": [DENY]})) is None


def test_filesystem_rules_do_not_matter_to_the_network_check():
    fs = {"id": "fs", "resource_type": "filesystem:write", "decision": "allow", "resources": ["**"]}
    assert sandbox.sbx_network_policy_problem(json.dumps({"rules": [fs, DENY]})) is None


def test_no_daemon_socket_is_refused_and_never_started(world):
    with pytest.raises(SandboxUnavailable, match="socket .* does not exist"):
        world.runner(env={"XDG_STATE_HOME": str(world.root / "elsewhere")}).run(spec())
    assert world.calls() == []  # not even `sbx version`: asking could start the daemon


def test_a_regular_file_at_the_socket_path_is_refused(world):
    state = world.root / "fake_state"
    sd = state / "sandboxes" / "sandboxes" / "sandboxd"
    sd.mkdir(parents=True)
    (sd / "sandboxd.sock").write_text("")
    with pytest.raises(SandboxUnavailable, match="not a socket owned by this user"):
        world.runner(env={"XDG_STATE_HOME": str(state)}).run(spec())
    assert world.calls() == []


@pytest.mark.parametrize("key", ["../x", "/abs", "a//b", "./p.py", "", "a\\b", ".git/config", "x\n"])
def test_a_bad_staging_key_starts_no_sbx_process_at_all(world, key):
    with pytest.raises((ValueError, InvariantViolation)):
        world.runner().run(spec(files={key: "x"}))
    assert world.calls() == []


def test_the_guardrails_file_is_stageable_like_in_docker(world):
    world.runner().run(spec(files={"guardrails.md": "# rules\n"}))
    assert world.listed() == []


@pytest.mark.parametrize("command", [[], [1], ["a\0b"]])
def test_a_bad_command_is_refused_before_any_sbx_call(world, command):
    with pytest.raises(ValueError):
        world.runner().run(SandboxSpec(command=command))
    assert world.calls() == []


def test_a_missing_sbx_binary_is_a_refusal(world):
    with pytest.raises(SandboxUnavailable, match="cannot execute"):
        SbxRunner("/nonexistent/sbx", environ=world.env, verified=frozenset({VERSION})).run(spec())


# ── the cleanup guarantee: rm --force in a finally, whatever failed ────────────────

FAILURES = [
    ("create fails", {"FAKE_SBX_CREATE": "fail"}, "create"),
    ("the sandbox allows the network", {"FAKE_SBX_CHECK": "allowed"}, "does not deny"),
    ("the policy check is garbage", {"FAKE_SBX_CHECK": "garbage"}, "does not deny"),
    ("cp fails", {"FAKE_SBX_CP": "fail"}, "cp"),
    ("cp silently drops the files", {"FAKE_SBX_CP": "drop"}, "not exactly the requested"),
    ("sbx cannot run the command (125)", {"FAKE_SBX_EXEC_RC": "125", "FAKE_SBX_EXEC_ERR": "boom\n"}, "exit 125"),
]


@pytest.mark.parametrize("label,env,why", FAILURES, ids=[f[0] for f in FAILURES])
def test_every_failure_still_removes_the_sandbox(world, label, env, why):
    with pytest.raises(SandboxUnavailable, match=why):
        world.runner(env=env).run(spec(files={"a.py": "x"}))
    verbs = world.verbs()
    assert verbs[-2:] == ["rm", "ls"], verbs
    assert world.listed() == []
    if "exec" not in label and "125" not in label and label != "cp silently drops the files":
        assert [c for c in world.calls() if c[0] == "exec" and c[1] == "-w"] == []  # the command never ran


def test_a_failure_before_the_command_never_runs_the_command(world):
    with pytest.raises(SandboxUnavailable):
        world.runner(env={"FAKE_SBX_CHECK": "allowed"}).run(spec("rm", "-rf", "/", files={"a.py": "x"}))
    assert not (world.fake / "exec_commands").exists()


def test_a_sandbox_that_will_not_go_away_is_an_error(world):
    with pytest.raises(SandboxUnavailable, match="was NOT removed.*sbx rm --force harness2-"):
        world.runner(env={"FAKE_SBX_RM": "leak"}).run(spec())


def test_an_ls_that_fails_after_rm_is_an_error_too(world):
    # `ls -q` works for the create check, then the fake starts failing: simulate by failing only the last ls.
    r = world.runner()
    orig = r._destroy

    def destroy(name):
        r.environ["FAKE_SBX_LS"] = "fail"
        orig(name)
    r._destroy = destroy
    with pytest.raises(SandboxUnavailable, match="could not confirm"):
        r.run(spec())


def test_the_staging_directory_is_removed(world, monkeypatch):
    made: list[Path] = []
    real = sandbox._stage_for_sbx

    def spy(s):
        p = real(s)
        made.append(p)
        return p
    monkeypatch.setattr(sandbox, "_stage_for_sbx", spy)
    world.runner().run(spec(files={"a.py": "x"}))
    with pytest.raises(SandboxUnavailable):
        world.runner(env={"FAKE_SBX_CP": "fail"}).run(spec(files={"a.py": "x"}))
    assert len(made) == 2 and not any(p.exists() for p in made)


def test_files_are_staged_read_only_with_exact_names(world, monkeypatch):
    seen: dict[str, str] = {}
    real = sandbox._stage_for_sbx

    def spy(s):
        p = real(s)
        root = p / "candidate"
        for f in sorted(root.rglob("*")):
            if f.is_file():
                seen[str(f.relative_to(root))] = oct(f.stat().st_mode & 0o777)
        return p
    monkeypatch.setattr(sandbox, "_stage_for_sbx", spy)
    world.runner().run(spec(files={"a.py": "x", "d/e.py": "y"}))
    assert seen == {"a.py": "0o444", "d/e.py": "0o444"}


# ── the wall clock ─────────────────────────────────────────────────────────────────

def test_a_hung_exec_is_killed_on_time_and_the_sandbox_is_removed(world):
    t0 = time.monotonic()
    r = world.runner(env={"FAKE_SBX_EXEC_HANG": "1", "FAKE_SBX_EXEC_OUT": "partial"}).run(spec("sleep", "999", timeout_s=1))
    took = time.monotonic() - t0
    assert r.timed_out and r.exit_code is None and r.stderr == "timed out"
    assert took < 15, took
    assert world.verbs()[-2:] == ["rm", "ls"] and world.listed() == []


# ── the real sbx, opt-in ───────────────────────────────────────────────────────────

def _live_ready() -> str | None:
    if os.environ.get("COGNITIVE_HARNESS2_SBX_LIVE") != "1":
        return "set COGNITIVE_HARNESS2_SBX_LIVE=1 to run against a real sbx"
    path = shutil.which("sbx")
    if not path:
        return "sbx is not installed"
    try:
        if subprocess.run([path, "ls", "-q"], capture_output=True, timeout=30, stdin=subprocess.DEVNULL).returncode != 0:
            return "`sbx ls` does not work (daemon down?)"
    except (OSError, subprocess.TimeoutExpired):
        return "`sbx ls` does not work"
    return None


@pytest.mark.requires_sbx_live
@pytest.mark.skipif(_live_ready() is not None, reason=_live_ready() or "")
def test_live_create_exec_rm_round_trip_leaves_nothing_behind():
    path = shutil.which("sbx")
    assert path
    version = sandbox.real_sbx_probes(os.environ).version(path)
    assert version
    before = set(subprocess.run([path, "ls", "-q"], capture_output=True, text=True).stdout.split())
    runner = SbxRunner(path, verified=frozenset({version}), create_timeout_s=900)
    try:
        r = runner.run(SandboxSpec(command=["sh", "-c", "echo out; echo err >&2; exit 3"],
                                   files={"a.txt": "hello\n"}, timeout_s=120))
        assert (r.exit_code, r.stdout.strip(), r.stderr.strip(), r.timed_out) == (3, "out", "err", False)
        missing = runner.run(SandboxSpec(command=["definitely-not-a-command-xyz"], timeout_s=120))
        assert missing.exit_code == 127
        net = runner.run(SandboxSpec(command=["sh", "-c", "wget -T 5 -q -O- http://example.com/ || curl -m 5 -sS http://example.com/"],
                                     timeout_s=120))
        assert net.exit_code not in (0, None)
    finally:
        # belt and braces: the runner already removed its own sandboxes; remove anything new that is ours
        after = set(subprocess.run([path, "ls", "-q"], capture_output=True, text=True).stdout.split())
        for leftover in sorted(n for n in after - before if n.startswith("harness2-")):
            subprocess.run([path, "rm", "--force", leftover], capture_output=True)
    final = set(subprocess.run([path, "ls", "-q"], capture_output=True, text=True).stdout.split())
    assert final == before
