"""Slice 3b WS-B: the PURE parts of the contained-session code touch nothing outside this process.

Same method as test_no_side_effects.py: every way to start a process or open, bind, listen on or connect a
socket (and to resolve a name) is replaced by a trap that records the attempt and raises a BaseException,
then the proxy decision logic, the host-bridge validators, the image reference checks, every docker argv
builder, the authorisation (all of its refusals) and the output hygiene helpers run with the traps armed.

The only places that DO reach the outside are shown to reach the trap: the runner's `_docker`, the bridge
context manager, and the proxy server.
"""
from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
import subprocess
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from cognitive_harness2 import channels, egress_proxy as ep, host_bridge, image, session_runner as sr, upstream_egress
from cognitive_harness2 import workspace_harvest
from cognitive_harness2.sandbox import VerifiedDocker


class SideEffectAttempted(BaseException):
    """A BaseException, so no `except Exception` in the code under test can hide it."""


def _targets():
    t = [(subprocess, n) for n in ("Popen", "run", "call", "check_call", "check_output")]
    t += [(os, n) for n in ("system", "popen", "fork", "execv", "execve", "execvp", "posix_spawn", "spawnv", "kill")]
    t += [(asyncio, n) for n in ("create_subprocess_exec", "create_subprocess_shell", "start_server", "start_unix_server",
                                 "open_connection", "open_unix_connection")]
    t += [(asyncio.base_events.BaseEventLoop, n) for n in ("subprocess_exec", "getaddrinfo", "create_connection", "create_server",
                                                           "create_unix_connection", "create_unix_server")]
    t += [(socket, n) for n in ("create_connection", "getaddrinfo", "gethostbyname")]
    t += [(socket.socket, n) for n in ("connect", "connect_ex", "bind", "listen", "accept", "sendto")]
    return [(o, a) for o, a in t if hasattr(o, a)]


@pytest.fixture
def traps(monkeypatch):
    attempts: list[str] = []

    def make(name):
        def trap(*a, **k):
            attempts.append(name)
            raise SideEffectAttempted(name)
        return trap
    for obj, attr in _targets():
        monkeypatch.setattr(obj, attr, make(f"{getattr(obj, '__name__', obj)}.{attr}"))
    return SimpleNamespace(attempts=attempts)


V = VerifiedDocker("/usr/bin/docker", "unix:///run/user/1000/docker.sock")
IMG = image.ImageRef("cognitive-harness2-proposer:0123456789ab", "sha256:" + "5" * 64)
OK = upstream_egress.Decision(True, ("approved",))
PROFILE = channels.parse_text(textwrap.dedent("""
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
    wall_s = 600
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
    wall_s = 600
    cost_usd = 1
"""), source="t")


def test_every_trap_trips_and_is_recorded(traps):
    probes = [lambda: subprocess.run(["true"]), lambda: subprocess.Popen(["true"]), lambda: os.system("true"),
              lambda: socket.socket().bind(("127.0.0.1", 0)), lambda: socket.socket().connect(("127.0.0.1", 9)),
              lambda: socket.socket(socket.AF_UNIX).bind("/tmp/x.sock"), lambda: socket.getaddrinfo("example.invalid", 80),
              lambda: asyncio.run(asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)),
              lambda: asyncio.run(asyncio.open_connection("127.0.0.1", 9)),
              lambda: asyncio.run(asyncio.start_unix_server(lambda r, w: None, "/tmp/y.sock"))]
    for p in probes:
        with pytest.raises(SideEffectAttempted):
            p()
    assert len(traps.attempts) >= len(probes)


def test_the_proxy_decision_logic_is_pure(traps):
    policy = ep.Policy.build([["api.anthropic.com", 443], ["203.0.113.9", 443]],
                             [{"host": "ollama.test", "port": 11434, "unix": "/bridge/o.sock"}], ["172.30.0.0/16"])
    for host in ("api.anthropic.com", "API.anthropic.com.", "api。anthropic。com", "127.1", "0x7f000001", "[::1]", "169.254.169.254",
                 "evil.com", "ollama.test", "203.0.113.9", "", "a b", "a@b", "x" * 300):
        policy.decide(host, 443)
    for addr in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "::ffff:10.0.0.1", "2002:a00:1::", "8.8.8.8", "2606:4700::1111", "172.30.1.1"):
        ep.address_problem(ipaddress.ip_address(addr), policy.allow_cidrs)
    for bad in (["8.8.8.8", "10.0.0.1"], ["bogus"]):
        with pytest.raises(ep.Refused):
            ep.validated_addresses(bad, policy)
    ep.validated_addresses(["8.8.8.8"], policy)
    req = ep.parse_head(b"GET http://a.example:81/p?q=1 HTTP/1.1\r\nHost: x\r\nProxy-Authorization: y")
    ep.rewrite_request(req, "a.example", 81, "/p?q=1")
    for target in ("http://u@a.example/", "/x", "https://a.example/", "http://a.example/#f"):
        with pytest.raises(ep.Refused):
            ep.split_absolute_uri(target)
    for head in (b"GET / HTTP/9", b"x" * 9000, b"GET http://a/ HTTP/1.1\nHost: a"):
        with pytest.raises(ep.Refused):
            ep.parse_head(head)
    ep.TokenBucket(1.0, 2).take()
    ep.parse_config({"allow": [["a.example", 443]], "limits": {"max_connections": 2}})
    with pytest.raises(ValueError):
        ep.parse_config({"allow": [["*.a.example", 443]]})
    assert traps.attempts == []


def test_bridge_validators_and_construction_are_pure(traps):
    host_bridge.validate_target("127.0.0.1", 11434)
    for bad in (("localhost", 1), ("10.0.0.1", 1), ("127.0.0.1", 0)):
        with pytest.raises(ValueError):
            host_bridge.validate_target(*bad)
    b = host_bridge.HostBridge("127.0.0.1", 11434)          # constructing binds nothing
    assert b.directory is None and b.socket_path is None
    assert traps.attempts == []


def test_image_reference_checks_are_pure(traps):
    image.validate_reference(IMG.tag)
    for bad in ("ubuntu:latest", "ubuntu", ""):
        with pytest.raises(image.ImageError):
            image.validate_reference(bad)
    tag, full, base = image.expected()
    assert tag.startswith(image.REPO) and base.startswith("ubuntu:26.04@sha256:")
    with pytest.raises(image.ImageError):
        image.base_reference("FROM ubuntu:26.04\n")
    assert traps.attempts == []


def test_authorisation_builders_and_output_hygiene_are_pure(traps, tmp_path):
    cli = tmp_path / "fake-cli"
    cli.write_text("x")
    ws = tmp_path / "ws"
    ws.mkdir()
    runner = sr.ProposerSessionRunner(V, IMG, PROFILE.channels["local"], zdr="strict", classification="restricted", proposer_decision=OK)
    agy = sr.ProposerSessionRunner(V, IMG, PROFILE.channels["agy"], zdr="off", classification="open")
    cred = sr.Credential("/home/agent/.t", "s3cret-value")
    for r, c in ((runner, None), (agy, cred)):
        spec = sr.SessionSpec(argv=("/opt/cli/fake-cli", "x"), workspace=str(ws), cli=str(cli), timeout_s=10, credential=c)
        plan = r.authorize(spec)
        names = sr.Names("abc")
        sr.proxy_config(plan)
        sr.session_env(plan)
        sr.build_session_run(plan, names, docker=V.path, host=V.host, cred_file="/x/y" if c else None)
        sr.build_proxy_create(plan, names, docker=V.path, host=V.host, proxy_dir="/p", bridge_dir="/b" if plan.local else None)
        sr.build_reclaim_run(plan, names, docker=V.path, host=V.host)
    for bad in (dict(argv=("/bin/sh",)), dict(timeout_s=10_000), dict(workspace="/etc"), dict(env={"HTTP_PROXY": "x"})):
        base = dict(argv=("/opt/cli/fake-cli",), workspace=str(ws), cli=str(cli), timeout_s=10)
        base.update(bad)
        with pytest.raises(sr.SessionRefused):
            runner.authorize(sr.SessionSpec(**base))
    denied = sr.ProposerSessionRunner(V, IMG, PROFILE.channels["agy"], zdr="strict", classification="open")
    with pytest.raises(sr.SessionRefused):
        denied.run(sr.SessionSpec(argv=("/opt/cli/fake-cli",), workspace=str(ws), cli=str(cli), timeout_s=10))
    assert sr.redact("a s3cret-value b", ["s3cret-value"]) == "a [REDACTED] b"
    assert sr.sanitize_terminal("\x1b[2Jok") == "ok"
    assert traps.attempts == []


def test_workspace_harvest_only_reads_the_directory_it_is_given(traps, tmp_path):
    (tmp_path / "a.md").write_text("x")
    assert workspace_harvest.inspect_workspace(str(tmp_path)).files == ("a.md",)
    assert traps.attempts == []


def test_the_places_that_do_reach_the_outside_are_the_ones_that_trip_the_traps(traps, tmp_path):
    runner = sr.ProposerSessionRunner(V, IMG, PROFILE.channels["local"], zdr="strict", classification="restricted", proposer_decision=OK)
    with pytest.raises(SideEffectAttempted):
        runner._docker(["ps"])                                   # the only door to the daemon
    with pytest.raises(host_bridge.BridgeError, match="did not start"):
        host_bridge.HostBridge(base_dir=tmp_path).__enter__()    # binding the unix socket (the trap fires in its thread)
    assert any("start_unix_server" in a for a in traps.attempts)
    assert list(tmp_path.iterdir()) == []                        # and it cleaned up after itself
    with pytest.raises(SideEffectAttempted):
        asyncio.run(ep.serve(ep.parse_config({"allow": [["a.example", 443]], "listen_host": "127.0.0.1", "listen_port": 8080})))
    with pytest.raises(SideEffectAttempted):
        asyncio.run(ep.resolve_once("a.example", 443))           # name resolution
    assert "run" in " ".join(traps.attempts) and "getaddrinfo" in " ".join(traps.attempts)
