# Case study: a permission dialog for a model the call was not using (2026-11-11)

> **Copy.** The original lives in `rockad/agentic-hub` at `plugins/jev/docs/case-studies/` (copied here 2026-11-12 from its `main`). Jev code changes are made in the agentic-hub plugin; this repository keeps the docs only (delivery plan, decision 19). Code paths below are relative to agentic-hub `plugins/jev/`.
>
> **Relevance to `packages/jev` here (read from the README and the merged #22 code, no new test run):** the dialog came from agentic-hub's `decideByTier`, which sent the delegation brief to the router and asked on a `pro` rating. This repository's guard (`src/guard.ts`) makes no network call and ranks the requested model against the session model only; the advisor's `PreToolUse` entry never asks, denies or blocks, and `tests/advisor-preflight.test.ts` pins "never asks, denies or blocks". A pro rating on a call pinned to the session model therefore has no path to a dialog here. A test with exactly that case is not present and would pin it (unverified until added).

Status: fixed in Jev 2.3.2 (branch `fix/jev-pretooluse-pinned-model`), not yet released or installed. Labels used below: **verified** (read in code, in the decisions log or in a tool result, or run), **reported** (stated by the user), **unverified** (plausible, not checked).

Sources:

- Hook decisions log: `~/.config/jev/claude_decisions.jsonl` (machine-wide; times UTC)
- Code paths are relative to `plugins/jev/`
- The earlier case study, `2026-11-09-model-tier-for-multi-stream-workflow.md`, for the hook design this one corrects

## 1. Summary

- A Claude Code session on Sonnet 5.5 (Jev mode `on`) launched subagents with `model: "sonnet"` pinned. Claude Code stopped each launch with a Jev permission dialog: "Jev recommends the pro tier (opus) for this delegation ... Approve to proceed, or deny and run it on sonnet." (reported, quoted from the dialog)
- The user chose No. The subagent did not start, and the assistant was told to stop and wait (verified from the tool result). Retrying produced the same dialog, because the cause was not the model.
- The dialog was not a model-elevation check. It was a tier rating of the task text, shown through Claude Code's permission prompt. The call pinned `sonnet`, which is the session model, so there was nothing to approve.
- The same dialog fired 51 times between 2026-10-28 and 2026-11-11 (verified, section 3), against 4 times for a model that really was above the ceiling.
- The user's observation: it happens only in Claude Code; in Antigravity (agy) the approval works as expected (reported). The cause is a harness mismatch: the dialog assumed "deny" means "use the cheaper model", and in Claude Code it means "cancel and stop".
- Fix: `PreToolUse` on `Agent` keeps one deterministic gate (a model stronger than the session's). The tier rating stays as advice in the turn context and as a log field. Section 5.

## 2. What happened

### 2.1 Timeline (verified from the decisions log and the tool results; UTC, 2026-11-11)

| Time | Event |
| --- | --- |
| 13:19:44 | Delegation "WS1: build tmp-handoff skill", `subagent_type: code-engineer`, `model: sonnet`. Hook logged tier `pro`, recommended `opus`, session model `claude-sonnet-5-5`, reason "Jev recommends the pro tier". Dialog shown; user chose No. |
| 13:19:56 | Same for "WS2: usage reminder Stop hook". No. |
| 13:20:56, 13:21:04 | The assistant retried both after the user said agents should start on the same tier. Same dialogs; No again. |
| 13:22:51, 13:22:59 | Third attempt. The dialogs were approved and both agents started on `sonnet` (the tool results report launches; which approval option was chosen was not recorded). |

- The user then asked whether the hook was broken and whether it should "just ask yes/no" (reported). It does ask yes or no. The problem is what each answer does.
- In the same hour `jev_batch_dispatch` on short descriptions of the same four tasks recommended tier `flash`, model `sonnet`, for all four (verified, tool output). The single-call hook rated the full briefs `pro` (verified, log).

### 2.2 What the dialog offered versus what No does

- The text said "Approve to proceed, or deny and run it on sonnet" (verified, `claude/logic.ts` before the fix).
- In Claude Code, No rejects the tool call. The harness tells the assistant to stop and wait for the user (verified, tool result text). The assistant cannot "run it on sonnet" as a follow-up, because the user did not ask for anything.
- Yes would have run the call as sent: the hook returns `permissionDecision: "ask"` and does not rewrite the tool input, so `model: "sonnet"` stayed (verified, `claude/hook.ts`).

## 3. Evidence from the decisions log (verified)

`PreToolUse` records, counted by outcome, from the log start (2026-10-27) to 2026-11-11 13:30 UTC:

| Outcome | Count |
| --- | --- |
| Dialog because Jev rated the task `pro` | 51 |
| of which the call named no model | 42 (2026-10-28 to 2026-11-10) |
| of which the call pinned `sonnet` | 9 (all on 2026-11-11) |
| Dialog because the model was above the session's | 4 |
| No dialog, pinned model | 3 (2026-11-11) |

- The first of the 51 was on 2026-10-28 and they continued through the whole period. Nothing flagged them: the existing tests asserted the behaviour (section 4, F5).
- "Named no model" means the record's `requested_model` is null, which the hook writes when the call's `model` field is empty (verified, `call.model ?? null`).

## 4. Findings

**F1. A permission dialog is the wrong mechanism for advice (verified).**
`decideByTier` returned `{ decision: 'ask' }` when the dispatch rating was `pro`. A Claude Code permission prompt is a gate with two answers, Yes and No, and No cancels the action. A tier rating asks for a different decision (which model should this use?) that Yes and No cannot express.

**F2. The guard never looked at the call's model (verified).**
`decideByTier(call, rec, intent)` read `call.description` and `call.prompt` for display and ignored `call.model`. It also resolved the tier with `sessionModel: null`, so the session ceiling never applied. A call pinned at or below the session model is by definition within the ceiling, and `decideAgentCall` already covers the one real approval case (a stronger model).

**F3. It scored the delegation brief, not the user's prompt (verified; cause of the rating unverified).**
`onPreToolUse` sent `call.prompt || call.description` to Jev's router as `Task: <text>`. A delegation brief is long and procedural. The same tasks, summarised in one line each, were rated `flash`. Why the router rated the briefs `pro` was not investigated; a content-sensitive score on long text is the likely cause (unverified). Two further effects: every `Agent` launch cost a network call under a 5 s hook budget, and the brief text left the machine for the router even when no dialog followed.

**F4. The design contradicted the user's standing rule and the skill text (reported and verified).**
The user's rule for Claude Code, reported from the user-level `CLAUDE.md`: the default is Sonnet, escalation to Opus needs a reason, and a `pro` recommendation "is an input to that decision, not a trigger for it". The hook made it a trigger. `skills/jev/SKILL.md` told the assistant to treat a denial as "run it on the session model or cheaper", which the harness does not allow (F1).

**F5. The tests encoded the defect (verified).**
`tests/claude-hook.test.ts` had "asks when Jev recommends the pro tier" and `tests/claude-intent.test.ts` had "still asks on a pro recommendation with no instruction". Both passed throughout. No test said what a within-ceiling call should do.

**Why Antigravity is unaffected (reported; the agy path was not re-run).**
In Antigravity the approval for a `pro` recommendation is a question in the conversation, with approve or decline choices where declining falls back to `flash` (the user-level rule text). A decline there is an answer. In Claude Code the same intent went through the permission layer, where a decline is a rejection.

## 5. The fix (Jev 2.3.2)

- `claude/logic.ts`: removed `decideByTier`. `decideAgentCall` stays the only gate. Its reason now says "Yes runs it on the stronger model. No cancels the delegation and the assistant waits for your instruction; it does not retry on a cheaper model by itself."
- `claude/hook.ts`: `onPreToolUse` no longer calls the dispatch router. It runs the ceiling check, adds the pre-flight reminder as before, and logs `tier` and `recommended_model` from the turn's stored recommendation (made for the user's prompt) next to `requested_model`. The brief is no longer sent anywhere.
- `skills/jev/SKILL.md` and `README.md`: describe the single gate, say what No does, and add the 2.3.2 release note.
- Version bumped to 2.3.2 in the plugin, package, marketplace manifests and tracing.

## 6. Verification

- Tests: seven new tests in `tests/claude-hook.test.ts` (pinned `sonnet` passes, pinned `haiku` passes, no model passes, the brief is never sent to Jev, the log pairs the prompt-time rating with the requested model, a model above the ceiling still asks, the reason says what No does and no longer promises a retry), one end-to-end test in `tests/claude-intent.test.ts`, and four obsolete `decideByTier` tests removed (one in `claude-hook.test.ts`, three in `claude-intent.test.ts`).
- The seven new tests fail on the old code and pass on the fix (run both ways, verified).
- Full suite: 296 pass, 5 fail. The same 5 fail on untouched `origin/main` (292 pass, 5 fail), so they are not caused by this change: custom-agent discovery, per-harness mode isolation and three Stage 2 workspace-agent discovery tests. They were not investigated here.
- `tsc --noEmit` exits 0 (verified).

## 7. Not verified, not fixed

- **Live check:** the new hook has not been observed inside Claude Code. The installed plugin is still 2.3.1. Run one pinned-`sonnet` delegation after updating and confirm no dialog; run one `model: opus` delegation on a Sonnet session and confirm the dialog and its new wording.
- **Unpinned calls** now get no notice at the tool call. The turn context from `UserPromptSubmit` still carries the rating and the instruction to ask before using Opus. Whether the assistant follows that is behaviour, not enforcement.
- **Scoring disagreement:** the router rates long briefs higher than one-line summaries. It no longer opens a dialog, but the prompt-time rating still feeds the turn context and the log. Whether `UserPromptSubmit` over-rates long pasted prompts is untested.
- **Workflow `agent()` calls** still bypass `PreToolUse` entirely (the earlier case study, finding 4.14); this fix does not change that.
- **Pre-existing failing tests:** the five listed in section 6.

## 8. Rollout

The marketplace entry `agentic-hub` is a local directory source, which is the shared `agentic-hub` checkout, so the plugin updates only when that checkout's HEAD has the fix (verified from `known_marketplaces.json` and the install record).

1. Review and merge the branch into `main`. Do not push or merge without the user's go.
2. Move the shared checkout to the merged `main` (it is currently on a detached HEAD; another session uses it, so do this when it is idle).
3. `claude plugin marketplace update agentic-hub`, then `claude plugin update jev@agentic-hub`.
4. Start a new session (hooks are read at session start) and run the live check in section 7.
5. Optional: tag `jev-v2.3.2` like the earlier releases.

## 9. Prevention

- A contract test for the permission layer: `onPreToolUse` must return no `permissionDecision` for any call at or below the session model, whatever the rating (added).
- A harness mismatch is a design question, not a wording question: before a hook uses `ask`, state what each answer does in each harness.
- The decisions log already held the signal: 51 dialogs against 4 real elevations. A weekly count of dialog reasons would have shown it.
