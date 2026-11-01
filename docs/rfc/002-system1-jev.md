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
  - Primary: Standard stdio FastMCP transport for zero-latency, direct integration with CLI harnesses (`agy mcp`, Claude Code, OpenCode).
  - Secondary: Streamable HTTP daemon on `127.0.0.1:8115/mcp` when supervised via `engine up`.
- **Multi-Harness Adapters (`adapters/`)**:
  - **Antigravity**: Sidecar integration and MCP registration (`~/.gemini/config/mcp_config.json`).
  - **Claude Code**: Native plugin bundle (`.claude-plugin/plugin.json`) and MCP registration. Hooks are not prohibited; Track 1 ships only the narrow subagent guard (see the Track 1 branch).
  - **OpenCode**: V2 plugin bundle and event interceptors.
  - **CLI Installer**: Unified `engine install-adapters` command configuring all three environments turnkey.

---

## 3. Interfaces & Invariants

- Inherits common invariants from [RFC 001](001-harness2-jev-unified-cognitive-substrate.md):
  - Strictly respects `zdr: strict`, blocking `openrouter/free` offloading when handling restricted content.
  - Enforces subagent tier ceilings (Pro tier strictly requires manual human approval).
  - Preserves `@Doc-Cr` alias routing rules.

---

## 4. Acceptance Criteria

- [ ] `packages/jev` compiles under Bun with 0 TypeScript errors.
- [ ] Stdio and Streamable HTTP transports (`:8115`) functional.
- [ ] `adapters/` installs cleanly via `engine install-adapters` across Antigravity, Claude Code, and OpenCode.
- [ ] All existing Jev unit tests and Decision API schemas pass in `packages/jev`.
- [ ] Root `LICENSE` (MIT) applied.
