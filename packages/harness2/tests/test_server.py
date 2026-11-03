"""The FastMCP surface: tools, strict arguments, refusals as tool errors, no secret leaks."""
import json

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from cognitive_harness2 import server as server_mod
from cognitive_harness2 import settings
from cognitive_harness2.engine import Engine
from cognitive_harness2.experience import ExperienceStore
from cognitive_harness2.quota import QuotaVerdict
from cognitive_harness2.sandbox import SandboxUnavailable
from cognitive_harness2.server import build_server


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("JEV_OPENROUTER_API_KEY", "sk-secret-123")
    # The real report runs read-only `sbx` commands when a daemon is up; this suite starts no host process.
    monkeypatch.setattr(server_mod, "sandbox_report", lambda: {
        "sandbox": "rootless-docker", "runner": "rootless-docker", "mode": "auto",
        "fallback_reason": {"code": "daemon-down", "detail": "sbx daemon is not reachable", "loud": True},
        "sbx": {"state": "present-but-unusable", "path": "/usr/bin/sbx", "version": None, "mode": "auto",
                "verified_versions": ["0.50.1"], "reasons": ["sbx daemon is not reachable"],
                "codes": ["daemon-down"], "settings": {}}})
    s = settings.from_env({settings.ENV_EXPERIENCE_ROOT: str(tmp_path)})
    async def q():
        return QuotaVerdict(True, "ok")
    def no_sbx():
        raise SandboxUnavailable("no sbx (fail closed)")
    eng = Engine(generator=None, sandbox_factory=no_sbx, store=ExperienceStore(tmp_path), quota_check=q,
                 base_url=settings.DEFAULT_BASE_URL, model=None)
    return Client(build_server(eng, s))


VALID = {"task": "t", "targets": [{"component": "prompt", "content": "x"}], "dry_run": True}


async def call(client, name, args):
    async with client as c:
        return (await c.call_tool(name, args)).structured_content


async def test_exposes_the_four_rfc_tools_plus_status_and_trigger(client):
    async with client as c:
        assert {t.name for t in await c.list_tools()} == {
            "harness2_propose", "harness2_probe", "harness2_compose", "harness2_reflect", "harness2_status",
            "harness2_trigger"}


@pytest.mark.parametrize("mutate", [
    lambda a: {**a, "visiblity": 1},                               # unknown argument
    lambda a: {**a, "zdr": "maybe"},                               # outside the enum
    lambda a: {**a, "n": 9},                                       # out of range
    lambda a: {**a, "n": "three"},                                 # wrong type
    lambda a: {**a, "targets": []},                                # too few
    lambda a: {**a, "targets": [{"component": "prompt", "content": "x", "extra": 1}]},   # nested unknown key
    lambda a: {k: v for k, v in a.items() if k != "task"},         # missing required
])
async def test_propose_rejects_bad_arguments(client, mutate):
    async with client as c:
        with pytest.raises(ToolError):
            await c.call_tool("harness2_propose", mutate(VALID))


async def test_dry_run_reports_the_policy_verdict(client):
    out = await call(client, "harness2_propose", VALID)
    assert out["dry_run"] is True and "local" in out["egress"]


async def test_a_protected_component_is_a_tool_error(client):
    async with client as c:
        with pytest.raises(ToolError, match="protected"):
            await c.call_tool("harness2_propose", {**VALID, "targets": [{"component": "egress_policy", "content": "x"}]})


async def test_probe_without_sbx_is_a_tool_error_that_says_fail_closed(client):
    async with client as c:
        with pytest.raises(ToolError, match="unknown run_id"):
            await c.call_tool("harness2_probe", {"run_id": "nope", "command": ["pytest"]})


async def test_reflect_rejects_an_unsafe_domain(client):
    async with client as c:
        with pytest.raises(ToolError):
            await c.call_tool("harness2_reflect", {"run_id": "x", "domain": "../escape"})


async def test_status_never_returns_the_api_key(client):
    out = await call(client, "harness2_status", {})
    assert out["api_key_configured"] is True
    assert "sk-secret-123" not in json.dumps(out)
    assert "egress_policy" in out["protected_components"] and "prompt" in out["mutable_components"]


async def test_status_keeps_the_sandbox_string_and_reports_sbx_separately(client):
    out = await call(client, "harness2_status", {})
    assert out["sandbox"] == "rootless-docker"
    assert out["sbx"]["state"] == "present-but-unusable" and out["sbx"]["reasons"]
    # the runner and why it is not sbx are data too, not only text
    assert (out["runner"], out["mode"]) == ("rootless-docker", "auto")
    assert out["fallback_reason"]["code"] == "daemon-down" and out["sbx"]["codes"] == ["daemon-down"]


async def test_status_rejects_unknown_arguments(client):
    async with client as c:
        with pytest.raises(ToolError):
            await c.call_tool("harness2_status", {"check_quotas": True})


# ── harness2_probe must not block the MCP event loop ──────────────────────────────────

async def test_probe_runs_off_the_event_loop_thread(tmp_path, monkeypatch):
    """Engine.probe is blocking (an sbx run is about 10 s a candidate): it runs in a worker thread, so
    harness2_status, harness2_trigger and the transport keepalive are not stalled while it does."""
    import threading

    from cognitive_harness2.engine import Candidate, Run
    from cognitive_harness2.sandbox import SandboxResult

    loop_thread = threading.get_ident()
    seen: list[int] = []

    class Runner:
        def run(self, spec):
            seen.append(threading.get_ident())
            return SandboxResult(0, "", "")

    async def q():
        return QuotaVerdict(True, "ok")
    s = settings.from_env({settings.ENV_EXPERIENCE_ROOT: str(tmp_path)})
    eng = Engine(generator=None, sandbox_factory=Runner, store=ExperienceStore(tmp_path), quota_check=q,
                 base_url=settings.DEFAULT_BASE_URL, model=None)
    eng._remember(Run("r1", "t", [Candidate("c1", "prompt", "a", "b"), Candidate("c2", "prompt", "a", "c")]))
    out = await call(Client(build_server(eng, s)), "harness2_probe", {"run_id": "r1", "command": ["true"]})
    assert len(out["results"]) == 2
    assert seen and loop_thread not in seen
