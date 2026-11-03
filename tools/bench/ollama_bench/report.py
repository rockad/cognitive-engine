"""summary.json -> report.md. The pre-registered rules are the header, before any result."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from . import scoring as sc

BENCH_DIR = Path(__file__).resolve().parents[1]


def git_info(repo: Path) -> dict[str, Any]:
    def run(*args: str) -> str:
        try:
            return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=20).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""
    added = run("log", "--diff-filter=A", "--format=%H", "--", "tools/bench/PREREGISTRATION.md").splitlines()
    return {"head": run("rev-parse", "HEAD"), "branch": run("rev-parse", "--abbrev-ref", "HEAD"),
            "prereg_commit": added[-1] if added else "",
            "prereg_latest_commit": run("log", "-n1", "--format=%H", "--", "tools/bench/PREREGISTRATION.md"),
            "dirty_bench": bool(run("status", "--porcelain", "--", "tools/bench"))}


def fmt(x: Any, nd: int = 1, none: str = "n/a") -> str:
    if x is None:
        return none
    if isinstance(x, float):
        return f"{x:.{nd}f}"
    return str(x)


def spread(d: dict[str, Any] | None, nd: int = 1) -> str:
    if not d or d.get("median") is None:
        return "n/a"
    return f"{d['median']:.{nd}f} [{d['min']:.{nd}f}-{d['max']:.{nd}f}] (n={d['n']})"


def pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:.0f}%"


def table(headers: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(out)


def variant_name(model: str) -> str:
    return model if model.endswith("-16k") else model.replace(":", "-") + "-16k"


def deploy_commands(model: str) -> str:
    v = variant_name(model)
    lines = []
    if model.endswith("-16k"):
        lines.append(f"# {model} already is a 16k variant (num_ctx 16384 is baked in); nothing to create.")
    else:
        lines += [f"cat > /tmp/Modelfile.{v} <<'EOF'", f"FROM {model}", "PARAMETER num_ctx 16384", "EOF",
                  f"ollama create {v} -f /tmp/Modelfile.{v}"]
    lines += ["", "# ~/h2-stage1/channels.toml", f'model = "ollama/{v}"']
    return "```sh\n" + "\n".join(lines) + "\n```"


def render(summary: dict[str, Any], git: dict[str, Any]) -> str:
    models = summary["models"]
    names = list(models)
    meta = summary["meta"]
    L: list[str] = []
    A = L.append
    A("# Ollama agentic benchmark: qwen3-4b-16k vs gemma4:e4b vs nemotron-3-nano:4b")
    A("")
    A("## Pre-registered rules (committed before any result)")
    A("")
    A(f"- Pre-registration commit: `{git.get('prereg_commit', '')[:12]}`, amended once before the main run (phase order and time shares only, see Amendment 1) in `{git.get('prereg_latest_commit', '')[:12]}` (tools/bench/PREREGISTRATION.md, tools/bench/ollama_bench/scoring.py); "
      f"code under test: `{git.get('head', '')[:12]}` on `{git.get('branch', '')}`"
      + (" (WARNING: tools/bench had uncommitted changes)" if git.get("dirty_bench") else " (clean)"))
    A("- Score = 100 x (0.50 agent success + 0.25 tool-suite pass rate + 0.15 speed + 0.10 robustness).")
    A("  - agent success: mini-agent tasks passed / attempted, thinking default, temperature 0 (6 sum-signed + 4 edit-prompt)")
    A("  - tool suite: pass rate, thinking default, temperature 0, 2 repetitions of every case")
    A("  - speed: fastest median task wall time / own median (0 when agent success is 0)")
    A("  - robustness: min(1, tool-suite pass rate at temperature 0.6 / at temperature 0)")
    A("- Hard gates (a failing model is not eligible to be optimal): no load failure, no crash (3 errors in a row), "
      "100% on the GPU at num_ctx 16384.")
    A(f"- Tie: eligible models within {sc.TIE_POINTS:.0f} points of the top are tied; the faster (lower median task wall time) wins.")
    A("- think:false is reported separately; disable thinking only if agent success and tool-suite pass rate "
      "(within 5 points) do not drop.")
    A("- Identical conditions: native /api/chat, num_ctx 16384, stream false, temperature 0 seed 42(+rep); robustness "
      "pass temperature 0.6 top_p 0.95; num_predict caps 2048 (tool call) / 4096 (agent step); request timeout 240 s; "
      "mini-agent max 12 steps and 180 s per task; no bash in the agent, path escapes refused.")
    A("- Clarification made at implementation time: the performance phase is never skipped by its phase deadline "
      "(only the later phases are); a request that fails with Ollama's 'error parsing tool call' is the model's "
      "failure (malformed_args) and does not count toward the three-errors-in-a-row gate.")
    A("")
    A("## Run conditions")
    A("")
    A(f"- Started {summary['run_started_utc']}, finished {summary['run_finished_utc']}; Ollama {meta.get('ollama_version')}; "
      f"GPU total {meta.get('gpu_total_mib')} MiB; budget {meta.get('budget_s')} s; model order (randomised, seed {meta.get('seed')}): "
      + " -> ".join(meta.get("order", [])))
    A(f"- Resident at start: {meta.get('resident_at_start')}; installed: {meta.get('installed')}")
    A("")
    rows = []
    for n in meta.get("order", []):
        m = models.get(n)
        if not m:
            continue
        b = (m.get("noise") or {}).get("before") or {}
        rows.append([n, fmt(b.get("load1")), fmt(b.get("load5")), fmt(b.get("mem_available_gb")),
                     fmt((m.get("noise") or {}).get("waited_s")), str((m.get("noise") or {}).get("settled")),
                     fmt(m.get("gpu_baseline_mib"), 0), fmt(m.get("budget_s"), 0), fmt(m.get("wall_s"), 0),
                     "yes" if m.get("incomplete") else "no"])
    A(table(["model", "load1", "load5", "avail RAM GB", "waited s", "settled", "GPU baseline MiB", "budget s",
             "used s", "incomplete"], rows))
    A("")
    A("Noise gate: wait up to 5 minutes if load1 > 4 or available RAM < 3 GB; 'settled' false means the limit was "
      "still exceeded after the maximum wait and the run went on.")
    A("")

    # A
    A("## A. Performance (num_ctx 16384, think:false probes, num_predict 160)")
    A("")
    rows = []
    for n in names:
        m = models[n]
        p = m.get("perf") or {}
        ps = p.get("ps") or {}
        gpu = m.get("gpu") or {}
        rows.append([n, spread(p.get("cold_load_wall_s")), spread(p.get("cold_load_duration_s")),
                     pct(p.get("gpu_fraction")), fmt((ps.get("size") or 0) / 2**30 if ps.get("size") else None, 2),
                     fmt((ps.get("size_vram") or 0) / 2**30 if ps.get("size_vram") else None, 2),
                     fmt(ps.get("context_length"), 0), fmt(gpu.get("peak_mib"), 0),
                     fmt((gpu.get("peak_mib") - m["gpu_baseline_mib"]) if gpu.get("peak_mib") and m.get("gpu_baseline_mib") is not None else None, 0)])
    A(table(["model", "cold load wall s", "Ollama load_duration s", "GPU %", "size GiB", "in VRAM GiB", "ctx",
             "peak VRAM MiB (nvidia-smi)", "peak minus baseline MiB"], rows))
    A("")
    rows = []
    for n in names:
        t = ((models[n].get("perf") or {}).get("timed") or {})
        for size in ("1000", "4000"):
            x = t.get(size) or {}
            rows.append([n, size, spread(x.get("prompt_tokens"), 0), spread(x.get("prompt_tps"), 0),
                         spread(x.get("gen_tps"), 1), spread(x.get("gen_tokens"), 0), spread(x.get("wall_s"), 2)])
    A(table(["model", "target tokens", "prompt tokens (actual)", "prompt-eval tok/s", "generation tok/s",
             "generated tokens", "wall s"], rows))
    A("")
    A("Median [min-max] over 3 timed runs after a warm-up; every prompt starts with a fresh nonce so the prompt cache "
      "cannot help. Peak VRAM includes the desktop's own baseline.")
    A("")

    # B
    A("## B. Tool-call suite")
    A("")
    rows = []
    for n in names:
        ph = models[n].get("phases") or {}
        for key, label in (("tools_t0", "thinking default, t=0"), ("tools_t06", "thinking default, t=0.6 top_p 0.95"),
                           ("tools_nothink", "think:false, t=0")):
            x = ph.get(key)
            if not x:
                rows.append([n, label, "not run", "", "", "", ""])
                continue
            rows.append([n, label, f"{x['passed']}/{x['attempted']} ({pct(x['rate'])})", str(x["skipped"]),
                         fmt(x["median_wall_s"], 1), fmt(x["median_thinking_chars"], 0), str(x["errors"])])
    A(table(["model", "setting", "passed", "skipped", "median wall s/call", "median thinking chars", "errors"], rows))
    A("")
    cats = sorted({c for n in names for c in ((models[n].get("phases") or {}).get("tools_t0") or {}).get("by_category", {})})
    rows = []
    for c in cats:
        row = [c]
        for n in names:
            x = (((models[n].get("phases") or {}).get("tools_t0") or {}).get("by_category") or {}).get(c)
            row.append(f"{x[0]}/{x[1]}" if x else "n/a")
        rows.append(row)
    A("Per category (thinking default, t=0):")
    A("")
    A(table(["category"] + names, rows))
    A("")
    for n in names:
        fr = ((models[n].get("phases") or {}).get("tools_t0") or {}).get("failure_reasons")
        if fr:
            A(f"- `{n}` tool-suite failure reasons (t=0): " + ", ".join(f"{k} x{v}" for k, v in fr.items()))
    A("")

    # C
    A("## C. Mini-agent tasks")
    A("")
    rows = []
    for n in names:
        ph = models[n].get("phases") or {}
        for key, label in (("agent_t0", "thinking default, t=0"), ("agent_nothink", "think:false, t=0")):
            x = ph.get(key)
            if not x:
                rows.append([n, label, "not run", "", "", "", "", ""])
                continue
            bk = x["by_kind"]
            kinds = ", ".join(f"{k} {v[0]}/{v[1]}" for k, v in bk.items())
            rows.append([n, label, f"{x['passed']}/{x['attempted']} ({pct(x['rate'])})", kinds,
                         f"{fmt(x['median_steps'], 1)} ({fmt(x['median_steps_passed'], 1)})",
                         f"{fmt(x['median_wall_s'], 1)} ({fmt(x['median_wall_s_passed'], 1)})",
                         f"{fmt(x['median_prompt_tokens'], 0)} / {fmt(x['median_gen_tokens'], 0)}", str(x["skipped"])])
    A(table(["model", "setting", "passed", "by task kind", "median steps (passed only)", "median wall s (passed only)",
             "median prompt / gen tokens per task", "skipped"], rows))
    A("")
    A("Failure categories per model:")
    A("")
    for n in names:
        ph = models[n].get("phases") or {}
        for key in ("agent_t0", "agent_nothink"):
            x = ph.get(key)
            if x:
                fc = x["failure_categories"]
                A(f"- `{n}` {key}: " + (", ".join(f"{k} x{v}" for k, v in fc.items()) if fc else "no failures"))
    A("")
    A("Per-task detail (thinking default, t=0):")
    A("")
    rows = []
    for n in names:
        for t in ((models[n].get("phases") or {}).get("agent_t0") or {}).get("tasks", []):
            rows.append([n, t["task"], "pass" if t["passed"] else "FAIL", t["category"] or "", str(t["steps"]),
                         fmt(t["wall_s"], 1), str(t["detail"])[:70]])
    A(table(["model", "task", "result", "category", "steps", "wall s", "detail"], rows))
    A("")

    # think:false
    A("## think:false versus thinking default")
    A("")
    rows = []
    for n in names:
        m = models[n]
        ph = m.get("phases") or {}
        v = m.get("thinking_verdict") or {}
        a0, a1 = ph.get("agent_t0"), ph.get("agent_nothink")
        t0, t1 = ph.get("tools_t0"), ph.get("tools_nothink")
        rows.append([n, pct(a0["rate"]) if a0 else "n/a", pct(a1["rate"]) if a1 else "n/a",
                     pct(t0["rate"]) if t0 else "n/a", pct(t1["rate"]) if t1 else "n/a",
                     fmt(a0["median_wall_s"]) if a0 else "n/a", fmt(a1["median_wall_s"]) if a1 else "n/a",
                     fmt(t1["median_thinking_chars"], 0) if t1 else "n/a",
                     {True: "disable", False: "keep thinking", None: "undetermined"}[v.get("disable_thinking")]])
    A(table(["model", "agent default", "agent think:false", "tools default", "tools think:false",
             "median task s default", "median task s think:false", "thinking chars with think:false", "rule says"], rows))
    A("")

    # scores
    A("## Scores")
    A("")
    rows = []
    for n in names:
        s = models[n]["score"]
        c = s["components"]
        rows.append([n, pct(c["agent"]), pct(c["tools"]), pct(c["speed"]), pct(c["robustness"]), f"{s['score']:.1f}",
                     ", ".join(s["gate_failures"]) or "none", "yes" if s["eligible"] else "no",
                     "INCOMPLETE" if s["incomplete"] else ""])
    A(table(["model", "A agent (50%)", "T tools (25%)", "S speed (15%)", "R robustness (10%)", "score", "gate failures",
             "eligible", "note"], rows))
    A("")

    # recommendation
    rec = summary["recommendation"]
    A("## Recommendation")
    A("")
    if rec.get("recommended"):
        A(f"- Recommended: **{rec['recommended']}**" + (f" (tie within {sc.TIE_POINTS:.0f} points with "
                                                          f"{', '.join(x for x in rec['tied_names'] if x != rec['recommended'])}; the faster wins)"
                                                          if rec.get("tied") else ""))
        A(f"- Runner-up: **{rec.get('runner_up')}**")
        A("")
        A("Exact commands for the USER to run (nothing here was executed):")
        A("")
        A(deploy_commands(rec["recommended"]))
    else:
        A(f"- {rec.get('note')}. Best score among gate failures: {rec.get('best_by_score_among_gate_failures')}.")
    A("")
    A("### Disabling thinking (not executed)")
    A("")
    A("- Per request: `\"think\": false` in the /api/chat body. opencode may not send it, so the deploy path needs one of the following.")
    A("- qwen3 variants have an explicit Modelfile TEMPLATE whose generation prompt ends with an open `<think>` block; a "
      "derived Modelfile (`FROM qwen3-4b-16k`, a changed `TEMPLATE` that closes the think block, `PARAMETER num_ctx 16384`) is "
      "the Modelfile route. This has NOT been tried here.")
    A("- gemma4 and nemotron-3-nano use Ollama's built-in `RENDERER`/`PARSER` with `TEMPLATE {{ .Prompt }}`: thinking is "
      "switched only by the API `think` field, so there is no Modelfile-only switch; a thin proxy or an opencode "
      "provider option that adds `think:false` is needed.")
    A("")
    A("## Caveats")
    A("")
    A("- Single run per setting; temperature 0 repetitions are near-identical, so the tool-suite repetitions mostly "
      "measure nondeterminism of the server, not sampling variance. 10 agent tasks per setting is a small sample: one "
      "task is 10 points of agent success.")
    A("- The other agent's sbx microVMs and the desktop share the CPU, RAM and GPU; see the noise table.")
    A("- `thinking_chars` is the length of Ollama's `thinking` field; tokens are estimated at 4 characters each.")
    A("")
    return "\n".join(L)


def write_report(run_dir: Path, summary: dict[str, Any], repo: Path) -> Path:
    git = git_info(repo)
    summary["meta"]["git"] = git
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    p = run_dir / "report.md"
    p.write_text(render(summary, git))
    return p
