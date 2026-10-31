# TypeSafe Jev: Rules & Guidelines

This document governs System 1 decision-making, model tier governance, thinking effort, and quota preservation for AI agents using the `jev` plugin.

## 1. Operating Modes

- **`off`**: MCP disabled (`agy mcp disable jev`), zero token overhead, zero OpenRouter calls.
- **`on-demand`**: MCP enabled. Passive System 1 coprocessor for multi-role workflows and ad-hoc routing.
- **`on`**: Aggressive System 1 enforcement: mandatory pre-flight dispatch, free-tier offloading, thinking effort governance, and automated quality gates.
- **Primary Switch Mechanism**: Managed directly via the in-chat `/jev` slash command / skill (`/jev on`, `/jev on-demand`, `/jev off`, `/jev status`) and the `jev_set_mode` / `jev_get_mode` MCP tools. The legacy shell script (`scripts/jev-mode`) is retired. `~/.config/jev/config.json` is the single canonical configuration store for global defaults (`defaults.mode`) and per-harness overrides (`harnesses.<harness>.mode`). Drift between `config.json` and `agy mcp` is automatically healed during pre-flight.

## 2. Pre-Flight Dispatch & Routing

- **Pre-Flight Dispatch (`jev_dispatch`)**:
  - Run before executing non-trivial tasks, refactors, or spawning subagents.
  - Evaluates calibrated probability distributions over candidate subagents and execution tiers (`openrouter_free`, `flash_lite`, `flash`, `pro`).
  - If `recommendedTier` is `openrouter_free`, execute immediately via `jev_execute_free` rather than spawning a paid subagent.
- **Post-Approval Execution Transition Gate (`jev_batch_dispatch`)**:
  - Upon plan approval or completion of `/grill-me` (`clarify`), the main orchestrator MUST immediately call `jev_batch_dispatch` and `manage_subagents(Action: "list")` to decompose and delegate independent workstreams in parallel rather than editing multiple files sequentially in the main context.
  - Enforced by `PreToolUse` (`jev-pre-tool.sh`), which checks `jev_telemetry.jsonl` and blocks consecutive direct edits (>3 files) in the session window if no recent dispatch (`jev_dispatch` / `jev_batch_dispatch`) or subagent delegation occurred.
- **Fast Option Routing (`jev_route`)**:
  - Use when a discrete choice between 2–8 options needs a fast calibrated probabilistic verdict (~800ms) via OpenRouter Decisions API.

## 3. Free-Tier Offloading & Quota Preservation

- **Zero-Cost Offloading (`jev_execute_free`)**:
  - Offload mechanical work—boilerplate scaffolding, markdown formatting, text transformations, log distillations, and draft summaries—to `openrouter/free` at **$0.00 cost** and **zero Gemini quota**.
  - Subagents performing bulk transformations or formatting must call `jev_execute_free` directly.
- **Concurrency Cap**:
  - Cap concurrent tasks targeting `openrouter_free` at **maximum 10 simultaneous instances** to respect OpenRouter rate limits.
- **Quota Exhaustion & 429 Fallback**:
  - If a subagent returns `RESOURCE_EXHAUSTED` (HTTP 429), never fall back to generating the task in the main agent context. Retry on the free tier via `jev_agent_free` or `jev_execute_free`.

## 4. Session Model Alignment & Thinking Effort Governance

- **Session Model Ceiling**:
  - The model running in the main agent session defines the absolute upper-bound model tier and version ceiling for all delegated subagents and tasks.
- **Subagent Tier Clamping**:
  - Subagents default to `Model: "inherit"` or lower (`flash`, `flash_lite`, `openrouter_free`).
- **Low-Quota Protocol (<20% Remaining)**:
  - When `gemini-weekly` or `gemini-5h` is under 20% remaining (as reported in `quota-state.json` or `jev_quota`):
    - All new subagents MUST be clamped strictly to `Model: "flash_lite"` (the lowest token-weight model available in Antigravity).
    - Pro tier is strictly prohibited.
    - All non-essential text formatting, markdown transformations, and scaffolding boilerplate must be offloaded to `jev_execute_free` or `jev_agent_free` ($0.00 cost, 0 Gemini quota).
    - When `jev off` and quota is `<20%`, pre-flight and pre-invocation hooks inject a native-only warning clamping subagents to `Model: "flash_lite"` without referencing disabled `jev_*` MCP tools, and advise running `jev on` to re-enable free-tier offloading.
- **Subagent Lifecycle & Pro Reuse Prohibition**:
  - Before invoking any new subagent, inspect active and idle workers with `manage_subagents(Action: "list")` and reuse idle workers via `send_message`.
  - **Never reuse an idle subagent originally spawned on `Model: "pro"`** during quota-conservation or routine tasks, as messaging it continues consuming Pro quota. Only reuse workers originally spawned on `flash` or `flash_lite`.
- **Execution Parameters vs. Dispatch Recommendations**:
  - Distinguish pre-flight `jev_dispatch` recommendations from actual execution parameters.
  - Native `invoke_subagent` runs exclusively on Gemini (`inherit`, `flash_lite`, `flash`, `pro`). Autonomous free-tier workspace tasks execute via `jev_agent_free` or `jev_execute_free` (`openrouter/free`).
- **Pro Tier Manual Approval Workflow**:
  - Pro tier models (`pro`, `opus`) must NEVER be used automatically.
  - If `jev_dispatch` recommends `pro`, prompt the user for explicit approval. If declined, execute on `flash` or cheaper.
  - `jev_dispatch` and its tier clamp are advice from the agent's own tools. Nothing the agent passes to them counts as approval. The only approval is the user answering the PreToolUse guard's prompt (Claude Code and Antigravity).
- **Thinking Effort**:
  - Match reasoning effort to task complexity. High thinking effort should be reserved for ambiguous architectural decisions, while routine transformations run with minimal or zero thinking overhead.

## 5. Quality Gates (`jev_judge`)

- Gate non-trivial diffs, code refactors, and test outcomes using `jev_judge`.
- Pre-commit: reject diffs containing merge conflict markers or unresolved lint failures.
