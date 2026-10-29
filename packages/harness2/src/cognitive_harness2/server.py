"""The Harness² MCP server: FastMCP, loopback, stateless, strict (RFC 001 D29-D30).

Meets the project-wide server contract: bound to 127.0.0.1 with no host option;
`host_origin_protection="auto"` (set from the first commit, it is OFF by default in
FastMCP); every argument model forbids extra keys, so a misspelled argument is a tool
error rather than a silent default.

Tools: `harness2_propose`, `harness2_probe`, `harness2_compose`, `harness2_reflect` (RFC
004 §2.7), the read-only pull trigger `harness2_trigger` (advice.py), plus a read-only
`harness2_status` that reports sandbox, key and storage readiness without a network call,
which is what Track 4's Gate 3 and the conformance probe need.
"""
from __future__ import annotations

import asyncio
import os
from typing import Annotated, Literal

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field

from . import __version__, advice, egress, invariants, quota, settings as cfg, triggers
from .engine import Engine, Refused, Target
from .experience import ExperienceStore
from .sandbox import default_runner, sandbox_status


class TargetArg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    component: Annotated[str, Field(description="what to mutate: prompt, tool_description, "
                                                "context_assembly or decomposition_strategy")]
    content: Annotated[str, Field(description="the current text of that component")]


class OpenAICompatGenerator:
    """POSTs to an OpenAI-compatible /chat/completions. Egress is checked before this is built."""

    def __init__(self, base_url: str, api_key: str | None):
        self.base_url, self.api_key = base_url.rstrip("/"), api_key

    async def generate(self, prompt: str, *, model: str) -> str:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.post(f"{self.base_url}/chat/completions", headers=headers,
                                  json={"model": model, "messages": [{"role": "user", "content": prompt}]})
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]


def _api_key() -> str | None:
    return os.environ.get("JEV_OPENROUTER_API_KEY") or os.environ.get("OPENROUTER_API_KEY")


def build_engine(s: cfg.Settings | None = None) -> Engine:
    s = s or cfg.from_env()
    return Engine(
        generator=OpenAICompatGenerator(s.base_url, _api_key()),
        sandbox_factory=default_runner,
        store=ExperienceStore(s.experience_root),
        quota_check=lambda: quota.preflight(_api_key()),
        base_url=s.base_url, model=s.model, zdr_endpoints=s.zdr_endpoints,
    )


def build_server(engine: Engine | None = None, s: cfg.Settings | None = None) -> FastMCP:
    s = s or cfg.from_env()
    eng = engine or build_engine(s)
    server = FastMCP("harness2", version=__version__)

    def guard(fn):
        async def wrapped(*a, **k):
            try:
                return await fn(*a, **k)
            except Refused as e:
                raise ToolError(str(e)) from e
        return wrapped

    @server.tool(name="harness2_status", description="Read-only readiness report: which local sandbox is usable "
                 "(sbx, or a verified rootless Docker; otherwise unavailable and fail closed), "
                 "whether an OpenRouter key is configured (never its value), the experience root, "
                 "and the immutable protected set. Makes no network call unless check_quota is true.")
    async def status(check_quota: bool = False) -> dict:
        out = {"version": __version__, "sandbox": await asyncio.to_thread(sandbox_status),
               "api_key_configured": _api_key() is not None, "experience_root": str(s.experience_root),
               "mutable_components": sorted(invariants.MUTABLE),
               "protected_components": sorted(invariants.PROTECTED_COMPONENTS),
               "endpoint": s.base_url, "model": s.model}
        if check_quota:
            v = await quota.preflight(_api_key())
            out["quota"] = {"ok": v.ok, "reason": v.reason}
        return out

    @server.tool(name="harness2_trigger", description="Read-only pull check: given a failure summary, "
                 "say whether a System 2 probe is worth suggesting (advise: a trigger fired), which trigger fired, "
                 "whether quota, egress, sandbox and model are ready (ready: false if any is known-bad, true only "
                 "if all are ok, null if unchecked), the cost class, and that human approval is required. Starts no "
                 "sandbox, calls no model, writes nothing; run harness2_propose and harness2_probe explicitly to act.")
    async def trigger(
        on_demand: Annotated[bool, Field(description="the user or agent explicitly asks for a probe")] = False,
        judge_passed: Annotated[bool | None, Field(description="jev_judge verdict, if there was one")] = None,
        judge_score: Annotated[float | None, Field(ge=0, le=1, description="jev_judge score, if any")] = None,
        consecutive_failures: Annotated[int, Field(ge=0, le=1000, description="failures in a row on this task")] = 0,
        failure_signatures: Annotated[list[Annotated[str, Field(max_length=200)]], Field(
            max_length=20, description="short fingerprints of the recent failures, newest last; "
            "a trailing identical run counts as that many consecutive failures. Not stored.")] = [],
        zdr: Annotated[Literal["strict", "off"], Field(description="zero-data-retention policy")] = "strict",
        classification: Annotated[Literal["open", "gated", "unstated", "restricted"], Field(
            description="classification of the content; unknown is treated as restricted")] = "unstated",
        check_quota: Annotated[bool, Field(description="run the OpenRouter quota pre-flight (one GET) "
                                                       "when the endpoint is OpenRouter")] = False,
    ) -> dict:
        decision = triggers.should_probe(
            judge_passed=judge_passed, judge_score=judge_score, consecutive_failures=consecutive_failures,
            on_demand=on_demand, failure_signatures=failure_signatures)
        return await advice.advise(eng, decision, zdr=zdr, classification=classification, check_quota=check_quota)

    @server.tool(name="harness2_propose", description="Generate mutation candidates for the given components. "
                 "Refuses protected or unknown components and any endpoint the ZDR/classification policy forbids, "
                 "before sending anything. dry_run reports what would happen without calling a model.")
    async def propose(
        task: Annotated[str, Field(description="the task the candidates should improve")],
        targets: Annotated[list[TargetArg], Field(min_length=1, description="components to mutate")],
        n: Annotated[int, Field(ge=1, le=8, description="candidates per target")] = 3,
        zdr: Annotated[Literal["strict", "off"], Field(description="zero-data-retention policy")] = "strict",
        classification: Annotated[Literal["open", "gated", "unstated", "restricted"], Field(
            description="classification of the content; unknown is treated as restricted")] = "unstated",
        dry_run: Annotated[bool, Field(description="report the policy verdict only")] = False,
    ) -> dict:
        return await guard(eng.propose)(task=task, targets=[Target(t.component, t.content) for t in targets],
                                        n=n, zdr=zdr, classification=classification, dry_run=dry_run)

    @server.tool(name="harness2_probe", description="Run each candidate in a local sbx microVM, or an ephemeral rootless "
                 "Docker container when sbx is absent, and collect outputs. With neither it fails closed "
                 "and runs nothing; allow_worktree_diff opts in to static diffs only, which execute no code.")
    async def probe(
        run_id: str,
        command: Annotated[list[str], Field(min_length=1, description="command to run in the sandbox")],
        timeout_s: Annotated[int, Field(ge=1, le=600)] = 120,
        allow_worktree_diff: bool = False,
    ) -> dict:
        async def go():
            return eng.probe(run_id=run_id, command=command, timeout_s=timeout_s,
                             allow_worktree_diff=allow_worktree_diff)
        return await guard(go)()

    @server.tool(name="harness2_compose", description="Contrast probe outcomes without labels and propose a "
                 "patch. The output always requires human approval before anything is applied.")
    async def compose(run_id: str) -> dict:
        async def go():
            return eng.compose(run_id)
        return await guard(go)()

    @server.tool(name="harness2_reflect", description="Stage a lesson from a probed run as a PENDING experience. "
                 "Nothing is committed: a human must approve it with the cognitive-harness2 CLI.")
    async def reflect(run_id: str, domain: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,62}$")]) -> dict:
        async def go():
            return eng.reflect(run_id=run_id, domain=domain)
        return await guard(go)()

    return server


def serve(stdio: bool = False, port: int | None = None) -> None:
    s = cfg.from_env()
    server = build_server(s=s)
    if stdio:
        server.run(transport="stdio", show_banner=False)
        return
    # ⚠️ host_origin_protection is off by default in FastMCP; omitting it once shipped a
    # DNS-rebinding hole in Lookup. The conformance runner fails the build without it.
    server.run(transport="http", host=cfg.HOST, port=port or s.port, path=cfg.PATH,
               stateless_http=True, show_banner=False, host_origin_protection="auto")
