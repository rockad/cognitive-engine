# Changelog

## 2.4.0

The Claude Code `UserPromptSubmit` advisor, ported from `agentic-hub/plugins/jev` with the improvements from its 2026-11-09 case study (model tier for a multi-stream workflow). The numbers in brackets are the case study's section 5 items. Details are in the README, "Prompt advisor hook (Claude Code)".

### Added

- `UserPromptSubmit` advisor (`src/advisor-hook.ts`, `src/advisor-logic.ts`), active in Jev mode `on`, advice only: never asks, denies or blocks, prints nothing on any error or after a 1.5 s budget.
- Explicit instructions in the prompt are honoured, imperatives only: "use opus", "run an opus subagent", "with sonnet", "minimal viable", "cheapest" (5.1). A negator within two words before the name ("do not use opus", "not opus", "instead of opus", "without opus") cancels it, the effect moves the tier by at most one step from the router's, and only the first 1500 characters are scanned.
- One ceiling sentence on the pro tier instead of two that contradicted each other (5.2).
- Stage rubric (`src/stage-rubric.ts`): mechanical edit gives haiku, settled spec or plan edit gives sonnet, expensive judgement gives opus (5.3).
- Silence for task notifications, other host-injected turns, giant pastes and status turns; prompts trimmed to 2000 characters before Jev sees them (5.5).
- Precedence order (instruction, then the Jev guard, then the standing rule), and the advisory line names the rule applied (5.9).
- Second `PreToolUse` entry for `Agent|Task|Workflow` (`advisor-hook.ts pre-tool-use`): logs the requested model next to the session's last recommendation and adds one pre-flight reminder for staged work with no recent dispatch call; never blocks (5.10).
- `jev_batch_dispatch` takes optional `kind` and `needsEdit` per task, honoured on Claude Code only (5.4).
- Advisor reminder to use `jev_batch_dispatch` when the prompt names a workflow or workstreams (5.4).
- Edit-capable personas (agentic-hub 2.3.1): `taskNeedsEdit` and `editCapableSubagent` in `src/core/heuristics.ts`. On Claude Code, `parseDispatch`, `jev_dispatch` and `jev_batch_dispatch` swap the read-only `Plan` and `Explore` for `general-purpose` on an editing task; the caller's `needsEdit` wins over the wording. The verb list is narrow on purpose (edit, implement, fix, refactor, write, add, update, rename, migrate, delete, apply; a negated verb does not count; not review, check, find, explain).
- Turn marker (`<session_id>.turn.json`: turn id, Jev mode, intent, no prompt text), written at the start of every real user turn and removed by a turn that runs with Jev off. The `PreToolUse` hook discards the advice and the intent when the mode differs from the stored one or the advice belongs to another turn.
- `tests/advisor-telemetry-fields.test.ts` pins the telemetry and decisions-log field names.
- The model-tier case study, moved from `agentic-hub` to `docs/case-studies/`, with a note mapping each finding to its fix here.

### Changed

- Advice records carry `session_id`, `turn` (prompt hash), `tier` and `recommended_model`; the telemetry field `modelRecommended` replaces `modelUsed` for this hook, which stays as a deprecated alias of the same value until 2.5 (5.7). The `PreToolUse` record keeps `modelUsed` for the requested model and adds `modelRecommended`; its decisions-log entry has `turn`, `tier`, `recommended_model`, `requested_model`, `user_intent` and `preflight_reminder` (was `reminded`; the reader accepts both).
- The free-tier keyword rule is whole-word everywhere (4.11), ported from agentic-hub 2.3.1 as `isFreeTierMechanicalTask`: `scaffold*`, `boilerplate`, `formatting`, `reformat*`, `auto-format` and `format <the|this|these|those|all|every|each|my|our|code|file(s)|json|yaml|markdown>`; the bare word "format" no longer counts. It replaces the substring rule in the batch tools of every harness, the local pre-filter (`evaluateLocalHeuristic`), `detectScaffoldingTask` and `defaultEffortForSubagent` (including its subagent-name test: `information-desk` is no longer a formatter). Antigravity, OpenCode and the CLI therefore route "information", "transformation" and a noun "dump format" to flash instead of the free tier, on purpose.
- On Claude Code only: `jev_dispatch` and `jev_batch_dispatch` speak `Agent(subagent_type, model)` and `agent()`, report the Claude session model in `activeSessionModel`, flag opus above the session model instead of clamping it, use whole-word keywords offline, and offer the router real subagent types (5.6, 5.11). Antigravity, OpenCode and the CLI keep their wording, fields and personas from 2.3.x; the only change there is the whole-word free-tier rule below.
- Dispatch telemetry records `harness: "claude-code"` on Claude Code; other harnesses keep `antigravity`.
- The Claude bundle's `.mcp.json` sets `JEV_HARNESS=claude-code`.
- `JEV_VERSION`, `packages/jev`, and the Claude plugin bundle share one version.

### Not changed

- The `PreToolUse` guard from #4 and #18 (Agent, Task and Workflow) is untouched.
- Workflow `agent()` calls cannot be intercepted by any hook (5.8); documented, with a TODO, in the README.
- The case study's 7.1 `review` kind, and an advisor port of the agentic-hub `PreToolUse` ask (the guard does that) were left out on purpose.
- Read-only personas are swapped only on Claude Code; agentic-hub 2.3.1 also swaps to `self` on other harnesses, which would change their output.
