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
  - Version-aware model tier ceiling governance.
- **Dual-Mode Transport**:
  - Primary: Standard stdio transport using official TypeScript MCP SDK for zero-latency, direct integration with CLI harnesses (`agy mcp`, Claude Code, OpenCode).
  - Secondary: Streamable HTTP daemon on `127.0.0.1:8115/mcp` when supervised via `engine up`.
- **Multi-Harness Adapters (`adapters/`)**:
  - **Antigravity**: Sidecar integration and MCP registration (`~/.gemini/config/mcp_config.json`).
  - **Claude Code**: Native plugin bundle (`.claude-plugin/plugin.json`) and MCP registration with lifecycle hooks (`hooks/hooks.json`).
  - **OpenCode**: V2 plugin bundle and event interceptors.
  - **CLI Installer**: Unified `engine install-adapters` command configuring all three environments turnkey.

---

## 3. Interfaces & Invariants

### 3.1 Common Invariants
- Inherits common invariants from [RFC 001](001-harness2-jev-unified-cognitive-substrate.md):
  - Strictly respects `zdr: strict`, blocking `openrouter/free` offloading when handling restricted content.
  - Preserves `@Doc-Cr` alias routing rules.

### 3.2 Version-Aware Subagent Tier Ceiling Governance
- **Version-Aware Escalation Rule**:
  - Subagent guard asks on ANY escalation above the session model.
  - The earlier Claude Code subagent model rule was temporary and is not encoded as permanent.
- **Ranking Tuple**:
  - Models are ranked by `(family, version)`.
  - Canonical aliases are resolved deterministically: `opus` -> 5.5, `sonnet` -> 5.5, `fable` -> 5.1.
- **Session Model Resolution**:
  - Active session model is resolved from a `PostModelSwitch` state file with transcript-tail fallback.
  - Unknown models fail closed by prompting (`ask`).
  - Every `ask` and safety fallback is audited in `~/.config/jev/claude_decisions.jsonl`.
- **Safety Fallback Exemption**:
  - Never block Claude Code's internal "switch models when a message is flagged" safety fallbacks.

### 3.3 Workflow Guard Seam (`PreToolUse[Workflow]`)
- **Static AST Resolution**:
  - Statically resolves `script`, `scriptPath`, `name`, nested `workflow()` and `agentType`.
  - Model omitted is treated as inherit (allowed).
  - Dynamic, interpolated, or unparseable model specifications trigger an interactive `ask` naming the exact call site.
- **Acorn AST Parser**:
  - Uses Acorn JavaScript AST parser with a fail-closed visitor.
  - Aliases, spread arguments, computed keys, template literals with expressions, dynamic model expressions, and parse errors all trigger `ask`.

### 3.4 Verification Fixtures & Canary Protocol
- Capture real event payloads (`PreToolUse[Workflow]`, `PostModelSwitch`, `SubagentStart`) using a log-only canary hook.
- Commit captured payloads as test fixtures before implementing production logic.
- Develop and verify parser tests against committed fixtures.

### 3.5 Native Hook Failure Policy
- Guard hooks use native `"onFailure": "block"` (Claude Code >= 2.1.295) to prevent silent fail-open bypasses.
- Log-only canary hooks remain fail-open (`"onFailure": "continue"`).
- Regression test: inject process termination to verify hook failure blocks tool execution.

### 3.6 Implementation Ownership Division
- **Claude Code Ownership**:
  - `adapters/claude/hooks/hooks.json` configuration.
  - `packages/jev/src/guard.ts` (fail-open shell wrapper, fable rank / version-aware, active in both `on` and `on-demand` modes).
  - `packages/jev/src/workflow-guard.ts` (Acorn AST inspector and tests).
- **Antigravity Ownership**:
  - Plugin agent definitions (`agentDefinitionModel`).
  - Antigravity hook path quoting in `adapters/antigravity/src/index.ts`.

---

## 4. Acceptance Criteria

- [ ] `packages/jev` compiles under Bun with 0 TypeScript errors.
- [ ] Stdio and Streamable HTTP transports (`:8115`) functional with strict tool argument validation.
- [ ] Subagent guard triggers `ask` on any escalation above active session model.
- [ ] Model alias normalization resolves `opus->5.5`, `sonnet->5.5`, `fable->5.1`.
- [ ] Session model state tracking via `PostModelSwitch` with transcript-tail fallback.
- [ ] Acorn AST workflow parser fails closed on dynamic expressions, spreads, and parse errors.
- [ ] Test fixtures committed from live canary payload captures.
- [ ] Guard hooks configured with `"onFailure": "block"` and verified via failure test.
- [ ] `adapters/` installs cleanly via `engine install-adapters` across Antigravity, Claude Code, and OpenCode.
- [ ] All existing Jev unit tests and Decision API schemas pass in `packages/jev`.
- [ ] Root `LICENSE` (MIT) applied.
