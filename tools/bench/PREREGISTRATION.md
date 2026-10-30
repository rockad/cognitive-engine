# Pre-registration: Ollama agentic model benchmark

This file and `ollama_bench/scoring.py` are committed BEFORE any benchmark result exists. The scoring rules below
are not changed after results are seen. `ollama_bench/scoring.py` is the executable form; a unit test pins the
numbers in both places to each other.

## Question

Which of `qwen3-4b-16k`, `gemma4:e4b`, `nemotron-3-nano:4b` is the best local model for agentic, tool-calling use
(opencode-style: a system prompt, six tool definitions, multi-turn tool use) on an RTX 3070 Ti 8 GB (desktop
using 2-4 GB), 16k context. The answer picks the model for stage 1 of the Harness2 smoke run.

## Identical conditions for every model

- Native `/api/chat`, `stream:false`, `options.num_ctx = 16384`, `keep_alive` 10m during a batch, one model
  resident at a time (unload with `keep_alive:0` and confirm via `/api/ps` between models).
- Main pass: `temperature 0`, `seed 42 + repetition index`. Robustness pass: `temperature 0.6`, `top_p 0.95`,
  `seed 1000 + repetition index`. Other sampling parameters stay at each model's Modelfile defaults.
- Thinking: `think` omitted (the model's default) for the main and robustness passes; `think:false` for the
  separate no-thinking variant of the tool suite and of the agent tasks.
- Response caps (identical for all): `num_predict` 2048 for a tool-suite call, 4096 for a mini-agent step, 160 for
  a performance probe. A response that stops with `done_reason = length` counts as a truncation failure.
- Per-request timeout 240 s. A request error or timeout is an "error"; three errors in a row mark the model
  failed and its remaining phases are skipped (recorded).
- Models run in a randomised order, recorded in the results. Each model is warmed by its first cold load before
  any timing. Load average and available RAM are recorded before each model; if load average (1 min) > 4 or
  available RAM < 3 GB, wait up to 5 minutes (re-checking every 15 s) and record the wait.

## Phases per model (in this order)

1. A. Performance: 3 cold loads (unload, confirm, load), `/api/ps` split at 16k, then 3 timed runs each at ~1k and
   ~4k prompt tokens (a fresh nonce at the start of every prompt defeats the prompt cache). nvidia-smi is sampled
   every second for the whole batch; the GPU baseline is read before the first load.
2. B. Tool suite, thinking default, temperature 0 (>= 24 cases x 2 repetitions).
3. B'. Tool suite, thinking default, temperature 0.6 / top_p 0.95 (robustness).
4. C. Mini-agent tasks, thinking default, temperature 0 (6 `sum-signed` instances + 4 `edit-prompt` tasks).
5. B''. Tool suite, `think:false`, temperature 0.
6. C'. Mini-agent tasks, `think:false`, temperature 0.

Wall-clock budget for the whole run: 60 minutes (55 for models, 5 reserved for the report). Each model gets an
equal share of the time left when it starts. Phase deadlines are cumulative shares of the model's budget
(A 10%, B 35%, B' 55%, C 80%, B'' 88%, C' 100%), so unused time rolls forward. Items not started by a phase
deadline are recorded as `skipped_budget` and excluded from that phase's pass rate; the report flags the model as
incomplete. Tool-suite items run repetition 1 of every case before repetition 2; agent tasks are interleaved
(sum, edit, sum, edit, ...), so a truncated phase is still representative.

## Scoring (pre-registered)

Components, each in [0, 1]:

- `A` mini-agent success: tasks passed / tasks attempted (thinking default, temperature 0, phase C).
- `T` tool-suite pass rate: cases passed / cases attempted (thinking default, temperature 0, phase B).
- `S` speed: `fastest_median / own_median`, where `median` is the median wall time over all attempted phase-C tasks
  of that model (failed and timed-out tasks count at their wall time) and `fastest_median` is the smallest such
  median among models that were not load failures. `S` is forced to 0 when `A == 0` (a model that fails every
  task quickly must not earn speed points).
- `R` robustness: `min(1, T(t=0.6) / T(t=0))`; 0 when `T(t=0) == 0`.

`Score = 100 * (0.50*A + 0.25*T + 0.15*S + 0.10*R)`.

### Hard gates (a model failing any gate is not eligible to be "optimal"; it is still scored and shown)

- `load_failure`: the model failed to load or answer its first request.
- `crash`: three errors or timeouts in a row.
- `gpu_split`: `/api/ps` at 16k shows `size_vram < size`, i.e. anything less than 100% on the GPU.

### Ties and the recommendation

- Among eligible models, the top score defines the tie band: every eligible model within 3.0 points of the top
  is tied. Among tied models the faster one (lower phase-C median wall time) is recommended. The runner-up is
  chosen from the remaining models by the same rule.
- If no model is eligible, the report says so and shows the best score among the gate failures; no model is
  called optimal.

### The think:false finding (reported separately, never part of `Score`)

For each model the report shows `A`, `T` and median task wall time with `think:false` next to the default.
"Disable thinking" is recommended for a model when `A(think:false) >= A(default)` AND
`T(think:false) >= T(default) - 0.05`. Otherwise the recommendation is to leave thinking on. Deploy options are
described in the report, not executed.

## Mini-agent tasks

Host-side loop: max 12 steps, 180 s per task, tools `read_file`, `write_file`, `edit_file`, `list_dir` only
(no bash), executed against a throwaway temp directory; absolute paths, `..` and symlink escapes are refused.
The loop stops early when the same tool call (name and arguments) is issued a third time (`loops`). Thinking
text is not sent back in later turns.

Failure categories: `timeout`, `no_tool_call`, `malformed_args`, `wrong_file`, `wrong_arithmetic`, `bad_format`,
`edit_violation`, `loops`, `gave_up`, `max_steps`, `truncation`, `context`, `error`.

`sum-signed` uses the instances and the pure-Python verdict of `cognitive_harness2.sum_signed` (pass = all three
criteria). `edit-prompt` passes when the new line is present exactly once, in the right place, every other line
is unchanged byte for byte, and no frontmatter appeared.
