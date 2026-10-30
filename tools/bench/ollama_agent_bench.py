#!/usr/bin/env python3
"""Run the local Ollama agentic benchmark (see tools/bench/README.md and PREREGISTRATION.md).

    uv run python tools/bench/ollama_agent_bench.py [--models a b c] [--out-root DIR] [--budget-s 3600]

Talks only to Ollama's HTTP API on 127.0.0.1:11434; never pulls, creates, copies or removes a model.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ollama_bench import scoring as sc  # noqa: E402
from ollama_bench.client import DEFAULT_HOST, Ollama  # noqa: E402
from ollama_bench.report import write_report  # noqa: E402
from ollama_bench.runner import DEFAULT_MODELS, Runner  # noqa: E402

DEFAULT_OUT = Path.home() / ".local/state/cognitive-engine/model-bench"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", nargs="+", default=DEFAULT_MODELS)
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--out-root", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--budget-s", type=float, default=sc.TOTAL_BUDGET_S)
    ap.add_argument("--reps", type=int, default=2, help="tool-suite repetitions per case and setting")
    ap.add_argument("--seed", type=int, default=None, help="model-order seed (recorded)")
    ap.add_argument("--phases", nargs="+", default=list(sc.PHASE_ORDER), choices=list(sc.PHASE_ORDER))
    ap.add_argument("--limit-cases", type=int, default=None, help="pilot: only the first N tool-suite cases")
    ap.add_argument("--limit-tasks", type=int, default=None, help="pilot: only the first N agent tasks")
    args = ap.parse_args(argv)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = args.out_root / stamp
    run_dir.mkdir(parents=True, exist_ok=False)
    prereg = Path(__file__).resolve().parent / "PREREGISTRATION.md"
    shutil.copyfile(prereg, run_dir / "PREREGISTRATION.md")      # in place before any result is written
    runner = Runner(Ollama(args.host), run_dir, models=args.models, budget_s=args.budget_s, reps=args.reps,
                    seed=args.seed, phases=tuple(args.phases), limit_cases=args.limit_cases,
                    limit_tasks=args.limit_tasks)
    summary = runner.run()
    repo = Path(__file__).resolve().parents[2]
    report = write_report(run_dir, summary, repo)
    print(f"report: {report}")
    rec = summary["recommendation"]
    print(f"recommended: {rec.get('recommended')}  runner-up: {rec.get('runner_up')}  tied: {rec.get('tied')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
