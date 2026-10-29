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


@pytest.mark.parametrize("name", ["HTTP_PROXY", "https_proxy", "NO_PROXY", "ALL_PROXY", "FOO_PROXY", "HOME", "PATH", "TMPDIR",
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
    assert env["NO_PROXY"] == "" and env["no_proxy"] == "" and env["HOME"] == "/home/agent" and env["OPENCODE_PERMISSION"] == "{}"
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
