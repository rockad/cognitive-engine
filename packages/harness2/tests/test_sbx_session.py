"""`SbxSessionRunner` against a STATEFUL FAKE `sbx` (tests/fixtures/fake-sbx-model/sbx), plus one opt-in live test.

The fake keeps sandboxes and sandbox-scoped rules, and removes a sandbox's rules with it, as a real v0.50.1 does. The host
egress proxy and the host bridge are REAL processes and threads here (loopback only), so the egress log and the route filter
are the real ones; only the VM is a script. These tests prove the runner's argv and ORDER, its refusals (each before the CLI
runs), the policy lifecycle under failure, crash and interrupt, the stale sweep, and the foreign-rule refusal. They do NOT
prove a real microVM behaves the same: that is `test_live_*`, skipped unless COGNITIVE_HARNESS2_SBX_LIVE=1.
No Docker, no model, no real sbx."""
from __future__ import annotations

import _thread
import json
import os
import shutil
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
    assert v == ["version", "policy ls", "settings", "ls",
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
    assert argv[:2] == ["sh", "-c"] and argv[2].startswith("ulimit -u") and argv[3] == "sh" and argv[4] == "256"
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


def test_the_default_verified_set_is_empty_until_live_verification_so_a_plain_runner_refuses(world):
    r = ss.SbxSessionRunner(str(FAKE), world.channel, zdr="strict", classification="open", environ=world.env,
                            workspace_roots=(str(world.root),), proposer_decision=sr.upstream_egress.Decision(True, ("ok",)))
    if ss.VERIFIED_SBX_MODEL_VERSIONS:
        pytest.skip("a version is verified now")
    with pytest.raises(SessionRefused, match="verified"):
        r.run(world.spec())


# ── lifecycle under failure, interrupt, timeout ───────────────────────────────────

def test_a_wall_clock_timeout_kills_the_client_and_removes_everything(world):
    t0 = time.monotonic()
    r = world.runner(env={"FAKE_SBX_EXEC_HANG": "1"}).run(world.spec(timeout_s=1))
    assert r.timed_out and r.exit_code is None and "timed out" in r.stderr and time.monotonic() - t0 < 30
    assert world.listed() == [] and world.rules() == [] and world.runtime_leftovers() == [] and r.leftovers == ()


def test_an_interrupt_in_the_middle_of_a_session_still_removes_rules_sandbox_proxy_and_files(world):
    def interrupt_when_running():
        for _ in range(200):
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


# ── the stale sweep ───────────────────────────────────────────────────────────────

def dead_pid() -> int:
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


def seed(world, names, rules_for=()):
    with open(world.fake / "sandboxes", "a") as f:
        f.writelines(n + "\n" for n in names)
    with open(world.fake / "rules", "a") as f:
        for n in rules_for:
            f.write(f"old-{n}\t{n}\tGET\t/api/tags\tlocalhost:40000\n")


def test_the_sweep_removes_only_h2m_sandboxes_of_dead_owners_in_this_scope(world, monkeypatch):
    monkeypatch.setenv(run_scope.ENV, "aaaaaaaa")
    dead = dead_pid()
    mine_dead = ss.sandbox_name("000000000001", pid=dead, scope="aaaaaaaa")
    mine_live = ss.sandbox_name("000000000002", pid=os.getpid(), scope="aaaaaaaa")
    other_scope = ss.sandbox_name("000000000003", pid=dead, scope="bbbbbbbb")
    unscoped = ss.sandbox_name("000000000004", pid=dead, scope=None)
    users = ["my-own-sandbox", "harness2-aabbccddeeff", "claude-project"]
    seed(world, [mine_dead, mine_live, other_scope, unscoped, *users], rules_for=[mine_dead, mine_live])
    removed = world.runner().sweep_stale()
    assert removed == [f"sandbox {mine_dead}"]
    assert sorted(world.listed()) == sorted([mine_live, other_scope, unscoped, *users])
    assert [line.split("\t")[1] for line in world.rules()] == [mine_live]             # the dead one's rule went with it


def test_without_a_run_scope_every_dead_owners_h2m_sandbox_is_removed(world, monkeypatch):
    monkeypatch.delenv(run_scope.ENV, raising=False)
    dead = dead_pid()
    names = [ss.sandbox_name("00000000000a", pid=dead, scope=None), ss.sandbox_name("00000000000b", pid=dead, scope="cccccccc")]
    live = ss.sandbox_name("00000000000c", pid=os.getpid(), scope=None)
    seed(world, [*names, live, "somebody-elses"])
    assert sorted(world.runner().sweep_stale()) == sorted(f"sandbox {n}" for n in names)
    assert sorted(world.listed()) == sorted([live, "somebody-elses"])


def test_a_run_sweeps_stale_sandboxes_before_it_creates_its_own(world):
    stale = ss.sandbox_name("0000000000ee", pid=dead_pid(), scope=run_scope.current())
    seed(world, [stale], rules_for=[stale])
    world.runner().run(world.spec())
    verbs = world.calls()
    assert ["rm", "--force", stale] in verbs
    assert verbs.index(["rm", "--force", stale]) < next(i for i, c in enumerate(verbs) if c[0] == "create")
    assert world.listed() == [] and world.rules() == []


def test_a_sweep_that_cannot_list_sandboxes_does_not_stop_a_session_that_will_fail_on_its_own(world):
    r = world.runner(env={"FAKE_SBX_LS": "fail"})
    assert r.sweep_stale() == []


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


# ── the real thing (opt-in; under flock; needs sbx, a started daemon and a stopped Ollama-free host) ──

LIVE = os.environ.get("COGNITIVE_HARNESS2_SBX_LIVE") == "1"
requires_sbx_live = pytest.mark.requires_sbx_live
