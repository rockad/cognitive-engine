"""Slice 3a proof: no seam starts a process, opens a connection or calls a model.

A fixture replaces every way this process could do those things (subprocess, os.system and the exec
and spawn family, asyncio subprocesses, sockets and DNS, urllib, http.client, httpx, and requests or
aiohttp when importable) with a trap that RECORDS the attempt and raises `SideEffectAttempted`. That is
a `BaseException`, so no seam's `except Exception` can swallow it. Then every seam's happy path and
refusal paths run, and the test asserts nothing was attempted.

Two things are allowed to touch the outside, and only when the caller wires them in: preflight's Docker
gate (read-only `docker info` and `image inspect`, pinned to the verified socket) and a caller-supplied
quota check. Both are shown below to be the ONLY places, and to reach the trap when they are real.
"""
from __future__ import annotations

import asyncio
import builtins
import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import textwrap
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from h2_profiles import PROFILE

from cognitive_harness2 import channels, settings, sweep_plan
from cognitive_harness2.invariants import InvariantViolation
from cognitive_harness2.quota import QuotaVerdict
from cognitive_harness2.substrate import CognitiveSubstrate, PreflightError, StagedInstall, openrouter_quota_check
from cognitive_harness2.sweep_plan import CostCap, SweepCandidate, SweepRefused, plan_sweep
from cognitive_harness2.upstream_egress import check_proposer
from cognitive_harness2.upstream_surface import check_surface

SOCK = "unix:///run/user/1000/docker.sock"
ROOTLESS = ["name=seccomp,profile=builtin", "name=rootless", "name=cgroupns"]
LOCAL = "http://127.0.0.1:11434/v1"
BENCH = SimpleNamespace(install_mode="workspace_agents_md")
READY = {"sandbox": {"ok": True}, "egress": {"ok": True}, "quota": {"ok": None}, "model": {"ok": True}}
SECRET = "SENTINEL-proof-9c1e"


class SideEffectAttempted(BaseException):
    """Raised by a trap. A BaseException so that `except Exception` cannot hide it."""


def _targets():
    t = [
        (subprocess, "Popen"), (subprocess, "run"), (subprocess, "call"), (subprocess, "check_call"),
        (subprocess, "check_output"), (os, "system"), (os, "popen"), (os, "fork"), (os, "forkpty"),
        (os, "execv"), (os, "execve"), (os, "execvp"), (os, "execvpe"), (os, "posix_spawn"), (os, "posix_spawnp"),
        (os, "spawnv"), (os, "spawnve"), (os, "spawnvp"), (os, "spawnvpe"),
        (asyncio, "create_subprocess_exec"), (asyncio, "create_subprocess_shell"),
        (asyncio.base_events.BaseEventLoop, "subprocess_exec"), (asyncio.base_events.BaseEventLoop, "subprocess_shell"),
        (socket, "create_connection"), (socket, "getaddrinfo"), (socket, "gethostbyname"),
        (socket.socket, "connect"), (socket.socket, "connect_ex"),
        (urllib.request, "urlopen"), (urllib.request.OpenerDirector, "open"),
        (http.client.HTTPConnection, "connect"), (http.client.HTTPConnection, "request"),
        (httpx.Client, "send"), (httpx.AsyncClient, "send"),
        (httpx.HTTPTransport, "handle_request"), (httpx.AsyncHTTPTransport, "handle_async_request"),
    ]
    for mod, attrs in (("requests", [("Session", "send"), ("Session", "request")]),
                       ("aiohttp", [("ClientSession", "_request")]),
                       ("urllib3", [("HTTPConnectionPool", "urlopen")])):
        try:
            m = __import__(mod)
        except ImportError:
            continue
        for cls, attr in attrs:
            if hasattr(getattr(m, cls, None), attr):
                t.append((getattr(m, cls), attr))
    return [(o, a) for o, a in t if hasattr(o, a)]


@pytest.fixture
def traps(monkeypatch):
    """Every process, network and model path trapped. `traps.attempts` lists what was tried."""
    attempts: list[str] = []

    def make(name):
        def trap(*a, **k):
            attempts.append(name)
            raise SideEffectAttempted(name)
        return trap

    for obj, attr in _targets():
        monkeypatch.setattr(obj, attr, make(f"{getattr(obj, '__name__', obj)}.{attr}"))
    return SimpleNamespace(attempts=attempts, make=make)


@pytest.fixture(scope="module")
def surface():
    from harness2.substrate.opencode import OPENCODE_SURFACE
    return OPENCODE_SURFACE


@pytest.fixture(autouse=False)
def private_tmp(tmp_path, monkeypatch):
    d = tmp_path / "tmp"
    d.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(d))
    return d


def fake_docker(calls):
    def run(argv, **kw):
        calls.append(list(argv))
        sub = argv[1:]
        assert sub[0] == "-H", f"docker call not pinned to the verified host: {argv}"
        verb = sub[2:]
        if verb[0] == "info":
            return subprocess.CompletedProcess(argv, 0, json.dumps(ROOTLESS), "")
        if verb[:2] == ["image", "inspect"]:
            return subprocess.CompletedProcess(argv, 0, "sha256:abc\n", "")
        raise AssertionError(f"docker verb outside the allowed read-only pair: {argv}")
    return run


# ── the traps are real ────────────────────────────────────────────────────────────

def test_every_trap_trips_and_is_recorded(traps):
    probes = [
        lambda: subprocess.run(["true"]), lambda: subprocess.Popen(["true"]), lambda: subprocess.check_output(["true"]),
        lambda: os.system("true"), lambda: os.popen("true"), lambda: os.execv("/bin/true", ["true"]),
        lambda: os.posix_spawn("/bin/true", ["true"], {}),
        lambda: socket.create_connection(("127.0.0.1", 9)), lambda: socket.socket().connect(("127.0.0.1", 9)),
        lambda: socket.getaddrinfo("example.invalid", 80),
        lambda: urllib.request.urlopen("http://127.0.0.1:9/"),
        lambda: http.client.HTTPConnection("127.0.0.1", 9).connect(),
        lambda: httpx.Client().send(httpx.Request("GET", "http://127.0.0.1:9/")),
        lambda: httpx.get("http://127.0.0.1:9/"),
        lambda: asyncio.run(asyncio.create_subprocess_exec("true")),
        lambda: asyncio.run(asyncio.create_subprocess_shell("true")),
    ]
    for probe in probes:
        with pytest.raises(SideEffectAttempted):
            probe()
    assert len(traps.attempts) >= len(probes)


def test_a_trap_is_not_swallowed_by_except_exception(traps):
    swallowed = False
    try:
        try:
            subprocess.run(["true"])
        except Exception:
            swallowed = True
    except SideEffectAttempted:
        pass
    assert not swallowed and traps.attempts == ["subprocess.run"]


# ── every seam, happy and refusing, with the traps armed ──────────────────────────

def exercise_every_seam(surface, tmp_path, profile) -> dict[str, int]:
    done = {"happy": 0, "refused": 0}

    def happy(fn):
        fn()
        done["happy"] += 1

    def refuses(fn, exc=(InvariantViolation, ValueError, SweepRefused, PreflightError, NotImplementedError)):
        try:
            fn()
        except exc:
            done["refused"] += 1
        else:
            raise AssertionError("expected a refusal")

    # seam 1: upstream_surface
    happy(lambda: check_surface(surface))
    happy(lambda: check_surface(surface, request=["c1", "c3", "c4", "c5"], tree=["systemprompt.md", "sub_agents/a/AGENT.md"]))
    for slot in ("c0", "c2", "c6", "c7", "c8", "c9"):
        refuses(lambda s=slot: check_surface(surface, request=[s]))
    for path in ("guardrails.md", "../x", "/etc/passwd", ".git/config", "hooks/x", "plugin/p.ts", "SystemPrompt.md"):
        refuses(lambda p=path: check_surface(surface, tree=[p]))

    # seam 2: upstream_egress (the proposer is described, never created)
    env = {"PATH": "/usr/bin", "HOME": "/tmp/h"}
    def propose(raw, channel="local", **kw):
        return check_proposer(raw, profile=profile, channel=channel, classification="open", **kw)

    ok = propose({"kind": "opencode", "model": "ollama/qwen3:30b", "base_url": LOCAL, "env": env, "cwd": "/tmp/work"})
    assert ok.allowed
    done["happy"] += 1
    for raw in (
        {"kind": "claude_cli", "model": "claude-x", "base_url": LOCAL, "env": env},
        {"kind": "opencode", "model": "google-vertex/g", "base_url": LOCAL, "env": env},
        {"kind": "opencode", "model": "ollama/q", "base_url": "https://openrouter.ai/api/v1", "env": env},
        {"kind": "opencode", "model": "ollama/q", "env": env},
        {"kind": "opencode", "model": "ollama/q", "base_url": LOCAL, "env": {**env, "OPENROUTER_API_KEY": SECRET}},
        {"kind": "nope"}, None,
    ):
        assert not propose(raw).allowed
        done["refused"] += 1

    # seam 3: substrate (construct, surface, materialise, release, execute/read refuse, preflight with injected pieces)
    tree = tmp_path / "tree"
    (tree / "sub_agents/a").mkdir(parents=True)
    (tree / "systemprompt.md").write_text("p\n")
    (tree / "sub_agents/a/AGENT.md").write_text("a\n")
    calls: list[list[str]] = []
    s = CognitiveSubstrate(surface, which=lambda _: "/usr/bin/docker", environ={"DOCKER_HOST": SOCK}, run=fake_docker(calls),
                           quota_check=lambda: QuotaVerdict(True, "ok"))
    happy(lambda: s.surface())
    inst = s.materialise(tree, BENCH, {"file:note.md": "n"}, changed=["systemprompt.md"])
    done["happy"] += 1
    s.release(inst)
    refuses(lambda: s.materialise(tree, BENCH, {}, changed=["guardrails.md"]))
    refuses(lambda: s.materialise(tree, BENCH, {"file:../x": "n"}))
    refuses(lambda: s.materialise(tree, SimpleNamespace(install_mode="nope"), {}))
    refuses(lambda: s.execute("t", StagedInstall(tree, "f", {}), ["r"], 1))
    refuses(lambda: s.read_trajectory(tree / "x", obs_trunc=1, max_chars=1))
    happy(s.preflight)
    refuses(CognitiveSubstrate(surface, which=lambda _: None, run=fake_docker([])).preflight)
    refuses(CognitiveSubstrate(surface, which=lambda _: "/usr/bin/docker", environ={"DOCKER_HOST": SOCK},
                               run=fake_docker([]), quota_check=lambda: QuotaVerdict(False, "exhausted")).preflight)
    happy(lambda: openrouter_quota_check(None)())   # no key: refuses locally, makes no request
    assert calls and all(c[1:3] == ["-H", SOCK] for c in calls)

    # seam 4: sweep plan
    d = ok
    base = dict(surface=surface, candidates=[SweepCandidate("sp", "systemprompt.md")], k=1, n_tasks=1, max_sessions=3,
                max_wall_seconds=600, max_cost_or_tokens=CostCap("tokens", 100_000), proposer=d, readiness=READY,
                profile=profile, channel="local", classification="open")
    happy(lambda: plan_sweep(**base))
    for over in ({"max_sessions": None}, {"max_wall_seconds": 0}, {"max_cost_or_tokens": CostCap("usd", float("inf"))},
                 {"readiness": {"sandbox": {"ok": False, "detail": "x"}}},
                 {"proposer": propose({"kind": "opencode", "model": "ollama/q", "base_url": "https://x.example"})},
                 {"channel": "undeclared"}, {"classification": "restricted"}, {"profile": None},
                 {"candidates": [SweepCandidate("g", "guardrails.md")]}):
        refuses(lambda o=over: plan_sweep(**{**base, **o}))
    return done


def test_every_seam_runs_with_every_process_and_network_path_trapped(traps, surface, tmp_path, private_tmp):
    done = exercise_every_seam(surface, tmp_path, PROFILE)
    assert traps.attempts == []
    assert done["happy"] >= 8 and done["refused"] >= 25      # the paths really ran


def test_enabling_the_quota_check_is_the_only_way_a_request_could_be_made(traps):
    # With a key, the helper WOULD make its one GET: the HTTP trap catches it. That is the single
    # network path in the package, and a caller has to pass it in explicitly.
    check = openrouter_quota_check("k", client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))))
    with pytest.raises(SideEffectAttempted):
        check()
    assert traps.attempts and "send" in traps.attempts[-1]


def test_a_real_preflight_reaches_the_process_trap_and_nothing_else(traps, surface):
    # Default `run` is subprocess.run: preflight, and only preflight, would start `docker info`.
    s = CognitiveSubstrate(surface, which=lambda _: "/usr/bin/docker", environ={"DOCKER_HOST": SOCK})
    with pytest.raises(SideEffectAttempted):
        s.preflight()
    assert traps.attempts == ["subprocess.run"]


def test_execute_and_read_trajectory_never_reach_a_trap(traps, surface):
    s = CognitiveSubstrate(surface)
    for call in (lambda: s.execute("t", StagedInstall(Path("/x"), "f", {}), ["r"], 1),
                 lambda: s.read_trajectory(Path("/x"), obs_trunc=1, max_chars=1)):
        with pytest.raises(NotImplementedError, match="slice 3b, not enabled"):
            call()
    assert traps.attempts == []


# ── the Docker daemon is asked only by preflight, only through the pinned runner path ──

class DockerLog:
    """Stands in for subprocess.run/Popen, answers like a rootless daemon, and records every docker argv."""

    def __init__(self, monkeypatch):
        self.calls: list[list[str]] = []
        inner = fake_docker(self.calls)

        def run(argv, **kw):
            return inner(argv, **kw)

        def popen(*a, **k):
            raise SideEffectAttempted("subprocess.Popen")

        monkeypatch.setattr(subprocess, "run", run)
        monkeypatch.setattr(subprocess, "Popen", popen)


def test_no_other_seam_touches_the_docker_daemon(monkeypatch, surface, tmp_path, private_tmp):
    log = DockerLog(monkeypatch)
    # everything except the real preflight (which the exercise only runs against an injected fake)
    exercise_every_seam(surface, tmp_path, PROFILE)
    assert log.calls == []


def test_preflight_alone_asks_the_daemon_and_only_pinned_read_only_questions(monkeypatch, surface):
    log = DockerLog(monkeypatch)
    CognitiveSubstrate(surface, which=lambda _: "/usr/bin/docker", environ={"DOCKER_HOST": SOCK}).preflight()
    assert [c[3] for c in log.calls] == ["info", "image"]
    assert all(c[1:3] == ["-H", SOCK] for c in log.calls)
    assert not {w for c in log.calls for w in c} & {"run", "create", "start", "pull", "exec", "build", "rm", "kill"}


def test_a_refused_preflight_leaves_the_daemon_alone_afterwards(monkeypatch, surface):
    log = DockerLog(monkeypatch)
    with pytest.raises(PreflightError):
        CognitiveSubstrate(surface, which=lambda _: None, environ={"DOCKER_HOST": SOCK}).preflight()
    assert log.calls == []


# ── importing the seams is itself side-effect free ───────────────────────────────

IMPORT_PROBE = textwrap.dedent("""
    import json, os, sys
    from types import SimpleNamespace
    import cognitive_harness2.upstream_surface, cognitive_harness2.upstream_egress, cognitive_harness2.channels
    import cognitive_harness2.substrate, cognitive_harness2.sweep_plan
    from cognitive_harness2.upstream_surface import check_surface
    spec = SimpleNamespace(name="x", always_on=("systemprompt.md",), skill_dir="skills", plugin_dir=None, hooks_file=None)
    check_surface(spec, tree=["systemprompt.md"])
    print(json.dumps({"upstream": sorted(m for m in sys.modules if m == "harness2" or m.startswith("harness2.")),
                      "dotenv": os.environ.get("H2_DOTENV_SENTINEL")}))
""")


def _probe(code: str, cwd: Path) -> dict:
    p = subprocess.run([sys.executable, "-I", "-c", code], cwd=cwd, capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout.strip().splitlines()[-1])


@pytest.fixture
def dotenv_cwd(tmp_path):
    d = tmp_path / "cwd"
    d.mkdir()
    (d / ".env").write_text("H2_DOTENV_SENTINEL=loaded\n")
    return d


def test_importing_every_seam_imports_no_upstream_and_reads_no_dotenv(dotenv_cwd):
    got = _probe(IMPORT_PROBE, dotenv_cwd)
    assert got == {"upstream": [], "dotenv": None}


def test_canary_upstreams_substrate_import_does_read_dotenv_from_the_cwd(dotenv_cwd):
    """Why the seams never import `harness2.substrate`: if upstream ever stops doing this, drop the workaround."""
    got = _probe("import os, harness2.substrate; print(__import__('json').dumps({'upstream': [], 'dotenv': os.environ.get('H2_DOTENV_SENTINEL')}))",
                 dotenv_cwd)
    assert got["dotenv"] == "loaded"


# ── the channel profile: the only file access is the pinned reader, and it only reads ──

PROFILE_TOML = textwrap.dedent('''
    workspace_roots = ["/tmp/work"]
    [channels.local]
    kind = "opencode"
    provider = "ollama"
    endpoint = "http://127.0.0.1:11434/v1"
    model = "ollama/qwen3:30b"
    retention = "local"
    max_classification = "restricted"
    approval = "per-sweep"
    [channels.local.caps]
    sessions = 30
    wall_s = 3600
    cost_usd = 5.0
''')


@pytest.fixture
def file_traps(monkeypatch):
    """Records every file the code opens and traps every way of writing, creating or removing one."""
    rec = SimpleNamespace(opened=[], writes=[])
    real_open, real_os_open = builtins.open, os.open
    write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND

    def guarded_open(file, mode="r", *a, **k):
        if any(c in mode for c in "wax+"):
            rec.writes.append(f"open({mode})")
            raise SideEffectAttempted("open-for-write")
        rec.opened.append(os.fspath(file))
        return real_open(file, mode, *a, **k)

    def guarded_os_open(path, flags, *a, **k):
        if flags & write_flags:
            rec.writes.append("os.open(write)")
            raise SideEffectAttempted("os.open-for-write")
        rec.opened.append(os.fspath(path))
        return real_os_open(path, flags, *a, **k)

    def trap(name):
        def t(*a, **k):
            rec.writes.append(name)
            raise SideEffectAttempted(name)
        return t

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(os, "open", guarded_os_open)
    for mod, names in ((os, ("mkdir", "makedirs", "rename", "replace", "remove", "unlink", "rmdir", "symlink", "link",
                             "chmod", "truncate", "mkfifo")),
                       (Path, ("write_text", "write_bytes", "mkdir", "touch", "unlink", "rename", "symlink_to"))):
        for n in names:
            if hasattr(mod, n):
                monkeypatch.setattr(mod, n, trap(f"{getattr(mod, '__name__', 'Path')}.{n}"))
    return rec


@pytest.fixture
def profile_file(tmp_path_factory):
    f = tmp_path_factory.mktemp("pf") / "channels.toml"
    f.write_text(PROFILE_TOML)
    return f


def test_the_file_traps_are_real(tmp_path, file_traps):
    for probe in (lambda: open(tmp_path / "x", "w"), lambda: open(tmp_path / "x", "a"), lambda: open(tmp_path / "x", "r+"),
                  lambda: os.open(tmp_path / "x", os.O_WRONLY | os.O_CREAT), lambda: (tmp_path / "y").write_text("z"),
                  lambda: os.mkdir(tmp_path / "d"), lambda: os.unlink(tmp_path / "nope"), lambda: (tmp_path / "t").touch()):
        with pytest.raises(SideEffectAttempted):
            probe()
    assert len(file_traps.writes) == 8


def test_the_channel_modules_read_only_the_profile_and_write_nothing(traps, profile_file, surface, monkeypatch, file_traps):
    readers = []
    real = channels._read_capped
    monkeypatch.setattr(channels, "_read_capped", lambda p, *a: (readers.append(p), real(p, *a))[1])

    profile = channels.load_profile({settings.ENV_CHANNELS_FILE: str(profile_file)})
    assert profile.declared and "local" in profile.channels
    env = {"PATH": "/usr/bin"}
    cfg = {"kind": "opencode", "model": "ollama/qwen3:30b", "base_url": LOCAL, "env": env, "cwd": "/tmp/work"}
    d = check_proposer(cfg, profile=profile, channel="local", classification="open")
    assert d.allowed
    for bad in ({**cfg, "model": "ollama/other"}, {**cfg, "base_url": "https://x.example"}):
        assert not check_proposer(bad, profile=profile, channel="local", classification="open").allowed
    assert not check_proposer(cfg, profile=profile, channel="undeclared").allowed
    assert not check_proposer(cfg, profile=profile, channel="local", classification="restricted", zdr="x").allowed
    assert channels.decide(profile.channels["local"], zdr="strict", classification=None).allowed
    plan_sweep(surface=surface, candidates=[SweepCandidate("sp", "systemprompt.md")], k=1, n_tasks=1, max_sessions=3,
               max_wall_seconds=600, max_cost_or_tokens=CostCap("tokens", 100_000), proposer=d, readiness=READY,
               profile=profile, channel="local", classification="open")
    # malformed profiles are refused loudly, through the same reader, without writing anything
    with pytest.raises(channels.ChannelProfileError):
        channels.load_profile({settings.ENV_CHANNELS_FILE: str(profile_file.parent / "missing.toml")})
    with pytest.raises(channels.ChannelProfileError):
        channels.parse_text("channels = 5", source="x")

    assert readers == [str(profile_file), str(profile_file.parent / "missing.toml")]      # only the pinned reader
    assert file_traps.opened == [str(profile_file)]                                       # one read, of that file only
    assert file_traps.writes == [] and traps.attempts == []
