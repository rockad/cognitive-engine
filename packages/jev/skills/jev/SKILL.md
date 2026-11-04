---
name: jev
description: Switch or inspect TypeSafe Jev operating modes (off, on-demand, on) directly from chat or with slash command /jev.
when_to_use: Switch Jev operational mode (off, on-demand, on), query Jev quota/status, calibrate task execution tier via jev_dispatch, or manage free-tier coprocessor offloading.
argument-hint: "[status | on | on-demand | off]"
metadata:
  author: aleksandr
  requires: jev-mcp
---

# TypeSafe Jev Mode Controller

Use this skill when the user or agent needs to inspect or switch TypeSafe Jev operating modes (`off`, `on-demand`, `on`) in Antigravity CLI or Claude Code.

## Operating Modes

| Mode | State | Description | Behavior |
| --- | --- | --- | --- |
| **`off`** | MCP Server Disabled | Zero token overhead, zero OpenRouter calls. | Antigravity runs strictly with built-in native tools. Background decisions, pre-flight routing, and free-tier offloading are completely disabled. |
| **`on-demand`** | MCP Server Enabled | Passive System 1 coprocessor. | MCP server remains active. Invoked selectively by user or agent for complex multi-role workflows (`jev_dispatch`), condition checks (`jev_judge`), or ad-hoc free tier generation (`jev_execute_free`). No mandatory pre-flight clamping. |
| **`on`** | MCP Server Enabled | Aggressive System 1 enforcement. | Active coprocessor with mandatory execution guards: (1) **Pre-flight dispatch** via `jev_dispatch` on non-trivial tasks to calibrate subagent role and tier; (2) **Free-tier offloading** to `openrouter/free` via `jev_execute_free`; (3) **Automated quality gates** via `jev_judge`; (4) **Subagent tier clamping** to flash/flash_lite or free tier to conserve Pro quota. |

---

## Instructions for Agent & User

### 1. View Current Status

To inspect the active operating mode, configuration state, and mode characteristics:

- **Via MCP Tool (Chat Interface)**:
  Call `jev_get_mode` (or `call_mcp_tool` with `ServerName: "jev_jev"` or `"jev"`, `ToolName: "jev_get_mode"`, `Arguments: {}`).
- **Via Configuration**:
  Inspect `~/.config/jev/config.json`.

### 2. Switch Operating Modes

To switch modes directly:

#### Switching to `on` (Aggressive System 1):
- **Via Skill / Command**:
  Update `defaults.mode: "on"` in `~/.config/jev/config.json`. In Antigravity, run `agy mcp enable jev` via `run_command` if disabled.
- **Via MCP Tool (When enabled)**:
  Call `jev_set_mode` with argument `{"mode": "on"}`.

#### Switching to `on-demand` (Passive Coprocessor):
- **Via Skill / Command**:
  Update `defaults.mode: "on-demand"` in `~/.config/jev/config.json`. In Antigravity, run `agy mcp enable jev` via `run_command` if disabled.
- **Via MCP Tool**:
  Call `jev_set_mode` with argument `{"mode": "on-demand"}`.

#### Switching to `off` (Zero Overhead):
- **Via Skill / Command**:
  Update `defaults.mode: "off"` in `~/.config/jev/config.json`. In Antigravity, run `agy mcp disable jev` via `run_command`.
- **Via MCP Tool**:
  Call `jev_set_mode` with argument `{"mode": "off"}`.

### 3. Model Tier Selection & Pro Tier User Approval Guardrail

Pro tier models (`pro`) are **never invoked automatically**. They are strictly gated behind manual user approval to protect the user's Gemini Pro quota:
- If `jev_dispatch` recommends `pro` (`requiresUserApproval: true`), the agent **must not** spawn a subagent on `pro` automatically.
- The agent must solicit explicit user approval before spawning any agent on `pro`, providing the reasoning returned by dispatch.
- If the user declines or does not explicitly approve `pro`, spawn the designated subagent on `flash` (or `flash_lite` / `openrouter_free`). The agent **must never bypass the subagent and do the task itself sequentially**.
- Lifecycle pre-tool hooks will actively intercept and block (`force_ask`) any `invoke_subagent` call requesting `Model: "pro"`.

### 4. In Claude Code

- Tiers map onto Claude models: `openrouter_free` and `flash_lite` → `haiku`, `flash` → `sonnet`, `pro` → `opus`.
- The session model is the ceiling. In mode `on`, a `PreToolUse` hook asks the user before an `Agent` call runs on a stronger model, and before a delegation Jev rates `pro`. Treat a denial as "run it on the session model or cheaper", not as "do it yourself".
- A `[JEV]` line added to the turn carries the recommended subagent, model and effort. It is advice: follow it unless the task clearly needs more, and say so when you deviate.
- Mode state is read directly from `~/.config/jev/config.json`.

---

## Slash Command Usage

When invoked via `/jev` or `/jev <mode>`:
- `/jev` or `/jev status`: Inspect current mode from `~/.config/jev/config.json`.
- `/jev on`: Sets mode to `on` in `~/.config/jev/config.json` and ensures MCP server is enabled (`agy mcp enable jev`).
- `/jev on-demand`: Sets mode to `on-demand` in `~/.config/jev/config.json` and ensures MCP server is enabled (`agy mcp enable jev`).
- `/jev off`: Sets mode to `off` in `~/.config/jev/config.json` and disables MCP server (`agy mcp disable jev`) for zero overhead.

## Underlying State & Files

- **Config File**: `~/.config/jev/config.json` (canonical configuration store, defaults to `mode: "off"`)
- **Telemetry Log**: `~/.gemini/antigravity-cli/jev_telemetry.jsonl`

---

## Operational Rules (when `jev on`)

The following rules apply when `mode == "on"` (Aggressive System 1). They are enforced by lifecycle hooks and must be followed by any agent running in this mode.

### 1. Pre-Flight Dispatch (`jev_dispatch`)
- Before executing non-trivial tasks, refactors, or spawning subagents, call `jev_dispatch` to calibrate the task.
- Jev returns probability distributions over candidate subagents and model tiers (`openrouter_free`, `flash_lite`, `flash`, `pro`).
- If `recommendedTier` is `openrouter_free`, execute immediately via `jev_execute_free` — do not spawn a paid subagent.
- If a Gemini tier is recommended, spawn the subagent with the designated tier (subject to the Pro Tier rule in the main Skill section above).

### 1b. Post-Approval Execution Transition Gate (`jev_batch_dispatch`)
- Upon plan approval or completion of `/grill-me` (`clarify`), the main orchestrator MUST immediately call `jev_batch_dispatch` and `manage_subagents(Action: "list")` to decompose and delegate independent workstreams in parallel rather than editing multiple files sequentially in the main context.
- `PreToolUse` (`jev-pre-tool.sh`) checks `jev_telemetry.jsonl` and blocks consecutive direct edits (`>=2` files) in the session window if no recent `jev_dispatch`, `jev_batch_dispatch`, or subagent delegation occurred.

### 2. Free-Tier Offloading (`jev_execute_free`)
- **Main agent**: Offload boilerplate scaffolding, markdown formatting, log distillations, and draft summaries to `openrouter/free` at **$0.00** cost and **zero Gemini quota**.
- **Subagents**: Subagents performing bulk transformations, formatting, or log distillations must call `jev_execute_free` directly rather than consuming subscription quota on long text outputs.
- **Concurrency cap**: Max **10 concurrent tasks** targeting `openrouter_free` simultaneously (OpenRouter's 20 RPM global limit).

### 3. 429 / Quota-Exhausted Fallback
- If a subagent returns `RESOURCE_EXHAUSTED` (HTTP 429 / "Individual quota reached"):
  1. **Do NOT fall back to doing the work in the main agent context** — that wastes Pro quota on the exact task being delegated.
  2. **Retry on free tier** via `jev_agent_free` (backend `auto`, model `openrouter/free`).
  3. **Gate the result** with `jev_judge` before committing or presenting to the user.
  4. Only fall back to main context if `jev_agent_free` also fails (network/timeout), and say so explicitly.

### 4. Quality Gates (`jev_judge`)
- Run `jev_judge` after major file edits or refactors before presenting results to the user.
- If `passed: false`, self-correct before presenting.
- Pre-commit: never commit diffs containing merge conflict markers (`<<<<<<<`, `=======`).

### 5. Rubric Scoring (`jev_score`)
- Use `jev_score` for multi-criteria evaluation (CV-to-job matching, code readability rubrics) without generating verbose LLM essays.

---

## Autonomous Free-Tier Agent (`jev_agent_free`)

For multi-step coding or workspace tasks that would consume significant Gemini quota:
- Executes autonomous agent loops on free models at **$0.00 cost**.
- **Backends**:
  - `opencode` — Full workspace agent via OpenCode CLI, with file read/write and command execution.
  - `openrouter` — Direct OpenRouter Chat Completions API (single-turn).
  - `auto` (default) — OpenCode with automatic fallback to OpenRouter on failure.
- **Primary model**: `openrouter/free` (200k context, supports tool calls). Fallback pool: `google/gemma-4-31b-it:free`, `cohere/north-mini-code:free`, `nvidia/nemotron-3.5-lightning:free`.
- Returns: completed content, backend used, model used, tokens saved, cost ($0.00), latency.

---

## Proactive Quota Guard

When Gemini quota remaining falls below 20%:
- Route ALL complex generation, boilerplate edits, and code transformations to free models via `jev_execute_free` or `jev_agent_free`.
- The main agent acts as orchestrator only — consuming trivial input tokens while free models handle bulk output.
- This guard fires independently of Jev mode and protects quota even when `jev off`.

---

## Observability

- **OpenRouter Dashboard**: Real-time token usage, latencies, and costs.
- **Local telemetry**: Execution events appended to `~/.gemini/antigravity-cli/jev_telemetry.jsonl`.
