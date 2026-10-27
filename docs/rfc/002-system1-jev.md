# RFC 002: Track 1 — System 1: TypeSafe Jev Coprocessor & Multi-Harness Adapters

- **RFC Number**: 002
- **Track**: Track 1
- **Status**: Proposed  
- **Runtime**: TypeScript / Bun  
- **License**: MIT  
- **Target Subsystem**: `packages/jev/`, `adapters/`, `cli/`  
- **Target Branch**: `feat/track1-jev-adapters`
- **Parent Blueprint**: [RFC 001: Cognitive Engine Architecture](001-harness2-jev-unified-cognitive-substrate.md)
- **Implementation Plan**: [Multi-Track Implementation Plan](../plans/2026-10-26-multi-track-implementation-plan.md)
- **Sibling Track Specifications**:
  - Track 2: [RFC 003: Memory & Lookup Substrates](003-memory-lookup.md)
  - Track 3: [RFC 004: System 2 — Google Harness² Recursive Reasoning Optimizer](004-system2-harness2.md)
  - Track 4: [RFC 005: Cross-Runtime Egress Gates & agentic-hub Retirement Protocol](005-egress-retirement.md)

---

## 1. Summary & Motivation

Establishes Track 1 of Cognitive Engine: migrating the TypeSafe Jev System 1 coprocessor into `packages/jev` and delivering first-class multi-harness adapters under `adapters/` for Google Antigravity, Claude Code, and OpenCode.

---

## 2. Technical Scope

- **TypeSafe Jev Engine (`packages/jev`)**:
  - Sub-second probabilistic router (`jev_dispatch`, `jev_route`).
  - Binary acceptance testing and quality gatekeeping (`jev_judge`).
  - Free-tier offloading coprocessor (`jev_execute_free`, `jev_agent_free`).
  - Model tier ceiling governance.
- **Dual-Mode Transport**:
  - Primary: Standard stdio MCP (official TypeScript SDK) transport for zero-latency, direct integration with CLI harnesses (`agy mcp`, Claude Code, OpenCode).
  - Secondary: Streamable HTTP daemon on `127.0.0.1:8115/mcp` when supervised via `engine up`.
- **Multi-Harness Adapters (`adapters/`)**:
  - **Antigravity**: Sidecar integration, MCP registration (`~/.gemini/config/mcp_config.json`), and the narrow subagent guard hook (see below).
  - **Claude Code**: Native plugin bundle (`.claude-plugin/plugin.json`), MCP registration, and the narrow subagent guard hook. No session, prompt or model-switch hooks, per the Oct 27 settings consolidation.
  - **OpenCode** (deferred): a V2 plugin bundle exists at `adapters/opencode/` but is unverified against a live OpenCode and is expected to need an overhaul; it is not part of the Track 1 review.
  - **CLI Installer** (deferred): the unified `engine install-adapters` command moves to the `engine` CLI work. Until then each adapter README documents manual install steps.
- **Narrow Subagent Guard (Claude Code and Antigravity)**:
  - Rationale: the tier-ceiling and Pro-approval invariants must be enforced, not merely advised. With no hook, a subagent could run on a stronger model than the session without any prompt to the user.
  - One `PreToolUse` hook per harness, backed by a single implementation (`packages/jev/src/guard.ts`, entrypoint `src/hook.ts`):
    - Claude Code (matcher `Agent|Task`): returns `ask` when the model the subagent will run on (the call's `model`, else the `model:` in its definition file) outranks the session model, read from the last assistant turn of the session transcript. Verified to fire under `bypassPermissions`.
    - Antigravity (matcher `invoke_subagent`): returns `force_ask` when any subagent requests a Pro-class model.
  - Scope limits: deterministic, no network access, no state files; active only in Jev mode `on`. Once on, it never passes a call it cannot evaluate silently: unreadable input, an internal error, or an unknown session model for an opus-class request produce an `ask`. Advice, dispatch and telemetry remain in the MCP tools and are not enforcement.
  - Out of scope: Antigravity quota lockouts, the free-tier model guard and the multi-file write guard stay in the consumer workspace's hook script.

---

## 3. Interfaces & Invariants

- Inherits common invariants from [RFC 001](001-harness2-jev-unified-cognitive-substrate.md):
  - Strictly respects `zdr: strict`, blocking `openrouter/free` offloading when handling restricted content.
  - Enforces subagent tier ceilings (Pro tier strictly requires manual human approval), by hook on Claude Code and Antigravity (active in Jev mode `on`). The hook is the only enforcement point: `jev_dispatch` and its clamp are advice, and nothing an agent passes to a tool counts as approval.

---

## 4. Acceptance Criteria

- [x] `packages/jev` compiles under Bun with 0 TypeScript errors.
- [x] Stdio and Streamable HTTP transports (`:8115`) functional.
- [ ] Claude Code and Antigravity adapters install following their README steps (`engine install-adapters` and OpenCode are deferred).
- [x] All ported Jev unit tests and Decision API schemas pass in `packages/jev` (Claude-hook and commit-script tests were not carried over).
- [x] Root `LICENSE` (MIT) applied.
