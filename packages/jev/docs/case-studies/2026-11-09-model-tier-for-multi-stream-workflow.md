# Case study: choosing models for a multi-stream docs workflow (2026-11-09)

> **Moved to `cognitive-engine` (`packages/jev/docs/case-studies/`).** This file was written in `agentic-hub` and moved here with the Jev 2.4.0 advisor port. Every code citation below (`claude/hook.ts:87`, `src/tools/dispatch.ts:388`, ...) points at the frozen `agentic-hub` `plugins/jev` copy at 2.3.0, as read on 2026-11-09, and stays as written because it is the evidence. It does not describe `packages/jev`. Where each finding is fixed now, in `packages/jev` 2.4.0:
>
> | Finding / item | Now in `packages/jev` |
> | --- | --- |
> | 4.11 keyword substring rule (also `core/heuristics.ts`, `core/governance.ts`) | `src/core/heuristics.ts` (`isFreeTierMechanicalTask`, `isFormattingTask`), used by `src/tools/dispatch.ts` (`classifyBatchTier`, `claudeKeywordTier`), `evaluateLocalHeuristic`, `detectScaffoldingTask` and `defaultEffortForSubagent` in `src/core/governance.ts` |
> | 4.13 read-only personas given edit work | `src/core/heuristics.ts` (`taskNeedsEdit`, `editCapableSubagent`), `src/advisor-logic.ts` (`parseDispatch`), `src/tools/dispatch.ts` (`resolveEditCapableSubagent`, `candidatesFor`) |
> | 5.1 explicit model and cost instructions | `src/advisor-logic.ts` (`parseUserIntent`, `applyUserIntent`); turn marker in `src/advisor-state.ts` |
> | 5.2 one ceiling sentence, 5.9 precedence | `src/advisor-logic.ts` (`directiveBody`, `resolveModel`) |
> | 5.3 stage rubric | `src/stage-rubric.ts` |
> | 5.4 stage plans | `src/tools/dispatch.ts` (`kind`, `needsEdit` on `jev_batch_dispatch`), `STAGE_PLAN_REMINDER` in `src/advisor-logic.ts` |
> | 5.5 silence on non-task turns | `src/advisor-logic.ts` (`isNonTaskPrompt`, `isStatusTurn`, `trimTask`), `src/advisor-hook.ts` |
> | 5.6 real subagent types | `src/advisor-logic.ts` (`withoutDisplayNames`, `resolveClaudeSubagentCriteria`) |
> | 5.7 recommended versus used telemetry | `src/advisor-hook.ts` (`adviceTelemetry`), `src/core/telemetry.ts` |
> | 5.8 workflow `agent()` calls (not interceptable) | `README.md`, "Prompt advisor hook (Claude Code)" |
> | 5.10 pre-flight reminder | `src/advisor-hook.ts` (`onPreToolUse`), `src/advisor-state.ts` |
> | 4.12, 5.11 harness-aware dispatch tools | `src/tools/harness.ts`, `src/claude-active-session.ts`, `src/tools/dispatch.ts` |
>
> Paths in this note are relative to `packages/jev/`. Hooks are registered in `adapters/claude/hooks/hooks.json`. The `agentic-hub` repo keeps only a pointer to this file.

Status: analysis done, outcome section open. Labels used below: **verified** (checked in the transcript, the decisions log or the code), **reported** (stated by the user or taken from the session facts file), **unverified** (plausible, not checked).

Sources:

- Transcript: `~/.claude/projects/-home-rockad-projects-doppelganger/09b30315-6696-4462-a75e-6f14304fd694.jsonl` (cited as `T:<line>`)
- Hook decisions log: `~/.config/jev/claude_decisions.jsonl`
- Code paths cited are relative to `plugins/jev/` in the `agentic-hub` repo at Jev 2.3.0 (see the note at the top for where each fix lives now)

## 1. Summary

- A Claude Code session (model Sonnet 5.5, Jev mode `on`) ended a `/clarify` series with a four-stream proposal: WS1 on Opus, WS2 to WS4 on Sonnet.
- The user then said "go ahead with the workstreams in a workflow, choos minimal viable model for each". The workflow ran WS1 to WS3 on Sonnet and WS4 on Haiku, with no Opus verification pass.
- The question was whether the Opus proposal was Jev's call. It was not (verified): no `jev_*` tool was called in the whole session, and the hook did not run on the turns where the proposal was made.
- The hook is a per-prompt advisor driven by prompt wording. It has no view of a plan, of a stage list, or of an explicit user model instruction, so it cannot take part in this kind of decision.
- Jev mode was `on` (reported), so the rules required `jev_dispatch` or `jev_batch_dispatch` before the workstreams and `jev_judge` on the diff. None ran. A batch dispatch run afterwards recommended sonnet for all four streams, which differs from both the proposal and the final run.
- The findings in section 4 and the small changes in section 5 aim to make the Claude Code adapter useful for staged work and consistent with the user's standing model rule.

## 2. Timeline

### 2.1 Turns in scope (all verified from `T` and the decisions log; times UTC, 2026-11-09)

| Time | User turn (short) | Hook recommendation (subagent, tier, model, effort) | Assistant action |
| --- | --- | --- | --- |
| 19:14 | "run an opus subagent to wrap findings, then commit and push" | `Code Engineer`, flash, sonnet, medium; ceiling sentence, "Delegate to custom persona" | Said it was spawning Opus because the user asked explicitly; `Agent(general-purpose, opus)` at 19:15 |
| 19:15 | (tool call, no prompt) | PreToolUse guard: `ask`, "asks for model opus, stronger than the session model" (`T:2124`) | Agent launched afterwards (approval path not inspected) |
| 19:21 | task notification from the Opus agent | `general-purpose`, pro, opus, medium; "present the cheaper and the stronger option" | Reviewed the diff, committed and pushed `1cdef37` |
| 19:26 | "fix the stale paths ... go through unknown items with /clarify" | `Vault Librarian`, flash, sonnet, medium | Loaded the `clarify` skill |
| 19:32 | "walk me through the storage question in plain english, eli5 ..." (`T:2370`, `T:2374`) | `general-purpose`, flash, sonnet, medium | Ran forks 1 to 10 with AskUserQuestion; no further hook output |
| 19:33 to 20:00 | Answers to ten forks, delivered as AskUserQuestion results | None: not user prompts, so the hook never ran | Decision table, then the workstream proposal (`T:2556`) |
| 20:01 | "go ahead with the workstreams in a workflow, choos minimal viable model for each" (`T:2570`, `T:2574`) | `Plan`, flash, sonnet, medium | Loaded `workflow-authoring`, launched Workflow `wf_461d006e-32d` at 20:02 |
| 20:04 | "capture last turns where jev suggested ... models" | `knowledge-researcher`, flash, sonnet, medium (`T:2618`, `T:2622`) | Wrote a facts file, started this documentation task |
| 20:05 | "I mean this one: (pasted proposal table)" | `Knowledge Researcher`, pro, opus, medium; "present the cheaper and the stronger option" (`T:2668`, `T:2672`) | Spawned this documentation task on Sonnet (`Agent(general-purpose, sonnet)`) |

The transcript shows each hook line twice (attachment plus rendered reminder), hence the paired line numbers.

### 2.2 The proposal and what ran (reported, from the facts file and the workflow script)

| Stream | Proposed (T:2556) | Ran in workflow |
| --- | --- | --- |
| WS1 bridge plan and spikes | Opus, "because it reverses a recorded decision" | Sonnet; files `consolidated-bridge-v2-plan.md`, `implementation-plan.md` |
| WS2 server API, go-live migration | Sonnet | Sonnet |
| WS3 spikes and research | Sonnet (ran after WS2: shared `spikes.md`) | Sonnet; `spikes.md` plus a new A8c runbook |
| WS4 tasklist, implementation plan | Sonnet | Haiku; `master-tasklist.md` only |

- File ownership was re-cut so no two agents edit one file (reported).
- No highest-tier final verification gate was added; the assistant said it would review the combined diff (reported).
- Zero `jev_*` tool calls in the session, including the clarify close and the workflow launch (verified: no `tool_use` record with a `jev` name in `T`).
- The decisions log has two PreToolUse entries that day (19:15 opus guard, 20:05 sonnet) and none for the workflow's four agents (verified): workflow `agent()` calls did not pass through Jev's PreToolUse hook.

### 2.3 Session-wide distribution (verified)

- 35 hook lines in the session: 18 flash/sonnet, 13 flash_lite/haiku, 4 pro/opus.
- The four opus turns: a side-agent note (11-08 20:51), a pasted agent answer (11-09 09:32, `self`, effort `low`), the Opus agent's completion notification (19:21), and the pasted proposal table (20:05).
- None of the four opus turns was a turn where a judgement was about to be delegated.

### 2.4 Mode and counterfactual batch dispatch (reported by the caller; code causes verified in section 4)

- **Mode.** `jev_get_mode` (claude-code harness) returned `on`, "ON (AGGRESSIVE)": pre-flight everything via `jev_dispatch`, quality gates via `jev_judge`, pro tier gated behind manual approval; model map cheap=haiku, standard=sonnet, deep=opus (reported). Jev was therefore active but not in the loop: only the per-prompt hook text reached the assistant (verified, section 2.2).
- **Counterfactual.** After the fact, `jev_batch_dispatch` was run on abstracted descriptions of the four streams with nine candidate personas (reported):

| Stream | Assistant proposal | Final run | Jev batch (tier / model / effort / persona) |
| --- | --- | --- | --- |
| WS1 | opus | sonnet | flash / sonnet / high / Plan |
| WS2 | sonnet | sonnet | flash / sonnet / medium / code-engineer |
| WS3 | sonnet | sonnet | flash / sonnet / medium / vault-librarian |
| WS4 | sonnet | haiku | flash / sonnet / medium / vault-librarian |

- All four had `requiresUserApproval=false`, `executionTool="invoke_subagent"`, `estimatedCostUsd` 0.001, and `activeSessionModel` "Gemini 3.8 Flash (Medium)" although the session model was Claude Sonnet 5.5 (reported).
- Reading: Jev separated judgement by effort (WS1 high), the assistant by tier (WS1 opus). The final WS4 haiku is below Jev's recommendation and was never offered by Jev.

## 3. Why the judgement moved

Separate the causes; only (a) to (d) below explain the change, and none came from Jev.

- **(a) The user's cost instruction (reported).** "choos minimal viable model for each" is an explicit instruction to minimise cost per stream. It replaced the "Opus as you've asked before for judgement edits" default the assistant had anchored on.
- **(b) The standing rule (reported).** The user-level rule says: default Sonnet, escalate only where a wrong judgement is expensive, ask when unsure, final verification on the top tier. The Opus proposal in WS1 applied the escalation clause to "reverses a recorded decision". Dropping to Sonnet applied the default clause once (c) removed the reason.
- **(c) The decision was already made (reported).** The user had resolved the reversal in the ten forks, so WS1 became "write down a settled decision" and the brief stated it as final. The expensive-to-be-wrong judgement had already happened, with the user, before the workflow existed.
- **(d) A per-file mechanical versus judgement split (reported).** `master-tasklist.md` edits are checklist updates, so Haiku; plan and spec edits with a settled decision are Sonnet. This split came from the assistant's reading of the files, not from any tool.
- **Whose call was what (verified for Jev, reported for the rest).**
  - Opus for WS1 was the assistant's own proposal. Jev's nearest recommendations (19:32 and 20:01) were both flash/sonnet.
  - The final Sonnet, Sonnet, Sonnet, Haiku set came from the user's instruction plus the assistant's rubric. Jev was not consulted for stage tiers: no `jev_dispatch`, `jev_batch_dispatch` or `jev_judge` call exists.
  - The one place Jev spoke before the workflow (20:01, `Plan`/sonnet) happened to agree with the final WS1 to WS3 tier, but it was a reaction to the prompt wording, not to the plan.
- **Dropped.** The facts file says the final verification gate on Opus was skipped. That is a deviation from the standing rule (reported) and is an open question in section 6, not a Jev finding.

## 4. Where the Jev hook fell short

### 4.1 Structurally blind to long phases (verified)

- **Evidence.** No hook output between `T:2374` and `T:2570`, a gap of about 28 minutes covering ten AskUserQuestion forks and the proposal. The proposal and the tier choices were made inside that gap.
- **Cause.** The only advisory hook is `UserPromptSubmit` (`claude/hooks.json:17-23`, `claude/hook.ts:87`). AskUserQuestion answers and Workflow internals are not prompts. The `PreToolUse` hook matches `Agent|Task` only (`claude/hooks.json:26`, `claude/hook.ts:121-122`) and did not fire for workflow agents (decisions log, section 2.2).

### 4.2 Recommendation follows prompt wording, not the plan (verified)

- **Evidence.** 20:01 "go ahead with the workstreams in a workflow" returned `Plan`/flash/sonnet while a four-stream plan with different needs per stream was in context. A pasted table at 20:05 returned pro/opus.
- **Cause.** The only task input sent to Jev is `Task: <prompt>` plus the quota strategy (`claude/hook.ts:187`). The tier question is "Determine the required model tier from cognitive load and complexity" with four generic descriptions (`claude/logic.ts:110-114`, `claude/logic.ts:40-45`). The full prompt is sent untrimmed (`claude/hook.ts:89`), so pasted material dominates. Task notifications also count as prompts: the Opus agent's completion at 19:21 returned `pro/opus`.

### 4.3 Explicit user model choice is ignored (verified in transcript; mechanism verified in code)

- **Evidence.** 19:14 "run an opus subagent": hook said sonnet. Later the PreToolUse guard asked the user to approve the very model they had named (`T:2124`).
- **Cause.** There is no parsing of model names or cost intent in the Claude adapter: grep for `minimal viable`, `cheapest` or a model-alias parser in `claude/` returns nothing. The prompt goes to the router as free text (`claude/hook.ts:187`); whether the router weighs it is unknown (unverified). The guard compares requested model rank with the session model only (`claude/logic.ts:154-164`).

### 4.4 Contradictory pro-tier message (verified)

- **Evidence.** Four turns carried both "The session model ... is the hard ceiling: never delegate to a stronger model without explicit user approval" and "present the cheaper and the stronger option ... before delegating on opus".
- **Cause.** The Claude adapter builds the ceiling sentence (`claude/logic.ts:120-122`) and the pro sentence (`claude/logic.ts:125-126`) independently and concatenates them (`claude/logic.ts:135-136`). `parseDispatch` maps `pro` to `opus` without clamping (`claude/logic.ts:90-99`, `claude/logic.ts:48-53`). The Antigravity path does clamp: `clampTierToSessionCeiling` (`src/core/governance.ts:326`) is called from `src/tools/dispatch.ts:207` and `:394`, but never from `claude/`.

### 4.5 Persona mismatch and duplicate names (verified)

- **Evidence.** `Code Engineer` for an Opus doc-editing request, `Vault Librarian` for plugin-doc path fixes plus `/clarify`, `Plan` for launching a workflow, `knowledge-researcher` for a case-study request. The same agent appeared as `knowledge-researcher` at 20:04 and `Knowledge Researcher` at 20:05.
- **Cause.** The router must pick one key from the discovered criteria (`claude/logic.ts:26-38`, `claude/logic.ts:103-109`), and discovery registers every agent under its display name, a kebab alias and the file name (`src/core/discovery.ts:93-110`). The router has no knowledge of the plan or the target files, so it matches on prompt keywords. A name Claude Code does not know falls back to `self` (`claude/logic.ts:92-93`); the display-name variants (`Code Engineer`) are not Claude Code `subagent_type` values (the agent list uses the kebab form, reported).

### 4.6 Same text on turns that need no delegation (verified)

- **Evidence.** 13 of 35 hook lines were `self`/haiku on status turns ("A3 completed the run"); every non-short prompt got the ceiling sentence.
- **Cause.** The hook emits for any prompt of 8 characters or more (`claude/hook.ts:89-90`) and `buildDirective` always adds the ceiling (`claude/logic.ts:120-122`). Turns shorter than 8 characters ("yea") and slash or bash-input turns produced no hook line, so "every prompt" is not literally true.

### 4.7 No signal for "minimal viable" or stage-level advice (verified)

- **Evidence.** The user's cost instruction left no trace in the hook output at 20:01.
- **Cause.** The output has one `{subagent, tier, model, effort}` tuple per turn (`claude/logic.ts:69-75`). `jev_batch_dispatch` (`src/tools/dispatch.ts:45`) and the instruction to call it after `/clarify` (`skills/jev/SKILL.md:100-102`) exist, but nothing in the Claude hook or the workflow path invokes it. The `clarify` skill reportedly says the same (reported, skill not in this repo).

### 4.8 Quota label from the wrong product (verified, smaller)

- **Evidence.** Every line says `quota=normal`; the README says the Claude value is "usually `unknown`" (`README.md:404-405`).
- **Cause.** `readQuotaView` reads Antigravity's `quota-state.json` (`src/core/governance.ts:18`, `src/core/governance.ts:196-218`), which exists on this machine. The label says nothing about Claude limits and sits next to a Claude model.

### 4.9 Recommended versus used cannot be joined (verified)

- **Evidence.** The decisions log has the recommendation per prompt and the requested model per Agent call, but no shared key or tier field on the second.
- **Cause.** `appendLog` maps `entry.model` to `modelUsed` for both hooks (`claude/hook.ts:199-211`). For UserPromptSubmit that field holds the recommended model, not a model used. The PreToolUse record (`claude/hook.ts:141`) carries `subagent_type`, `model`, `sessionModel`, `reason`, but no `tier`.

### 4.10 Effort values (verified mechanism, unverified for the specific turns)

- Effort is a keyword heuristic on subagent name and prompt (`src/core/governance.ts:369-408`), then quota-clamped (`src/core/governance.ts:410`). The `effort=low` on the 09:32 pasted-answer turn is probably a keyword hit, but the matching word was not checked.
- The same applies to the batch result: WS1 `effort=high` with persona `Plan` cannot come from the persona name (the rule looks for `architect`, `security`, `auditor`), so it most likely came from the word "architecture" in the description (`src/core/governance.ts:376-383`; the abstracted descriptions were not re-read, unverified).

### 4.11 Batch tier is a keyword rule, so it cannot express this decision (verified in code)

- **Evidence.** All four streams came back `flash`/sonnet, including the one the assistant judged to need Opus, and no stream came back as haiku.
- **Cause.** In `jev_batch_dispatch` the LLM router chooses only the persona (`src/tools/dispatch.ts:342-366`). The tier is set by string matching on the description: `flash` by default, `openrouter_free` if it contains "scaffold", "format" or "boilerplate", `pro` only if it contains "deep reasoning" or "architecture overhaul" (`src/tools/dispatch.ts:387-391`). `flash_lite` is never produced, so haiku is reachable only through the free path, which also swaps the execution tool to `jev_execute_free` (`src/tools/dispatch.ts:413`). A mechanical checklist edit and a decision reversal read the same to this rule unless they contain those words. The single-task `jev_dispatch` does ask the router for a tier (`src/tools/dispatch.ts:124-135`), so the two tools disagree on method.

### 4.12 Antigravity vocabulary and session model in a Claude Code session (verified in code)

- **Evidence.** `executionTool="invoke_subagent"`, guidance text mentioning "0 Gemini quota", and `activeSessionModel` "Gemini 3.8 Flash (Medium)" (reported).
- **Cause.** The guidance strings are hard-coded (`src/tools/dispatch.ts:215-218`, `:413-416`). The session model comes from `readQuotaView().active_model`, which reads Antigravity's `quota-state.json` (`src/core/governance.ts:18`, `:218`), not from the Claude session the hook stores (`claude/hook.ts:152-162`). The MCP tools have no access to that file, so the ceiling clamp in both dispatch tools (`src/tools/dispatch.ts:207`, `:394`) compares against the wrong model. In this run it could have forced `pro` down to `flash` regardless of the task (the clamp has an escape only for certain phrases, `src/core/governance.ts:336-347`), so "all flash" is partly explained by the wrong ceiling (not verified for this call: the batch output does not report whether a clamp happened, `src/tools/dispatch.ts:421-437`).

### 4.13 Persona selection ignores what the persona can do (verified in code and session agent list)

- **Evidence.** WS1 got `Plan`; WS2 to WS4 got `code-engineer` and `vault-librarian`. In Claude Code the `Plan` agent type has no Edit or Write tool (session agent list), so it could not perform the WS1 edits; `vault-librarian` is about note organisation and `code-engineer` about scripts and tests (reported agent descriptions), neither is a doc-edit specialist.
- **Cause.** The router picks from descriptions only (`src/tools/dispatch.ts:344-352`, `claude/logic.ts:15-20`); `Plan` is described as "Designing an implementation plan before writing code" (`claude/logic.ts:18`). Nothing records or filters by tool access, and the keyword fallback (`matchSubagentByKeywords`, `src/tools/dispatch.ts:359-370`) is also text-driven (its body was not read).

### 4.14 Enforcement relies on the assistant remembering (verified)

- **Evidence.** Mode `on`, a rule that says to dispatch after `/clarify`, and zero dispatch calls.
- **Cause.** The Claude plugin registers four events: `SessionStart`, `PostModelSwitch`, `UserPromptSubmit`, `PreToolUse` on `Agent|Task` (`claude/hooks.json:3-32`). There is no `Workflow` matcher, no `PostToolUse`, and nothing reads whether a dispatch call happened. The "block direct edits when no recent dispatch" guard in `skills/jev/SKILL.md:102` and `rules/AGENTS.md:20` is an Antigravity shell hook (`jev-pre-tool.sh`, referenced only from `scripts/audit-agentic-gaps.ts:106-107`); no Claude equivalent ships. Both dispatch tools do append to the shared `jev_telemetry.jsonl` (`src/core/telemetry.ts:6-8`, `:34`, `:53`), so the data for a check exists.

### 4.15 Three sources, no precedence rule (verified by absence)

- **Evidence.** The user's instruction ("minimal viable model"), the standing rule (default sonnet, one final verification on the top tier) and Jev's guard (pro gated behind manual approval) pull in different directions on the same decision.
- **Cause.** Jev applies only its own guard (`claude/logic.ts:154-176`) and has no notion of an instruction or a standing rule; the README does not define an order (`README.md:378-410`).

## 5. Proposed improvements

Each is small and local to the Claude adapter. Order is by value for the case above.

### 5.1 Honour explicit model and cost instructions

- **Change.** Add a pure `parseUserIntent(prompt)` that returns `{ model?: haiku|sonnet|opus, cost?: 'minimal'|'normal' }` from phrases such as "use opus", "run an opus subagent", "sonnet", "haiku", "minimal viable", "cheapest". Apply it after `parseDispatch`: an explicit model overrides the tier; `minimal` lowers the tier by one step (never below `flash_lite`).
- **Where.** New function in `claude/logic.ts`, called from `onUserPromptSubmit` in `claude/hook.ts:98-103`; text says "You asked for X".
- **Test.** In `tests/claude-hook.test.ts`: prompt "run an opus subagent to wrap findings" with `dispatch` returning flash gives a directive naming opus and "explicit user request"; prompt "choose minimal viable model" with a pro recommendation gives sonnet.
- **Risk.** False positives on prose that mentions a model. Mitigate: match only imperative patterns and cap the effect at one step.

### 5.2 Resolve the ceiling versus recommendation contradiction

- **Change.** When the tier is `pro` and the session is below opus, emit one sentence: "Jev rates this opus-level; the session model is the ceiling, so ask the user before delegating on opus." Never print both sentences. When the user explicitly asked for opus (5.1), say so and drop the ask text.
- **Where.** `buildDirective` in `claude/logic.ts:119-138`. Optionally clamp in `parseDispatch` as `src/tools/dispatch.ts:207` does.
- **Test.** `buildDirective(proRec, 'claude-sonnet-5', 'normal')` contains the ceiling once and does not contain "present the cheaper".
- **Risk.** The existing test `asks for a user choice on the pro tier` (`tests/claude-hook.test.ts:83-88`) checks "let the user choose"; keep that phrase.

### 5.3 One deterministic per-stage rubric

- **Change.** Export a `STAGE_RUBRIC` table: mechanical edit or checklist update gives haiku; spec or plan edit with a settled decision gives sonnet; judgement whose error is expensive (reversal not yet decided, adversarial verification, synthesis across disagreeing sources) gives opus. Include it in the context line on delegation-intent turns so the model has the same rule the user wrote.
- **Where.** `claude/logic.ts` next to `TIER_CRITERIA` (`claude/logic.ts:40-45`); reuse as the criteria text in `dispatchQuestions` (`claude/logic.ts:110-114`).
- **Test.** Snapshot test that the criteria strings contain the three rubric lines; `parseDispatch` unchanged.
- **Risk.** Wording drift from the user-level rule; keep it a short, linked copy and test its presence.

### 5.4 Stage plans for Workflow scripts and `/clarify`

- **Change.** A documented, optional call from the orchestrator: pass the stream list (name, files, one-line scope) to `jev_batch_dispatch` and get back tier, model and ordering per stream. Document the call in the Claude Code section and in `skills/jev/SKILL.md`, using rubric 5.3 as criteria.
- **Where.** `src/tools/dispatch.ts:45` (already exists); README Claude Code section; a prompt reminder emitted when the prompt contains "workflow" or "workstreams". The tool needs two fixes first (section 4.11 to 4.13): ask the router for the tier per task using the 5.3 rubric instead of the keyword rule at `src/tools/dispatch.ts:387-391`, and accept an optional per-task `kind` (`mechanical`, `settled-edit`, `judgement`) and `needsEdit` flag so the caller's own classification is used and read-only personas such as `Plan` are filtered out for edit tasks.
- **Test.** Existing batch-dispatch wave tests (`tests/antigravity-native.test.ts`) plus: a three-stream input with kinds `judgement`, `settled-edit`, `mechanical` returns opus, sonnet, haiku; an edit task never gets `Plan`.
- **Risk.** Another LLM call (cost, 1.5 s hook budget); keep it a tool call, not a hook, and keep the offline keyword fallback.

### 5.5 Stay silent on turns without delegation intent

- **Change.** Skip the context line for pasted agent output, task notifications, status turns (`self` with flash_lite and no verb of work) and prompts over a length cap; keep the ceiling sentence out of those turns.
- **Where.** A pre-check in `onUserPromptSubmit` (`claude/hook.ts:89-90`), plus trimming `task` to its first 2000 characters before `liveDispatch` (`claude/hook.ts:187`).
- **Test.** Prompt starting with `<task-notification>` returns `null`; a 6000-character pasted table is trimmed in the `dispatch` call.
- **Risk.** Dropping a useful hint on a turn that did contain a task; err on the side of emitting.

### 5.6 Align personas with real subagent types

- **Change.** Offer the router only kebab-case keys that exist as Claude Code `subagent_type` values (drop display-name aliases) and remove duplicates.
- **Where.** `resolveClaudeSubagentCriteria` (`claude/logic.ts:26-38`); the alias registration is in `src/core/discovery.ts:93-110` and used by other harnesses, so filter in the Claude adapter, not there.
- **Test.** `tests/claude-hook.test.ts` discovery case (`:200`): a discovered `Code Engineer` yields only `code-engineer`.
- **Risk.** A custom agent defined with a display name only; keep the file-name alias.

### 5.7 Log recommended and used with one key

- **Change.** Add `tier` and `recommended_model` to the PreToolUse record and rename the UserPromptSubmit telemetry field from `modelUsed` to `modelRecommended`; add a `turn` id (hash of the prompt) to both. `jev_telemetry.jsonl` already stores `tierRecommended`, `modelUsed`, `quotaStrategy` (`src/core/telemetry.ts:10-22`) but with the wrong meaning for the first hook.
- **Where.** `appendLog` in `claude/hook.ts:199-211` and the log call at `claude/hook.ts:141`.
- **Test.** Unit test with a stub `log`: both hooks produce the same `turn` for the same prompt; PreToolUse includes `tier`.
- **Risk.** Schema change for consumers of `jev_telemetry.jsonl` (the audit drift detector `src/audit/drift-detector.ts`); add fields, keep old ones for one release.

### 5.8 Workflow agents and the guard (needs a design check)

- **Change.** Find out whether Workflow `agent()` calls can be intercepted by any hook event. If not, document the gap and rely on 5.4.
- **Where.** `claude/hooks.json:24-31` only if an event exists.
- **Test.** Manual: run a one-agent workflow and check the decisions log for a PreToolUse entry.
- **Risk.** None for docs; low for a matcher change.

### 5.9 Precedence order between instruction, guard and standing rule

- **Change.** State and implement one order: (1) an explicit user instruction in the current turn ("use opus", "minimal viable model"), (2) the Jev mode guard (pro gated behind approval, which an explicit request for opus satisfies), (3) the standing user rule (default sonnet, escalate only where a wrong judgement is expensive, one final gate on the top tier). Jev's text should say which rule applied ("per your instruction", "per standing rule").
- **Where.** A `resolveModel({ userIntent, rec, sessionModel })` function in `claude/logic.ts` used by `buildDirective` (`claude/logic.ts:119`) and `decideAgentCall` (`claude/logic.ts:154`); a short "Precedence" paragraph in the README Claude Code section.
- **Test.** Table-driven cases in `tests/claude-hook.test.ts`: instruction opus plus guard gives allow without a second ask; instruction "minimal viable" plus rec pro gives sonnet or lower; no instruction plus rec pro gives the ask text; no instruction plus rec flash gives sonnet.
- **Risk.** Jev cannot read the user-level `CLAUDE.md`, so layer (3) stays the router's default unless the rubric (5.3) mirrors it; keep layer (1) conservative.

### 5.10 Pre-flight reminder and recommended-versus-used log for Agent and Workflow

- **Change.** In mode `on`, when an `Agent` call (and a `Workflow` call, if the host allows a matcher) runs and the shared telemetry file has no `jev_dispatch` or `jev_batch_dispatch` entry for this session in the last N minutes, return an `additionalContext` reminder naming the tool and the rubric; log the requested model next to the last recommendation for the session. Optional second step: ask before an Opus agent without approval (`decideAgentCall` already does this for agents above the session model).
- **Where.** `onPreToolUse` in `claude/hook.ts:121-149` (reuse the match on `Agent|Task`, `claude/hooks.json:26`); read `jev_telemetry.jsonl` (`src/core/telemetry.ts:6-8`), where both dispatch tools already append (`:53`). Add a `Workflow` matcher entry to `claude/hooks.json` if Claude Code accepts it (see 5.8).
- **Test.** With a stub telemetry reader: no recent dispatch gives a reminder; a recent `jev_batch_dispatch` gives none; the log entry contains `recommended_model` and `requested_model`.
- **Risk.** Reminder fatigue on one-off agents; scope it to sessions where the prompt or the agent count suggests a staged plan, and never block.
- **Not a copy of the Antigravity guard.** That one blocks direct edits (`skills/jev/SKILL.md:102`); here the aim is a reminder and data for calibration.

### 5.11 Make the MCP dispatch tools harness-aware

- **Change.** Replace the hard-coded `invoke_subagent` and "Gemini quota" strings with a per-harness table (Claude Code: `Agent(subagent_type, model)` or `agent()` in a Workflow), and take the ceiling from the Claude session file when the harness is `claude-code`, not from Antigravity's quota file.
- **Where.** `src/tools/dispatch.ts:215-218` and `:413-416`; `readQuotaView` use at `:207` and `:394`; `claude/hook.ts:172-180` already reads the session model.
- **Test.** `jev_batch_dispatch` under a claude-code stub returns `Agent(` guidance and an `activeSessionModel` that matches the stored session model.
- **Risk.** Touches tools shared with Antigravity and OpenCode; gate by `detectActiveHarness()` (`src/tools/dispatch.ts:90`).

## 6. Open questions

- Did the clamp in the counterfactual batch run fire (wrong ceiling, section 4.12)? Re-run `jev_dispatch` on one stream description (it reports `wasClampedByCeiling` and the raw tier) and add the field to the batch output.
- Which persona should own a spec or plan edit in this plugin set? None of the nine fits well; a `docs-editor` persona with edit tools may be needed.

- Does the router read model names in the prompt? A recorded call with "run an opus subagent" and the raw response would settle it (unverified).
- Was the 19:15 `ask` approved by the user or auto-allowed by a permission rule? The Agent launched, but the approval path was not inspected.
- Should the guard ask at all when the user's prompt already named the stronger model? Current code (`claude/logic.ts:154-164`) cannot tell.
- Is a final Opus gate on a docs-only workflow worth its cost when the user has already made the decisions? The standing rule says yes for the end of a body of work; the run skipped it.
- Should the clarify skill itself end with `jev_batch_dispatch`, as `skills/jev/SKILL.md:100-102` says, or is a rubric in the skill (5.3) enough?

## 7. Outcome

Partly filled 2026-11-09 after a first review of the workflow's combined diff (`wf_461d006e-32d`, 4 agents, 285 tool uses, about 612k subagent tokens, 7 minutes). This is a reviewer read of the diff by the orchestrating Sonnet session, not an independent Opus check; label every line accordingly.

Observed (reported, from the diff review; the diff is committed in the coordination repo history for anyone to re-check):

- All four streams finished with no agent errors and no file collisions (disjoint file sets worked).
- Haiku on `master-tasklist.md` (WS4): the edits were correct and complete against the decision table. One stale line: it wrote "A8c runbook not yet written" because WS3 created the runbook at the same time; fixed by hand (race between streams, not a model error).
- Sonnet streams WS1 to WS3: all ten settled decisions were applied. Rework found by the review: two stale cross-references in `implementation-plan.md` (a "spec text to amend" note and the "spec amendments owed" row, both written before WS2 finished), again a race between streams.
- The streams reported their own mismatches and open design items honestly (for example WS2: a handle that exists inside `cards_allowed` still yields a held task while an unknown one is rejected, so existence stays observable to allowlisted peers; surfaced for the user to confirm).
- No decision was misapplied in the parts read. Not read line by line: the full 249-line `implementation-plan.md` diff and the 166-line `server-api.md` diff.
- Cost versus the Opus plus three Sonnet plan: not measured (no per-model token split is available from the run; total is the figure above).
- Would a final Opus gate have caught anything: unknown. The two rework items were cross-stream timing races that an end-of-run consistency pass of any tier would catch; a model tier upgrade of WS1 would not have prevented them.

Still to measure per stream and model:

- Edits that needed rework after review (count and kind)
- Review time per stream
- Whether Haiku on `master-tasklist.md` produced a correct result, with the exact errors if not
- Whether any Sonnet stream missed the settled decision (WS1 in particular)
- Total token or quota cost versus the Opus plus three Sonnet plan
- Whether the missing final Opus gate would have caught anything

Provisional verdict (reported): the cheaper plan (three Sonnet, one Haiku, no Opus) produced a usable result for a docs-only fold-in where the decisions were already made. The failures seen were cross-stream timing races, not model-tier failures. The case does not show that Opus was needed for WS1. It also does not show that the missing Jev pre-flight and final gate were harmless: Jev recommended Sonnet for all four streams and would have agreed with the outcome, but it was never asked, and it could not have recommended Haiku or Opus for these descriptions (section 4.11). Final verdict after an independent check: _TO BE FILLED_.

### 7.1 Later runs in the same session: rounds 2 and 3 (2026-11-09)

After the first run, Jev's pre-flight was used (lesson of sections 4.14 and 5.10). Two more docs workflows followed; both are verified from the session transcript and the dispatch code unless marked.

Round 2 (workflow `wf_a066b02d-b7f`, three Sonnet agents: spec stream, plan stream, consistency sweep):

- `jev_batch_dispatch` was called before launch (mode `on`). Result: both streams `flash` / `sonnet` / effort `medium`, personas `general-purpose` and `Plan`, `executionTool` `invoke_subagent`, `activeSessionModel` "Gemini 3.8 Flash (Medium)" (wrong, section 4.12).
- Followed: the models (Sonnet). Not followed: the `Plan` persona (read-only in Claude Code, cannot edit files), so the default workflow agents ran.
- Added by the assistant, not by Jev: a final consistency sweep, because the first run had left two stale cross-references from stream races.
- `jev_judge` was run on a summary of the diff, not the diff itself: `passed: true`, `noul 0.74`, `confidence 0.48`. Weak evidence, kept as a gate only.
- Result: committed as `eaaf6bb` with three small hand fixes; no model-tier failure.

Round 3 (workflow `wf_d49b1e67-50b`, two Sonnet edit streams, a Sonnet sweep and a final review on Opus at the user's request):

- `jev_batch_dispatch` returned: stream 1 (spec files) `openrouter_free` via `jev_execute_free`, effort `low`; stream 2 `Plan` / `sonnet`; the sweep and the final review `general-purpose` / `sonnet`.
- Deviation 1, not followed: `openrouter_free` for the spec stream. Cause (verified): `src/tools/dispatch.ts:388` tests `tLower.includes('format')`, and the task text contained "dump format". It is a substring match, so ordinary words such as "information" and "transformation" also contain "format" and would be routed to the free tier. A free text-completion path also cannot run the Edit tool across a 1,600-line spec in the repo (reported, from the tool description; not tested). The assistant ran the stream on Sonnet.
- Deviation 2, not followed: Sonnet for the final review. The user asked for Opus, and the assistant followed the user. Jev has no keyword or task kind for review, verification or judgement (section 4.11), so it could not have recommended Opus here.
- `Plan` was again recommended for a file-editing stream and again not used.
- Outcome of round 3: TO BE FILLED when the workflow's review verdict and the commit are known.

What these two runs add to the findings:

- The keyword rule has false positives on common words (`format` as a substring), which is a new finding beyond section 4.11 (which showed only false negatives).
- The pre-flight call costs one tool call and a few seconds, but in both rounds the useful decisions (Sonnet default, consistency sweep, Opus for the final review) came from the assistant and the user, not from the manifest.
- A final-review stage had to be added by hand each time; the dispatch output has no place for it.
- Proposed addition to 5.3: match whole words (`\bformat\b`) or classify by task kind (edit an existing document, create a new document, review or verify, mechanical checklist) instead of substrings, and add a `review` kind that maps to the highest tier when the user names it or the work is a final gate.

## 8. Start a new session here (handoff)

Purpose: another Claude Code session can continue the Jev work from this section alone. Everything in it was verified or labelled in sections 2 to 7.

Where things are:

- This document: `packages/jev/docs/case-studies/2026-11-09-model-tier-for-multi-stream-workflow.md` in the `cognitive-engine` repo, linked from `packages/jev/README.md` (it was `plugins/jev/docs/case-studies/` in `agentic-hub`, where only a pointer remains).
- Jev plugin source cited here: `plugins/jev/` in `agentic-hub` at 2.3.0 (hook `claude/hook.ts`, logic `claude/logic.ts`, hook registration `claude/hooks.json`, governance `src/core/governance.ts`, dispatch `src/tools/dispatch.ts`, tests `tests/claude-hook.test.ts`).
- Jev's decision log: `~/.config/jev/claude_decisions.jsonl`; mode config `~/.config/jev/config.json` (mode `on` for all harnesses on 2026-11-09).
- Source session transcript (large JSONL, search with `rg`, never print whole): a Claude Code session transcript on the author's workstation (not published); the workflow script was `fold-postgres-and-clarify-decisions` (run `wf_461d006e-32d`).

Where the fixes go (verified 2026-11-09 against `cognitive-engine` `origin/main` at `9fd9af1` after a fetch; confirmed by the owner of the cognitive-engine plan):

- The code citations in this document are to the frozen `agentic-hub` copy (Jev 2.3.0, final, pr-plan decision 14). The same defects are present on `cognitive-engine` `origin/main`, `packages/jev`: the keyword substring rule at `src/tools/dispatch.ts:360` (and `core/heuristics.ts:73-76`, `core/governance.ts:376`), `jev_batch_dispatch` at `src/tools/dispatch.ts:49`, the hard-coded `invoke_subagent` at `src/tools/dispatch.ts:190-193` and `:385` (finding 4.12). The ceiling clamp exists there (`dispatch.ts:182-183`, `clampTierToSessionCeiling`), so the 4.4 contradiction is specific to the Claude adapter path.
- Commits `9d5a9bc` (workflow guard, effort threshold) and `12b77c6` (ceiling for inheriting subagents) are on `cognitive-engine` `origin/main` (merged as #18); they do not fix 4.4 or 4.14. The workflow-guard work covers the 5.8 design check only.
- The fixes (5.1 to 5.11, handoff items 1 to 6) are the advisor port of pr-plan decision 15 in `cognitive-engine` `packages/jev` (2.4.0); do not implement them in `agentic-hub`. De-duplicate against RFC 006 before adding items.
- This document stayed in `agentic-hub` until the advisor-port branch landed, then moved to `cognitive-engine` docs with the jev 2.4.0 packaging PR, leaving a pointer in `agentic-hub` (decision of the plan owner, 2026-11-09). The RFC 005 Phase A notice keeps the wording "relocating to cognitive-engine".
- Correction of an earlier version of this analysis: an interim check against an un-fetched local `main` wrongly concluded that the substring rule and the batch tool were absent from `cognitive-engine` and that `9d5a9bc` was not on `main`. Both statements were wrong; the fetched `origin/main` shows the opposite.

What is settled (verified):

- Jev mode was `on` (aggressive) and no `jev_*` tool call was made anywhere in the session; the stage tiers were the assistant's own call under the standing rule, not Jev's.
- The hook fires only on typed prompts of 8 or more characters; the `/clarify` answers and the workstream proposal had no Jev signal (section 4.1).
- A counterfactual `jev_batch_dispatch` returned Sonnet for all four streams, by a keyword rule that can never return Haiku (`flash_lite`) or, without trigger words, Opus (section 4.11).
- Jev output uses Antigravity vocabulary and the wrong session model in a Claude Code session; personas ignore what a subagent type can do (sections 4.12, 4.13).
- The Claude adapter does not clamp to the session model while the Antigravity path does, which explains the contradictory "ceiling" plus "present both" message (section 4.4).
- The workflow `agent()` calls did not trigger Jev's PreToolUse hook (section 4.14, 5.8, design check needed).

What is open (do first, in this order):

1. Re-run `jev_dispatch` on one stream description and confirm whether the ceiling clamp fired (section 6 first bullet). Read-only, cheap.
2. Decide the precedence order (instruction > mode guard > standing rule) and implement 5.1 and 5.9 with a test case each in `tests/claude-hook.test.ts` (explicit "use opus" must not trigger an approval ask; "minimal viable model" must lower the tier).
3. Implement 5.10 (pre-flight reminder and recommended-versus-used log for `Agent` and `Workflow` calls) after the 5.8 design check shows whether PreToolUse fires for workflow agents.
4. Make the dispatch tools harness-aware (5.11): Claude Code tool names and agent types, real session model.
5. Rework the batch tier rule (5.3) so it can express haiku, sonnet and opus by task kind, and fix the substring false positive at `src/tools/dispatch.ts:388` first (a one-line change to whole-word matching plus a test with "dump format" and "information"); see section 7.1.
6. Fill the Outcome verdict after an independent check of the coordination diff.

Rules for the new session:

- Planning-docs repo conventions: edit with the Edit tool, commit only when asked, push only when asked, use `jq` and `rg` (see the user's CLAUDE.md).
- Subagent models: default Sonnet; Opus only where a wrong judgement is expensive; pass the model explicitly; one final verification gate on the highest tier at the end of a body of work (user-level rule, 2026-10-28). The user may override per request ("minimal viable model").
- Jev mode is `on`: run `jev_dispatch` or `jev_batch_dispatch` before delegating non-trivial work and `jev_judge` on diffs and commits, and say so in the turn. The session this case study came from did not, which is the finding.
- The coordination work in the same repo (Postgres decision and the 10 `/clarify` decisions) has its own state in `plugins/coordination/docs/plans/master-tasklist.md`; it is not part of the Jev work.
