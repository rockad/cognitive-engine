"""Propose, Probe, Compose, Reflect (RFC 004 §2.1, §2.5).

A deliberately small baseline of the four stages. Every collaborator is injected (the
text generator, the sandbox, the quota check, the experience store) so each rule is
testable without a network, a microVM or a model, and the refusals are the point:

  * propose  refuses protected or unknown components, and any endpoint the egress policy
             forbids, BEFORE a request exists; and aborts when the quota gate says no
  * probe    runs candidate code only in a sandbox; with none it fails closed, and the
             only alternative is an explicit static-diff opt-in that executes nothing
  * compose  contrasts executed outcomes without labels and ALWAYS marks its output as
             needing human approval
  * reflect  stages lessons as pending; an agent cannot commit them

The composer here is a heuristic baseline (exit status, timeouts, output, stderr), not a
learned one. It does not yet call upstream google-research/harness2 (a pinned dependency,
see NOTICE and the README); binding these stages to upstream is the next step, not done here.
"""
from __future__ import annotations

import secrets
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlparse

from . import egress, invariants
from .experience import ExperienceStore, Pending
from .quota import QuotaVerdict
from .sandbox import SandboxRunner, SandboxSpec, SandboxUnavailable, static_diff

MAX_RUNS = 64
MAX_CANDIDATES = 8


class Refused(RuntimeError):
    """An expected, policy-driven refusal (shown to the caller as a tool error)."""


class Generator(Protocol):
    async def generate(self, prompt: str, *, model: str) -> str: ...


@dataclass(frozen=True)
class Target:
    component: str
    content: str


@dataclass(frozen=True)
class Candidate:
    id: str
    component: str
    original: str
    mutated: str


@dataclass(frozen=True)
class ProbeResult:
    candidate_id: str
    mode: str  # "sandbox" | "diff_only"
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    diff: str


@dataclass
class Run:
    id: str
    task: str
    candidates: list[Candidate]
    results: list[ProbeResult] = field(default_factory=list)


class Engine:
    def __init__(
        self,
        *,
        generator: Generator | None,
        sandbox_factory: Callable[[], SandboxRunner],
        store: ExperienceStore,
        quota_check: Callable[[], Awaitable[QuotaVerdict]],
        base_url: str,
        model: str | None,
        zdr_endpoints: frozenset[str] = frozenset(),
    ):
        self.generator, self.sandbox_factory, self.store = generator, sandbox_factory, store
        self.quota_check, self.base_url, self.model, self.zdr_endpoints = quota_check, base_url, model, zdr_endpoints
        self.runs: OrderedDict[str, Run] = OrderedDict()

    # ── propose ──────────────────────────────────────────────────────────────────
    def _policy(self, targets: list[Target], zdr: str, classification: str) -> str:
        if not targets:
            raise Refused("at least one target is required")
        for t in targets:
            try:
                invariants.assert_mutable(t.component)
            except invariants.InvariantViolation as e:
                raise Refused(str(e)) from e
        try:
            return egress.check_endpoint(zdr=zdr, base_url=self.base_url, model=self.model or "",
                                         classification=classification, zdr_endpoints=self.zdr_endpoints)
        except egress.EgressDenied as e:
            raise Refused(f"egress refused: {e}") from e

    async def propose(self, *, task: str, targets: list[Target], n: int, zdr: str,
                      classification: str, dry_run: bool) -> dict:
        if not 1 <= n <= MAX_CANDIDATES:
            raise Refused(f"n must be 1-{MAX_CANDIDATES}")
        if self.model is None and not dry_run:
            raise Refused("no generator model configured (set COGNITIVE_HARNESS2_MODEL)")
        verdict_line = self._policy(targets, zdr, classification)
        if dry_run:
            return {"dry_run": True, "egress": verdict_line, "quota": "not checked (dry run)",
                    "would_generate": n * len(targets), "endpoint": self.base_url, "model": self.model}
        if self.generator is None:
            raise Refused("no generator configured")
        if (urlparse(self.base_url).hostname or "") == "openrouter.ai":
            q = await self.quota_check()
            if not q.ok:
                raise Refused(f"probing aborted by the quota pre-flight: {q.reason}")
        run = Run(secrets.token_hex(6), task, [])
        for t in targets:
            for i in range(n):
                text = await self.generator.generate(
                    f"Task: {task}\nRewrite this {t.component} to do better. Variant {i + 1} of {n}. "
                    f"Return only the new text.\n\n{t.content}", model=self.model or "")
                run.candidates.append(Candidate(secrets.token_hex(4), invariants.normalise_component(t.component),
                                                t.content, text))
        self._remember(run)
        return {"run_id": run.id, "egress": verdict_line,
                "candidates": [{"id": c.id, "component": c.component, "chars": len(c.mutated)} for c in run.candidates]}

    def _remember(self, run: Run) -> None:
        self.runs[run.id] = run
        while len(self.runs) > MAX_RUNS:
            self.runs.popitem(last=False)

    def _run(self, run_id: str) -> Run:
        if run_id not in self.runs:
            raise Refused(f"unknown run_id {run_id!r}")
        return self.runs[run_id]

    # ── probe ────────────────────────────────────────────────────────────────────
    def probe(self, *, run_id: str, command: list[str], timeout_s: int, allow_worktree_diff: bool) -> dict:
        run = self._run(run_id)
        if not command:
            raise Refused("command cannot be empty")
        results: list[ProbeResult] = []
        try:
            runner = self.sandbox_factory()
        except SandboxUnavailable as e:
            if not allow_worktree_diff:
                raise Refused(str(e)) from e
            runner = None
        for c in run.candidates:
            diff = static_diff(c.component, c.original, c.mutated, allow_worktree_diff=True) if runner is None else ""
            if runner is None:
                results.append(ProbeResult(c.id, "diff_only", None, "", "", False, diff))
                continue
            try:
                out = runner.run(SandboxSpec(command=command, files={c.component: c.mutated}, timeout_s=timeout_s))
            except SandboxUnavailable as e:  # the runtime vanished mid-probe: still fail closed, never degrade
                raise Refused(str(e)) from e
            results.append(ProbeResult(c.id, "sandbox", out.exit_code, out.stdout, out.stderr, out.timed_out, ""))
        run.results = results
        return {"run_id": run.id, "mode": results[0].mode if results else "none",
                "results": [r.__dict__ for r in results]}

    # ── compose ──────────────────────────────────────────────────────────────────
    def compose(self, run_id: str) -> dict:
        run = self._run(run_id)
        if not run.results:
            raise Refused("nothing to compose: run harness2_probe first")
        base = {"run_id": run.id, "requires_human_approval": True}
        if all(r.mode == "diff_only" for r in run.results):
            return {**base, "evidence": "static diffs only (nothing was executed)", "winner": None,
                    "diffs": {r.candidate_id: r.diff for r in run.results}}
        ranked = sorted(run.results, key=lambda r: (r.exit_code == 0, not r.timed_out, bool(r.stdout.strip()),
                                                    -len(r.stderr)), reverse=True)
        best = ranked[0]
        cand = next(c for c in run.candidates if c.id == best.candidate_id)
        diverged = sorted({r.exit_code for r in run.results})
        return {**base, "evidence": "executed in sandbox", "winner": best.candidate_id,
                "patch": {"component": cand.component, "replacement": cand.mutated},
                "ranking": [r.candidate_id for r in ranked],
                "divergence": f"exit codes seen: {diverged}",
                "note": "a heuristic ranking: exit status, timeouts, output, stderr; review before applying"}

    # ── reflect ──────────────────────────────────────────────────────────────────
    def reflect(self, *, run_id: str, domain: str) -> dict:
        run = self._run(run_id)
        if not run.results:
            raise Refused("nothing to reflect on: run harness2_probe first")
        passed = [r for r in run.results if r.exit_code == 0]
        lesson = (f"Task: {run.task}\n{len(passed)} of {len(run.results)} candidate(s) succeeded "
                  f"({'sandbox' if run.results[0].mode == 'sandbox' else 'static diff only'}).")
        p: Pending = self.store.stage(domain, lesson)
        return {"pending_id": p.id, "domain": domain, "committed": False,
                "next": f"a human must run: cognitive-harness2 experience approve {p.id} --by <name>"}
