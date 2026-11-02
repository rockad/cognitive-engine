# Plan: System 1 TypeSafe Jev Antigravity & Gemini 4 Argon Hardening

- **Document Type**: Architecture & Engineering Implementation Plan
- **Date**: 2026-11-02
- **Author**: Antigravity (`gemini-2.5-flash`)
- **Reviewers**: Aleksandr Korolev ([@rockad](https://github.com/rockad)), Claude Code (Sonnet 5.5), [@Doc-Cr](https://github.com/Doc-Cr)
- **Target Workstream**: Track 1 (`packages/jev/` and `adapters/antigravity/`)
- **Base Branches Considered**: `feat/track1-jev-adapters` (PR #3) / `fix/agent-guard-unknown-agents` (PR #4)
- **Status**: Ready for implementation / User review

---

## 1. Executive Summary & Provenance

During production validation of the TypeSafe Jev coprocessor on 2026-10-29 within the live environment (`~/.gemini/config/plugins/jev/`), four systemic integration edge cases were identified and hardened:

1. **PreInvocation SSE Socket Hang**: Local HTTP fetches without an `AbortController` kept Bun's event loop alive, causing Antigravity's supervisor (`prehooks.go:43`) to trigger a 5.0s `SIGKILL` timeout that dropped hook stdout.
2. **Missing PreInvocation Telemetry**: Pre-flight dispatch did not append to `jev_telemetry.jsonl`, triggering false-positive edit blocks under `PreToolUse` consecutive edit guards.
3. **Frontier Model Governance & Quota Mappings**: Gemini 4 Argon and Claude Fable 5.1 were misclassified as Tier 2 (`flash`) instead of Tier 3 (`pro` / `deep`), and Argon's dedicated quota bucket (`gemini-argon-wtu-weekly`) was unmapped.
4. **Antigravity Environment Detection**: Harness detection inspected obsolete environment markers, misidentifying Antigravity as a generic CLI and emitting invalid model enum values.

These fixes are proven in production on the host machine. This document defines the engineering plan to port, test, and sequence these hardening specifications into `cognitive-engine`.

---

## 2. Technical Specifications & Architecture

### 2.1 Area 1: `PreInvocation` Hook SSE Socket & Event-Loop Termination

- **Subsystems**:
  - `adapters/antigravity/`
  - `packages/jev/src/pre-invocation.ts`
  - `adapters/antigravity/src/hook.ts`
- **Failure Mode**:
  - In Bun, `fetch('http://127.0.0.1:8110/mcp')` or `:8105/mcp` wrapped in `Promise.race([fetch(...), timeoutPromise])` leaves open socket handles if the timeout wins or if connection keep-alive remains active.
  - Antigravity's Go supervisor (`prehooks.go:43`) waits 5.0 seconds before issuing `SIGKILL`.
  - Buffered stdout containing the pre-flight recommendation is discarded by `SIGKILL`.
- **Implementation Requirements**:
  - **Explicit `AbortController`**: Every outbound daemon fetch must attach `signal: controller.signal` and execute `controller.abort()` in a mandatory `finally` block immediately after payload parsing.
  - **Explicit `process.exit(0)`**: Hook scripts must invoke `process.exit(0)` on all successful and recovered error code paths to force event-loop termination.
  - **Fail-Soft Shell Guard**: The hook execution command in Antigravity's configuration must be wrapped with a 3.5s timeout:
    ```bash
    timeout 3.5 bun run /path/to/hook.ts || exit 0
    ```

### 2.2 Area 2: Live `PreInvocation` Dispatch Telemetry Logging

- **Subsystems**:
  - `packages/jev/src/tools/dispatch.ts`
  - `packages/jev/src/pre-invocation.ts`
  - `packages/jev/src/core/telemetry.ts`
- **Failure Mode**:
  - Antigravity's `PreToolUse` hook (`jev-pre-tool.sh`) checks `jev_telemetry.jsonl` and blocks consecutive edits (>3 files) if no recent dispatch recommendation occurred.
  - Pre-flight dispatch calculations that execute purely in memory without logging telemetry cause subsequent file modifications to be blocked.
- **Implementation Requirements**:
  - When `PreInvocation` generates a calibrated tier recommendation, write an entry to `.agents/memory/jev_telemetry.jsonl` with:
    - `timestamp`: ISO-8601 string
    - `tool`: `"jev_dispatch"`
    - `toolOrHook`: `"PreInvocation"`
    - `recommendedTier`: Calibrated tier (`openrouter_free`, `flash_lite`, `flash`, `pro`)
    - `confidence`: Calibrated probability score
  - Inject the recommendation into the hook's stdout payload under `injectSteps`.

### 2.3 Area 3: Frontier Model Tier 3 Ranking & Dedicated Quota Bucket Mapping

- **Subsystems**:
  - `packages/jev/src/core/governance.ts`
  - `packages/jev/src/core/config.ts`
- **Failure Mode**:
  - `modelTierRank()` defaults unknown models to rank 2 (`flash`).
  - Running in a `"Gemini 4 Argon (Medium)"` or `"Claude Fable 5.1"` session caused `clampTierToSessionCeiling()` to assign ceiling rank 2, improperly clamping subagents.
  - Gemini 4 Argon reports quota under `gemini-argon-wtu-weekly` in `quota-state.json`, which was unread by `readQuotaView()`.
- **Implementation Requirements**:
  - Update `modelTierRank()` in `governance.ts` to assign rank 3:
    ```typescript
    if (
      t.includes('pro') ||
      t.includes('argon') ||
      t.includes('opus') ||
      t.includes('fable') ||
      t.includes('expensive') ||
      t === 'deep'
    ) {
      return 3;
    }
    ```
  - Update `normalizeTier()` in `config.ts` to map `argon` and `fable` variants to `'deep'`.
  - In `clampTierToSessionCeiling()`, expand `explicitProOverride` to match `argon`, `gemini argon`, `fable`, `claude fable`.
  - In `readQuotaView()`, inspect `active_model` for `argon` and map weekly quota from `gemini-argon-wtu-weekly`.

### 2.4 Area 4: Antigravity Environment Variable Detection & Model Enum Alignment

- **Subsystems**:
  - `packages/jev/src/core/config.ts`
  - `adapters/antigravity/`
- **Failure Mode**:
  - Antigravity exports `ANTIGRAVITY_AGENT`, `ANTIGRAVITY_APP_DATA_DIR`, `ANTIGRAVITY_LS_ADDRESS`, `ANTIGRAVITY_CONVERSATION_ID`, etc.
  - Unmatched detection resulted in `'cli'` fallback, yielding invalid model string `"typesafe/jev-router"` for `invoke_subagent`.
- **Implementation Requirements**:
  - Inspect all active Antigravity environment variables in `detectActiveHarness()`:
    - `ANTIGRAVITY_AGENT`
    - `ANTIGRAVITY_APP_DATA_DIR`
    - `ANTIGRAVITY_LS_ADDRESS`
    - `ANTIGRAVITY_LS_VERSION`
    - `ANTIGRAVITY_CONVERSATION_ID`
    - `ANTIGRAVITY_PROJECT_ID`
    - `ANTIGRAVITY_CLI`
    - `GEMINI_CLI`
    - `CORTEX_SESSION`
  - Fallback check: Inspect `process.argv` and `~/.gemini/antigravity-cli/quota-state.json` mtime.
  - Guarantee `getHarnessModels('antigravity')` strictly maps:
    - `free` -> `'openrouter/free'`
    - `cheap` -> `'flash_lite'`
    - `standard` -> `'flash'`
    - `deep` -> `'pro'`

---

## 3. Sequencing, Stacking & Cross-Track Impact Analysis

### 3.1 PR Stacking & Merge Sequence Options

The current baseline merge order agreed with the user is:
1. PR #3 (`feat/track1-jev-adapters`)
2. PR #4 (`fix/agent-guard-unknown-agents`)
3. PR #8 (`feat/track2-memory-lookup`)
4. PR #9 (`feat/mcp-conformance`)
5. PR #5 (`feat/stage1-integration`)
6. PR #6 (`feat/model-bench`)
7. PR #10 (`feat/lookup-smb-sources`)
8. PR #7 (`docs/stage1-findings`)

There are two viable implementation options for Argon Hardening:

- **Option A (Recommended: Stacked PR #12 after PR #4)**:
  - Branch `feat/argon-hardening` from `fix/agent-guard-unknown-agents` (PR #4 head `fb9fb8d`).
  - Implement and verify changes with `bun test`.
  - Leaves PR #3 and PR #4 completely untouched, preserving their reviewed state (1204 passing tests).
  - Merges cleanly into `main` after PR #4.
- **Option B (Direct Integration into PR #3)**:
  - Commit directly to `feat/track1-jev-adapters`.
  - Requires rebasing PR #4 (`fix/agent-guard-unknown-agents`) on top of the updated PR #3.
  - Invalidates previous independent review records on PR #4.

**Recommendation**: Option A. It cleanly isolates the new hardening specifications, allows Antigravity to work independently in a dedicated worktree, and maintains strict git linear stacking without disruption to Claude Code or existing PRs.

### 3.2 Cross-Track & Workstream Independence

- **Track 2 (Memory & Lookup)**: Zero code overlap. Track 2 operates in Python (`packages/memory`, `packages/lookup`).
- **Track 3 (Harness2)**: Zero code overlap. Harness2 runs in Python (`packages/harness2`).
- **Claude Code R-18 Workstream**: Zero overlap. Claude is currently modifying `egress_proxy.py` and `stage1.py` in `cognitive-engine-int` for model denial metric counting.

---

## 4. Verification & Testing Checklist

| Id | Check Target | Command / Validation Procedure | Expected Outcome |
| --- | --- | --- | --- |
| V-1 | Jev Test Suite | `bun test packages/jev` | All existing unit tests pass cleanly |
| V-2 | Antigravity Adapter Suite | `bun test adapters/antigravity` | Path quoting, timeout handling, and hook tests pass |
| V-3 | Governance & Model Ranking | `bun test packages/jev/tests/governance.test.ts` | Argon and Fable return rank 3; ceiling clamped appropriately |
| V-4 | Quota Bucket Resolution | Unit test in `packages/jev/tests/governance.test.ts` | `gemini-argon-wtu-weekly` correctly resolves weekly fraction |
| V-5 | Environment Detection | Unit test in `packages/jev/tests/config.test.ts` | `ANTIGRAVITY_*` variables resolve harness `'antigravity'` and valid model enums |
| V-6 | TypeScript Compilation | `bun run lint` | `tsc --noEmit` exits 0 with zero type errors across all packages |
