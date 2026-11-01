"""`SbxSessionRunner` against a STATEFUL FAKE `sbx` (tests/fixtures/fake-sbx-model/sbx), plus one opt-in live test.

The fake keeps sandboxes and sandbox-scoped rules, and removes a sandbox's rules with it, as a real v0.50.1 does. The host
egress proxy and the host bridge are REAL processes and threads here (loopback only), so the egress log and the route filter
are the real ones; only the VM is a script. These tests prove the runner's argv and ORDER, its refusals (each before the CLI
runs), the policy lifecycle under failure, crash and interrupt, the stale sweep, and the foreign-rule refusal. They do NOT
prove a real microVM behaves the same: that is `test_live_*`, skipped unless COGNITIVE_HARNESS2_SBX_LIVE=1.
No Docker, no model, no real sbx."""
from __future__ import annotations

import _thread
import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from cognitive_harness2 import egress_proxy, run_scope, sandbox, sbx_session as ss, stage1
from cognitive_harness2 import session_runner as sr
from cognitive_harness2.session_runner import SessionCleanupError, SessionError, SessionRefused, SessionSpec

HERE = Path(__file__).parent
FAKE = HERE / "fixtures" / "fake-sbx-model" / "sbx"
DENY_ALL = HERE / "fixtures" / "sbx_policy_ls_deny_all.json"
CLIENT = HERE / "fixtures" / "sbx_model_client.py"
VERSION = "9.9.9"
GATEWAY = "http://gateway.docker.internal:3128"


@pytest.fixture(autouse=True)
def _fake_env(monkeypatch):
    monkeypatch.setattr(sandbox, "_SBX_ENV_ALLOW_EXTRA", ("FAKE_SBX_",))


class World:
    def __init__(self, root: Path, state: Path, sock: socket.socket, ollama: stage1.FakeOllama):
        self.root, self.state, self.sock, self.ollama = root, state, sock, ollama
        self.fake = root / "fake"
        self.fake.mkdir()
        self.ws = root / "ws"
        self.ws.mkdir()
        self.cli = root / "bin" / "opencode"
        self.cli.parent.mkdir()
        self.cli.write_text("#!/bin/sh\nexit 0\n")
        self.cli.chmod(0o755)
        self.runtime = Path(tempfile.mkdtemp(prefix="h2rt"))
        self.profile = stage1.selftest_profile(root, ollama.port)
        self.channel = self.profile.channels[stage1.CHANNEL]
        self.env = {"XDG_STATE_HOME": str(state), "FAKE_SBX_DIR": str(self.fake), "FAKE_SBX_POLICY_FILE": str(DENY_ALL),
                    "FAKE_SBX_VERSION": f"v{VERSION}", "FAKE_SBX_PY": sys.executable, "PATH": os.environ["PATH"]}

    def runner(self, *, env=None, **kw) -> ss.SbxSessionRunner:
        kw.setdefault("verified", frozenset({VERSION}))
        kw.setdefault("proposer_decision", sr.upstream_egress.Decision(True, ("approved",)))
        return ss.SbxSessionRunner(str(FAKE), self.channel, zdr="strict", classification="open",
                                   workspace_roots=(str(self.root),), environ={**self.env, **(env or {})}, **kw)

    def spec(self, **kw) -> SessionSpec:
        base = dict(argv=(sr.container_cli_path(str(self.cli)), "run", "--model", "gemma4:12b", "do the thing"),
                    workspace=str(self.ws), cli=str(self.cli), timeout_s=60,
                    env={"OPENCODE_CONFIG_CONTENT": json.dumps({"baseURL": "http://harness2-proxy.internal:%d/v1" % self.ollama.port}),
                         "OPENCODE_DISABLE_AUTOUPDATE": "1"})
        base.update(kw)
        return SessionSpec(**base)

    def calls(self) -> list[list[str]]:
        f = self.fake / "calls"
        return [line.split("\x1f")[:-1] for line in f.read_text().splitlines()] if f.exists() else []

    def verbs(self) -> list[str]:
        return [c[0] if c[0] != "policy" else f"policy {c[1]}" for c in self.calls()]

    def listed(self) -> list[str]:
        return (self.fake / "sandboxes").read_text().split() if (self.fake / "sandboxes").exists() else []

    def rules(self) -> list[str]:
        f = self.fake / "rules"
        return f.read_text().splitlines() if f.exists() else []

    def session_argv(self) -> list[str]:
        return (self.fake / "session_argv").read_bytes().decode().split("\0")[:-1]

    def session_env(self) -> dict[str, str]:
        argv = self.session_argv()
        i = argv.index("env")
        assert argv[i + 1] == "-i"
        out, j = {}, i + 2
        while "=" in argv[j].split("/")[0] and not argv[j].startswith("/"):
            k, _, v = argv[j].partition("=")
            out[k] = v
            j += 1
        return out

    def runtime_leftovers(self) -> list[str]:
        return sorted(p.name for p in self.runtime.iterdir())

    def script(self, text: str) -> str:
        p = self.root / "session.sh"
        p.write_text(text)
        return str(p)


@pytest.fixture
def world(monkeypatch):
    root = Path(tempfile.mkdtemp(prefix="h2sbxs")).resolve()
    state = root / "state"
    sd = state / "sandboxes" / "sandboxes" / "sandboxd"
    sd.mkdir(parents=True)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(sd / "sandboxd.sock"))
    s.listen(8)
    with stage1.FakeOllama() as ollama:
        w = World(root, state, s, ollama)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(w.runtime))        # the bridge and the proxy files live here: leaks are visible
        try:
            yield w
        finally:
            s.close()
            shutil.rmtree(root, ignore_errors=True)
            shutil.rmtree(w.runtime, ignore_errors=True)


def port_closed(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) != 0


def rule_port(world_rules: list[str]) -> int:
    return int(world_rules[0].split("\t")[4].rpartition(":")[2])


# ── authorisation: every refusal happens before any sbx process ───────────────────

def test_a_hosted_channel_is_refused_before_anything_starts(world):
    hosted = stage1.channels.validate({"workspace_roots": [str(world.root)], "channels": {"c": dict(
        kind="claude_cli", provider="anthropic", endpoints=["https://api.anthropic.com"], model="m", retention="unknown",
        max_classification="open", approval="per-sweep", caps={"sessions": 3, "wall_s": 600, "cost_usd": 1})}}, source="t").channels["c"]
    r = ss.SbxSessionRunner(str(FAKE), hosted, zdr="off", classification="open", environ=world.env, verified=frozenset({VERSION}),
                            proposer_decision=sr.upstream_egress.Decision(True, ("ok",)))
    with pytest.raises(SessionRefused, match="local"):
        r.run(world.spec())
    assert world.calls() == []


@pytest.mark.parametrize("change, match", [
    (dict(credential=sr.Credential("/run/secrets/k", "SENTINEL-secret-1234")), "credential"),
    (dict(timeout_s=0), "timeout"),
    (dict(argv=("/bin/sh", "-c", "x")), "argv"),
    (dict(env={"HTTP_PROXY": "http://evil:1"}), "proxy"),
])
def test_the_same_refusals_as_the_docker_route_apply_before_any_sbx_call(world, change, match):
    with pytest.raises(SessionRefused, match=match):
        world.runner().run(world.spec(**change))
    assert world.calls() == []


def test_a_workspace_path_with_a_colon_is_refused_because_sbx_reads_a_suffix_as_a_mode(world):
    odd = world.root / "ws:ro"
    odd.mkdir()
    with pytest.raises(SessionRefused, match="':'"):
        world.runner().run(world.spec(workspace=str(odd)))
    assert world.calls() == []


def test_the_runner_never_needs_docker(world):
    r = world.runner()
    assert r.verified is None and r.image is None


# ── the happy path: exact commands, exact order ───────────────────────────────────

def test_the_whole_flow_every_argv_and_the_order(world):
    out = world.runner().run(world.spec(tmpfs_dirs=(sr.HOME + "/.local/share",)))
    assert (out.exit_code, out.timed_out, out.role) == (0, False, "proposer")
    v = world.verbs()
    assert v == ["ls", "version", "policy ls", "settings",             # the stale sweep first, then the preflight
                 "create", "ls", *["policy allow"] * 5, "policy ls", *["policy check"] * 6,
                 "exec", "exec", "cp", "exec", "exec", "exec", "exec",
                 *["policy rm"] * 5, "rm", "ls", "policy ls", "ls"]
    calls = world.calls()
    name = next(c for c in calls if c[0] == "create")[3]
    parsed = ss.parse_sandbox_name(name)
    assert parsed and parsed[1] == os.getpid() and parsed[0] == run_scope.current()
    create = next(c for c in calls if c[0] == "create")
    assert create[:3] == ["create", "shell", "--name"] and create[4:] == [
        "--no-progress", "--cpus", "2", "--memory", "2g", "--pull", "missing", "--skills", "off", str(world.ws)]
    allows = [c for c in calls if c[:3] == ["policy", "allow", "network"]]
    port = int(allows[0][5].rpartition(":")[2])
    assert port != world.ollama.port and 1024 <= port <= 65535
    assert [(c[3], c[4], c[5], c[6], c[7], c[8], c[9]) for c in allows] == [
        ("--sandbox", name, f"localhost:{port}", "--method", m, "--path", p) for m, p in egress_proxy.OLLAMA_ROUTE_ALLOW]
    per_sandbox = [c for c in calls if c[:3] == ["policy", "ls", "--json"] and len(c) == 4]
    assert per_sandbox == [["policy", "ls", "--json", name]]
    checked = [c[-1] for c in calls if c[:3] == ["policy", "check", "network"]]
    assert checked == [*sandbox.SBX_DENIED_TARGETS, f"localhost:{world.ollama.port}", f"localhost:{port + 1}"]
    execs = [c for c in calls if c[0] == "exec"]
    assert execs[0] == ["exec", "--", name, "printenv", "HTTP_PROXY"]
    assert execs[1] == ["exec", "-u", "root", "--", name, "mkdir", "-p", sr.CLI_DIR]
    assert [c for c in calls if c[0] == "cp"] == [["cp", str(world.cli), f"{name}:{sr.CLI_DIR}/"]]
    assert execs[2] == ["exec", "-u", "root", "--", name, "chmod", "0755", f"{sr.CLI_DIR}/opencode"]
    assert execs[3] == ["exec", "--", name, "test", "-x", f"{sr.CLI_DIR}/opencode"]
    assert execs[4] == ["exec", "--", name, "mkdir", "-p", sr.HOME + "/.local/share"]
    assert execs[5][:5] == ["exec", "-w", str(world.ws), "--", name]          # no `-i` (stdin) right after `exec`
    removed = [c for c in calls if c[:3] == ["policy", "rm", "network"]]
    assert [(c[3], c[4], c[5]) for c in removed] == [("--sandbox", name, "--id")] * 5 and all(c[-1] == "--force" for c in removed)
    assert [c for c in calls if c[0] == "rm"] == [["rm", "--force", name]]
    assert not any(c[0] in ("run", "ssh", "attach") for c in calls)
    # nothing is left anywhere
    assert world.listed() == [] and world.rules() == [] and world.runtime_leftovers() == []
    assert port_closed(port) and out.leftovers == () and out.reclaimed is True


def test_the_session_command_line_and_the_complete_environment(world, monkeypatch):
    monkeypatch.setenv("SENTINEL_HOST_VAR", "must-not-reach-the-vm")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    world.runner().run(world.spec())
    argv = world.session_argv()
    assert argv[:2] == ["bash", "-c"] and argv[2].startswith("ulimit -u") and argv[3] == "bash" and argv[4] == "256"
    i = argv.index("env")
    assert argv[-5:] == [sr.container_cli_path(str(world.cli)), "run", "--model", "gemma4:12b", "do the thing"]
    env = world.session_env()
    port = rule_port(["x\ty\tz\tw\tlocalhost:%d" % int(env["OPENCODE_CONFIG_CONTENT"].split("host.docker.internal:")[1].split("/")[0])])
    assert json.loads(env["OPENCODE_CONFIG_CONTENT"]) == {"baseURL": f"http://host.docker.internal:{port}/v1"}
    assert env["HTTP_PROXY"] == env["HTTPS_PROXY"] == env["http_proxy"] == env["https_proxy"] == GATEWAY
    assert env["NO_PROXY"] == env["no_proxy"] == "127.0.0.1,localhost,::1"          # the VM's own loopback and nothing else
    assert "host.docker.internal" not in env["NO_PROXY"] and "ALL_PROXY" not in env
    assert env["HOME"] == sr.HOME and env["TMPDIR"] == "/tmp" and env["OPENCODE_DISABLE_AUTOUPDATE"] == "1"
    assert "SENTINEL_HOST_VAR" not in env and "SSH_AUTH_SOCK" not in env and not any("harness2-proxy" in v for v in env.values())
    assert i > 4


def test_stdin_stdout_stderr_and_the_exit_code_come_back(world):
    r = world.runner(env={"FAKE_SBX_EXEC_RC": "3", "FAKE_SBX_EXEC_OUT": "out\x1b[31mred\x1b[0m\n", "FAKE_SBX_EXEC_ERR": "err\n"}).run(
        world.spec(stdin=b"prompt over stdin\n"))
    assert (r.exit_code, r.stdout, r.stderr, r.timed_out) == (3, "outred\n", "err\n", False)
    assert (world.fake / "session_stdin").read_bytes() == b"prompt over stdin\n"
    session = [c for c in world.calls() if c[0] == "exec" and "-w" in c][0]
    assert session[1] == "-i"


def test_sbx_exit_125_is_a_failure_of_sbx_not_of_the_session(world):
    with pytest.raises(SessionError, match="125"):
        world.runner(env={"FAKE_SBX_EXEC_RC": "125", "FAKE_SBX_EXEC_ERR": "sandbox gone\n"}).run(world.spec())
    assert world.listed() == [] and world.rules() == []


# ── the filter is the real one ────────────────────────────────────────────────────

def test_requests_reach_our_proxy_and_the_route_filter_and_model_pinning_apply(world):
    cmd = (f'"$FAKE_SBX_PY" {CLIENT} "$FAKE_SBX_PORT" GET:/api/tags POST:/api/delete POST:/api/pull '
           f'POST:/v1/chat/completions:some-other-model GET:/api/ps')
    r = world.runner(env={"FAKE_SBX_SESSION": world.script(cmd + "\n")}).run(world.spec())
    assert r.stdout.split("\n")[:-1] == ["200 GET /api/tags", "403 POST /api/delete", "403 POST /api/pull",
                                         "403 POST /v1/chat/completions", "403 GET /api/ps"]
    summary = sr.egress_summary(r.egress_log)
    assert summary["model_requests"] == 1 and summary["egress_denied"] == 4
    assert summary["denied_requests"] == {"POST /api/delete": 1, "POST /api/pull": 1, "GET /api/ps": 1}
    assert "unix-route-model-denied" in summary["denied_reasons"] and "unix-route-path-denied" in summary["denied_reasons"]
    assert world.ollama.hits == 1 and world.ollama.rejected == []          # the pinned Host is one a real Ollama accepts
    assert any(line.startswith("ACCESS-SUMMARY ") and '"final":true' in line for line in r.egress_log)


def test_the_host_proxy_serves_only_the_session_port_and_dies_with_the_session(world):
    seen = {}
    cmd = f'echo "$FAKE_SBX_PORT" > "$FAKE_SBX_DIR/port"\n"$FAKE_SBX_PY" {CLIENT} "$FAKE_SBX_PORT" GET:/api/tags\n'
    world.runner(env={"FAKE_SBX_SESSION": world.script(cmd)}).run(world.spec())
    seen["port"] = int((world.fake / "port").read_text())
    assert port_closed(seen["port"])


# ── what the policy says decides whether the session starts ───────────────────────

def test_a_global_allow_rule_at_selection_refuses_before_any_sandbox_exists(world):
    allow = {"id": "global-allow", "scope": "global", "resource_type": "network", "decision": "allow", "resources": ["example.com"],
             "status": "active", "actions": ["net:connect:tcp"]}
    with pytest.raises(SessionRefused, match="global"):
        world.runner(env={"FAKE_SBX_EXTRA": json.dumps([allow])}).run(world.spec())
    assert "create" not in world.verbs() and world.listed() == []


def test_a_foreign_allow_rule_that_applies_to_the_sandbox_stops_the_session_before_the_cli_starts(world):
    allow = {"id": "late-global-allow", "scope": "global", "resource_type": "network", "decision": "allow",
             "resources": ["**"], "status": "active", "actions": ["net:connect:tcp"]}
    with pytest.raises(SessionError, match="unexpected allow"):
        world.runner(env={"FAKE_SBX_EXTRA_FOR_SANDBOX": json.dumps([allow])}).run(world.spec())
    assert not (world.fake / "session_argv").exists()
    assert world.listed() == [] and world.rules() == [] and world.runtime_leftovers() == []


def test_a_policy_check_that_allows_a_forbidden_target_stops_the_session(world):
    with pytest.raises(SessionError, match="does not deny"):
        world.runner(env={"FAKE_SBX_CHECK": "allowed"}).run(world.spec())
    assert not (world.fake / "session_argv").exists()
    assert world.listed() == [] and world.rules() == []


def test_a_vm_whose_proxy_is_not_the_gateway_is_refused(world):
    with pytest.raises(SessionError, match="HTTP_PROXY"):
        world.runner(env={"FAKE_SBX_GATEWAY": "http://evil.example:3128"}).run(world.spec())
    assert not (world.fake / "session_argv").exists() and world.listed() == []


def test_a_missing_cli_in_the_vm_is_a_failure_before_the_session(world):
    with pytest.raises(SessionError, match="CLI"):
        world.runner(env={"FAKE_SBX_NO_CLI": "1"}).run(world.spec())
    assert world.listed() == [] and world.rules() == []


@pytest.mark.parametrize("env, match", [
    ({"FAKE_SBX_CREATE": "fail"}, "create"),
    ({"FAKE_SBX_ALLOW_FAIL_AT": "3"}, "allow"),
    ({"FAKE_SBX_ALLOW_FAIL_AT": "1"}, "allow"),
])
def test_a_failure_while_building_the_route_tears_down_what_exists(world, env, match):
    with pytest.raises(SessionError, match=match):
        world.runner(env=env).run(world.spec())
    assert world.listed() == [] and world.rules() == [] and world.runtime_leftovers() == []
    assert not (world.fake / "session_argv").exists()


def test_an_unverified_sbx_version_refuses_before_anything_is_created(world):
    with pytest.raises(SessionRefused, match="verified"):
        world.runner(verified=frozenset({"0.0.1"})).run(world.spec())
    assert "create" not in world.verbs()


def test_a_plain_runner_trusts_only_the_versions_the_live_test_passed_on(world):
    """No `verified` argument: the default set is `VERIFIED_SBX_MODEL_VERSIONS`, which holds a version only after the live
    test passed on it. The fake reports 9.9.9, which nobody verified."""
    assert "9.9.9" not in ss.VERIFIED_SBX_MODEL_VERSIONS
    r = ss.SbxSessionRunner(str(FAKE), world.channel, zdr="strict", classification="open", environ=world.env,
                            workspace_roots=(str(world.root),), proposer_decision=sr.upstream_egress.Decision(True, ("ok",)))
    with pytest.raises(SessionRefused, match="verified"):
        r.run(world.spec())
    assert "create" not in world.verbs()


# ── lifecycle under failure, interrupt, timeout ───────────────────────────────────

def test_a_wall_clock_timeout_kills_the_client_and_removes_everything(world):
    t0 = time.monotonic()
    r = world.runner(env={"FAKE_SBX_EXEC_HANG": "1"}).run(world.spec(timeout_s=1))
    assert r.timed_out and r.exit_code is None and "timed out" in r.stderr and time.monotonic() - t0 < 30
    assert world.listed() == [] and world.rules() == [] and world.runtime_leftovers() == [] and r.leftovers == ()


def test_an_interrupt_in_the_middle_of_a_session_still_removes_rules_sandbox_proxy_and_files(world):
    def interrupt_when_running():
        for _ in range(1200):                         # up to 60 s: the proxy start and the create are slow on a loaded host
            if (world.fake / "session_argv").exists():
                _thread.interrupt_main()
                return
            time.sleep(0.05)

    t = threading.Thread(target=interrupt_when_running, daemon=True)
    t.start()
    with pytest.raises(KeyboardInterrupt):
        world.runner(env={"FAKE_SBX_EXEC_HANG": "1"}).run(world.spec(timeout_s=60))
    t.join()
    assert world.listed() == [] and world.rules() == [] and world.runtime_leftovers() == []
    verbs = world.verbs()
    assert verbs[-4:] == ["rm", "ls", "policy ls", "ls"] or "rm" in verbs


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP])
def test_a_terminate_signal_in_the_middle_of_a_session_runs_the_same_teardown_as_ctrl_c(world, sig):
    """A plain `kill`, a systemd stop, `timeout` or a closed terminal must not skip the `finally`: the microVM would keep
    running the CLI with the workspace mounted read-write and its allow rules in place."""
    seen: list[int] = []
    sentinel = lambda signum, frame: seen.append(signum)       # noqa: E731 - what the process had before the session
    old = signal.signal(sig, sentinel)
    during: list[object] = []

    def deliver_when_running():
        for _ in range(1200):                         # up to 60 s: the proxy start and the create are slow on a loaded host
            if (world.fake / "session_argv").exists():
                during.append(signal.getsignal(sig))
                os.kill(os.getpid(), sig)
                return
            time.sleep(0.05)

    t = threading.Thread(target=deliver_when_running, daemon=True)
    t.start()
    try:
        with pytest.raises(KeyboardInterrupt):
            world.runner(env={"FAKE_SBX_EXEC_HANG": "1"}).run(world.spec(timeout_s=60))
        t.join()
        assert seen == [] and during and during[0] is not sentinel         # the session had its own handler, not the old one
        assert signal.getsignal(sig) is sentinel                           # and the previous one is back afterwards
    finally:
        signal.signal(sig, old)
    assert world.listed() == [] and world.rules() == [] and world.runtime_leftovers() == []
    verbs = world.verbs()
    assert verbs[-4:] == ["rm", "ls", "policy ls", "ls"]                   # the whole teardown ran, with its proof


def test_the_previous_handlers_are_restored_after_a_normal_session_too(world):
    before = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)}
    world.runner().run(world.spec())
    assert {s: signal.getsignal(s) for s in before} == before


def test_the_terminate_handler_is_not_installed_outside_the_main_thread():
    out: list[object] = []

    def body():
        before = signal.getsignal(signal.SIGTERM)
        with ss._terminate_as_interrupt():
            out.append(signal.getsignal(signal.SIGTERM) is before)
    t = threading.Thread(target=body)
    t.start()
    t.join()
    assert out == [True]                                                   # nothing to install there, and nothing raised


def test_an_interrupt_inside_one_teardown_step_does_not_skip_the_others(world, monkeypatch):
    real = ss.SbxSessionRunner._remove_rules
    calls = []

    def flaky(self, name, ids):
        calls.append(1)
        if len(calls) == 1:
            raise KeyboardInterrupt
        return real(self, name, ids)

    monkeypatch.setattr(ss.SbxSessionRunner, "_remove_rules", flaky)
    with pytest.raises(KeyboardInterrupt):
        world.runner().run(world.spec())
    assert len(calls) == 2                                           # retried once, as `_Cleanup` does for every step
    assert world.listed() == [] and world.rules() == [] and world.runtime_leftovers() == []


def test_a_sandbox_that_will_not_go_away_is_reported_as_a_leftover_and_the_rest_is_still_cleaned(world):
    with pytest.raises(SessionCleanupError) as e:
        world.runner(env={"FAKE_SBX_RM": "leak"}).run(world.spec())
    left = " ".join(e.value.leftovers)
    assert "sandbox h2m-" in left and "directory" not in left and "process" not in left
    assert e.value.result is not None and e.value.result.exit_code == 0
    name = world.listed()[0]
    # the rules were removed one by one first, so the leaked sandbox has no route even though it is still there
    assert world.rules() == []
    assert ss.parse_sandbox_name(name)
    subprocess.run([str(FAKE), "rm", "--force", name], env={**os.environ, **world.env}, check=False)


def test_a_rule_that_cannot_be_removed_by_id_still_dies_with_its_sandbox(world):
    r = world.runner(env={"FAKE_SBX_POLICY_RM": "fail"}).run(world.spec())
    assert r.leftovers == () and world.rules() == [] and world.listed() == []


# ── teardown order: the proof comes before the host-side workspace fix ───────────

def test_the_workspace_is_fixed_only_after_the_teardown_proved_the_sandbox_gone(world, monkeypatch):
    order: list[str] = []
    real_left, real_reclaim = ss.SbxSessionRunner._leftovers_sbx, ss.SbxSessionRunner._reclaim_host

    def left(self, name, live):
        order.append("proof")
        return real_left(self, name, live)

    def reclaim(workspace):
        order.append("reclaim")
        return real_reclaim(workspace)

    monkeypatch.setattr(ss.SbxSessionRunner, "_leftovers_sbx", left)
    monkeypatch.setattr(ss.SbxSessionRunner, "_reclaim_host", staticmethod(reclaim))
    r = world.runner().run(world.spec())
    assert order == ["proof", "reclaim"] and r.reclaimed is True


def test_a_sandbox_that_is_still_listed_gets_no_host_side_chmod_walk(world, monkeypatch):
    """`sbx rm` failed: the VM may still run and change the mount while the host walks it (a swapped symlink)."""
    seen: list[str] = []
    monkeypatch.setattr(ss.SbxSessionRunner, "_reclaim_host", staticmethod(lambda ws: seen.append(ws) or True))
    with pytest.raises(SessionCleanupError) as e:
        world.runner(env={"FAKE_SBX_RM": "leak"}).run(world.spec())
    assert seen == [] and e.value.result is not None and e.value.result.reclaimed is False
    assert any(x.startswith("sandbox h2m-") for x in e.value.leftovers)
    subprocess.run([str(FAKE), "rm", "--force", world.listed()[0]], env={**os.environ, **world.env}, check=False)


def test_a_sandbox_listing_that_fails_also_blocks_the_host_side_chmod_walk(world, monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(ss.SbxSessionRunner, "_reclaim_host", staticmethod(lambda ws: seen.append(ws) or True))
    with pytest.raises(SessionCleanupError):
        world.runner(env={"FAKE_SBX_RM": "leak", "FAKE_SBX_LS": "fail"}).run(world.spec())
    assert seen == []
    subprocess.run([str(FAKE), "rm", "--force", world.listed()[0]], env={**os.environ, **world.env, "FAKE_SBX_LS": ""}, check=False)


# ── the scope of an added rule ────────────────────────────────────────────────────

def test_a_rule_added_with_the_wrong_scope_is_removed_again_and_the_session_stops_before_anything_runs(world):
    """`allow network --sandbox N` must say `(scope: sandbox:N)`. If it says global, the allow reaches EVERY sandbox: remove it
    (a global rule is removed without --sandbox), name it, and refuse."""
    with pytest.raises(SessionError, match="scope"):
        world.runner(env={"FAKE_SBX_ADDED_SCOPE": "global"}).run(world.spec())
    assert world.rules() == [] and world.listed() == [] and world.runtime_leftovers() == []
    calls = world.calls()
    assert any(c[:3] == ["policy", "rm", "network"] and "--sandbox" not in c and "--id" in c for c in calls)   # the global form
    assert not any(c[0] == "exec" for c in calls)                       # nothing ran in the sandbox


@pytest.mark.parametrize("scope", ["other", "none"])
def test_a_rule_that_names_another_sandbox_or_no_scope_is_refused_too(world, scope):
    with pytest.raises(SessionError, match="scope"):
        world.runner(env={"FAKE_SBX_ADDED_SCOPE": scope}).run(world.spec())
    assert world.listed() == [] and not any(c[0] == "exec" for c in world.calls())


def test_a_wrongly_scoped_rule_that_cannot_be_removed_is_reported_as_a_leftover_whatever_its_scope(world):
    """The sandbox goes with its own rules, but a GLOBAL allow to our port outlives it: it must be reported, not forgotten."""
    with pytest.raises(SessionCleanupError) as e:
        world.runner(env={"FAKE_SBX_ADDED_SCOPE": "global", "FAKE_SBX_POLICY_RM": "fail"}).run(world.spec())
    left = " ".join(e.value.leftovers)
    assert "rule rule-1-" in left and "sandbox h2m-" not in left
    assert len(world.rules()) == 1
    (world.fake / "rules").write_text("")


# ── the stale sweep ───────────────────────────────────────────────────────────────

ME = ss.current_owner()
needs_proc = pytest.mark.skipif(ME is None, reason="the owner identity needs /proc (Linux)")


def dead_pid() -> int:
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


def owned(session: str, *, pid: int, scope: str | None, owner: "ss.Owner | None" = None) -> str:
    """A sandbox name as a process with this pid and owner identity would have made it (default: this host, this pid namespace)."""
    return ss.sandbox_name(session, pid=pid, scope=scope, owner=owner or (ME if pid != os.getpid() else ME))


def seed(world, names, rules_for=()):
    with open(world.fake / "sandboxes", "a") as f:
        f.writelines(n + "\n" for n in names)
    with open(world.fake / "rules", "a") as f:
        for n in rules_for:
            f.write(f"old-{n}\t{n}\tGET\t/api/tags\tlocalhost:40000\n")


@needs_proc
def test_the_sweep_removes_only_h2m_sandboxes_of_dead_owners_in_this_scope(world, monkeypatch):
    monkeypatch.setenv(run_scope.ENV, "aaaaaaaa")
    dead = dead_pid()
    gone = ss.Owner(ME.boot, ME.ns, 1)                           # whatever it was, no process with this pid exists now
    mine_dead = owned("000000000001", pid=dead, scope="aaaaaaaa", owner=gone)
    mine_live = owned("000000000002", pid=os.getpid(), scope="aaaaaaaa")
    other_scope = owned("000000000003", pid=dead, scope="bbbbbbbb", owner=gone)
    unscoped = owned("000000000004", pid=dead, scope=None, owner=gone)
    users = ["my-own-sandbox", "harness2-aabbccddeeff", "claude-project"]
    seed(world, [mine_dead, mine_live, other_scope, unscoped, *users], rules_for=[mine_dead, mine_live])
    removed = world.runner().sweep_stale()
    assert removed == [f"sandbox {mine_dead}"]
    assert sorted(world.listed()) == sorted([mine_live, other_scope, unscoped, *users])
    assert [line.split("\t")[1] for line in world.rules()] == [mine_live]             # the dead one's rule went with it


@needs_proc
def test_without_a_run_scope_every_dead_owners_h2m_sandbox_is_removed(world, monkeypatch):
    monkeypatch.delenv(run_scope.ENV, raising=False)
    dead, gone = dead_pid(), ss.Owner(ME.boot, ME.ns, 1)
    names = [owned("00000000000a", pid=dead, scope=None, owner=gone), owned("00000000000b", pid=dead, scope="cccccccc", owner=gone)]
    live = owned("00000000000c", pid=os.getpid(), scope=None)
    seed(world, [*names, live, "somebody-elses"])
    assert sorted(world.runner().sweep_stale()) == sorted(f"sandbox {n}" for n in names)
    assert sorted(world.listed()) == sorted([live, "somebody-elses"])


@needs_proc
def test_a_recycled_pid_does_not_keep_a_leak_alive(world, monkeypatch):
    """The owner's pid is alive in this namespace, but it is another process (its start time differs): the sandbox is a leak."""
    monkeypatch.delenv(run_scope.ENV, raising=False)
    recycled = owned("0000000000a1", pid=os.getpid(), scope=None, owner=ss.Owner(ME.boot, ME.ns, ME.start + 7))
    seed(world, [recycled], rules_for=[recycled])
    assert world.runner().sweep_stale() == [f"sandbox {recycled}"]
    assert world.listed() == [] and world.rules() == []


@needs_proc
def test_a_sandbox_of_another_pid_namespace_is_left_alone_even_when_its_pid_does_not_exist_here(world, monkeypatch):
    """A process in a devcontainer or a second WSL distro shares the sbx daemon but not our pids: pid 42 over there is nothing here."""
    monkeypatch.delenv(run_scope.ENV, raising=False)
    foreign = owned("0000000000b1", pid=dead_pid(), scope=None, owner=ss.Owner(ME.boot, "f0000fff", 99))
    seed(world, [foreign], rules_for=[foreign])
    assert world.runner().sweep_stale() == []
    assert world.listed() == [foreign] and len(world.rules()) == 1


@needs_proc
def test_a_name_without_an_owner_is_never_swept_and_nor_is_anything_that_is_not_ours(world, monkeypatch):
    monkeypatch.delenv(run_scope.ENV, raising=False)
    legacy = ss.sandbox_name("0000000000d1", pid=dead_pid(), scope=None)               # a dead pid, but no proof of whose it was
    lookalikes = ["h2m-0-1-0badc0de-f0000000-1-aabbccddeeffaa", "H2M-0-1-aabbccddeeff", "h2-sbxm-leftover", "h2m-x"]
    seed(world, [legacy, *lookalikes])
    assert world.runner().sweep_stale() == []
    assert sorted(world.listed()) == sorted([legacy, *lookalikes])


@needs_proc
def test_a_run_sweeps_stale_sandboxes_before_it_creates_its_own(world):
    stale = owned("0000000000ee", pid=dead_pid(), scope=run_scope.current(), owner=ss.Owner(ME.boot, ME.ns, 1))
    seed(world, [stale], rules_for=[stale])
    world.runner().run(world.spec())
    verbs = world.calls()
    assert ["rm", "--force", stale] in verbs
    assert verbs.index(["rm", "--force", stale]) < next(i for i, c in enumerate(verbs) if c[0] == "create")
    assert world.listed() == [] and world.rules() == []


@needs_proc
@pytest.mark.parametrize("env", [
    pytest.param({"FAKE_SBX_VERSION": "v0.0.1"}, id="an unverified version"),
    pytest.param({"FAKE_SBX_EXTRA": json.dumps([{"id": "g", "scope": "global", "resource_type": "http", "decision": "allow", "resources": ["**"],
                                                  "status": "active", "methods": ["ANY"]}])}, id="a global http allow"),
    pytest.param({"FAKE_SBX_EXTRA": json.dumps([{"id": "x", "scope": "global", "resource_type": "port_forward", "decision": "allow"}])},
                 id="a rule of a kind nobody has seen"),
])
def test_a_leaked_sandbox_is_removed_by_the_next_run_even_when_its_preflight_refuses(world, env):
    """The hard-killed run's sandbox holds a running VM and a read-write mount: the sweep cannot wait for a preflight that passes."""
    stale = owned("0000000000ef", pid=dead_pid(), scope=run_scope.current(), owner=ss.Owner(ME.boot, ME.ns, 1))
    seed(world, [stale], rules_for=[stale])
    with pytest.raises(SessionRefused):
        world.runner(env=env).run(world.spec())
    assert world.listed() == [] and world.rules() == []
    assert "create" not in world.verbs()


@needs_proc
def test_a_leaked_sandbox_is_removed_by_the_next_run_even_when_the_daemon_policy_cannot_be_read(world):
    stale = owned("0000000000f0", pid=dead_pid(), scope=run_scope.current(), owner=ss.Owner(ME.boot, ME.ns, 1))
    seed(world, [stale])
    with pytest.raises(SessionRefused):
        world.runner(env={"FAKE_SBX_POLICY_FILE": str(world.root / "missing.json")}).run(world.spec())
    assert world.listed() == []


def test_a_sweep_that_cannot_list_sandboxes_does_not_stop_a_session_that_will_fail_on_its_own(world):
    r = world.runner(env={"FAKE_SBX_LS": "fail"})
    assert r.sweep_stale() == []


@needs_proc
def test_the_module_level_sweep_is_what_stage1_calls_whatever_the_model_runtime_is(world, monkeypatch):
    monkeypatch.delenv(run_scope.ENV, raising=False)
    stale = owned("0000000000a2", pid=dead_pid(), scope=None, owner=ss.Owner(ME.boot, ME.ns, 1))
    seed(world, [stale, "someone-elses"], rules_for=[stale])
    assert ss.sweep_stale_sandboxes(str(FAKE), world.env) == [f"sandbox {stale}"]
    assert world.listed() == ["someone-elses"] and world.rules() == []
    assert ss.sweep_stale_if_installed(world.env, lambda name: None) == []                  # no sbx on PATH: nothing at all
    seed(world, [stale])
    assert ss.sweep_stale_if_installed(world.env, lambda name: str(FAKE) if name == "sbx" else None) == [f"sandbox {stale}"]


def test_the_module_level_sweep_never_starts_a_daemon(world, tmp_path):
    """No socket that answers: not even `sbx ls` is run (asking could start the daemon)."""
    before = len(world.calls())
    env = {**world.env, "XDG_STATE_HOME": str(tmp_path / "elsewhere")}
    assert ss.sweep_stale_sandboxes(str(FAKE), env) == []
    assert len(world.calls()) == before


def test_stale_rule_sandboxes_is_gone():
    """It had no caller: rules go with their sandbox (`sbx rm`), so the sweep keys on the sandbox, never on a rule."""
    assert not hasattr(ss, "stale_rule_sandboxes")


# ── the rest of the contract ──────────────────────────────────────────────────────

def test_the_host_side_fix_makes_a_directory_the_session_left_unreadable_harvestable(world):
    cmd = ('WS=$(cut -f2 "$FAKE_SBX_DIR/workspaces" | head -1); mkdir "$WS/locked"; echo x > "$WS/locked/f"; '
           'echo changed > "$WS/out.txt"; chmod 000 "$WS/locked"\n')
    r = world.runner(env={"FAKE_SBX_SESSION": world.script(cmd)}).run(world.spec())
    assert r.reclaimed is True and r.harvest is not None
    assert (world.ws / "out.txt").read_text() == "changed\n" and (world.ws / "locked" / "f").exists()


def test_the_observer_hears_start_and_end_and_the_run_id_is_the_session_id(world):
    events = []
    r = world.runner(observer=lambda ev, f: events.append((ev, dict(f)))).run(world.spec())
    assert [e for e, _ in events] == ["session_start", "session_end"]
    assert events[0][1]["session"] == r.run_id == events[1][1]["session"]
    assert ss.parse_sandbox_name(next(c for c in world.calls() if c[0] == "create")[3])[2] == r.run_id
    assert events[1][1]["leftovers"] == 0 and events[1][1]["exit_code"] == 0


def test_a_solver_runner_needs_no_proposer_decision_and_says_so_in_the_result(world):
    r = world.runner(role="solver").run(world.spec())
    assert r.role == "solver"


def test_two_sessions_get_different_names_and_ports(world):
    runner = world.runner()
    runner.run(world.spec())
    first = [c for c in world.calls() if c[:3] == ["policy", "allow", "network"]][0][5]
    n1 = next(c for c in world.calls() if c[0] == "create")[3]
    runner.run(world.spec())
    creates = [c[3] for c in world.calls() if c[0] == "create"]
    assert creates[0] == n1 and creates[1] != n1 and len(set(creates)) == 2
    assert first.startswith("localhost:")


def test_the_budget_is_reserved_and_released_like_on_docker(world):
    from cognitive_harness2.sweep_budget import SweepBudget
    budget = SweepBudget(60, 30, 1)
    r = world.runner(budget=budget)
    r.run(world.spec())
    snap = budget.snapshot()
    assert snap["counts"]["proposer"] == 1 and snap["sessions"][0]["outcome"] == "ok"
    with pytest.raises(Exception, match="session cap"):
        r.run(world.spec())
    assert len([c for c in world.calls() if c[0] == "create"]) == 1


# ── the real sbx, opt-in ──────────────────────────────────────────────────────────

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


def _sbx_json(path: str, *args: str):
    p = subprocess.run([path, *args], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


@pytest.mark.requires_sbx_live
@pytest.mark.skipif(_live_ready() is not None, reason=_live_ready() or "")
def test_live_a_vm_reaches_our_proxy_and_nothing_else(tmp_path):
    """The whole route on a real sbx microVM: a STUB model on the host's loopback behind the real host bridge and egress
    proxy, a trivial in-VM client (the session's "CLI") that gets 200 on the allowed routes and the stub's answer, 403 on
    /api/delete, /api/pull, /api/ps, a pinned-out model, example.com, the host's ssh port, the real Ollama's port and the next
    port, and no data on a raw socket or a CONNECT tunnel. The global policy and `sbx ls` are the same afterwards.
    Run under the shared lock: `flock .../docker-tests.lock env COGNITIVE_HARNESS2_SBX_LIVE=1 uv run pytest ...`."""
    from real_cli_probe import MODEL_ID, StubModel

    path = shutil.which("sbx")
    assert path
    version = sandbox.real_sbx_probes(os.environ).version(path)
    assert version
    policy_before, ls_before = _sbx_json(path, "policy", "ls", "--json"), set(
        subprocess.run([path, "ls", "-q"], capture_output=True, text=True).stdout.split())
    root = Path(tempfile.mkdtemp(prefix="h2sbxl", dir=str(tmp_path))).resolve()
    ws = root / "ws"
    ws.mkdir()
    cli = root / "h2probe"
    cli.write_bytes((HERE / "fixtures" / "sbx_live_client.py").read_bytes())
    cli.chmod(0o755)
    with StubModel([]) as stub:
        profile = stage1.selftest_profile(root, stub.port)
        runner = ss.SbxSessionRunner(path, profile.channels[stage1.CHANNEL], zdr="strict", classification="restricted", role="solver",
                                     workspace_roots=profile.workspace_roots, verified=frozenset({version}),
                                     environ=dict(os.environ), create_timeout_s=900)
        spec = SessionSpec(argv=(sr.container_cli_path(str(cli)),), workspace=str(ws), cli=str(cli), timeout_s=120,
                           env={"H2_MODEL_URL": f"http://{sr.PROXY_ALIAS}:{stub.port}/v1", "H2_MODEL": MODEL_ID})
        try:
            r = runner.run(spec)
        finally:
            leaked = sorted(n for n in set(subprocess.run([path, "ls", "-q"], capture_output=True, text=True).stdout.split()) - ls_before
                            if ss.parse_sandbox_name(n))
            for n in leaked:                                       # belt and braces: the runner removes its own
                subprocess.run([path, "rm", "--force", n], capture_output=True)
    assert r.exit_code == 0 and r.leftovers == () and not r.timed_out, (r.stdout, r.stderr)
    got = json.loads(r.stdout.strip().splitlines()[-1])
    print("\nLIVE in-VM results:", json.dumps(got, sort_keys=True), f"\nsession wall {r.duration_s:.1f}s; egress:", sr.egress_summary(r.egress_log))
    assert (got["models"], got["tags"], got["chat"]) == (200, 200, 200), got
    for what in ("chat_other_model", "delete", "pull", "ps", "example_com", "host_ssh_port", "real_ollama_port", "next_port",
                 "ipv6_loopback", "ipv6_loopback_name"):
        assert got[what] == 403, (what, got)
    assert got["connect_tunnel"] != 200 and got["connect_tunnel"] in (403, "RemoteDisconnected", "ConnectionResetError", "ConnectionError",
                                                                      "TimeoutError", "timeout", "BrokenPipeError", 502, 400, 405), got
    assert got["direct_to_model_port"].startswith("NODATA") and got["direct_to_gateway_model_port"].startswith("NODATA"), got
    assert got["vm_loopback_model_port"] == "refused", got
    # the stub saw exactly the three allowed requests, with a Host a real Ollama accepts
    assert [(x["method"], x["path"].split("?")[0]) for x in stub.requests] == [
        ("GET", "/v1/models"), ("GET", "/api/tags"), ("POST", "/v1/chat/completions")], stub.requests
    assert stub.rejected == [], stub.rejected
    summary = sr.egress_summary(r.egress_log)
    assert summary["model_requests"] == 3 and "unix-route-model-denied" in summary["denied_reasons"], summary
    # the global policy is what it was and nothing of ours is left
    assert _sbx_json(path, "policy", "ls", "--json") == policy_before
    assert set(subprocess.run([path, "ls", "-q"], capture_output=True, text=True).stdout.split()) == ls_before
    assert not leaked


# ── live: SIGTERM and a hard kill, on a real microVM ──────────────────────────────

def _sbx_names(path: str) -> list[str]:
    return subprocess.run([path, "ls", "-q"], capture_output=True, text=True, timeout=60).stdout.split()


def _children_of(pid: int) -> list[int]:
    out = []
    for d in Path("/proc").iterdir():
        if d.name.isdigit():
            try:
                stat = (d / "stat").read_text()
                if int(stat[stat.rindex(")") + 2:].split()[1]) == pid:
                    out.append(int(d.name))
            except (OSError, ValueError, IndexError):
                pass
    return out


def _start_live_child(tmp_path, path: str):
    """A real session in its own process, up when its five rules are in place and the CLI (`sleep`) is running in the VM."""
    root = Path(tempfile.mkdtemp(prefix="h2sbxk", dir=str(tmp_path))).resolve()
    child = subprocess.Popen([sys.executable, str(HERE / "fixtures" / "sbx_live_session_child.py"), str(root), path],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    name = None
    for _ in range(600):
        if child.poll() is not None:
            pytest.fail(f"the child session ended early: {child.stderr.read()[-800:]}")        # type: ignore[union-attr]
        mine = [n for n in _sbx_names(path) if (p := ss.parse_sandbox_name(n)) and p[1] == child.pid]
        if mine:
            rules = [r for r in _sbx_json(path, "policy", "ls", "--json")["rules"] if r.get("scope") == f"sandbox:{mine[0]}"]
            if len(rules) == 5:
                name = mine[0]
                break
        time.sleep(0.5)
    assert name, "the session never got its five rules"
    time.sleep(8)                                          # the CLI is copied in and `sleep` is running in the VM
    proxies = [p for p in _children_of(child.pid) if "host_proxy_launcher" in Path(f"/proc/{p}/cmdline").read_text()]
    assert len(proxies) == 1
    return child, name, proxies[0]


@pytest.mark.requires_sbx_live
@pytest.mark.skipif(_live_ready() is not None, reason=_live_ready() or "")
def test_live_sigterm_during_a_real_session_removes_the_sandbox_its_rules_and_the_proxy(tmp_path):
    path = shutil.which("sbx")
    assert path
    ls_before, policy_before = set(_sbx_names(path)), _sbx_json(path, "policy", "ls", "--json")
    child, name, proxy = _start_live_child(tmp_path, path)
    try:
        child.send_signal(signal.SIGTERM)
        child.wait(timeout=180)
        assert child.returncode != 0
        assert name not in _sbx_names(path)
        assert not [r for r in _sbx_json(path, "policy", "ls", "--json")["rules"] if r.get("scope") == f"sandbox:{name}"]
        assert not Path(f"/proc/{proxy}").exists() or "host_proxy_launcher" not in Path(f"/proc/{proxy}/cmdline").read_text()
        assert set(_sbx_names(path)) == ls_before and _sbx_json(path, "policy", "ls", "--json") == policy_before
    finally:
        if child.poll() is None:
            child.kill()
        for n in set(_sbx_names(path)) - ls_before:
            if ss.parse_sandbox_name(n):
                subprocess.run([path, "rm", "--force", n], capture_output=True)


@pytest.mark.requires_sbx_live
@pytest.mark.skipif(_live_ready() is not None, reason=_live_ready() or "")
def test_live_a_hard_killed_session_leaks_a_sandbox_that_the_next_run_sweeps(tmp_path):
    """SIGKILL: no teardown. The proxy still dies with its parent (lifeline), the sandbox and its rules stay, and the next run's
    sweep removes them because the name proves the owner is gone."""
    path = shutil.which("sbx")
    assert path
    ls_before, policy_before = set(_sbx_names(path)), _sbx_json(path, "policy", "ls", "--json")
    child, name, proxy = _start_live_child(tmp_path, path)
    try:
        child.kill()
        child.wait(timeout=30)
        for _ in range(100):
            if not Path(f"/proc/{proxy}").exists():
                break
            time.sleep(0.1)
        assert not Path(f"/proc/{proxy}").exists(), "the host proxy outlived the killed runner"
        assert name in _sbx_names(path)                    # the leak: a running VM with its five allow rules
        assert len([r for r in _sbx_json(path, "policy", "ls", "--json")["rules"] if r.get("scope") == f"sandbox:{name}"]) == 5
        assert ss.sweep_stale_sandboxes(path, dict(os.environ)) == [f"sandbox {name}"]
        assert name not in _sbx_names(path)
        assert set(_sbx_names(path)) == ls_before and _sbx_json(path, "policy", "ls", "--json") == policy_before
    finally:
        if child.poll() is None:
            child.kill()
        for n in set(_sbx_names(path)) - ls_before:
            if ss.parse_sandbox_name(n):
                subprocess.run([path, "rm", "--force", n], capture_output=True)


# ── choosing the runner ───────────────────────────────────────────────────────────

def _docker_args():
    from cognitive_harness2.image import ImageRef
    return (sandbox.VerifiedDocker("/usr/bin/docker", "unix:///run/user/1000/docker.sock"),
            ImageRef("cognitive-harness2-proposer:0123456789ab", "sha256:" + "5" * 64))


def test_make_runner_gives_docker_for_docker_or_none_and_sbx_only_for_a_usable_sbx(world):
    verified, image = _docker_args()
    kw = dict(zdr="strict", classification="open", workspace_roots=(str(world.root),), role="solver")
    for rt in (ss.ModelRuntime("docker", True), None):
        r = ss.make_runner(rt, verified, image, world.channel, **kw)
        assert type(r) is sr.ProposerSessionRunner and r.verified is verified
    r = ss.make_runner(ss.ModelRuntime("sbx", True, str(FAKE), "0.50.1"), verified, image, world.channel, environ=world.env, **kw)
    assert type(r) is ss.SbxSessionRunner and isinstance(r, sr.ProposerSessionRunner) and r.verified is None and r.sbx_path == str(FAKE)
    assert r.local_endpoint == f"http://harness2-proxy.internal:{world.ollama.port}"          # the logical URL stage 1 builds its config from


@pytest.mark.parametrize("rt, match", [
    (ss.ModelRuntime("sbx", False, "/usr/bin/sbx", None, ("the daemon is down",), ("daemon-down",)), "the daemon is down"),
    (ss.ModelRuntime("refused", False, reasons=("COGNITIVE_HARNESS2_MODEL_SANDBOX='x' is not recognised",)), "not recognised"),
])
def test_make_runner_refuses_a_runtime_that_is_not_usable_and_never_returns_a_docker_runner(world, rt, match):
    verified, image = _docker_args()
    with pytest.raises(SessionRefused, match=match):
        ss.make_runner(rt, verified, image, world.channel, zdr="strict", classification="open", role="solver")


@pytest.mark.parametrize("extra", [dict(egress_network="h2-egress"), dict(_test_allow_cidrs=("10.0.0.0/8",))])
def test_make_runner_refuses_docker_only_options_on_the_sbx_route(world, extra):
    verified, image = _docker_args()
    with pytest.raises(SessionRefused, match="Docker"):
        ss.make_runner(ss.ModelRuntime("sbx", True, str(FAKE), "0.50.1"), verified, image, world.channel, zdr="strict",
                       classification="open", role="solver", environ=world.env, **extra)


def test_the_stage1_solver_environment_is_built_from_the_sbx_runners_logical_endpoint(world):
    from cognitive_harness2.stage1 import SolverJob, solver_env_builder
    r = world.runner(role="solver")
    job = SolverJob("household/sum-1", "r", "prompt", str(world.ws), "ollama/gemma4:12b", sr.container_cli_path(str(world.cli)))
    env = solver_env_builder(r)(job)
    assert f"http://harness2-proxy.internal:{world.ollama.port}/v1" in env["OPENCODE_CONFIG_CONTENT"]


@pytest.mark.requires_sbx_live
@pytest.mark.skipif(_live_ready() is not None, reason=_live_ready() or "")
def test_live_the_real_opencode_binary_reaches_the_stub_model_through_the_route(tmp_path):
    """The REAL opencode v2.0.25 in the VM, as a stage-1 SOLVER session (`stage1.solver_argv`, `solver_env_builder`) against the
    stub model: it must get through the sbx proxy and our filter, stream the stub's reply and exit 0. No model, no Ollama; the
    binary runs in the microVM, never on the host. Skipped when the binary is absent."""
    from real_cli_probe import AGENTS_MD, MODEL_ID, OPENCODE, TASK, StubModel

    if not OPENCODE.is_file():
        pytest.skip(f"no opencode at {OPENCODE}")
    from cognitive_harness2 import contained_proposer as cp, sum_signed

    path = shutil.which("sbx")
    assert path
    version = sandbox.real_sbx_probes(os.environ).version(path)
    ls_before = set(subprocess.run([path, "ls", "-q"], capture_output=True, text=True).stdout.split())
    root = Path(tempfile.mkdtemp(prefix="h2sbxo", dir=str(tmp_path))).resolve()
    ws = root / "ws"
    ws.mkdir()
    task = sum_signed.solver_task(TASK)
    (ws / "AGENTS.md").write_text(AGENTS_MD)
    for name, body in task.files.items():
        (ws / name).write_text(body)
    (ws / "output").mkdir()
    with StubModel([]) as stub:
        profile = stage1.selftest_profile(root, stub.port)
        ch = profile.channels[stage1.CHANNEL]
        runner = ss.SbxSessionRunner(path, ch, zdr="strict", classification="restricted", role="solver",
                                     workspace_roots=profile.workspace_roots, verified=frozenset({version}),
                                     environ=dict(os.environ), create_timeout_s=900)
        job = stage1.SolverJob(TASK, "sbx-live", task.prompt, str(ws), ch.model, sr.container_cli_path(str(OPENCODE)))
        spec = SessionSpec(argv=stage1.solver_argv(job), workspace=str(ws), cli=str(OPENCODE), timeout_s=300,
                           env=dict(stage1.solver_env_builder(runner)(job)), tmp_exec=stage1.TMP_EXEC["opencode"],
                           harvest=cp.DEFAULT_HARVEST)
        try:
            r = runner.run(spec)
        finally:
            for n in sorted(set(subprocess.run([path, "ls", "-q"], capture_output=True, text=True).stdout.split()) - ls_before):
                if ss.parse_sandbox_name(n):
                    subprocess.run([path, "rm", "--force", n], capture_output=True)
    events = []
    for line in r.stdout.splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            events.append({"_unparsed": line[:200]})
    print("\nLIVE opencode: exit", r.exit_code, "wall", f"{r.duration_s:.1f}s", "egress", sr.egress_summary(r.egress_log),
          "\nstub requests", [(x["method"], x["path"]) for x in stub.requests], "\nstderr", r.stderr[-400:])
    texts = [e["part"]["text"] for e in events if e.get("type") == "text"]
    assert r.exit_code == 0 and texts == ["STUB-DONE"], (r.exit_code, texts, r.stderr[-500:], r.stdout[-500:])
    posts = [x for x in stub.requests if x["method"] == "POST"]
    assert posts and posts[0]["model"] == MODEL_ID and stub.rejected == [], (stub.requests, stub.rejected)
    assert r.leftovers == () and set(subprocess.run([path, "ls", "-q"], capture_output=True, text=True).stdout.split()) == ls_before


def _gone(pid: int) -> bool:
    for _ in range(120):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        try:
            if os.waitpid(pid, os.WNOHANG) != (0, 0):          # not our child: only reached if it was reparented to us
                return True
        except ChildProcessError:
            pass
        time.sleep(0.05)
    return False


def test_the_host_proxy_dies_with_a_runner_that_is_killed_hard_and_survives_the_thread_that_spawned_it(tmp_path):
    """No teardown runs after SIGKILL: the proxy must not be left listening for a stale sandbox. It watches a pipe whose other end
    only the runner process holds (no `preexec_fn`, which can deadlock in a multithreaded parent; no `PR_SET_PDEATHSIG`, which is
    tied to the spawning THREAD, so it would kill the proxy as soon as that thread ended)."""
    parent = subprocess.Popen([sys.executable, str(HERE / "fixtures" / "pdeathsig_parent.py")], stdout=subprocess.PIPE, text=True)
    try:
        child = int(parent.stdout.readline())              # type: ignore[union-attr]
        time.sleep(0.5)
        os.kill(child, 0)                                  # alive although the thread that started it is long gone
        parent.kill()
        parent.wait()
        assert _gone(child), "the proxy outlived its killed parent"
    finally:
        if parent.poll() is None:
            parent.kill()


def test_the_proxy_is_not_spawned_with_preexec_fn(tmp_path, monkeypatch):
    seen: dict = {}
    real = subprocess.Popen

    class Spy(real):                                       # type: ignore[misc, valid-type]
        def __init__(self, *a, **kw):
            seen.update(kw, argv=a[0])
            super().__init__(["true"], **{k: v for k, v in kw.items() if k in ("stdin", "stdout", "stderr")})

    monkeypatch.setattr(ss.subprocess, "Popen", Spy)
    with open(tmp_path / "log", "wb") as log:
        proc, lifeline = ss._spawn_proxy(sys.executable, "/x/egress_proxy.py", str(tmp_path / "c.json"), log)
    try:
        assert "preexec_fn" not in seen and seen["start_new_session"] is True and seen["close_fds"] is True
        fd = seen["pass_fds"][0]
        argv = list(seen["argv"])
        assert argv[1:3] == ["-I", ss.launcher_file()] and argv[3:5] == ["--parent-fd", str(fd)] and argv[5] == "--" and lifeline != fd
        assert argv[6:] == ["/x/egress_proxy.py", "--config", str(tmp_path / "c.json")]
    finally:
        proc.wait()
        os.close(lifeline)


def _proxy_cfg(tmp_path) -> Path:
    port, forward = ss._free_ports(2)
    cfg = tmp_path / "p.json"
    cfg.write_text(json.dumps({"listen_host": "127.0.0.1", "listen_port": forward, "allow": []}))
    return cfg


def test_the_proxy_exits_gracefully_when_its_lifeline_pipe_closes(tmp_path):
    cfg = _proxy_cfg(tmp_path)
    with open(tmp_path / "log", "wb") as log:
        proc, lifeline = ss._spawn_proxy(sys.executable, egress_proxy.__file__, str(cfg), log)
    for _ in range(200):
        if egress_proxy.READY_LINE in (tmp_path / "log").read_text(errors="replace"):
            break
        time.sleep(0.05)
    assert proc.poll() is None
    os.close(lifeline)                                     # what the kernel does when the runner dies
    assert proc.wait(timeout=15) == 0
    assert "ACCESS-SUMMARY" in (tmp_path / "log").read_text(errors="replace")           # the same orderly stop as SIGTERM


def test_the_launcher_refuses_to_start_on_a_parent_fd_that_is_not_open_or_on_a_bad_command_line(tmp_path):
    cfg = _proxy_cfg(tmp_path)
    run = lambda *a: subprocess.run([sys.executable, "-I", ss.launcher_file(), *a], capture_output=True, text=True, timeout=30)  # noqa: E731
    p = run("--parent-fd", "987", "--", egress_proxy.__file__, "--config", str(cfg))
    assert p.returncode == 2 and "parent-fd" in p.stderr and "ready" not in p.stderr
    assert run("--parent-fd", "x", "--", egress_proxy.__file__).returncode == 2
    assert run(egress_proxy.__file__, "--config", str(cfg)).returncode == 2


def test_the_proxy_source_gained_no_host_only_option():
    """It is baked into the Docker image by content hash: the launcher exists so that changing it was not needed."""
    assert "parent-fd" not in Path(egress_proxy.__file__).read_text()


# ── round 3: the sbx teardown shields all three signals in one pass and retries like the Docker runner ──────────────────────

def test_the_sbx_shield_reads_all_three_handlers_before_ignoring_any(monkeypatch):
    events = []
    real_get, real_set = signal.getsignal, signal.signal
    monkeypatch.setattr(sr.signal, "getsignal", lambda s: (events.append(("get", s)), real_get(s))[1])
    monkeypatch.setattr(sr.signal, "signal", lambda s, h: (events.append(("set", s)), real_set(s, h))[1])
    sigs = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    olds = {s: real_get(s) for s in sigs}
    try:
        with ss._shielded_teardown():
            assert all(real_get(s) == signal.SIG_IGN for s in sigs)
        assert all(real_get(s) == olds[s] for s in sigs)
    finally:
        for s, h in olds.items():
            real_set(s, h)
    kinds = [k for k, _ in events]
    assert kinds[:3] == ["get"] * 3 and kinds[3:6] == ["set"] * 3 and {s for k, s in events if k == "set"} >= set(sigs)


def _counting_shield(monkeypatch):
    real, entries = ss._shielded_teardown, []

    @contextlib.contextmanager
    def shield():
        entries.append(1)
        with real():
            yield
    monkeypatch.setattr(ss, "_shielded_teardown", shield)
    return entries


def _flaky_teardown(monkeypatch, failures, calls=None):
    real, seen = ss.SbxSessionRunner._teardown_sbx, []

    def teardown(self, *a, **k):
        seen.append(tuple(signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)))
        if calls is not None:
            calls.append((self, a))
        if failures is None or len(seen) <= failures:
            raise KeyboardInterrupt(f"teardown call {len(seen)}")
        return real(self, *a, **k)
    monkeypatch.setattr(ss.SbxSessionRunner, "_teardown_sbx", teardown)
    return seen


def test_an_interrupted_teardown_is_retried_inside_one_shield_with_all_three_signals_ignored(world, monkeypatch):
    entries = _counting_shield(monkeypatch)
    seen = _flaky_teardown(monkeypatch, 2)
    with pytest.raises(KeyboardInterrupt):
        world.runner().run(world.spec())
    assert len(seen) == 3 and len(entries) == 1
    assert all(h == (signal.SIG_IGN,) * 3 for h in seen)                   # SIGHUP included, on every retry
    assert world.listed() == [] and world.rules() == [] and world.runtime_leftovers() == []


def test_an_interrupt_on_entering_the_shield_does_not_skip_the_teardown(world, monkeypatch):
    real, left = ss._shielded_teardown, [2]

    @contextlib.contextmanager
    def shield():
        if left[0] > 0:
            left[0] -= 1
            raise KeyboardInterrupt("second Ctrl-C at the finally entry")
        with real():
            yield
    monkeypatch.setattr(ss, "_shielded_teardown", shield)
    with pytest.raises(KeyboardInterrupt):
        world.runner().run(world.spec())
    assert world.listed() == [] and world.rules() == [] and world.runtime_leftovers() == []


def test_a_teardown_that_never_completes_is_a_cleanup_error_with_the_interrupt_as_its_cause(world, monkeypatch):
    calls: list = []
    real = ss.SbxSessionRunner._teardown_sbx
    _flaky_teardown(monkeypatch, None, calls)
    with pytest.raises(SessionCleanupError) as e:
        world.runner().run(world.spec())
    assert isinstance(e.value.__cause__, KeyboardInterrupt) and e.value.leftovers
    runner, args = calls[-1]                                               # nothing was torn down: do it now, for the fake's sake
    real(runner, *args[:3], sr._Cleanup())
    assert world.listed() == [] and world.rules() == [] and world.runtime_leftovers() == []


# ── round 3: a different boot id is proof only on the same machine; `removed` names only what `sbx rm` removed ──────────────

@pytest.fixture
def machine(tmp_path, monkeypatch):
    """This host with a known machine id, so the machine tag is the same on every CI machine. Returns (me, a tag of another machine)."""
    f = tmp_path / "machine-id"
    f.write_text("0123456789abcdef0123456789abcdef\n")
    monkeypatch.setattr(ss, "_MACHINE_ID_FILES", (str(f),))
    me = ss.current_owner()
    assert me is not None and me.machine
    return me, ("0000" if me.machine != "0000" else "ffff")


@needs_proc
def test_a_sandbox_of_an_earlier_boot_of_this_machine_is_removed(world, monkeypatch, machine):
    me, _ = machine
    monkeypatch.delenv(run_scope.ENV, raising=False)
    old = ss.sandbox_name("0000000000c1", pid=os.getpid(), scope=None, owner=ss.Owner("00000000", me.ns, me.start, me.machine))
    seed(world, [old], rules_for=[old])
    assert world.runner().sweep_stale() == [f"sandbox {old}"]
    assert world.listed() == []


@needs_proc
def test_a_sandbox_that_may_belong_to_another_kernel_on_the_same_daemon_is_left_alone(world, monkeypatch, machine):
    """A Lima VM and a devcontainer share one sbx daemon with different boot ids; neither owner is dead."""
    me, other = machine
    monkeypatch.delenv(run_scope.ENV, raising=False)
    foreign = ss.sandbox_name("0000000000c2", pid=dead_pid(), scope=None, owner=ss.Owner("00000000", me.ns, me.start, other))
    untagged = ss.sandbox_name("0000000000c3", pid=dead_pid(), scope=None, owner=ss.Owner("00000000", me.ns, me.start))
    seed(world, [foreign, untagged], rules_for=[foreign, untagged])
    assert world.runner().sweep_stale() == []
    assert sorted(world.listed()) == sorted([foreign, untagged]) and len(world.rules()) == 2


@needs_proc
def test_an_unreadable_machine_id_here_means_an_earlier_boot_is_not_judged(world, monkeypatch, machine, tmp_path):
    me, _ = machine
    monkeypatch.delenv(run_scope.ENV, raising=False)
    old = ss.sandbox_name("0000000000c4", pid=os.getpid(), scope=None, owner=ss.Owner("00000000", me.ns, me.start, me.machine))
    seed(world, [old])
    monkeypatch.setattr(ss, "_MACHINE_ID_FILES", (str(tmp_path / "gone"),))
    assert world.runner().sweep_stale() == [] and world.listed() == [old]


@needs_proc
@pytest.mark.parametrize("how", ["exit-1", "timeout", "oserror"])
def test_a_failed_or_timed_out_rm_is_not_reported_as_removed(world, monkeypatch, how):
    monkeypatch.delenv(run_scope.ENV, raising=False)
    gone = ss.Owner(ME.boot, ME.ns, 1)
    a, b = owned("0000000000d3", pid=dead_pid(), scope=None, owner=gone), owned("0000000000d4", pid=dead_pid(), scope=None, owner=gone)
    seed(world, [a, b])
    real = subprocess.run

    def run(argv, *args, **kw):
        if argv[1:3] == ["rm", "--force"] and argv[3] == a:
            if how == "timeout":
                raise subprocess.TimeoutExpired(argv, 1)
            if how == "oserror":
                raise OSError("gone")
            return subprocess.CompletedProcess(argv, 1, "", "error")
        return real(argv, *args, **kw)
    monkeypatch.setattr(ss.subprocess, "run", run)
    assert ss.sweep_stale_sandboxes(str(FAKE), world.env) == [f"sandbox {b}"]                    # only the one that really went
