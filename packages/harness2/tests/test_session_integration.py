"""Contained proposer sessions on the REAL rootless daemon, driven by a FAKE CLI (tests/fixtures/fake-cli).

No model is called and no real CLI runs. The "remote endpoint" is a throwaway container on a throwaway
network; the "local model" is an echo server on this host's loopback. These do not skip: a daemon that is
not rootless, or a missing image, fails them, which is the correct signal.

Threat-model coverage (~/.agents/cognitive-engine-wsb-threat-model.md) is noted per test.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import socket
import stat
import subprocess
import textwrap
import threading
import time
from pathlib import Path

import pytest

from cognitive_harness2 import channels, sandbox, session_runner as sr, upstream_egress
from cognitive_harness2.image import ImageRef
from cognitive_harness2.session_runner import (Credential, ProposerSessionRunner, SessionLimits, SessionRefused,
                                               SessionSpec)

pytestmark = pytest.mark.requires_rootless_docker

FIX = Path(__file__).resolve().parent / "fixtures"
CLI = str(FIX / "fake-cli")
CLI_IN = "/opt/cli/fake-cli"
OK = upstream_egress.Decision(True, ("approved",))
CANARY = "CANARY-VALUE-do-not-leak-31337"
UPSTREAM_HOST = "upstream.harness2.test"


# ── helpers ──────────────────────────────────────────────────────────────────────

def docker(verified, *args, timeout=60) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("DOCKER_")}
    return subprocess.run([verified.path, "-H", verified.host, *args], capture_output=True, text=True, timeout=timeout, env=env)


def session_leftovers(verified) -> list[str]:
    out = docker(verified, "ps", "-a", "-q", "--filter", f"label={sr.LABEL}").stdout.split()
    out += docker(verified, "network", "ls", "-q", "--filter", f"label={sr.LABEL}").stdout.split()
    return out


def runtime_dirs() -> list[str]:
    base = sr._runtime_base_dir()
    return sorted(n for n in os.listdir(base) if n.startswith(("h2s-", "h2bridge-")))


def kv(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        k, sep, v = line.partition("=")
        if sep:
            out[k] = v
    return out


def profile(endpoint: str, *, kind="agy_cli", retention="unknown", ceiling="open", wall_s=600) -> channels.Channel:
    plural = "endpoints" if kind != "opencode" else "endpoint"
    value = json.dumps([endpoint]) if plural == "endpoints" else json.dumps(endpoint)
    text = textwrap.dedent(f"""
        [channels.t]
        kind = "{kind}"
        provider = "{'ollama' if kind == 'opencode' else 'google'}"
        {plural} = {value}
        model = "{'ollama/gemma4:12b' if kind == 'opencode' else 'gemini-3-flash'}"
        retention = "{retention}"
        max_classification = "{ceiling}"
        approval = "per-sweep"
        [channels.t.caps]
        sessions = 3
        wall_s = {wall_s}
        cost_usd = 1
    """)
    return channels.parse_text(text, source="test").channels["t"]


@pytest.fixture
def ws(tmp_path):
    d = tmp_path / "ws"
    d.mkdir()
    d.chmod(0o777)            # the container user (65534) is not the host user; see the README on workspaces
    return d


@pytest.fixture(scope="module")
def egress_world(verified, proposer_image, tmp_path_factory):
    """A throwaway egress network with one 'remote' endpoint on it: upstream.harness2.test:9000 (and :9001)."""
    suffix = secrets.token_hex(4)
    net, up = f"h2t-{suffix}-egr", f"h2t-{suffix}-up"
    assert docker(verified, "network", "create", "--label", "cognitive-harness2.test=1", net).returncode == 0
    script = tmp_path_factory.mktemp("up") / "upstream.py"
    script.write_bytes((FIX / "upstream.py").read_bytes())
    script.chmod(0o644)
    script.parent.chmod(0o755)
    try:
        p = docker(verified, "run", "-d", "--name", up, "--label", "cognitive-harness2.test=1", "--network", net,
                   "--network-alias", UPSTREAM_HOST, "--user", "65534:65534", "--read-only", "--cap-drop", "ALL",
                   "--pull", "never", "--mount", f"type=bind,src={script},dst=/srv/upstream.py,readonly",
                   proposer_image.id, "python3", "-I", "/srv/upstream.py")
        assert p.returncode == 0, p.stderr
        subnet = docker(verified, "network", "inspect", "-f", "{{(index .IPAM.Config 0).Subnet}}", net).stdout.strip()
        ip = docker(verified, "inspect", "-f", "{{(index .NetworkSettings.Networks \"" + net + "\").IPAddress}}", up).stdout.strip()
        deadline = time.monotonic() + 15
        while "listening" not in docker(verified, "logs", up).stdout and time.monotonic() < deadline:
            time.sleep(0.2)
        yield type("World", (), {"network": net, "container": up, "subnet": subnet, "ip": ip,
                                 "log": staticmethod(lambda: docker(verified, "logs", up).stdout)})
    finally:
        docker(verified, "rm", "-f", up)
        docker(verified, "network", "rm", net)


def hosted_runner(verified, image, world, **kw) -> ProposerSessionRunner:
    ch = profile(f"https://{UPSTREAM_HOST}:9000")
    return ProposerSessionRunner(verified, image, ch, zdr="off", classification="open", egress_network=world.network,
                                 _test_allow_cidrs=[world.subnet], **kw)


def spec(ws, *args, **kw) -> SessionSpec:
    base = dict(argv=(CLI_IN, *args), workspace=str(ws), cli=CLI, timeout_s=120)
    base.update(kw)
    return SessionSpec(**base)


class Echo:
    """A throwaway server on this host's loopback standing in for ollama."""

    def __enter__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port, self.accepted = self.sock.getsockname()[1], 0
        threading.Thread(target=self._serve, daemon=True).start()
        return self

    def _serve(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            self.accepted += 1
            threading.Thread(target=self._echo, args=(c,), daemon=True).start()

    @staticmethod
    def _echo(c):
        with c:
            while data := c.recv(65536):
                c.sendall(b"ECHO:" + data)

    def __exit__(self, *a):
        self.sock.close()


@pytest.fixture(autouse=True)
def nothing_left_behind(verified):
    before = (set(session_leftovers(verified)), runtime_dirs())
    yield
    assert (set(session_leftovers(verified)), runtime_dirs()) == before


# ── the big probe: one hosted session, many assertions ───────────────────────────

@pytest.fixture(scope="module")
def probe(verified, proposer_image, egress_world, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("probe")
    ws = tmp / "ws"
    ws.mkdir()
    ws.chmod(0o777)
    (tmp / "sentinel").write_text("host file next to the workspace")
    mp = pytest.MonkeyPatch()
    # hostile ambient state: none of it may reach any docker call or the session
    cfg = tmp / "dockercfg"
    cfg.mkdir()
    (cfg / "config.json").write_text('{"proxies": {"default": {"httpProxy": "http://attacker.example:3128"}}}')
    for k, v in (("DOCKER_CONFIG", str(cfg)), ("DOCKER_HOST", "unix:///nonexistent/evil.sock"), ("DOCKER_CONTEXT", "evil"),
                 ("HARNESS2_CANARY_SECRET", CANARY), ("OPENROUTER_API_KEY", CANARY), ("SSH_AUTH_SOCK", "/tmp/agent.sock"),
                 ("GITHUB_TOKEN", CANARY), ("HTTPS_PROXY", "http://attacker.example:3128")):
        mp.setenv(k, v)
    try:
        r = hosted_runner(verified, proposer_image, egress_world)
        res = r.run(spec(ws, "probe-all", egress_world.ip, str(ws)))
    finally:
        mp.undo()
    class Probe:
        def __init__(self):
            self.values, self.ws, self.tmp = kv(res.stdout), ws, tmp

        def __getattr__(self, name):
            return getattr(res, name)
    return Probe()


def test_the_probe_session_ran_and_reports(probe):
    assert probe.exit_code == 0 and not probe.timed_out and probe.stderr == "" and len(probe.values) > 60, probe.stdout[-500:]


# 1. confinement ---------------------------------------------------------------

def test_privileges_are_dropped_and_the_root_filesystem_is_read_only(probe):
    v = probe.values
    assert v["uid"] == "65534" and v["capeff"] == v["capbnd"] == "0000000000000000" and v["nnp"] == "1"
    assert v["rootmount"] == "ro"
    for p in ("/etc", "/usr", "/", "cli", "var"):
        assert v[f"write[{p}]"] == "DENIED", p
    assert v["overwrite_cli"] == "DENIED" and v["climount"] == "ro"


def test_only_the_workspace_and_tmpfs_are_writable_and_none_of_them_is_executable_by_default(probe):
    v = probe.values
    assert v["write[ws]"] == v["write[tmp]"] == v["write[home]"] == "WROTE"
    assert v["wsmount"] == "rw" and v["tmpmount"].startswith("tmpfs ") and v["homemount"].startswith("tmpfs ")
    assert "noexec" in v["tmpmount"] and "nosuid" in v["tmpmount"] and "nosuid" in v["homemount"]
    assert v["exec_tmp"] == v["exec_home"] == "DENIED" and v["homeowner"] == "65534"


def test_nothing_else_is_mounted_and_the_host_is_invisible(probe):
    v = probe.values
    mounts = v["mount_list"].split()
    assert str(probe.ws) in mounts and "/opt/cli/fake-cli" in mounts
    assert not any(m in ("/home/rockad", "/run/user", "/var/run", "/run") or m.endswith(".sock") for m in mounts), mounts
    assert v["sockets"] == "" and "No such file" in v["dockerdev"] and v["host_home"].split() == ["agent", "ubuntu"]
    # `../` from the workspace: only the (empty) mount-point directories exist, never the host's sibling files
    assert v["parent_listing"].split() == ["ws"] and "sentinel" not in v["traverse"]
    assert not (probe.tmp / "ws" / "sentinel").exists()
    assert int(v["pidns"]) < 15            # a private pid namespace, not the host's


def test_kernel_and_privileged_operations_fail(probe):
    v = probe.values
    for k in ("mount_tmpfs", "unshare", "chown_root"):
        assert v[k] == "DENIED", k
    assert v["kcore_bytes"].strip() == "0"                 # masked: reading /proc/kcore yields nothing
    for k in ("sysrq", "core_pattern", "cgroup"):
        assert v[k] == "DENIED", k


def test_hardlinks_across_the_mount_boundary_fail_and_symlinks_are_only_data(probe):
    v = probe.values
    assert v["hardlink_cli"] == "DENIED" and v["hardlink_etc"] == "DENIED"
    assert v["symlink_ws"] == "ALLOWED"                   # creating one is possible; harvest refuses escaping ones (below)
    assert (probe.ws / "sl").is_symlink() and not (probe.ws / "hl").exists()


def test_a_setuid_bit_the_model_sets_is_neutralised_by_the_reclaim_and_flagged_by_harvest(probe):
    assert probe.values["setuid_ws"] == "ALLOWED"
    su = probe.ws / "su"
    assert su.exists() and not su.stat().st_mode & stat.S_ISUID       # chown by the reclaim step clears the setuid bit
    assert {"executable", "symlink-escape"} <= probe.harvest.rules()   # (the suid-bit rule itself is unit-tested)


def test_resource_limits_are_applied(probe):
    v = probe.values
    assert v["pids_max"] == "256" and v["mem_max"] == str(2 * 1024 ** 3) and v["swap_max"] == "0"
    assert v["cpu_max"] == "200000 100000"


# 2. environment ---------------------------------------------------------------

def test_the_session_environment_is_closed(probe):
    v = probe.values
    assert v["env_docker"] == "0" and v["env_canary"] == "0"
    proxies = v["env_proxy"]
    assert proxies.count("http://harness2-proxy.internal:3128") == 6 and "attacker.example" not in proxies
    assert "NO_PROXY=127.0.0.1,localhost,::1 " in proxies and "no_proxy=127.0.0.1,localhost,::1 " in proxies
    assert CANARY not in probe.stdout


# 3. network -------------------------------------------------------------------

def test_the_session_has_no_interface_but_loopback_and_the_internal_network_and_no_default_route(probe):
    v = probe.values
    assert v["default_route"].strip() == "" and v["ifaces"].split() == ["eth0", "lo"]
    assert v["resolv"].split() == ["nameserver", "127.0.0.11"]


@pytest.mark.parametrize("target", ["1.1.1.1:443", "8.8.8.8:53", "93.184.216.34:80", "169.254.169.254:80",
                                    "172.17.0.1:11434", "172.17.0.1:8105", "10.0.2.2:11434", "192.168.127.1:11434",
                                    "192.168.127.1:445", "host.docker.internal:11434"])
def test_the_session_cannot_reach_the_internet_or_any_host_address_directly(probe, target):
    assert probe.values[f"tcp[{target}]"] == "FAILED"


def test_the_session_cannot_reach_host_loopback_services(probe):
    assert probe.values["tcp[127.0.0.1:11434]"] == "FAILED" and probe.values["tcp[127.0.0.1:8100]"] == "FAILED"


def test_the_session_cannot_reach_the_remote_endpoint_except_through_the_proxy(probe, egress_world):
    assert probe.values[f"tcp[{egress_world.ip}:9000]"] == "FAILED" and probe.values[f"tcp[{egress_world.ip}:9001]"] == "FAILED"
    assert "FAILED" not in probe.values["via_proxy_ok"] and "UPSTREAM-OK-9000" in probe.values["via_proxy_ok"]
    assert "UPSTREAM-OK-9000" in probe.values["via_proxy_get_ok"] and "200 OK" in probe.values["via_proxy_get_ok"]


def test_unsetting_the_proxy_variables_only_makes_requests_fail(probe):
    assert probe.values["unset_proxy"] != "" and "UPSTREAM" not in probe.values["unset_proxy"]


def test_external_and_exfiltrating_dns_names_do_not_resolve_only_the_proxy_does(probe):
    v = probe.values
    assert v["dns[example.com]"] == v["dns[upstream]"] == v["dns[leak]"] == "NORESOLVE" and v["dns[proxy]"] == "RESOLVED"


@pytest.mark.parametrize("key", ["via_proxy_port", "via_proxy_host", "via_proxy_ip", "via_proxy_meta", "via_proxy_loop",
                                 "via_proxy_upip", "via_proxy_get_deny", "via_proxy_oct"])
def test_the_proxy_denies_everything_that_is_not_the_listed_host_and_port(probe, key):
    out = probe.values[key]
    assert re.search(r"HTTP/1\.1 (403|400) ", out) and "UPSTREAM" not in out, out


def test_the_proxy_log_has_decisions_and_only_decisions(probe, egress_world):
    log = list(probe.egress_log)
    assert f"ACCESS decision=allow host={UPSTREAM_HOST} port=9000 reason=allow-listed" in log
    denied = [line for line in log if "decision=deny" in line]
    assert any("host=example.com port=443 reason=not-allow-listed" in line for line in denied)
    assert any("host=169.254.169.254" in line for line in denied) and any("ambiguous-numeric-host" in line for line in denied)
    assert all(re.fullmatch(r"ACCESS decision=(allow|deny) host=\S+ port=\d+ reason=[a-z-]+", line) for line in log)
    seen = egress_world.log()
    assert seen.count("CONN 9000") == 2 and "CONN 9001" not in seen     # only the two allowed requests ever reached the endpoint


# ── the local channel: the unix-socket bridge ────────────────────────────────────

def local_runner(verified, image, echo, **kw):
    ch = profile(f"http://127.0.0.1:{echo.port}", kind="opencode", retention="local", ceiling="restricted")
    return ProposerSessionRunner(verified, image, ch, zdr="strict", classification="restricted", proposer_decision=OK, **kw)


def test_the_local_channel_reaches_the_host_model_only_through_the_bridge_and_the_proxy(verified, proposer_image, ws, monkeypatch):
    with Echo() as echo, Echo() as other:
        r = local_runner(verified, proposer_image, echo)
        issued = []
        real_must = r._must
        monkeypatch.setattr(r, "_must", lambda args: (issued.append(list(args)), real_must(args))[1])
        res = r.run(spec(ws, "raw", "harness2-proxy.internal", str(echo.port), "hello"))
        assert "ECHO:hello" in res.stdout, res
        probes = [r.run(spec(ws, "connect", "127.0.0.1", str(echo.port))), r.run(spec(ws, "connect", "127.0.0.1", str(other.port))),
                  r.run(spec(ws, "connect", "example.com", "443")), r.run(spec(ws, "tcp", "127.0.0.1", str(echo.port)))]
        assert "200 Connection Established" in probes[0].stdout and "ECHO:" in probes[0].stdout
        assert "403" in probes[1].stdout and "403" in probes[2].stdout           # no other loopback service, no internet
        assert "FAILED" in probes[3].stdout                                       # the session's own loopback is empty
        assert other.accepted == 0 and echo.accepted >= 2
        assert r.local_endpoint == f"http://harness2-proxy.internal:{echo.port}"
        creates = [a for a in issued if a[:2] == ["network", "create"]]
        assert len(creates) == 5 and all("--internal" in a for a in creates)       # one internal network per session, NO egress network
        proxy_created = [a for a in issued if a[0] == "create"]
        assert all("/bridge,readonly" in " ".join(a) for a in proxy_created) and not [a for a in issued if a[:2] == ["network", "connect"]]


def test_the_bridge_directory_is_private_and_gone_after_the_session(verified, proposer_image, ws):
    with Echo() as echo:
        before = runtime_dirs()
        r = local_runner(verified, proposer_image, echo)
        seen = {}

        def watcher():
            for _ in range(150):
                for n in runtime_dirs():
                    if n.startswith("h2bridge-"):
                        p = Path(sr._runtime_base_dir()) / n
                        try:
                            seen["dir"] = stat.S_IMODE(p.stat().st_mode)
                            seen["sock"] = stat.S_IMODE((p / "ollama.sock").stat().st_mode)
                            seen["list"] = os.listdir(p)
                            return
                        except OSError:
                            pass
                time.sleep(0.1)
        t = threading.Thread(target=watcher)
        t.start()
        r.run(spec(ws, "sleep", "4"))
        t.join()
        assert seen == {"dir": 0o700, "sock": 0o600, "list": ["ollama.sock"]}
        assert runtime_dirs() == before


# ── credentials ──────────────────────────────────────────────────────────────────

def test_a_credential_is_a_read_only_file_never_argv_env_or_inspect_and_is_redacted(verified, proposer_image, egress_world, ws):
    token = "tok_" + secrets.token_hex(16)
    cred = Credential("/home/agent/.cred/token", token, env_var="TOKEN_FILE")
    r = hosted_runner(verified, proposer_image, egress_world)
    captured: dict = {}

    def inspect_while_running():
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            ids = docker(verified, "ps", "-q", "--filter", f"label={sr.LABEL}.component=session").stdout.split()
            if ids:
                out = [docker(verified, "inspect", i).stdout for i in ids]
                procs = [docker(verified, "ps", "--no-trunc", "--format", "{{.Command}} {{.Names}}").stdout]
                host_ps = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout
                captured.update(inspect="\n".join(out), ps="\n".join(procs) + host_ps)
                return
            time.sleep(0.1)
    t = threading.Thread(target=inspect_while_running)
    t.start()
    res = r.run(spec(ws, "cat", "/home/agent/.cred/token", credential=cred, tmpfs_dirs=("/home/agent/.cred",), timeout_s=60,
                     argv=(CLI_IN, "cat", "/home/agent/.cred/token")))
    t.join()
    assert res.exit_code == 0 and "[REDACTED]" in res.stdout and token not in res.stdout + res.stderr
    assert token not in repr(res) and captured, "the container was never inspected"
    assert token not in captured["inspect"] and token not in captured["ps"]          # docker inspect, docker ps, host ps: no secret
    assert "/home/agent/.cred/token" in captured["inspect"] and "TOKEN_FILE=/home/agent/.cred/token" in captured["inspect"]


def test_the_credential_file_properties_inside_the_container(verified, proposer_image, egress_world, ws):
    token = "tok_" + secrets.token_hex(16)
    cred = Credential("/home/agent/.cred/token", token)
    r = hosted_runner(verified, proposer_image, egress_world)
    res = r.run(spec(ws, "cred-props", "/home/agent/.cred/token", str(ws), credential=cred, tmpfs_dirs=("/home/agent/.cred",)))
    v = kv(res.stdout)
    assert res.exit_code == 0 and token not in res.stdout
    assert v["readable_prefix"] == "tok_"                     # the unprivileged session user can read it at its path...
    assert v["mount"] == "ro" and v["append"] == "DENIED"      # ...and cannot change it
    assert v["env_hits"] == "0" and v["cmdline_hits"] == "0"   # not in the environment, not in the process arguments
    assert v["elsewhere"] == "0" and v["in_workspace"] == "0"  # not anywhere else in the filesystem, never in the workspace


def test_a_credential_written_into_the_workspace_by_the_model_is_caught_at_harvest(verified, proposer_image, egress_world, ws):
    token = "tok_" + secrets.token_hex(16)
    r = hosted_runner(verified, proposer_image, egress_world)
    res = r.run(spec(ws, "leak-cred", "/home/agent/.cred/t", str(ws), credential=Credential("/home/agent/.cred/t", token),
                     tmpfs_dirs=("/home/agent/.cred",)))
    assert res.exit_code == 0 and "credential-in-file" in res.harvest.rules() and "notes.md" not in res.harvest.files


def test_the_credential_file_and_its_directory_are_gone_after_the_run(verified, proposer_image, egress_world, ws):
    r = hosted_runner(verified, proposer_image, egress_world)
    seen: list[str] = []

    def watch():
        base = Path(sr._runtime_base_dir())
        for _ in range(200):
            for d in runtime_dirs():
                cred = base / d / "cred"
                if cred.is_dir():
                    seen.append(f"{oct(stat.S_IMODE((base / d).stat().st_mode))} {oct(stat.S_IMODE(cred.stat().st_mode))} "
                                f"{[oct(stat.S_IMODE(f.stat().st_mode)) for f in cred.iterdir()]} {[len(f.name) for f in cred.iterdir()]}")
                    return
            time.sleep(0.05)
    t = threading.Thread(target=watch)
    t.start()
    r.run(spec(ws, "sleep", "3", credential=Credential("/home/agent/.cred/t", "v" * 20), tmpfs_dirs=("/home/agent/.cred",)))
    t.join()
    assert seen and seen[0].startswith("0o700 0o711 ['0o444'] [24]"), seen      # random 24-hex name under a 0700 parent
    assert runtime_dirs() == []


# ── lifecycle ────────────────────────────────────────────────────────────────────

def test_a_timeout_kills_the_session_and_leaves_nothing_behind(verified, proposer_image, egress_world, ws):
    r = hosted_runner(verified, proposer_image, egress_world)
    t0 = time.monotonic()
    res = r.run(spec(ws, "sleep", "300", timeout_s=4))
    assert res.timed_out and res.exit_code is None and "started" in res.stdout and res.stderr.startswith("timed out")
    assert time.monotonic() - t0 < 45 and res.leftovers == ()


def test_a_crash_in_the_middle_of_the_lifecycle_still_cleans_up(verified, proposer_image, egress_world, ws, monkeypatch):
    r = hosted_runner(verified, proposer_image, egress_world)

    def boom(names):
        raise sr.SessionError("simulated crash after the proxy started")
    monkeypatch.setattr(r, "_wait_ready", boom)
    with pytest.raises(sr.SessionError, match="simulated crash"):
        r.run(spec(ws, "sleep", "1"))
    # the autouse fixture asserts no container, network or directory remains


def test_a_keyboard_interrupt_during_the_session_still_cleans_up(verified, proposer_image, egress_world, ws, monkeypatch):
    r = hosted_runner(verified, proposer_image, egress_world)
    real = subprocess.Popen

    class Interrupting(real):
        def wait(self, timeout=None):
            raise KeyboardInterrupt
    monkeypatch.setattr(sr.subprocess, "Popen", Interrupting)
    with pytest.raises(KeyboardInterrupt):
        r.run(spec(ws, "sleep", "30"))


def test_exit_codes_stdin_and_a_session_that_finishes_fast(verified, proposer_image, egress_world, ws):
    r = hosted_runner(verified, proposer_image, egress_world)
    assert r.run(spec(ws, "exit", "7")).exit_code == 7
    res = r.run(spec(ws, "stdin", stdin=b"prompt over stdin\n"))
    assert res.stdout == "prompt over stdin\n" and res.exit_code == 0
    res = r.run(spec(ws, "echo", "$(id)", "a b", "`x`", ";rm -rf /"))
    assert res.stdout.splitlines() == ["$(id)", "a b", "`x`", ";rm -rf /"]          # argv is data, never a shell string


def test_output_is_capped_stripped_of_escapes_and_nothing_else_leaks(verified, proposer_image, egress_world, ws):
    r = hosted_runner(verified, proposer_image, egress_world)
    res = r.run(spec(ws, "noisy", "3000000", limits=SessionLimits(max_output_bytes=2048)))
    assert res.truncated and len(res.stdout) < 2300 and "output truncated" in res.stdout
    res = r.run(spec(ws, "ansi"))
    assert res.stdout == "clear red\n" and "\x1b" not in res.stdout


# ── resource exhaustion ──────────────────────────────────────────────────────────

def test_a_fork_bomb_is_contained(verified, proposer_image, egress_world, ws):
    r = hosted_runner(verified, proposer_image, egress_world)
    res = r.run(spec(ws, "forkbomb", limits=SessionLimits(pids=24), timeout_s=60))
    text = res.stdout + res.stderr
    assert ("Resource temporarily unavailable" in text or "fork" in text or res.exit_code != 0) and "children=400" not in text


def test_memory_hogs_are_oom_killed_without_affecting_the_host(verified, proposer_image, egress_world, ws):
    r = hosted_runner(verified, proposer_image, egress_world)
    res = r.run(spec(ws, "memhog", "600", limits=SessionLimits(memory="128m"), timeout_s=90))
    assert res.exit_code in (137, 1, -9) and "allocated" not in res.stdout


def test_the_tmpfs_is_size_capped(verified, proposer_image, egress_world, ws):
    r = hosted_runner(verified, proposer_image, egress_world)
    res = r.run(spec(ws, "tmpfill", "100", limits=SessionLimits(tmpfs_size="16m")))
    assert "No space left" in res.stdout and "rc=1" in res.stdout


# ── harvest and workspace ownership ──────────────────────────────────────────────

def test_the_workspace_is_reclaimed_for_the_host_user_and_harvest_refuses_the_mess(verified, proposer_image, egress_world, ws):
    r = hosted_runner(verified, proposer_image, egress_world)
    res = r.run(spec(ws, "harvest-mess", str(ws)))
    assert res.exit_code == 0 and res.reclaimed is True
    uid = os.getuid()
    for p in ws.rglob("*"):
        if not p.is_symlink():
            assert p.stat().st_uid == uid, p                    # the container user's files now belong to the host user
    h = res.harvest
    assert {"special-file", "symlink-escape", "executable", "hardlink", "file-too-large", "bad-name"} <= h.rules(), h.findings
    assert "plain.md" in h.files and "sub/n.md" in h.files and "inner" in h.links
    assert not any("\x1b" in f.path for f in h.findings)
    # and the host can remove everything the container created (no stuck subuid-owned files)
    for p in sorted(ws.rglob("*"), reverse=True):
        p.unlink() if not p.is_dir() or p.is_symlink() else p.rmdir()
    assert list(ws.iterdir()) == []


# ── fail closed ──────────────────────────────────────────────────────────────────

def test_a_daemon_that_is_not_rootless_is_refused_before_anything_is_created(verified, proposer_image, egress_world, ws, tmp_path):
    log = tmp_path / "calls.log"
    fake = tmp_path / "docker"
    fake.write_text(f'#!/bin/sh\necho "$@" >> {log}\ncase "$3" in info) echo \'["name=seccomp,profile=builtin"]\';; *) exit 1;; esac\n')
    fake.chmod(0o755)
    r = hosted_runner(sandbox.VerifiedDocker(str(fake), verified.host), proposer_image, egress_world)
    with pytest.raises(sandbox.SandboxUnavailable, match="rootless"):
        r.run(spec(ws, "echo", "x"))
    calls = log.read_text().splitlines()
    assert len(calls) == 1 and calls[0].startswith(f"-H {verified.host} info")


def test_a_stopped_daemon_and_a_missing_image_fail_closed(verified, proposer_image, egress_world, ws):
    r = hosted_runner(sandbox.VerifiedDocker(verified.path, "unix:///nonexistent/docker.sock"), proposer_image, egress_world)
    with pytest.raises(sandbox.SandboxUnavailable, match="unreachable"):
        r.run(spec(ws, "echo", "x"))
    ghost = ImageRef(proposer_image.tag, "sha256:" + "0" * 64)
    with pytest.raises(sandbox.SandboxUnavailable, match="not present"):
        hosted_runner(verified, ghost, egress_world).run(spec(ws, "echo", "x"))


def test_a_denied_channel_decision_creates_nothing(verified, proposer_image, egress_world, ws):
    ch = profile(f"https://{UPSTREAM_HOST}:9000")
    for kw in (dict(zdr="strict", classification="open"), dict(zdr="off", classification="restricted")):
        r = ProposerSessionRunner(verified, proposer_image, ch, egress_network=egress_world.network, **kw)
        with pytest.raises(SessionRefused, match="channel decision"):
            r.run(spec(ws, "echo", "x"))


def test_a_wall_clock_above_the_channel_cap_and_a_foreign_executable_are_refused(verified, proposer_image, egress_world, ws):
    r = hosted_runner(verified, proposer_image, egress_world)
    with pytest.raises(SessionRefused, match="timeout_s"):
        r.run(spec(ws, "echo", "x", timeout_s=601))
    with pytest.raises(SessionRefused, match="argv\\[0\\]"):
        r.run(spec(ws, argv=("/bin/sh", "-c", "id")))


def test_reap_all_removes_only_labelled_leftovers(verified, proposer_image, egress_world):
    r = hosted_runner(verified, proposer_image, egress_world)
    name = f"h2s-{secrets.token_hex(4)}-orphan"
    docker(verified, "network", "create", "--internal", "--label", f"{sr.LABEL}=orphan", name)
    docker(verified, "create", "--name", f"{name}-c", "--label", f"{sr.LABEL}=orphan", "--pull", "never", proposer_image.id, "true")
    removed = r.reap_all()
    assert any(x.startswith("container") for x in removed) and any(x.startswith("network") for x in removed)
    assert session_leftovers(verified) == []
    assert egress_world.network in docker(verified, "network", "ls", "--format", "{{.Name}}").stdout      # not labelled: untouched


# ── additions from the threat model ──────────────────────────────────────────────

def test_a_symlink_pre_created_in_the_workspace_cannot_reach_the_host_file_it_names(verified, proposer_image, egress_world, ws, tmp_path):
    secret = tmp_path / "host-secret.txt"
    secret.write_text("HOST-ONLY-CONTENT")
    (ws / "hostlink").symlink_to(secret)                    # an absolute symlink to a host file outside the workspace
    (ws / "uplink").symlink_to("../host-secret.txt")
    r = hosted_runner(verified, proposer_image, egress_world)
    res = r.run(spec(ws, "cat", str(ws / "hostlink")))
    assert res.exit_code != 0 and "HOST-ONLY-CONTENT" not in res.stdout + res.stderr
    res = r.run(spec(ws, "cat", str(ws / "uplink")))
    assert res.exit_code != 0 and "HOST-ONLY-CONTENT" not in res.stdout + res.stderr
    assert {f.rule for f in res.harvest.findings} == {"symlink-escape"}


def test_a_single_file_cannot_exceed_the_file_size_limit_on_the_host_backed_workspace(verified, proposer_image, egress_world, ws):
    r = hosted_runner(verified, proposer_image, egress_world)
    res = r.run(spec(ws, "fsfill", str(ws), "20", limits=SessionLimits(file_bytes=4 * 1024 * 1024)))
    assert "rc=0" not in res.stdout and int(kv(res.stdout)["size"]) <= 4 * 1024 * 1024


# ── the session's own loopback is not the proxy's business (first real stage-1 run, 2026-10-30) ───────────────────
#
# `opencode run --standalone` starts a private HTTP server on a random port of the SESSION's own 127.0.0.1 and talks to
# it with a proxy-aware client. With HTTP_PROXY set and NO_PROXY empty that traffic went to the proxy, which denied it
# (403, host=127.0.0.1): all 7 sessions died in ~2 s and the model was never called. The runner now sets NO_PROXY to the
# container's own loopback (and nothing else).

class HttpStub:
    """A throwaway HTTP server on this host's loopback standing in for the model endpoint. Counts requests."""

    def __enter__(self):
        import http.server
        outer = self
        self.hits = 0

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):                                     # noqa: N802
                outer.hits += 1
                body = b"MODEL-STUB-OK"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a): pass
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *a):
        self.server.shutdown()
        self.server.server_close()


def test_a_cli_that_serves_itself_on_its_own_loopback_reaches_it_despite_the_proxy_variables(verified, proposer_image, egress_world, ws):
    res = hosted_runner(verified, proposer_image, egress_world).run(spec(ws, "selfserve"))
    out = kv(res.stdout)
    assert out["selfserve[127.0.0.1]"] == "200 SELF-SERVER-OK" and out["selfserve[localhost]"] == "200 SELF-SERVER-OK", res
    assert out["no_proxy"] == "127.0.0.1,localhost,::1"
    assert not [line for line in res.egress_log if "host=127.0.0.1" in line or "host=localhost" in line], res.egress_log


def test_the_bypass_covers_the_sessions_own_loopback_only_every_other_name_still_goes_through_the_proxy(verified, proposer_image, egress_world, ws):
    r = hosted_runner(verified, proposer_image, egress_world)
    ok = r.run(spec(ws, "pget", f"http://{UPSTREAM_HOST}:9000/"))
    assert "pget=200 UPSTREAM-OK-9000" in ok.stdout, ok                                  # an allow-listed name still works, via the proxy
    assert f"ACCESS decision=allow host={UPSTREAM_HOST} port=9000 reason=allow-listed" in ok.egress_log
    for url in ("http://example.com/", f"http://{UPSTREAM_HOST}:9001/", "http://169.254.169.254/", f"http://{egress_world.ip}:9000/"):
        denied = r.run(spec(ws, "pget", url))
        assert "pget=HTTP403" in denied.stdout and any("decision=deny" in line for line in denied.egress_log), (url, denied)
    own = r.run(spec(ws, "pget", "http://127.0.0.1:9000/"))                              # bypassed, so it is the session's own (empty) loopback
    assert "pget=ERR" in own.stdout and not own.egress_log, own
    assert "CONN 9001" not in egress_world.log()


def test_the_local_model_endpoint_is_reached_through_the_proxy_and_the_hosts_other_loopback_is_not(verified, proposer_image, ws):
    with HttpStub() as model, HttpStub() as other:
        r = local_runner(verified, proposer_image, model)
        got = r.run(spec(ws, "pget", f"http://harness2-proxy.internal:{model.port}/v1/models"))
        assert "pget=200 MODEL-STUB-OK" in got.stdout, got
        assert model.hits == 1
        assert f"ACCESS decision=allow host=harness2-proxy.internal port={model.port} reason=unix-route" in got.egress_log
        for url in (f"http://127.0.0.1:{model.port}/", f"http://127.0.0.1:{other.port}/", f"http://harness2-proxy.internal:{other.port}/"):
            res = r.run(spec(ws, "pget", url))
            assert "pget=200" not in res.stdout, (url, res)                              # the session's own loopback is empty; no other port is routed
        assert model.hits == 1 and other.hits == 0
        own = kv(r.run(spec(ws, "selfserve")).stdout)
        assert own["selfserve[127.0.0.1]"] == "200 SELF-SERVER-OK"                       # a local-channel CLI can still serve itself
