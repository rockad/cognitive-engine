# Ollama agentic benchmark

Measures local Ollama models for opencode-style tool use on this machine: performance, a tool-call suite and
mini-agent tasks, then scores them with rules that were committed **before** any result
(`PREREGISTRATION.md`, `ollama_bench/scoring.py`).

It talks only to Ollama's HTTP API (`/api/tags`, `/api/ps`, `/api/chat`, `/api/generate` with `keep_alive: 0`).
It never pulls, creates, copies or removes a model, never touches Ollama's configuration and never starts
a container or opencode.

## Run

```sh
uv run python tools/bench/ollama_agent_bench.py                       # the three default models, 60 min budget
uv run python tools/bench/ollama_agent_bench.py --models qwen3-4b-16k --phases perf tools_t0 --limit-cases 4   # pilot
```

Output goes to `~/.local/state/cognitive-engine/model-bench/<UTC timestamp>/`:

- `results.jsonl`: every raw response (`kind: chat`), every tool-suite case, every agent task with its steps
- `summary.json`: per-model metrics, scores, recommendation
- `report.md`: the pre-registered rules first, then the tables and the recommendation
- `PREREGISTRATION.md`: the copy in force when the run started

## Test (no Ollama needed)

```sh
uv run pytest -q tools/bench
```

The tests cover the scorers (every tool-suite case has a hand-written perfect answer and failing variants), the
sandboxed executor including path-escape refusals (absolute, `..`, NUL, symlink), the agent loop, and the runner
end to end against a fake Ollama server (`tests/fake_ollama.py`).

## Layout

- `ollama_bench/scoring.py`: weights, gates, tie rule, thinking rule, budgets (pre-registered)
- `ollama_bench/cases.py`: the 27 tool-suite cases and their deterministic checks
- `ollama_bench/tools.py`: the six tool definitions and the argument validator
- `ollama_bench/sandbox.py`: the host-side executor (no bash; confined to one temp directory)
- `ollama_bench/agent.py`: `sum-signed` (via `cognitive_harness2.sum_signed`) and `edit-prompt` tasks, the loop
- `ollama_bench/perf.py`: the ~1k and ~4k opencode-like prompts
- `ollama_bench/runner.py`, `report.py`, `client.py`, `system.py`: orchestration, report, HTTP, host noise and GPU
- `ollama_agent_bench.py`: the entry point
