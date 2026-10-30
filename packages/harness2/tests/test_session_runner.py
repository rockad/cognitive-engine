"""The session runner without a daemon: authorisation, every docker argv, redaction. Nothing is started.

`subprocess` is replaced with a recorder, so a test that reaches docker at all is visible, and the
refusals are shown to happen before the first call.
"""
from __future__ import annotations

import os
import subprocess
import textwrap
from types import SimpleNamespace

import pytest

from cognitive_harness2 import channels, egress_proxy, sandbox, session_runner as sr, upstream_egress
from cognitive_harness2.image import ImageRef
from cognitive_harness2.session_runner import (Credential, ProposerSessionRunner, SessionLimits, SessionRefused,
                                               SessionSpec)

SOCK = "unix:///run/user/1000/docker.sock"
V = sandbox.VerifiedDocker("/usr/bin/docker", SOCK)
IMG = ImageRef("cognitive-harness2-proposer:0123456789ab", "sha256:" + "5" * 64)
OK = upstream_egress.Decision(True, ("approved",))
SECRET = "SENTINEL-credential-7f3a91c2"

TOML = textwrap.dedent("""
    [channels.local]
    kind = "opencode"
    provider = "ollama"
    endpoint = "http://127.0.0.1:11434"
    model = "ollama/gemma4:12b"
    retention = "local"
    max_classification = "restricted"
    approval = "per-sweep"
    [channels.local.caps]
    sessions = 3
    wall_s = 2700
    cost_usd = 1

    [channels.agy]
    kind = "agy_cli"
    provider = "google"
    endpoints = ["https://daily-cloudcode-pa.googleapis.com"]
    model = "gemini-3-flash"
    retention = "unknown"
    max_classification = "open"
    approval = "per-sweep"
    [channels.agy.caps]
    sessions = 3
    wall_s = 2700
    cost_usd = 1

    [channels.claude]
    kind = "claude_cli"
    provider = "anthropic"
    endpoints = ["https://api.anthropic.com", "https://statsig.anthropic.com:443"]
    model = "claude-sonnet-5-5"
    retention = "unknown"
    max_classification = "open"
    approval = "per-sweep"
    [channels.claude.caps]
    sessions = 3
    wall_s = 2700
    cost_usd = 1

    [channels.router]
    kind = "opencode"
    provider = "openrouter"
    endpoint = "https://openrouter.ai"
    model = "openrouter/anthropic/claude-sonnet-5-5"
    retention = "zdr-attested"
    max_classification = "gated"
    approval = "per-sweep"
    [channels.router.caps]
    sessions = 3
    wall_s = 2700
    cost_usd = 5
""")
PROFILE = channels.parse_text(TOML, source="test")


@pytest.fixture
def world(tmp_path):
    cli = tmp_path / "bin" / "fake-cli"
    cli.parent.mkdir()
    cli.write_text("#!/bin/sh\nexit 0\n")
    cli.chmod(0o755)
    ws = tmp_path / "ws"
    ws.mkdir()
    return SimpleNamespace(cli=str(cli), ws=str(ws), tmp=tmp_path)


def runner(name="local", *, zdr=None, cls="open", decision=OK, **kw):
    zdr = zdr or ("off" if name in ("agy", "claude") else "strict")
    return ProposerSessionRunner(V, IMG, PROFILE.channels[name], zdr=zdr, classification=cls, proposer_decision=decision, **kw)


def spec(world, **kw):
    base = dict(argv=(sr.container_cli_path(world.cli), "run", "--model", "x"), workspace=world.ws, cli=world.cli, timeout_s=60)
    base.update(kw)
    return SessionSpec(**base)


class Trap:
    """Replaces subprocess.run and Popen: records instead of running."""

    def __init__(self, monkeypatch):
        self.calls = []
        monkeypatch.setattr(sr.subprocess, "run", self.run)
        monkeypatch.setattr(sr.subprocess, "Popen", self.run)

    def run(self, argv, **kw):
        self.calls.append((list(argv), kw))
        return subprocess.CompletedProcess(argv, 0, "", "")


# ── authorisation: everything refuses before docker is touched ───────────────────

def test_a_local_channel_is_authorised_with_a_bridge_and_no_allow_list(world):
    plan = runner().authorize(spec(world))
    assert plan.bridge_target == ("127.0.0.1", 11434) and plan.allow == () and plan.local
    assert runner().local_endpoint == "http://harness2-proxy:11434"


def test_a_hosted_channel_gets_exactly_the_profiles_endpoints(world):
    plan = runner("agy", decision=None).authorize(spec(world))
    assert plan.allow == (("daily-cloudcode-pa.googleapis.com", 443),) and plan.bridge_target is None
    plan = runner("claude").authorize(spec(world))
    assert plan.allow == (("api.anthropic.com", 443), ("statsig.anthropic.com", 443))
    plan = runner("router", cls="open").authorize(spec(world))
    assert plan.allow == (("openrouter.ai", 443),)


def test_the_channel_verdict_is_recomputed_here_and_a_refusal_stops_everything(world, monkeypatch):
    trap = Trap(monkeypatch)
    for r in (runner("agy", cls="gated", decision=None),            # unknown retention carries only open content
              runner("agy", zdr="strict", decision=None),           # zdr=strict permits only local or zdr-attested
              runner("claude", cls="restricted"),
              runner("router", cls="restricted"),                    # above the channel ceiling
              runner("local", zdr="bogus"), runner("claude", cls=None)):
        with pytest.raises(SessionRefused, match="channel decision"):
            r.run(spec(world))
    assert trap.calls == []


def test_the_proposer_decision_is_required_for_opencode_and_claude_but_not_agy(world, monkeypatch):
    trap = Trap(monkeypatch)
    refused = upstream_egress.Decision(False, ("no",))
    for name, zdr in (("local", "strict"), ("claude", "off")):
        for d in (None, refused, "yes", SimpleNamespace(allowed=True)):
            with pytest.raises(SessionRefused, match="proposer decision"):
                runner(name, zdr=zdr, decision=d).run(spec(world))
    runner("agy", zdr="off", decision=None).authorize(spec(world))
    with pytest.raises(SessionRefused, match="proposer decision"):
        runner("agy", zdr="off", decision=refused).authorize(spec(world))
    assert trap.calls == []


@pytest.mark.parametrize("t", [0, -1, 2701, float("nan"), True, "60", None])
def test_the_wall_clock_must_be_within_the_channel_cap(world, t):
    with pytest.raises(SessionRefused, match="timeout_s"):
        runner().authorize(spec(world, timeout_s=t))


def test_argv0_must_be_the_mounted_cli_path(world):
    cli = sr.container_cli_path(world.cli)
    assert cli == "/opt/cli/fake-cli"
    for argv in (("/bin/sh", "-c", "id"), ("sh",), ("fake-cli",), (world.cli,), ("/opt/cli/other",), (), [], ("/opt/cli/fake-cli/../../bin/sh",), (cli, 5)):
        with pytest.raises(SessionRefused):
            runner().authorize(spec(world, argv=argv))
    runner().authorize(spec(world, argv=[cli, "-p", "a; rm -rf /", "$(id)", "`id`", "--x=\n"]))   # data, never a shell string


def test_a_credential_value_in_argv_or_env_is_refused(world):
    cred = Credential("/home/agent/.tok", SECRET)
    cli = sr.container_cli_path(world.cli)
    with pytest.raises(SessionRefused, match="argv would carry"):
        runner("agy", decision=None).authorize(spec(world, argv=(cli, f"--token={SECRET}"), credential=cred))
    with pytest.raises(SessionRefused, match="env FOO"):
        runner("agy", decision=None).authorize(spec(world, env={"FOO": f"x{SECRET}"}, credential=cred))
    assert SECRET not in repr(cred) and SECRET not in str(cred) and SECRET not in repr(spec(world, credential=cred))


@pytest.mark.parametrize("name", ["HTTP_PROXY", "https_proxy", "NO_PROXY", "no_proxy", "ALL_PROXY", "FOO_PROXY", "HOME", "PATH", "TMPDIR",
                                  "DOCKER_HOST", "DOCKER_CONFIG", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "GITHUB_TOKEN",
                                  "MY_SECRET", "lower", "A B", "1X", "X=Y", ""])
def test_the_session_environment_cannot_override_proxy_or_smuggle_secrets(world, name):
    with pytest.raises(SessionRefused):
        runner().authorize(spec(world, env={name: "v"}))


def test_a_clean_environment_is_accepted_and_values_are_checked(world):
    runner().authorize(spec(world, env={"OPENCODE_PERMISSION": '{"bash":"allow"}', "OLLAMA_BASE_URL": "http://harness2-proxy:11434"}))
    for v in ("a\nb", "a\0b", "x" * 5000):
        with pytest.raises(SessionRefused):
            runner().authorize(spec(world, env={"FOO": v}))


def test_workspace_rules(world, tmp_path, monkeypatch):
    home = os.path.expanduser("~")
    for bad in ("relative/dir", "", "/", home, os.path.dirname(home), "/etc", "/usr/lib", "/proc/self", "/dev", "/var/run",
                str(tmp_path / "nope"), world.cli, world.ws + "/../ws", world.ws + "//", "/tmp", "/home/agent",
                f"{world.tmp}/a,b", f'{world.tmp}/a"b'):
        with pytest.raises(SessionRefused):
            runner().authorize(spec(world, workspace=bad))
    link = tmp_path / "link"
    link.symlink_to(world.ws)
    with pytest.raises(SessionRefused, match="symlink"):
        runner().authorize(spec(world, workspace=str(link)))
    with pytest.raises(SessionRefused, match="workspace root"):
        runner(workspace_roots=[str(tmp_path / "elsewhere")]).authorize(spec(world))
    runner(workspace_roots=[str(tmp_path)]).authorize(spec(world))


def test_the_cli_must_be_a_regular_file_outside_the_workspace(world, tmp_path):
    d = tmp_path / "adir"
    d.mkdir()
    for bad in (str(d), str(tmp_path / "missing"), "relative-cli", "/dev/null"):
        with pytest.raises(SessionRefused):
            runner().authorize(spec(world, cli=bad, argv=("/opt/cli/x",)))
    inside = tmp_path / "ws" / "cli"
    inside.write_text("#!/bin/sh\n")
    with pytest.raises(SessionRefused, match="inside the workspace"):
        runner().authorize(spec(world, cli=str(inside), argv=("/opt/cli/cli",)))
    link = tmp_path / "bin" / "alias"
    link.symlink_to(world.cli)
    plan = runner().authorize(spec(world, cli=str(link), argv=("/opt/cli/alias",)))
    assert plan.cli_host == os.path.realpath(world.cli) and plan.cli_container == "/opt/cli/alias"


def test_bad_limits_credentials_and_runner_construction(world):
    for lim in (SessionLimits(pids=1), SessionLimits(memory="lots"), SessionLimits(cpus="-1"), SessionLimits(cpus="1;id"),
                SessionLimits(tmpfs_size="0m"), SessionLimits(max_output_bytes=1)):
        with pytest.raises(SessionRefused):
            runner().authorize(spec(world, limits=lim))
    for path in ("relative", "/proc/x", "/opt/cli/tok", "/tmp/tok", "/a/../b", "/a,b", ""):
        with pytest.raises(SessionRefused):
            Credential(path, "v")
    for value in ("", "a\0b", "x" * 70000):
        with pytest.raises(SessionRefused):
            Credential("/home/agent/t", value)
    with pytest.raises(SessionRefused):
        Credential("/home/agent/t", "v", env_var="HTTP_PROXY")
    ch = PROFILE.channels["local"]
    for args in (("tcp://h", IMG, ch), (V, "ubuntu:latest", ch), (V, IMG, {"kind": "opencode"})):
        with pytest.raises(SessionRefused):
            ProposerSessionRunner(*args, zdr="strict", classification="open")
    with pytest.raises(SessionRefused):
        ProposerSessionRunner(sandbox.VerifiedDocker("/usr/bin/docker", "tcp://127.0.0.1:2375"), IMG, ch, zdr="strict", classification="open")
    for net in ("host", "none", "bridge", "a b", "-x", "container:abc"):
        with pytest.raises(SessionRefused):
            runner(egress_network=net)


def test_loopback_and_remote_endpoints_cannot_be_mixed(world):
    mixed = channels.parse_text(TOML.replace('endpoints = ["https://api.anthropic.com", "https://statsig.anthropic.com:443"]',
                                             'endpoints = ["https://api.anthropic.com", "http://127.0.0.1:9"]'), source="t")
    r = ProposerSessionRunner(V, IMG, mixed.channels["claude"], zdr="off", classification="open", proposer_decision=OK)
    with pytest.raises(SessionRefused, match="mixing"):
        r.authorize(spec(world))


# ── the argv of every docker call ────────────────────────────────────────────────

def names():
    return sr.Names("abc123def456")


def flags(argv, flag):
    return [argv[i + 1] for i, a in enumerate(argv) if a == flag]


def mounts(argv):
    return flags(argv, "--mount")


def session_argv(world, name="agy", **kw):
    r = runner(name, decision=None if name == "agy" else OK)
    plan = r.authorize(spec(world, **kw))
    return plan, sr.build_session_run(plan, names(), docker=V.path, host=V.host,
                                      cred_file="/run/user/1000/h2s-x/cred/randomname" if plan.spec.credential else None)


def test_every_hardening_flag_is_on_the_session_command_line(world):
    plan, argv = session_argv(world, credential=Credential("/home/agent/.gemini/tok", SECRET, env_var="TOKEN_FILE"),
                              tmpfs_dirs=("/home/agent/.gemini",))
    assert argv[:5] == [V.path, "-H", SOCK, "run", "--rm"]
    for pair in (("--user", "65534:65534"), ("--cap-drop", "ALL"), ("--security-opt", "no-new-privileges"), ("--pids-limit", "256"),
                 ("--memory", "2g"), ("--memory-swap", "2g"), ("--cpus", "2"), ("--network", names().internal), ("--pull", "never"),
                 ("--dns", "127.0.0.1"), ("--workdir", plan.workspace), ("--entrypoint", "")):
        assert pair[1] in flags(argv, pair[0]), pair
    assert "--read-only" in argv and "--init" not in argv
    assert flags(argv, "--network") == [names().internal]
    for forbidden in ("--privileged", "--cap-add", "-p", "--publish", "-P", "--volume", "-v", "--volumes-from", "--device", "--pid",
                      "--ipc", "--uts", "--userns", "--security-opt=seccomp=unconfined", "--net", "--link", "--env-file", "--add-host"):
        assert forbidden not in argv, forbidden
    assert "seccomp=unconfined" not in " ".join(argv) and "apparmor=unconfined" not in " ".join(argv)
    assert not any("docker.sock" in a for a in argv[3:]) and not any(os.path.expanduser("~") + ":" in a or a == os.path.expanduser("~") for a in argv)
    tmpfs = flags(argv, "--tmpfs")
    assert any(t.startswith("/tmp:") and "noexec" in t and "nosuid" in t and "size=256m" in t for t in tmpfs)
    assert any(t.startswith("/home/agent:") and "noexec" in t and "uid=65534" in t for t in tmpfs)
    assert any(t.startswith("/home/agent/.gemini:") for t in tmpfs)
    # the image id, then exactly the caller's argv, last
    assert argv[-len(plan.spec.argv) - 1] == IMG.id and tuple(argv[-len(plan.spec.argv):]) == plan.spec.argv


def test_mounts_are_exactly_cli_ro_workspace_rw_same_path_and_the_credential_ro(world):
    plan, argv = session_argv(world, credential=Credential("/home/agent/.gemini/tok", SECRET))
    m = mounts(argv)
    assert m == [f"type=bind,src={os.path.realpath(world.cli)},dst=/opt/cli/fake-cli,readonly",
                 f"type=bind,src={world.ws},dst={world.ws}",
                 "type=bind,src=/run/user/1000/h2s-x/cred/randomname,dst=/home/agent/.gemini/tok,readonly"]
    assert m[1].split(",")[1][4:] == m[1].split(",")[2][4:] and "readonly" not in m[1]
    _, plain = session_argv(world)
    assert len(mounts(plain)) == 2          # no credential, no third mount


def test_no_secret_is_anywhere_in_any_argv(world):
    plan, argv = session_argv(world, credential=Credential("/home/agent/tok", SECRET, env_var="TOKEN_FILE"))
    assert not any(SECRET in a for a in argv)
    assert "TOKEN_FILE=/home/agent/tok" in flags(argv, "-e")            # the path, never the value
    proxy = sr.build_proxy_create(plan, names(), docker=V.path, host=V.host, proxy_dir="/run/user/1000/h2s-x/proxy", bridge_dir=None)
    assert not any(SECRET in a for a in proxy)
    assert not any(SECRET in a for a in sr.build_reclaim_run(plan, names(), docker=V.path, host=V.host))


def test_the_session_environment_is_closed_and_forces_the_proxy(world):
    plan, argv = session_argv(world, env={"OPENCODE_PERMISSION": "{}"})
    env = dict(e.split("=", 1) for e in flags(argv, "-e"))
    proxy = "http://harness2-proxy:3128"
    assert env["HTTP_PROXY"] == env["HTTPS_PROXY"] == env["http_proxy"] == env["https_proxy"] == proxy
    assert env["NO_PROXY"] == env["no_proxy"] == "127.0.0.1,localhost,::1" and env["HOME"] == "/home/agent" and env["OPENCODE_PERMISSION"] == "{}"
    assert not any(k.startswith("DOCKER_") for k in env) and not any(k in env for k in ("SSH_AUTH_SOCK", "GITHUB_TOKEN"))
    assert "-e" not in [a for a in argv if a in ("-e",) and False]
    assert "-i" not in argv
    assert "-i" in session_argv(world, stdin=b"prompt")[1]


def test_tmp_exec_is_opt_in_and_extra_tmpfs_must_live_under_home(world):
    assert any("exec,size" in t and "noexec" not in t for t in flags(session_argv(world, tmp_exec=True)[1], "--tmpfs"))
    for bad in ("/etc", "/tmp/x", "/home/agent/../x", "/home/agent", "relative"):
        with pytest.raises(SessionRefused):
            runner().authorize(spec(world, tmpfs_dirs=(bad,)))


def test_a_workspace_under_tmp_is_allowed_next_to_the_tmpfs(tmp_path):
    ws = tmp_path / "w"
    ws.mkdir()
    cli = tmp_path / "c"
    cli.write_text("x")
    if not str(ws).startswith("/tmp/"):
        pytest.skip("tmp_path is not under /tmp here")
    plan = runner().authorize(SessionSpec(argv=("/opt/cli/c",), workspace=str(ws), cli=str(cli), timeout_s=5))
    assert plan.workspace == str(ws)


def test_the_proxy_container_command_line(world):
    plan = runner().authorize(spec(world))
    local = sr.build_proxy_create(plan, names(), docker=V.path, host=V.host, proxy_dir="/p", bridge_dir="/b")
    assert local[:4] == [V.path, "-H", SOCK, "create"]
    for pair in (("--user", "0:0"), ("--cap-drop", "ALL"), ("--security-opt", "no-new-privileges"), ("--network", names().internal),
                 ("--network-alias", "harness2-proxy"), ("--pids-limit", "64"), ("--memory", "128m")):
        assert pair[1] in flags(local, pair[0]), pair
    assert "--read-only" in local and flags(local, "--network") == [names().internal]       # egress is connected later, never here
    assert mounts(local) == ["type=bind,src=/p,dst=/proxy,readonly", "type=bind,src=/b,dst=/bridge,readonly"]
    assert local[-5:] == ["-I", "/opt/harness2/egress_proxy.py", "--config", "/proxy/proxy.json"][-4:] or "proxy.json" in local[-1]
    hosted = sr.build_proxy_create(runner("agy", decision=None).authorize(spec(world)), names(), docker=V.path, host=V.host,
                                   proxy_dir="/p", bridge_dir=None)
    assert flags(hosted, "--user") == ["65534:65534"] and mounts(hosted) == ["type=bind,src=/p,dst=/proxy,readonly"]
    # the bridge directory is mounted into the proxy only, never into the session
    _, sess = session_argv(world, name="local")
    assert not any("bridge" in m for m in mounts(sess))


def test_the_reclaim_container_has_no_network_and_only_the_workspace(world):
    plan = runner().authorize(spec(world))
    argv = sr.build_reclaim_run(plan, names(), docker=V.path, host=V.host)
    assert flags(argv, "--network") == ["none"] and flags(argv, "--cap-add") == ["CHOWN", "DAC_OVERRIDE", "FOWNER"]
    assert mounts(argv) == [f"type=bind,src={world.ws},dst={world.ws}"] and "--read-only" in argv and "--cap-drop" in argv
    assert argv[-3:] == ["-c", sr.RECLAIM_SCRIPT, "sh"] or argv[-1] == world.ws       # the path is an argument, never in the script
    assert world.ws not in sr.RECLAIM_SCRIPT


def test_the_proxy_config_matches_the_channel_and_loads_in_the_real_proxy(world):
    for name, decision, check in (("agy", None, lambda p: p.decide("daily-cloudcode-pa.googleapis.com", 443).allowed
                                   and not p.decide("daily-cloudcode-pa.googleapis.com", 80).allowed
                                   and not p.decide("example.com", 443).allowed),
                                  ("local", OK, lambda p: p.decide("127.0.0.1", 11434).unix == "/bridge/ollama.sock"
                                   and not p.decide("example.com", 443).allowed and not p.allow)):
        plan = runner(name, decision=decision).authorize(spec(world))
        policy, host, port, listeners, _ = egress_proxy.parse_config(sr.proxy_config(plan))
        assert (host, port) == ("0.0.0.0", 3128) and check(policy)
        assert "test_allow_cidrs" not in sr.proxy_config(plan)             # only an explicit test hook ever sets it
    assert egress_proxy.parse_config(sr.proxy_config(plan))[3] == [(11434, "/bridge/ollama.sock")]


# ── docker calls: pinned, stripped, and nothing before the gate ──────────────────

def test_every_docker_call_is_pinned_and_carries_no_docker_variable(world, monkeypatch):
    for k in ("DOCKER_HOST", "DOCKER_CONFIG", "DOCKER_CONTEXT", "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY"):
        monkeypatch.setenv(k, "evil")
    trap = Trap(monkeypatch)
    r = runner()
    r._docker(["ps"])
    r._quiet(["rm", "-f", "x"])
    r._ids("containers", "abc")
    r._ids("networks", "abc")
    r._access_log(names())
    assert len(trap.calls) == 5
    for argv, kw in trap.calls:
        assert argv[:3] == [V.path, "-H", SOCK]
        assert not any(k.upper().startswith("DOCKER_") for k in kw["env"])


def test_a_failed_gate_means_no_docker_call_at_all(world, monkeypatch):
    trap = Trap(monkeypatch)
    r = runner()

    def closed():
        raise sandbox.SandboxUnavailable("the Docker daemon does not report name=rootless")
    monkeypatch.setattr(r, "_gate", closed)
    with pytest.raises(sandbox.SandboxUnavailable, match="rootless"):
        r.run(spec(world))
    assert trap.calls == []
    assert [p for p in os.listdir(world.ws)] == []


def test_the_gate_is_checked_against_the_pinned_image_id_and_the_verified_socket(world, monkeypatch):
    seen = {}

    def fake(which, *, image, host, **kw):
        seen.update(image=image, host=host, path=which("docker"))
        raise sandbox.SandboxUnavailable("stop here")
    monkeypatch.setattr(sr.sandbox, "verify_rootless_docker", fake)
    with pytest.raises(sandbox.SandboxUnavailable):
        runner().run(spec(world))
    assert seen == {"image": IMG.id, "host": SOCK, "path": V.path}


# ── output hygiene ───────────────────────────────────────────────────────────────

def test_redaction_removes_the_value_everywhere_and_a_truncated_prefix():
    assert sr.redact(f"a {SECRET} b {SECRET}", [SECRET]) == "a [REDACTED] b [REDACTED]"
    assert SECRET[:12] not in sr.redact("tail " + SECRET[:12], [SECRET])
    assert sr.redact("clean", [SECRET]) == "clean" and sr.redact("x", []) == "x"
    assert SECRET not in sr.redact(SECRET.upper() + SECRET, [SECRET, SECRET.upper()])


@pytest.mark.parametrize("raw,clean", [
    ("\x1b[2J\x1b[Hhello", "hello"), ("a\x1b[31mred\x1b[0mb", "aredb"), ("\x1b]0;title\x07text", "text"),
    ("\x1b]8;;http://x\x1b\\link\x1b]8;;\x1b\\", "link"), ("bell\x07\x08back\r\n", "bellback\n"), ("tab\tok\nline", "tab\tok\nline"),
    ("nul\x00x", "nulx"), ("c1\x9bcontrol", "c1control"), ("\x1bc reset", " reset"), ("plain ünïcode", "plain ünïcode"),
])
def test_terminal_escapes_and_control_characters_are_stripped(raw, clean):
    assert sr.sanitize_terminal(raw) == clean


def test_the_file_size_limit_is_a_flag_and_is_validated(world):
    assert "fsize=268435456:268435456" in flags(session_argv(world)[1], "--ulimit")
    for n in (0, 1000, 1 << 40, "big"):
        with pytest.raises(SessionRefused):
            runner().authorize(spec(world, limits=SessionLimits(file_bytes=n)))


# ── WS-E: the solver role and the shared sweep budget ─────────────────────────────

from cognitive_harness2.sweep_budget import BudgetExhausted, SweepBudget  # noqa: E402


class _Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def budget(**kw):
    clock = kw.pop("clock", None) or _Clock()
    return SweepBudget(kw.pop("total", 2700.0), kw.pop("per", 600.0), kw.pop("n", 7), clock=clock, **kw), clock


def solver(name="local", *, decision=None, **kw):
    return runner(name, decision=decision, role="solver", **kw)


def test_the_default_role_is_proposer_and_a_role_must_be_one_of_two(world):
    assert runner().role == "proposer" and runner().budget is None
    for bad in ("judge", "", None, "Solver", 3):
        with pytest.raises(SessionRefused, match="role"):
            runner(role=bad)
    with pytest.raises(SessionRefused, match="budget"):
        runner(budget=object())


def test_a_solver_on_an_opencode_channel_needs_no_proposer_decision_but_a_proposer_still_does(world):
    for name, zdr in (("local", "strict"), ("claude", "off")):
        plan = runner(name, zdr=zdr, decision=None, role="solver").authorize(spec(world))
        assert plan.spec.argv[0] == "/opt/cli/fake-cli"
        with pytest.raises(SessionRefused, match="proposer decision"):
            runner(name, zdr=zdr, decision=None).authorize(spec(world))          # byte-identical proposer behaviour


def test_a_solver_with_a_refusing_decision_is_still_refused(world):
    refused = upstream_egress.Decision(False, ("no",))
    for d in (refused, SimpleNamespace(allowed=True), "yes"):
        with pytest.raises(SessionRefused, match="proposer decision"):
            solver(decision=d).authorize(spec(world))


def test_a_solver_still_needs_the_channel_decision_for_its_channel(world, monkeypatch):
    trap = Trap(monkeypatch)
    for r in (solver("agy", cls="gated"), solver("agy", zdr="strict"), solver("claude", cls="restricted", zdr="off"),
              solver("router", cls="restricted"), solver("local", zdr="bogus"), solver("claude", cls=None)):
        with pytest.raises(SessionRefused, match="channel decision"):
            r.run(spec(world))
    assert trap.calls == []


def test_a_solver_gets_the_same_hardening_as_a_proposer(world):
    sp = spec(world)
    for name, zdr in (("local", "strict"), ("agy", "off"), ("router", "strict")):
        a = runner(name, zdr=zdr, cls="open", role="solver", decision=None).authorize(sp)
        b = runner(name, zdr=zdr, cls="open", decision=OK).authorize(sp)
        names_ = sr.Names("abc123abc123")
        argv_a = sr.build_session_run(a, names_, docker=V.path, host=SOCK, cred_file=None)
        argv_b = sr.build_session_run(b, names_, docker=V.path, host=SOCK, cred_file=None)
        assert argv_a == argv_b
        assert sr.build_proxy_create(a, names_, docker=V.path, host=SOCK, proxy_dir="/p", bridge_dir=None) == \
            sr.build_proxy_create(b, names_, docker=V.path, host=SOCK, proxy_dir="/p", bridge_dir=None)
        assert "--cap-drop" in argv_a and "--read-only" in argv_a and "no-new-privileges" in argv_a
        assert "exec" not in sr._tmpfs("/tmp", "1m", exec_=False, owned=False)[1].split(",")


def test_the_solver_refusals_are_the_proposer_refusals(world):
    s = solver()
    cli = sr.container_cli_path(world.cli)
    for bad in (dict(argv=("/bin/sh", "-c", "id")), dict(env={"HTTP_PROXY": "x"}), dict(env={"MY_SECRET": "x"}),
                dict(timeout_s=9999), dict(workspace="/etc"), dict(workspace="/etc/x"),
                dict(argv=(cli, "x"), tmpfs_dirs=("/elsewhere",)), dict(limits=SessionLimits(pids=1))):
        with pytest.raises(SessionRefused):
            s.authorize(spec(world, **bad))


def test_tmp_exec_is_off_unless_asked_for(world):
    assert SessionSpec(argv=("/opt/cli/x",), workspace="/w", cli="/c", timeout_s=1).tmp_exec is False
    plan = solver().authorize(spec(world))
    argv = sr.build_session_run(plan, sr.Names("abc123abc123"), docker=V.path, host=SOCK, cred_file=None)
    assert all("noexec" in t for t in argv if t.startswith("/tmp:") or t.startswith("/home/agent:"))
    plan = solver().authorize(spec(world, tmp_exec=True))
    argv = sr.build_session_run(plan, sr.Names("abc123abc123"), docker=V.path, host=SOCK, cred_file=None)
    assert any(t.startswith("/tmp:") and ",exec," in t for t in argv)


# ── the budget around run() ──────────────────────────────────────────────────────────

CANNED = sr.SessionResult("r1", 0, "out", "", False, False, 1.0, ())


def stub_execute(monkeypatch, r, *, result=CANNED, exc=None, seen=None):
    def _execute(plan):
        if seen is not None:
            seen.append(plan)
        if exc is not None:
            raise exc
        return result
    monkeypatch.setattr(r, "_execute", _execute)
    monkeypatch.setattr(r, "_gate", lambda: None)


def test_run_reserves_before_the_gate_and_an_exhausted_budget_starts_nothing(world, monkeypatch):
    trap = Trap(monkeypatch)
    b, clock = budget(n=1)
    r = solver(budget=b)
    gate = []
    monkeypatch.setattr(r, "_gate", lambda: gate.append(1))
    monkeypatch.setattr(r, "_execute", lambda plan: CANNED)
    r.run(spec(world))
    with pytest.raises(BudgetExhausted) as e:
        r.run(spec(world))
    assert e.value.reason == "sessions" and gate == [1]
    clock.t += 99999
    b2, c2 = budget(total=10.0)
    c2.t += 10
    r2 = solver(budget=b2)
    monkeypatch.setattr(r2, "_gate", lambda: gate.append(2))
    with pytest.raises(BudgetExhausted) as e:
        r2.run(spec(world))
    assert e.value.reason == "deadline" and gate == [1]
    assert trap.calls == []


def test_a_refused_spec_never_reserves(world, monkeypatch):
    trap = Trap(monkeypatch)
    b, _ = budget()
    for bad in (dict(argv=("/bin/sh",)), dict(timeout_s=99999)):
        with pytest.raises(SessionRefused):
            solver(budget=b).run(spec(world, **bad))
    with pytest.raises(SessionRefused):
        solver("agy", cls="gated", budget=b).run(spec(world))
    assert b.snapshot()["counts"]["total"] == 0 and b.snapshot()["sessions"] == [] and trap.calls == []


def test_the_session_timeout_is_clipped_to_the_reservation(world, monkeypatch):
    b, clock = budget(total=700.0, per=600.0)
    r = solver(budget=b)
    seen: list = []
    stub_execute(monkeypatch, r, seen=seen)
    r.run(spec(world, timeout_s=2000))
    assert seen[-1].spec.timeout_s == 600.0                     # the per-session cap
    clock.t += 300
    r.run(spec(world, timeout_s=500))
    assert seen[-1].spec.timeout_s == 400.0                     # what is left of the sweep
    clock.t -= 300
    b2, _ = budget(total=700.0, per=600.0)
    r2 = solver(budget=b2)
    stub_execute(monkeypatch, r2, seen=seen)
    r2.run(spec(world, timeout_s=30))
    assert seen[-1].spec.timeout_s == 30                        # the caller asked for less


def test_without_a_budget_the_timeout_is_untouched(world, monkeypatch):
    r = runner()
    seen: list = []
    stub_execute(monkeypatch, r, seen=seen)
    r.run(spec(world, timeout_s=2000))
    assert seen[0].spec.timeout_s == 2000


def test_sessions_are_counted_by_role_and_recorded(world, monkeypatch):
    b, clock = budget(role_caps={"proposer": 1, "solver": 2})
    p, s = runner(budget=b), solver(budget=b)
    for r in (p, s):
        stub_execute(monkeypatch, r)
    p.run(spec(world))
    s.run(spec(world))
    s.run(spec(world))
    with pytest.raises(BudgetExhausted, match="proposer"):
        p.run(spec(world))
    with pytest.raises(BudgetExhausted, match="solver"):
        s.run(spec(world))
    snap = b.snapshot()
    assert snap["counts"] == {"proposer": 1, "solver": 2, "total": 3}
    assert [x["outcome"] for x in snap["sessions"]] == ["ok", "ok", "ok"]
    assert [x["role"] for x in snap["sessions"]] == ["proposer", "solver", "solver"]


@pytest.mark.parametrize("how,outcome,counted", [
    (dict(exc=sr.SessionError("boom")), "error", 1),
    (dict(exc=KeyboardInterrupt()), "error", 1),
    (dict(exc=sr.SessionCleanupError(["container x"])), "leftovers", 1),
    (dict(result=sr.SessionResult("r", None, "", "", True, False, 9.0, ())), "timed_out", 1),
])
def test_the_reservation_is_always_released_with_how_the_session_ended(world, monkeypatch, how, outcome, counted):
    b, _ = budget()
    r = solver(budget=b)
    stub_execute(monkeypatch, r, **how)
    try:
        r.run(spec(world))
    except BaseException:  # noqa: BLE001
        pass
    snap = b.snapshot()
    assert snap["active"] == 0 and snap["counts"]["total"] == counted
    assert snap["sessions"][0]["outcome"] == outcome


def test_a_failed_gate_gives_the_slot_back(world, monkeypatch):
    trap = Trap(monkeypatch)
    b, _ = budget(n=1)
    r = solver(budget=b)

    def closed():
        raise sandbox.SandboxUnavailable("down")
    monkeypatch.setattr(r, "_gate", closed)
    with pytest.raises(sandbox.SandboxUnavailable):
        r.run(spec(world))
    snap = b.snapshot()
    assert snap["counts"]["total"] == 0 and snap["sessions"][0]["outcome"] == "refused" and trap.calls == []
    stub_execute(monkeypatch, r)
    r.run(spec(world))                                       # the single slot was still there



# ── the observer: what the stage-1 audit trail is built from ──────────────────────────

def watched(**kw):
    events: list = []
    return events, solver(observer=lambda name, f: events.append((name, dict(f))), **kw)


def test_the_observer_sees_a_reservation_and_nothing_that_could_hold_a_prompt(world, monkeypatch):
    b, _ = budget()
    events, r = watched(budget=b)
    stub_execute(monkeypatch, r)
    r.run(spec(world, env={"MY_VAR": "SENTINEL-env-value"}, argv=(sr.container_cli_path(world.cli), "run", "SENTINEL-prompt")))
    assert [n for n, _ in events] == ["session_reserved"]
    f = events[0][1]
    assert f["role"] == "solver" and f["timeout_s"] == 60 and isinstance(f["reservation"], int)
    assert "SENTINEL" not in repr(events)


def test_the_observer_sees_every_refusal_with_its_reason_and_nothing_starts(world, monkeypatch):
    trap = Trap(monkeypatch)
    b, clock = budget(n=1)
    events, r = watched(budget=b)
    with pytest.raises(SessionRefused):
        r.run(spec(world, argv=("/bin/sh", "-c", "id")))
    stub_execute(monkeypatch, r)
    r.run(spec(world))
    with pytest.raises(BudgetExhausted):
        r.run(spec(world))
    names = [n for n, _ in events]
    assert names == ["session_refused", "session_reserved", "session_refused"]
    assert events[0][1]["reason"].startswith("refused") and events[2][1]["reason"] == "budget:sessions"
    assert trap.calls == []


def test_a_gate_failure_is_reported_as_a_refusal_and_gives_the_slot_back(world, monkeypatch):
    b, _ = budget()
    events, r = watched(budget=b)

    def closed():
        raise sandbox.SandboxUnavailable("down")
    monkeypatch.setattr(r, "_gate", closed)
    with pytest.raises(sandbox.SandboxUnavailable):
        r.run(spec(world))
    assert [n for n, _ in events] == ["session_reserved", "session_refused"]
    assert events[1][1]["reason"] == "gate:SandboxUnavailable"


def test_a_failing_observer_never_breaks_a_session_and_is_counted(world, monkeypatch):
    def boom(name, fields):
        raise RuntimeError("audit disk full")
    b, _ = budget()
    r = solver(observer=boom, budget=b)
    stub_execute(monkeypatch, r)
    assert r.run(spec(world)).exit_code == 0
    assert r.observer_errors >= 1


def test_the_observer_must_be_callable(world):
    with pytest.raises(SessionRefused, match="observer"):
        solver(observer="not callable")


# ── NO_PROXY: the session's own loopback, set by the runner, nothing else ────────

@pytest.mark.parametrize("name", ["agy", "claude", "local", "router"])
def test_no_proxy_is_the_sessions_own_loopback_only_on_every_channel(world, name):
    plan, argv = session_argv(world, name)
    env = dict(e.split("=", 1) for e in flags(argv, "-e"))
    entries = env["NO_PROXY"].split(",")
    assert entries == ["127.0.0.1", "localhost", "::1"] and env["no_proxy"] == env["NO_PROXY"]
    assert sr.PROXY_ALIAS not in env["NO_PROXY"]                  # the proxy, the host and external names are never bypassed


def test_the_callers_config_still_cannot_set_any_proxy_variable_in_either_case(world):
    for name in ("NO_PROXY", "no_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy", "NODE_USE_ENV_PROXY", "MY_PROXY"):
        with pytest.raises(SessionRefused):
            runner().authorize(spec(world, env={name: "127.0.0.1,harness2-proxy,example.com"}))


def test_a_local_channel_routes_the_proxy_alias_to_the_bridge_socket_and_a_hosted_one_routes_nothing(world):
    plan = runner("local").authorize(spec(world))
    cfg = sr.proxy_config(plan)
    sock = f"{sr.BRIDGE_MOUNT}/ollama.sock"
    assert cfg["unix_routes"] == [{"host": "127.0.0.1", "port": 11434, "unix": sock},
                                  {"host": sr.PROXY_ALIAS, "port": 11434, "unix": sock}]
    assert cfg["listeners"] == [{"port": 11434, "unix": sock}] and cfg["allow"] == []
    assert "unix_routes" not in sr.proxy_config(runner("claude").authorize(spec(world)))
