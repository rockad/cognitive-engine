"""`SbxRunner` against a FAKE `sbx` (tests/fixtures/fake-sbx/sbx), plus one opt-in real-sbx round trip.

The fake emulates only what the runner calls, with the outputs and exit codes observed on a real v0.50.1, and records
every call. These tests prove the runner's argv, its refusal paths (each one BEFORE anything is created), its
cleanup guarantee (`rm --force` in a `finally`, whatever failed) and its wall clock. They do NOT prove that a real
microVM behaves the same: that is the `requires_sbx_live` test, skipped unless COGNITIVE_HARNESS2_SBX_LIVE=1.
No Docker, no model, no network.
"""
from __future__ import annotations

import json
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
    s.listen(8)                       # a live daemon accepts: the runner now connects, as the selection probe does
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
    assert world.verbs() == ["version", "policy ls", "settings", "create", "ls", *["policy check"] * 4,
                             "cp", "exec", "exec", "rm", "ls"]
    # The host-reach settings are re-read on every run (read-only), then the flow below is unchanged.
    assert calls[2] == ["settings", "ls", "--format", "json"]
    calls = calls[:2] + calls[3:]
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
    name = next(c for c in world.calls() if c[0] == "create")[3]
    assert [c for c in world.calls() if c[0] == "exec"] == [["exec", "-w", "/tmp", "--", name, "true"]]


def test_output_and_exit_code_come_back(world):
    r = world.runner(env={"FAKE_SBX_EXEC_RC": "3", "FAKE_SBX_EXEC_OUT": "out\n", "FAKE_SBX_EXEC_ERR": "err\n"}).run(spec())
    assert (r.exit_code, r.stdout, r.stderr, r.timed_out) == (3, "out\n", "err\n", False)


def test_exit_127_is_the_commands_own_result_not_a_refusal(world):
    r = world.runner(env={"FAKE_SBX_EXEC_RC": "127", "FAKE_SBX_EXEC_ERR": "no such command\n"}).run(spec("nope"))
    assert r.exit_code == 127 and "no such command" in r.stderr
    assert world.listed() == []


def test_exit_255_is_the_commands_own_unless_the_stderr_is_sbxs(world):
    """A wedged VM made the real sbx print `failed to start stdin stream` and exit 255: that is a refusal.
    A candidate that merely exits 255 is a result."""
    r = world.runner(env={"FAKE_SBX_EXEC_RC": "255", "FAKE_SBX_EXEC_ERR": "plain failure\n"}).run(spec())
    assert r.exit_code == 255
    for err in ("failed to start stdin stream: failed to read ack length: i/o timeout\n",
                "error: inspect exec: context deadline exceeded\n"):
        with pytest.raises(SandboxUnavailable, match="exit 255"):
            world.runner(env={"FAKE_SBX_EXEC_RC": "255", "FAKE_SBX_EXEC_ERR": err}).run(spec())
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


def test_the_hosts_ssh_agent_socket_never_reaches_an_sbx_process(world):
    world.runner(env={"SSH_AUTH_SOCK": "/tmp/ssh-agent.sock", "ssh_auth_sock": "/tmp/lower"}).run(spec())
    seen = (world.fake / "env.last").read_text().lower()
    assert "ssh_auth_sock" not in seen and "/tmp/ssh-agent.sock" not in seen


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


@pytest.mark.parametrize("setting,named", [("ssh", ["ssh.agentForwardingEnabled"]), ("browser", ["browser.enabled"]),
                                           ("both", ["ssh.agentForwardingEnabled", "browser.enabled"])])
def test_a_host_reach_setting_that_is_on_is_refused_on_every_run(world, setting, named):
    """Re-read per run, not trusted from the selection probe's cache: a flip after selection still stops the run."""
    with pytest.raises(SandboxUnavailable, match="host-reach setting") as e:
        world.runner(env={"FAKE_SBX_SETTINGS": setting}).run(spec())
    assert all(f"{n}=true" in str(e.value) for n in named) and sandbox.ENV_ALLOW_HOST_SETTINGS in str(e.value)
    never_created(world)


@pytest.mark.parametrize("setting", ["fail", "garbage"])
def test_unreadable_host_reach_settings_are_refused_not_assumed_off(world, setting):
    with pytest.raises(SandboxUnavailable, match="settings"):
        world.runner(env={"FAKE_SBX_SETTINGS": setting}).run(spec())
    never_created(world)


def test_the_documented_override_accepts_the_settings_and_nothing_else(world):
    r = world.runner(env={"FAKE_SBX_SETTINGS": "both", sandbox.ENV_ALLOW_HOST_SETTINGS: "1"}).run(spec())
    assert r.exit_code == 0
    with pytest.raises(SandboxUnavailable, match="not in the verified set"):  # every other check still applies
        world.runner(env={"FAKE_SBX_SETTINGS": "both", sandbox.ENV_ALLOW_HOST_SETTINGS: "1"},
                     verified=frozenset({"1.0.0"})).run(spec())
    with pytest.raises(SandboxUnavailable, match="host-reach setting"):  # only the exact value `1` counts
        world.runner(env={"FAKE_SBX_SETTINGS": "both", sandbox.ENV_ALLOW_HOST_SETTINGS: "true"}).run(spec())


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


# What a real sbx v0.50.1 printed for an HTTP allow rule (resource_type "http"): the proxy enforces it, `policy check network`
# does not see it. A candidate sandbox must never be created while one exists.
HTTP_ALLOW = {"id": "h1", "name": "allow-http", "scope": "global", "applies_to": "all", "resource_type": "http", "decision": "allow",
              "resources": ["**"], "methods": ["ANY"], "http_targets": [{"host": "**", "path": "/**"}], "status": "active",
              "actions": ["http:request:get"]}
PORT_FORWARD = {"id": "pf", "scope": "global", "resource_type": "port_forward", "decision": "allow", "resources": ["**"],
                "status": "active"}


@pytest.mark.parametrize("rules,why", [
    ([DENY, ALLOW], "allow rule exists"),
    ([DENY, HTTP_ALLOW], "allow rule exists"),                                         # a GLOBAL http allow reaches the gateway proxy
    ([DENY, {**HTTP_ALLOW, "status": "inactive"}], "allow rule exists"),
    ([DENY, {**HTTP_ALLOW, "scope": "sandbox:somebody", "applies_to": "sandbox:somebody"}], "allow rule exists"),
    ([DENY, {**HTTP_ALLOW, "decision": "prompt"}], "unknown decision"),
    ([DENY, PORT_FORWARD], "unknown resource type"),                                   # fail closed on a kind nobody has seen
    ([DENY, {**PORT_FORWARD, "decision": "deny"}], "unknown resource type"),
    ([DENY, {**PORT_FORWARD, "resource_type": "Network"}], "unknown resource type"),
    ([DENY, {**PORT_FORWARD, "resource_type": "filesystem:exec"}], "unknown resource type"),
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


def test_every_rule_of_the_observed_default_policy_has_an_allow_listed_shape():
    """The fixture is what `sbx policy ls --json` printed on this host (2026-11-01): the two filesystem rules and the deny-all.
    The allow-list is these shapes; every rule type outside it is a foreign rule."""
    doc = json.loads(DENY_ALL.read_text())
    assert sorted(r["resource_type"] for r in doc["rules"]) == ["filesystem:read", "filesystem:write", "network"]
    assert sandbox.SBX_NONNETWORK_RULE_TYPES == {"filesystem:read", "filesystem:write"}
    assert sandbox.sbx_network_policy_problem(DENY_ALL.read_text()) is None


def test_the_default_probe_and_every_default_policy_problem_use_the_same_allow_list(world):
    for rules in ([DENY, HTTP_ALLOW], [DENY, PORT_FORWARD]):
        text = json.dumps({"rules": rules})
        assert sandbox.sbx_network_policy_problem(text)
    assert SbxRunner("/x", environ={}).policy_problem is sandbox.sbx_network_policy_problem


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
    # The candidate here is this test's own code, so the host-reach settings (on by default) are accepted explicitly;
    # without the override the runner refuses (test_a_host_reach_setting_that_is_on_is_refused_on_every_run).
    runner = SbxRunner(path, verified=frozenset({version}), create_timeout_s=900,
                       environ={**os.environ, sandbox.ENV_ALLOW_HOST_SETTINGS: "1"})
    try:
        r = runner.run(SandboxSpec(command=["sh", "-c", "echo out; echo err >&2; cat a.txt d/b.txt; pwd; exit 3"],
                                   files={"a.txt": "hello\n", "d/b.txt": "nested\n"}, timeout_s=120))
        assert (r.exit_code, r.stdout.split(), r.stderr.strip(), r.timed_out) == (
            3, ["out", "hello", "nested", "/tmp/candidate"], "err", False), r
        assert runner.run(SandboxSpec(command=["definitely-not-a-command-xyz"], timeout_s=120)).exit_code == 127
        # Flag-looking candidate arguments reach the command, not sbx.
        assert runner.run(SandboxSpec(command=["sh", "-c", "echo $0 $1", "-x", "-y"], timeout_s=120)).stdout.strip() == "-x -y"
        # The network, measured from inside: the proxy answers 403 (or the connection is dropped), never real content,
        # and a direct socket to a host loopback service (ollama) gets no data back.
        probe = (
            "import socket\n"
            "for h in ('host.docker.internal', 'gateway.docker.internal'):\n"
            "    s = socket.socket(); s.settimeout(4)\n"
            "    try:\n"
            "        s.connect((h, 11434)); s.sendall(b'GET /api/version HTTP/1.0\\r\\n\\r\\n')\n"
            "        print('DATA' if s.recv(100) else 'NODATA')\n"
            "    except Exception:\n"
            "        print('NODATA')\n")
        net = runner.run(SandboxSpec(command=["python3", "-c", probe], timeout_s=120))
        assert net.stdout.split() == ["NODATA", "NODATA"], net
        codes = runner.run(SandboxSpec(command=["sh", "-c", (
            "curl -m 5 -s -o /dev/null -w '%{http_code}\\n' http://example.com/; "
            "curl --noproxy '*' -m 5 -s -o /dev/null -w '%{http_code}\\n' http://example.com/; "
            "curl -m 5 -s -o /dev/null -w '%{http_code}\\n' http://host.docker.internal:11434/api/version; true")], timeout_s=120))
        assert all(x in ("403", "000") for x in codes.stdout.split()) and len(codes.stdout.split()) == 3, codes
        # The wall clock: a sleeper is cut off on time, and its sandbox is gone afterwards (checked below).
        t0 = time.monotonic()
        slow = runner.run(SandboxSpec(command=["sleep", "300"], timeout_s=3))
        assert slow.timed_out and slow.exit_code is None and time.monotonic() - t0 < 60
    finally:
        # belt and braces: the runner already removed its own sandboxes; remove anything new that is ours
        after = set(subprocess.run([path, "ls", "-q"], capture_output=True, text=True).stdout.split())
        for leftover in sorted(n for n in after - before if n.startswith("harness2-")):
            subprocess.run([path, "rm", "--force", leftover], capture_output=True)
    final = set(subprocess.run([path, "ls", "-q"], capture_output=True, text=True).stdout.split())
    assert final == before


@pytest.mark.requires_sbx_live
@pytest.mark.skipif(_live_ready() is not None, reason=_live_ready() or "")
def test_live_the_sum_signed_verifier_gives_the_same_verdict_in_sbx_as_in_python():
    """`VERIFY_SH` is written for busybox `sh` and `awk` (the Docker image); the sbx shell image is Ubuntu (dash,
    mawk or gawk). The verifier is a consumer that `auto` now moves to sbx, so its verdict is checked there, on both
    task sets, against the pure-Python grader."""
    from cognitive_harness2 import sum_signed as ss
    from cognitive_harness2 import sum_signed_hard as hd

    path = shutil.which("sbx")
    assert path
    version = sandbox.real_sbx_probes(os.environ).version(path)
    runner = SbxRunner(path, verified=frozenset({version}), create_timeout_s=900,
                       environ={**os.environ, sandbox.ENV_ALLOW_HOST_SETTINGS: "1"})
    before = set(subprocess.run([path, "ls", "-q"], capture_output=True, text=True).stdout.split())
    try:
        for ts, task in ((ss.EASY, "household/sum-1"), (hd.TASKSET, "household/hard-1")):
            inst = ts.instance(task)
            for raw in ((inst.expected_line + "\n").encode(), b"Total: 1.00\n", b"junk", None):
                got = ss.verify_in_container(runner, task, raw, taskset=ts)
                assert got == ss.grade_answer(raw, inst.expected_total), (ts.name, raw, got)
    finally:
        after = set(subprocess.run([path, "ls", "-q"], capture_output=True, text=True).stdout.split())
        for leftover in sorted(n for n in after - before if n.startswith("harness2-")):
            subprocess.run([path, "rm", "--force", leftover], capture_output=True)
    assert set(subprocess.run([path, "ls", "-q"], capture_output=True, text=True).stdout.split()) == before


def test_the_result_names_the_runner(world):
    r = world.runner().run(spec())
    assert (r.runner, r.fallback_reason) == ("sbx", None)
    hung = world.runner(env={"FAKE_SBX_EXEC_HANG": "1"}).run(spec(timeout_s=1))
    assert hung.timed_out and hung.runner == "sbx"


# ── auto only: a failure BEFORE the candidate started falls back to verified Docker ───

from cognitive_harness2.sandbox import SandboxNotStarted, SandboxResult  # noqa: E402


class FakeDocker:
    """The verified Docker runner the fallback factory hands over. Records every spec it is given."""
    def __init__(self, unusable: str | None = None):
        self.runs, self.reasons, self.unusable = [], [], unusable

    def factory(self, reason):
        if self.unusable:
            raise SandboxUnavailable(self.unusable)
        self.reasons.append(reason)
        return self

    def run(self, spec):
        self.runs.append(spec)
        return SandboxResult(0, "on docker", "", runner="rootless-docker", fallback_reason=self.reasons[-1])


PRE_EXEC = [
    ("create fails (image pull, VM boot)", {"FAKE_SBX_CREATE": "fail"}),
    ("the sandbox allows the network", {"FAKE_SBX_CHECK": "allowed"}),
    ("cp fails", {"FAKE_SBX_CP": "fail"}),
    ("cp silently drops the files", {"FAKE_SBX_CP": "drop"}),
    ("a host-reach setting was flipped on", {"FAKE_SBX_SETTINGS": "ssh"}),
    ("the daemon went away", {"XDG_STATE_HOME": "ELSEWHERE"}),
    ("an unreadable version", {"FAKE_SBX_VERSION": "nightly"}),
]


@pytest.mark.parametrize("label,env", PRE_EXEC, ids=[p[0] for p in PRE_EXEC])
def test_a_failure_before_the_command_started_runs_that_spec_on_docker(world, caplog, label, env):
    env = {k: str(world.root / "elsewhere") if v == "ELSEWHERE" else v for k, v in env.items()}
    docker = FakeDocker()
    sbx_spec = spec("rm", "-rf", "/", files={"a.py": "x"})
    with caplog.at_level("WARNING", logger=sandbox.__name__):
        r = world.runner(env=env, fallback=docker.factory).run(sbx_spec)
    assert (r.stdout, r.runner) == ("on docker", "rootless-docker")
    assert docker.runs == [sbx_spec]  # exactly that spec, on Docker
    reason = r.fallback_reason
    assert reason["code"] == "sbx-run-failed" and reason["loud"] is True and reason["detail"]
    assert not (world.fake / "exec_commands").exists()  # the candidate never ran in sbx
    assert not (world.fake / "sandboxes").exists() or world.listed() == []  # a half-created sandbox is still removed
    (w,) = [x for x in caplog.records if x.levelno == logging.WARNING]
    assert "Docker" in w.getMessage() and "sbx-run-failed" in w.getMessage() and reason["detail"] in w.getMessage()


def test_the_fallback_is_not_used_when_sbx_works(world):
    docker = FakeDocker()
    r = world.runner(fallback=docker.factory).run(spec(files={"a.py": "x"}))
    assert r.runner == "sbx" and r.fallback_reason is None
    assert docker.runs == [] and docker.reasons == []


def test_after_a_fallback_the_rest_of_the_runners_life_stays_on_docker(world):
    """A broken sbx would otherwise cost every remaining candidate its preflight (and up to 600 s of create)."""
    docker = FakeDocker()
    runner = world.runner(env={"FAKE_SBX_CREATE": "fail"}, fallback=docker.factory)
    runner.run(spec())
    seen = len(world.calls())
    again = runner.run(spec("echo", "2"))
    assert again.runner == "rootless-docker" and len(docker.runs) == 2
    assert len(world.calls()) == seen  # no sbx process at all for the second candidate


def test_a_fallback_drops_the_cached_selection(world, monkeypatch):
    dropped = []
    monkeypatch.setattr(sandbox, "clear_sbx_cache", lambda: dropped.append(1))
    world.runner(env={"FAKE_SBX_CREATE": "fail"}, fallback=FakeDocker().factory).run(spec())
    assert dropped == [1]


def test_no_fallback_once_the_command_has_started(world):
    docker = FakeDocker()
    with pytest.raises(SandboxUnavailable, match="exit 125") as e:
        world.runner(env={"FAKE_SBX_EXEC_RC": "125", "FAKE_SBX_EXEC_ERR": "boom\n"}, fallback=docker.factory).run(spec())
    assert not isinstance(e.value, SandboxNotStarted) and docker.runs == []
    ok = world.runner(env={"FAKE_SBX_EXEC_RC": "3"}, fallback=docker.factory).run(spec())
    assert ok.exit_code == 3 and ok.runner == "sbx" and docker.runs == []  # a failing candidate is a result, not a fallback


def test_no_fallback_when_the_sandbox_could_not_be_removed(world):
    docker = FakeDocker()
    with pytest.raises(SandboxUnavailable, match="NOT removed"):
        world.runner(env={"FAKE_SBX_RM": "leak"}, fallback=docker.factory).run(spec())
    with pytest.raises(SandboxUnavailable, match="NOT removed"):  # not even after a failure before the command
        world.runner(env={"FAKE_SBX_RM": "leak", "FAKE_SBX_CHECK": "allowed"}, fallback=docker.factory).run(spec())
    assert docker.runs == []


def test_an_unusable_docker_fails_closed_and_names_both(world):
    docker = FakeDocker(unusable="daemon is not rootless")
    with pytest.raises(SandboxUnavailable) as e:
        world.runner(env={"FAKE_SBX_CREATE": "fail"}, fallback=docker.factory).run(spec())
    assert "create" in str(e.value) and "daemon is not rootless" in str(e.value) and "fallback" in str(e.value)


def test_without_a_fallback_the_failure_stays_an_error(world):
    with pytest.raises(SandboxUnavailable, match="create"):
        world.runner(env={"FAKE_SBX_CREATE": "fail"}).run(spec())


def test_a_bad_spec_is_still_a_value_error_not_a_fallback(world):
    docker = FakeDocker()
    with pytest.raises(ValueError):
        world.runner(fallback=docker.factory).run(SandboxSpec(command=[]))
    assert docker.runs == [] and world.calls() == []


# ── a restart-flagged setting changed after the daemon started is refused on every run too ──

def _cfg_env(world, *, newer: bool) -> dict:
    f = world.root / "cfg" / "sandboxes" / "sandboxes" / "settings.json"
    f.parent.mkdir(parents=True)
    f.write_text("{}")
    sock_mtime = sandbox._sbx_socket(world.env).stat().st_mtime
    os.utime(f, (sock_mtime + (60 if newer else -60),) * 2)
    return {"XDG_CONFIG_HOME": str(world.root / "cfg"), "FAKE_SBX_SETTINGS": "restart"}


def test_the_runner_refuses_while_the_daemon_has_not_been_restarted_and_creates_nothing(world):
    with pytest.raises(SandboxUnavailable, match="sbx daemon restart") as e:
        world.runner(env=_cfg_env(world, newer=True)).run(spec())
    assert isinstance(e.value, SandboxNotStarted) and "ssh.agentForwardingEnabled" in str(e.value)
    assert world.verbs() == ["version", "policy ls", "settings"]  # nothing created, nothing run


def test_the_runner_runs_when_the_daemon_started_after_the_override(world):
    assert world.runner(env=_cfg_env(world, newer=False)).run(spec()).exit_code == 0


# ── F9: the environment of every sbx call is an allow-list, not a deny-list ────────────────────────────────────────

@pytest.fixture(autouse=True)
def _fake_sbx_knobs_reach_the_fake(monkeypatch):
    """The fake `sbx` is configured through FAKE_SBX_* variables. Production allows none of that prefix; the tests do."""
    monkeypatch.setattr(sandbox, "_SBX_ENV_ALLOW_EXTRA", ("FAKE_SBX_",))


ALLOWED = {"PATH": "/usr/bin", "HOME": "/home/u", "XDG_STATE_HOME": "/s", "XDG_RUNTIME_DIR": "/run/u", "XDG_CONFIG_HOME": "/c",
           "LANG": "C.UTF-8", "LC_ALL": "C", "LC_CTYPE": "C", "TERM": "xterm", "TMPDIR": "/tmp"}
NOT_ALLOWED = {"DOCKER_HOST": "tcp://evil:2375", "DOCKER_CONFIG": "/evil", "DOCKER_AUTH_CONFIG": '{"auths":{}}',
               "DOCKER_CONTEXT": "evil", "DOCKER_TLS_VERIFY": "1", "SSH_AUTH_SOCK": "/tmp/a.sock", "SBX_CRED_X": "y",
               "DOCKER_SANDBOXES_API": "http://evil", "ANTHROPIC_API_KEY": "k", "GITHUB_TOKEN": "t", "AWS_SECRET_ACCESS_KEY": "s",
               "OPENAI_API_KEY": "k", "HTTP_PROXY": "http://p", "https_proxy": "http://p", "LD_PRELOAD": "/evil.so",
               "LD_LIBRARY_PATH": "/evil", "PYTHONPATH": "/evil", "GIT_SSH_COMMAND": "evil", "FOO": "bar", "lang": "lower",
               "path": "/lower", "xdg_state_home": "/lower", "NPM_TOKEN": "t", "KUBECONFIG": "/k", "EDITOR": "vi"}


def test_the_sbx_environment_is_exactly_the_allow_list():
    out = sandbox._sbx_env({**ALLOWED, **NOT_ALLOWED})
    assert out == ALLOWED


def test_the_allow_list_is_path_home_xdg_lang_lc_term_tmpdir_and_nothing_extra_in_production(monkeypatch):
    monkeypatch.setattr(sandbox, "_SBX_ENV_ALLOW_EXTRA", ())
    assert sandbox._SBX_ENV_ALLOW == frozenset({"PATH", "HOME", "LANG", "TERM", "TMPDIR"})
    assert sandbox._SBX_ENV_ALLOW_PREFIXES == ("XDG_", "LC_")
    assert sandbox._sbx_env({"FAKE_SBX_DIR": "/x", **ALLOWED}) == ALLOWED


@pytest.mark.parametrize("name", sorted(NOT_ALLOWED))
def test_no_denied_variable_reaches_an_sbx_process(world, name):
    world.runner(env={name: "sentinel-" + name.lower()}).run(spec())
    seen = (world.fake / "env.last").read_text()
    assert ("sentinel-" + name.lower()) not in seen


def test_the_allowed_variables_do_reach_it(world):
    world.runner(env={"LANG": "C.UTF-8", "LC_ALL": "C", "TERM": "xterm", "TMPDIR": "/tmp", "XDG_CONFIG_HOME": "/c"}).run(spec())
    seen = (world.fake / "env.last").read_text()
    for needle in ("LANG=C.UTF-8", "LC_ALL=C", "TERM=xterm", "TMPDIR=/tmp", "XDG_CONFIG_HOME=/c", "XDG_STATE_HOME=", "PATH="):
        assert needle in seen


# ── F10: the runner's preflight uses the same connect() probe as selection ───────────────────────────────────────

def _stale_world():
    """A socket FILE with nobody listening: a daemon that died without removing it (stat passes, connect is refused)."""
    root = Path(tempfile.mkdtemp(prefix="h2sbx"))
    sd = root / "state" / "sandboxes" / "sandboxes" / "sandboxd"
    sd.mkdir(parents=True)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(sd / "sandboxd.sock"))
    s.close()                                              # bound, never listened, closed: the file remains
    assert (sd / "sandboxd.sock").exists()
    (root / "fake").mkdir()
    return root


def test_a_stale_socket_file_does_not_pass_the_preflight():
    root = _stale_world()
    try:
        env = {"PATH": f"{FAKE_DIR}:{os.environ['PATH']}", "XDG_STATE_HOME": str(root / "state"), "FAKE_SBX_DIR": str(root / "fake"),
               "FAKE_SBX_POLICY_FILE": str(DENY_ALL)}
        r = SbxRunner(str(FAKE_DIR / "sbx"), environ=env, verified=frozenset({VERSION}))
        with pytest.raises(SandboxUnavailable, match="not reachable|not running") as e:
            r.run(spec())
        assert "fail closed" in str(e.value)
        calls = root / "fake" / "calls"
        assert not calls.exists() or calls.read_text() == ""            # not even `sbx version` was started
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_the_runner_and_the_selection_probe_share_one_daemon_check(world):
    assert sandbox._sbx_daemon_reachable(world.env) is True
    assert sandbox.real_sbx_probes(world.env).daemon_reachable() is True
    root = _stale_world()
    try:
        assert sandbox._sbx_daemon_reachable({"XDG_STATE_HOME": str(root / "state")}) is False
    finally:
        shutil.rmtree(root, ignore_errors=True)
