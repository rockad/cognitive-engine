# Changelog

## 2.4.0

The Claude Code `UserPromptSubmit` advisor, ported from `agentic-hub/plugins/jev` with the improvements from its 2026-11-09 case study (model tier for a multi-stream workflow). The numbers in brackets are the case study's section 5 items. Details are in the README, "Prompt advisor hook (Claude Code)".

### Added

- `UserPromptSubmit` advisor (`src/advisor-hook.ts`, `src/advisor-logic.ts`), active in Jev mode `on`, advice only: never asks, denies or blocks, prints nothing on any error or after a 1.5 s budget.
- Explicit instructions in the prompt are honoured: "use opus", "run an opus subagent", "minimal viable model", "cheapest" (5.1).
- One ceiling sentence on the pro tier instead of two that contradicted each other (5.2).
- Stage rubric (`src/stage-rubric.ts`): mechanical edit gives haiku, settled spec or plan edit gives sonnet, expensive judgement gives opus (5.3).
- Silence for task notifications, other host-injected turns, giant pastes and status turns; prompts trimmed to 2000 characters before Jev sees them (5.5).
- Precedence order (instruction, then the Jev guard, then the standing rule), and the advisory line names the rule applied (5.9).
- Second `PreToolUse` entry for `Agent|Task|Workflow` (`advisor-hook.ts pre-tool-use`): logs the requested model next to the session's last recommendation and adds one pre-flight reminder for staged work with no recent dispatch call; never blocks (5.10).
- `jev_batch_dispatch` takes optional `kind` and `needsEdit` per task, honoured on Claude Code only (5.4).
- Advisor reminder to use `jev_batch_dispatch` when the prompt names a workflow or workstreams (5.4).

### Changed

- Advice records carry `session_id`, `turn` (prompt hash), `tier` and `recommended_model`; the telemetry field `modelRecommended` replaces `modelUsed` for this hook, which stays as a deprecated alias of the same value until 2.5 (5.7).
- On Claude Code only: `jev_dispatch` and `jev_batch_dispatch` speak `Agent(subagent_type, model)` and `agent()`, report the Claude session model in `activeSessionModel`, flag opus above the session model instead of clamping it, use whole-word keywords offline, and offer the router real subagent types (5.6, 5.11). Antigravity, OpenCode and the CLI are byte-identical to 2.3.x.
- Dispatch telemetry records `harness: "claude-code"` on Claude Code; other harnesses keep `antigravity`.
- The Claude bundle's `.mcp.json` sets `JEV_HARNESS=claude-code`.
- `JEV_VERSION`, `packages/jev`, and the Claude plugin bundle share one version.

### Not changed

- The `PreToolUse` guard from #4 and #18 (Agent, Task and Workflow) is untouched.
- Workflow `agent()` calls cannot be intercepted by any hook (5.8); documented, with a TODO, in the README.
- The substring tier rule in the non-Claude harnesses' batch tool (`"information"` contains `"format"`), the case study's 7.1 `review` kind, and an advisor port of the agentic-hub `PreToolUse` ask (the guard does that) were left out on purpose.
