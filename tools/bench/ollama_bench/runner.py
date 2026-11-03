"""The benchmark orchestrator: per model, the six phases in the pre-registered order, with budgets and gates."""
from __future__ import annotations

import json
import random
import statistics
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import scoring as sc
from .agent import Task, build_tasks, run_agent_task
from .cases import SYSTEM_TOOLS, Case, build_cases
from .client import ChatResult, Ollama, is_malformed_tool_call, same_model
from .perf import build_perf_request
from .system import GpuSampler, gpu_total_mib, gpu_used_mib, load_state, wait_for_quiet
from .tools import agent_tools, tool_definitions

DEFAULT_MODELS = ["qwen3-4b-16k", "gemma4:e4b", "nemotron-3-nano:4b"]

SETTINGS: dict[str, dict[str, Any]] = {
    "tools_t0": {"temperature": 0.0, "top_p": None, "think": None, "seed_base": 42},
    "tools_t06": {"temperature": 0.6, "top_p": 0.95, "think": None, "seed_base": 1000},
    "tools_nothink": {"temperature": 0.0, "top_p": None, "think": False, "seed_base": 42},
    "agent_t0": {"temperature": 0.0, "top_p": None, "think": None, "seed_base": 42},
    "agent_nothink": {"temperature": 0.0, "top_p": None, "think": False, "seed_base": 42},
}
PERF_SIZES = (1000, 4000)
COLD_LOADS = 3
TIMED_RUNS = 3


class ModelFailed(Exception):
    """Three errors in a row (or a load that never worked)."""


def make_options(setting: str, kind: str, rep: int = 0) -> dict[str, Any]:
    s = SETTINGS[setting]
    opts: dict[str, Any] = {"num_ctx": sc.NUM_CTX, "temperature": s["temperature"], "seed": s["seed_base"] + rep,
                            "num_predict": sc.NUM_PREDICT[kind]}
    if s["top_p"] is not None:
        opts["top_p"] = s["top_p"]
    return opts


def _med(values: list[float]) -> float | None:
    return round(statistics.median(values), 3) if values else None


def _spread(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"median": None, "min": None, "max": None, "n": 0}
    return {"median": round(statistics.median(values), 2), "min": round(min(values), 2),
            "max": round(max(values), 2), "n": len(values)}


class Jsonl:
    def __init__(self, path: Path):
        self.path = path
        self._f = open(path, "a", encoding="utf-8")

    def write(self, rec: dict[str, Any]) -> None:
        self._f.write(json.dumps(rec, default=str, ensure_ascii=False) + "\n")
        self._f.flush()

    def close(self) -> None:
        self._f.close()


@dataclass
class ModelState:
    name: str
    index: int
    start: float = 0.0
    budget_s: float = 0.0
    streak: int = 0
    ever_ok: bool = False
    load_failure: bool = False
    crash: bool = False
    noise: dict[str, Any] = field(default_factory=dict)
    gpu_baseline_mib: int | None = None
    perf: dict[str, Any] = field(default_factory=dict)
    ps_16k: dict[str, Any] | None = None
    phases: dict[str, dict[str, Any]] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    gpu: dict[str, Any] = field(default_factory=dict)
    unload_confirmed: bool | None = None
    wall_s: float = 0.0


class Runner:
    def __init__(self, client: Ollama, out_dir: Path, *, models: list[str] | None = None, budget_s: float = sc.TOTAL_BUDGET_S,
                 reps: int = 2, seed: int | None = None, phases: tuple[str, ...] = sc.PHASE_ORDER,
                 limit_cases: int | None = None, limit_tasks: int | None = None,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 sampler_factory: Callable[[], GpuSampler] | None = None,
                 gpu_read: Callable[[], int | None] = gpu_used_mib,
                 noise_gate: Callable[[], Any] | None = None, log: Callable[[str], None] = print):
        self.client, self.out_dir = client, Path(out_dir)
        self.models = list(models or DEFAULT_MODELS)
        self.budget_s, self.reps = budget_s, reps
        self.seed = int(time.time()) if seed is None else seed
        self.phases = phases
        self.limit_cases, self.limit_tasks = limit_cases, limit_tasks
        self.clock, self.sleep, self.log = clock, sleep, log
        self.gpu_read = gpu_read
        self.sampler_factory = sampler_factory or (lambda: GpuSampler(gpu_read))
        self.noise_gate = noise_gate or wait_for_quiet
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.jsonl = Jsonl(self.out_dir / "results.jsonl")
        self.states: list[ModelState] = []

    # ── chat with accounting ─────────────────────────────────────────────────────────
    def _chat(self, st: ModelState, phase: str, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None,
              options: dict[str, Any], think: bool | None, timeout: float, meta: dict[str, Any] | None = None) -> ChatResult:
        res = self.client.chat(st.name, messages, tools=tools, options=options, think=think, timeout=timeout)
        infra_error = (not res.ok) and not is_malformed_tool_call(res)
        if infra_error:
            st.streak += 1
        else:
            st.streak = 0
            if res.ok:
                st.ever_ok = True
        self.jsonl.write({"kind": "chat", "model": st.name, "phase": phase, "ok": res.ok, "error": res.error,
                          "timed_out": res.timed_out, "stats": res.stats, "options": options, "think": think,
                          "raw": res.raw, **(meta or {})})
        if st.streak >= sc.ERROR_STREAK_LIMIT:
            raise ModelFailed(f"{st.streak} errors in a row; last: {res.error}")
        return res

    # ── phase A ─────────────────────────────────────────────────────────────────────
    def phase_perf(self, st: ModelState) -> None:
        loads: list[dict[str, Any]] = []
        opts = {"num_ctx": sc.NUM_CTX, "temperature": 0.0, "seed": 42, "num_predict": 8}
        for i in range(COLD_LOADS):
            try:
                self.client.unload(st.name)
            except Exception as e:  # noqa: BLE001 - an unload error is recorded, the load below decides
                st.notes.append(f"unload before cold load {i + 1} failed: {e}")
            confirmed = self.client.wait_unloaded(st.name)
            t0 = self.clock()
            res = self._chat(st, "perf", [{"role": "user", "content": "Reply with the single word: ready."}],
                             tools=None, options=opts, think=False, timeout=sc.REQUEST_TIMEOUT_S,
                             meta={"step": "cold_load", "i": i})
            loads.append({"i": i, "ok": res.ok, "error": res.error, "unload_confirmed": confirmed,
                          "wall_s": round(self.clock() - t0, 3), "load_duration_s": (res.stats.get("load_duration") or 0) / 1e9,
                          "total_duration_s": (res.stats.get("total_duration") or 0) / 1e9})
            if not res.ok:
                self.sleep(5)
        st.perf["cold_loads"] = loads
        ok_loads = [x for x in loads if x["ok"]]
        st.perf["cold_load_wall_s"] = _spread([x["wall_s"] for x in ok_loads])
        st.perf["cold_load_duration_s"] = _spread([x["load_duration_s"] for x in ok_loads])
        if not ok_loads:
            raise ModelFailed("no cold load succeeded")
        ps = [m for m in self.client.ps() if same_model(m.get("name") or m.get("model"), st.name)]
        st.ps_16k = ps[0] if ps else None
        if st.ps_16k:
            st.perf["ps"] = {k: st.ps_16k.get(k) for k in ("size", "size_vram", "context_length", "expires_at")}
            st.perf["gpu_fraction"] = sc.gpu_fraction(st.ps_16k)
        # one untimed warm-up at 1k, then the timed runs
        msgs, tools = build_perf_request(PERF_SIZES[0], f"warm-{st.index}")
        self._chat(st, "perf", msgs, tools=tools, think=False, timeout=sc.REQUEST_TIMEOUT_S,
                   options={"num_ctx": sc.NUM_CTX, "temperature": 0.0, "seed": 42, "num_predict": sc.NUM_PREDICT["perf"]},
                   meta={"step": "warmup"})
        timed: dict[int, list[dict[str, Any]]] = {n: [] for n in PERF_SIZES}
        for rep in range(TIMED_RUNS):
            for n in PERF_SIZES:
                msgs, tools = build_perf_request(n, f"{st.index}-{n}-{rep}-{int(time.time() * 1000)}")
                res = self._chat(st, "perf", msgs, tools=tools, think=False, timeout=sc.REQUEST_TIMEOUT_S,
                                 options={"num_ctx": sc.NUM_CTX, "temperature": 0.0, "seed": 42,
                                          "num_predict": sc.NUM_PREDICT["perf"]},
                                 meta={"step": "timed", "target_tokens": n, "rep": rep})
                if res.ok:
                    timed[n].append(res.stats)
        st.perf["timed"] = {}
        for n, rows in timed.items():
            st.perf["timed"][str(n)] = {
                "prompt_tokens": _spread([r["prompt_eval_count"] for r in rows if r.get("prompt_eval_count")]),
                "prompt_tps": _spread([r["prompt_tps"] for r in rows if r.get("prompt_tps")]),
                "gen_tps": _spread([r["gen_tps"] for r in rows if r.get("gen_tps")]),
                "gen_tokens": _spread([r["eval_count"] for r in rows if r.get("eval_count")]),
                "wall_s": _spread([r["wall_s"] for r in rows]),
            }

    # ── phase B ─────────────────────────────────────────────────────────────────────
    def phase_tools(self, st: ModelState, phase: str, deadline: float) -> None:
        setting = SETTINGS[phase]
        cases = build_cases()
        if self.limit_cases:
            cases = cases[: self.limit_cases]
        tools = tool_definitions()
        recs: list[dict[str, Any]] = []
        skipped = 0
        for rep in range(self.reps):
            for case in cases:
                if self.clock() > deadline:
                    skipped += 1
                    continue
                recs.append(self._tool_case(st, phase, setting, case, rep, tools))
        st.skipped[phase] = skipped
        st.phases[phase] = summarize_tools(recs, skipped)

    def _tool_case(self, st: ModelState, phase: str, setting: dict[str, Any], case: Case, rep: int,
                   tools: list[dict[str, Any]]) -> dict[str, Any]:
        messages = [{"role": "system", "content": SYSTEM_TOOLS}] + case.messages
        res = self._chat(st, phase, messages, tools=tools, options=make_options(phase, "tools", rep),
                         think=setting["think"], timeout=sc.REQUEST_TIMEOUT_S,
                         meta={"step": "tool_case", "case": case.id, "rep": rep})
        if res.ok:
            calls = res.tool_calls
            ok, reason = case.check(calls, res.content)
            if not ok and res.stats.get("done_reason") == "length":
                reason = "truncated (num_predict): " + reason
        else:
            calls = []
            ok, reason = False, ("malformed tool call JSON: " if is_malformed_tool_call(res) else "error: ") + str(res.error)
        rec = {"kind": "case", "model": st.name, "phase": phase, "case": case.id, "category": case.category, "rep": rep,
               "passed": ok, "reason": reason, "calls": calls, "content": res.content[:500], "stats": res.stats,
               "error": res.error}
        self.jsonl.write(rec)
        return rec

    # ── phase C ─────────────────────────────────────────────────────────────────────
    def phase_agent(self, st: ModelState, phase: str, deadline: float) -> None:
        setting = SETTINGS[phase]
        tasks: list[Task] = build_tasks()
        if self.limit_tasks:
            tasks = tasks[: self.limit_tasks]
        recs: list[dict[str, Any]] = []
        skipped = 0
        opts = make_options(phase, "agent", 0)
        for task in tasks:
            if self.clock() > deadline:
                skipped += 1
                continue

            def chat(messages: list[dict[str, Any]], tools: list[dict[str, Any]], timeout: float, _t=task) -> ChatResult:
                return self._chat(st, phase, messages, tools=tools, options=opts, think=setting["think"],
                                  timeout=timeout, meta={"step": "agent_step", "task": _t.id})
            rec = run_agent_task(task, chat, clock=self.clock)
            rec.update({"kind": "task", "model": st.name, "phase": phase})
            self.jsonl.write(rec)
            recs.append(rec)
        st.skipped[phase] = skipped
        st.phases[phase] = summarize_agent(recs, skipped)

    # ── one model ───────────────────────────────────────────────────────────────────
    def run_model(self, st: ModelState, models_left: int, run_end: float) -> None:
        noise = self.noise_gate()
        st.noise = {"before": noise.before, "waited_s": noise.waited_s, "settled": noise.settled,
                    "polls": noise.polls}
        st.start = self.clock()
        st.budget_s = max(60.0, (run_end - st.start) / max(1, models_left))
        self.log(f"[{st.name}] start; budget {st.budget_s:.0f}s; load {noise.before}")
        for n in self.models:                     # one model resident at a time, ours only
            try:
                self.client.unload(n)
            except Exception as e:  # noqa: BLE001
                st.notes.append(f"pre-run unload of {n} failed: {e}")
        self.client.wait_unloaded(None, wait_s=20)
        st.gpu_baseline_mib = self.gpu_read()
        sampler = self.sampler_factory()
        sampler.start()
        try:
            for phase in self.phases:
                sampler.mark(phase)
                deadline = sc.phase_deadline(st.start, st.budget_s, phase)
                self.log(f"[{st.name}] phase {phase} (deadline in {deadline - self.clock():.0f}s)")
                try:
                    if phase == "perf":
                        self.phase_perf(st)
                    elif phase.startswith("tools"):
                        self.phase_tools(st, phase, deadline)
                    else:
                        self.phase_agent(st, phase, deadline)
                except ModelFailed as e:
                    st.notes.append(f"{phase}: {e}")
                    if st.ever_ok:
                        st.crash = True
                    else:
                        st.load_failure = True
                    self.log(f"[{st.name}] FAILED in {phase}: {e}")
                    break
        finally:
            st.gpu = sampler.stop()
            try:
                self.client.unload(st.name)
                st.unload_confirmed = self.client.wait_unloaded(None, wait_s=30)
            except Exception as e:  # noqa: BLE001
                st.unload_confirmed = False
                st.notes.append(f"final unload failed: {e}")
            st.wall_s = round(self.clock() - st.start, 1)
            self.jsonl.write({"kind": "model_end", "model": st.name, "load_failure": st.load_failure,
                              "crash": st.crash, "notes": st.notes, "gpu": st.gpu, "wall_s": st.wall_s,
                              "unload_confirmed": st.unload_confirmed})

    def run(self) -> dict[str, Any]:
        started = datetime.now(timezone.utc)
        rng = random.Random(self.seed)
        order = list(self.models)
        rng.shuffle(order)
        t0 = self.clock()
        run_end = t0 + self.budget_s - sc.REPORT_RESERVE_S
        meta = {"kind": "run_start", "started_utc": started.isoformat(), "order": order, "seed": self.seed,
                "budget_s": self.budget_s, "gpu_total_mib": gpu_total_mib(), "reps": self.reps,
                "phases": list(self.phases), "load_state": load_state()}
        try:
            meta["ollama_version"] = self.client._request("/api/version", None, 10).get("version")
            meta["installed"] = {m["name"]: m.get("size") for m in self.client.tags()}
            meta["resident_at_start"] = [m.get("name") for m in self.client.ps()]
        except Exception as e:  # noqa: BLE001
            meta["ollama_error"] = str(e)
        self.jsonl.write(meta)
        for i, name in enumerate(order):
            st = ModelState(name=name, index=i)
            self.states.append(st)
            self.run_model(st, len(order) - i, run_end)
        summary = build_summary(self, meta, started, datetime.now(timezone.utc))
        self.jsonl.close()
        return summary


# ── summaries ─────────────────────────────────────────────────────────────────────────────

def summarize_tools(recs: list[dict[str, Any]], skipped: int) -> dict[str, Any]:
    n = len(recs)
    p = sum(1 for r in recs if r["passed"])
    by_cat: dict[str, list[int]] = {}
    fails = Counter()
    for r in recs:
        c = by_cat.setdefault(r["category"], [0, 0])
        c[1] += 1
        if r["passed"]:
            c[0] += 1
        else:
            fails[_reason_key(r["reason"])] += 1
    stats = [r["stats"] for r in recs if r["stats"].get("eval_count") is not None]
    return {"attempted": n, "passed": p, "rate": (p / n) if n else 0.0, "skipped": skipped, "by_category": by_cat,
            "failure_reasons": dict(fails.most_common()),
            "median_wall_s": _med([r["stats"].get("wall_s") for r in recs if r["stats"].get("wall_s") is not None]),
            "median_thinking_chars": _med([s.get("thinking_chars") or 0 for s in stats]),
            "median_gen_tokens": _med([s.get("eval_count") or 0 for s in stats]),
            "errors": sum(1 for r in recs if r.get("error")),
            "truncated": sum(1 for r in recs if str(r["reason"]).startswith("truncated"))}


def _reason_key(reason: str) -> str:
    r = str(reason)
    for key in ("no tool call", "wrong tool", "schema", "unexpected tool call", "expected one call", "expected ",
                "no acceptable", "first call", "empty answer", "answer lacks", "truncated", "malformed tool call",
                "error:", "content wrong", "content is not valid JSON", "path", "touches"):
        if r.startswith(key) or key in r[:40]:
            return key.strip(": ")
    return "value mismatch"


def summarize_agent(recs: list[dict[str, Any]], skipped: int) -> dict[str, Any]:
    n = len(recs)
    p = sum(1 for r in recs if r["passed"])
    cats = Counter(r["category"] or "gave_up" for r in recs if not r["passed"])
    by_kind: dict[str, list[int]] = {}
    for r in recs:
        k = by_kind.setdefault(r["task_kind"], [0, 0])
        k[1] += 1
        k[0] += 1 if r["passed"] else 0
    passed = [r for r in recs if r["passed"]]
    return {"attempted": n, "passed": p, "rate": (p / n) if n else 0.0, "skipped": skipped, "by_kind": by_kind,
            "failure_categories": dict(cats.most_common()),
            "median_wall_s": _med([r["wall_s"] for r in recs]),
            "median_wall_s_passed": _med([r["wall_s"] for r in passed]),
            "median_steps": _med([r["steps_used"] for r in recs]),
            "median_steps_passed": _med([r["steps_used"] for r in passed]),
            "median_gen_tokens": _med([r["gen_tokens"] for r in recs]),
            "median_prompt_tokens": _med([r["prompt_tokens"] for r in recs]),
            "total_thinking_chars": sum(r["thinking_chars"] for r in recs),
            "escape_attempts": sum(r["escape_attempts"] for r in recs),
            "tasks": [{"task": r["task_id"], "passed": r["passed"], "category": r["category"], "steps": r["steps_used"],
                       "wall_s": r["wall_s"], "detail": r["detail"]} for r in recs]}


def metrics_for(st: ModelState) -> sc.ModelMetrics:
    ph = st.phases
    g = lambda phase, key, default=None: (ph.get(phase) or {}).get(key, default)   # noqa: E731
    attempted = lambda phase: (ph.get(phase) or {}).get("attempted", 0)            # noqa: E731
    expected = ("tools_t0", "tools_t06", "agent_t0", "tools_nothink", "agent_nothink")
    incomplete = any((attempted(p) == 0) or (st.skipped.get(p, 0) > 0) for p in expected)
    return sc.ModelMetrics(
        name=st.name, load_failure=st.load_failure, crash=st.crash,
        gpu_fraction=st.perf.get("gpu_fraction"),
        agent_success=g("agent_t0", "rate", 0.0), tools_t0=g("tools_t0", "rate", 0.0),
        tools_t06=g("tools_t06", "rate", 0.0), agent_median_s=g("agent_t0", "median_wall_s"),
        agent_success_nothink=g("agent_nothink", "rate") if attempted("agent_nothink") else None,
        tools_nothink=g("tools_nothink", "rate") if attempted("tools_nothink") else None,
        agent_median_nothink_s=g("agent_nothink", "median_wall_s"), incomplete=incomplete)


def build_summary(runner: Runner, meta: dict[str, Any], started: datetime, finished: datetime) -> dict[str, Any]:
    metrics = [metrics_for(s) for s in runner.states]
    scored = sc.score_all(metrics)
    models: dict[str, Any] = {}
    for st, m in zip(runner.states, metrics):
        models[st.name] = {
            "order_index": st.index, "wall_s": st.wall_s, "budget_s": round(st.budget_s, 1), "noise": st.noise,
            "gpu_baseline_mib": st.gpu_baseline_mib, "gpu": st.gpu, "perf": st.perf, "ps_16k": st.ps_16k,
            "phases": st.phases, "skipped": st.skipped, "notes": st.notes, "load_failure": st.load_failure,
            "crash": st.crash, "unload_confirmed": st.unload_confirmed, "incomplete": m.incomplete,
            "thinking_verdict": sc.thinking_verdict(m), "score": scored[st.name],
        }
    return {"run_started_utc": started.isoformat(), "run_finished_utc": finished.isoformat(),
            "meta": {k: v for k, v in meta.items() if k != "kind"}, "weights": sc.WEIGHTS, "tie_points": sc.TIE_POINTS,
            "models": models, "scores": scored, "recommendation": sc.recommend(scored)}
