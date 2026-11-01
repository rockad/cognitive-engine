# Multi-Track Implementation Plan: Cognitive Engine Substrate & Reasoning

- **Document Type**: Multi-Track Engineering Implementation Plan & Execution DAG
- **Date**: 2026-10-26
- **Authors**: Aleksandr Korolev ([@rockad](https://github.com/rockad)), Antigravity Pair Architect
- **Target Reviewers**: [@Doc-Cr](https://github.com/Doc-Cr), Aleksandr Korolev ([@rockad](https://github.com/rockad))
- **Baseline Architecture**: [RFC 001: Cognitive Engine Architecture](../rfc/001-harness2-jev-unified-cognitive-substrate.md) (`v0.1-baseline` on `main`)
- **Child Track Specifications**:
  - Track 1: [RFC 002: System 1 — TypeSafe Jev Coprocessor & Multi-Harness Adapters](../rfc/002-system1-jev.md)
  - Track 2: [RFC 003: Memory & Lookup Substrates](../rfc/003-memory-lookup.md)
  - Track 3: [RFC 004: System 2 — Google Harness² Recursive Reasoning Optimizer](../rfc/004-system2-harness2.md)
  - Track 4: [RFC 005: Cross-Runtime Egress Gates & agentic-hub Retirement Protocol](../rfc/005-egress-retirement.md)

---

## 1. Executive Summary & Clarification Outcomes

Following the formal `/clarify` architectural review and incorporated feedback from collaborator [@Doc-Cr](https://github.com/Doc-Cr), the Cognitive Engine substrate execution roadmap is partitioned into four decoupled, modular tracks. This multi-track implementation plan defines the dependency graph, technical scopes, feature branching strategy, and GitHub review workflows.

### Core Architectural Decisions & Clarified Outcomes

- **4 Modular Child RFC Tracks (RFCs 002 through 005)**:
  - Decouples large, complex subsystem designs into focused, reviewable specifications.
  - Centralizes shared invariants, licensing boundaries, and monorepo topology in [RFC 001](../rfc/001-harness2-jev-unified-cognitive-substrate.md).
- **Independent Feature Branching Model**:
  - Each track develops on an independent feature branch created directly from `main`:
    - `feat/track1-jev-adapters`
    - `feat/track2-memory-lookup`
    - `feat/track3-harness2`
    - `feat/track4-egress-retirement`
  - Merges directly back to `main` via discrete Pull Requests with automated test verification.
- **Root CLI Multiplexer & Polyglot Workspaces**:
  - Bun workspace (`package.json`) manages TypeScript packages (`packages/jev/`, `adapters/`, `cli/`).
  - Python `uv` workspace (`pyproject.toml`) manages Python 3.12 packages (`packages/memory/`, `packages/lookup/`, `packages/harness2/`).
  - Standalone compiled executable binary (`bin/engine` / Makefile) provides high-level orchestration (`engine up`, `engine status`, `engine memory`, `engine jev`).
- **Substrate-First Parallel Execution DAG**:
  - Phase 1: Tracks 1 and 2 execute concurrently in parallel to lay the foundational System 1 coprocessor and native memory/lookup daemons.
  - Phase 2: Track 3 implements System 2 (Harness² recursive optimizer) on top of the validated System 1 judge and sandbox substrate.
  - Phase 3: Track 4 executes cross-runtime egress testing, privacy audits, consumer rebinding, and gated retirement of legacy `agentic-hub` plugins.

---

## 2. Substrate-First Execution Dependency Graph (DAG)

```mermaid
flowchart TD
    MainBase["RFC 001: Baseline on main (v0.1-baseline)"] --> Phase1

    subgraph Phase1 ["Phase 1: Substrate Foundations (Parallel Execution)"]
        direction LR
        Track1["Track 1 (RFC 002)<br/>Branch: feat/track1-jev-adapters<br/>- packages/jev/<br/>- adapters/<br/>- engine install-adapters"]
        Track2["Track 2 (RFC 003)<br/>Branch: feat/track2-memory-lookup<br/>- packages/memory/ (:8105)<br/>- packages/lookup/ (:8100)<br/>- docs.visibility & RAM tuning"]
    end

    Phase1 --> Merge1["Merge Track 1 & Track 2 PRs to main"]
    Merge1 --> Phase2

    subgraph Phase2 ["Phase 2: System 2 Reasoning Optimizer"]
        Track3["Track 3 (RFC 004)<br/>Branch: feat/track3-harness2<br/>- packages/harness2/<br/>- Local sbx MicroVM Sandboxes<br/>- Decoupled Probe Triggers<br/>- Daily Quota Pre-Flight"]
    end

    Phase2 --> Merge2["Merge Track 3 PR to main"]
    Merge2 --> Phase3

    subgraph Phase3 ["Phase 3: Egress Audits & agentic-hub Retirement"]
        Track4["Track 4 (RFC 005)<br/>Branch: feat/track4-egress-retirement<br/>- Cross-Runtime Egress Gates<br/>- Classification Inheritance<br/>- Consumer Rebinding (doppelganger)<br/>- agentic-hub Gated Split & Tag"]
    end

    Phase3 --> FinalProd["Production Substrate Active (v0.2.0)"]
```

---

## 3. Detailed Breakdown by Track

### 3.1 Track 1 (RFC 002): System 1 — TypeSafe Jev Coprocessor & Multi-Harness Adapters

- **Target Branch**: `feat/track1-jev-adapters`
- **Specification**: [RFC 002: System 1 Coprocessor](../rfc/002-system1-jev.md)
- **Runtime & License**: TypeScript / Bun | MIT
- **Target Directories**: `packages/jev/`, `adapters/`, `cli/`
- **Key Deliverables**:
  - **TypeSafe Jev Core (`packages/jev`)**:
    - Sub-second probabilistic model tier dispatch (`jev_dispatch`, `jev_route`).
    - Binary quality gatekeeping and diff acceptance (`jev_judge`).
    - Free-tier offloading coprocessor (`jev_execute_free`, `jev_agent_free`) saving Gemini/Claude quota.
    - Model tier ceiling governance (clamping subagents to parent tier; strict user gate for Pro tier).
  - **Dual-Mode Transport**:
    - Default: stdio transport (official TypeScript SDK; FastMCP is a Python library) for zero-latency direct integration with CLI harnesses. Tool arguments are strictly validated per [§3.5](#35-cross-cutting-mcp-selection--server-contract-2026-10-28).
    - Supervised Daemon: Streamable HTTP transport on `127.0.0.1:8115/mcp` managed via `engine up`.
  - **Multi-Harness Adapters (`adapters/`)**:
    - Google Antigravity sidecar integration and MCP registration (`~/.gemini/config/mcp_config.json`).
    - Claude Code native plugin package (`.claude-plugin/plugin.json`) and MCP registration. Hooks are not prohibited: Track 1 ships only the narrow subagent guard, and others may be added where a workflow needs one.
    - OpenCode V2 plugin bundle and event interceptors.
    - Turnkey installer command: `engine install-adapters`.
- **Invariants & Gates**:
  - Blocks `openrouter/free` offloading when handling private workspaces or under `zdr: strict`.
  - Enforces `@Doc-Cr` alias routing.

---

### 3.2 Track 2 (RFC 003): Memory & Lookup Substrates

- **Target Branch**: `feat/track2-memory-lookup`
- **Specification**: [RFC 003: Memory & Lookup Substrates](../rfc/003-memory-lookup.md)
- **Runtime & License**: Python 3.12 / uv | AGPL-3.0 (`packages/memory`) + MIT (`packages/lookup`)
- **Target Directories**: `packages/memory/`, `packages/lookup/`
- **Key Deliverables**:
  - **`packages/memory` (Native Upstream `basic-memory`, AGPL-3.0)**:
    - Runs upstream-maintained `basic-memory` package natively in Python via `uv` without Docker.
    - Binds strictly to `127.0.0.1:8105/mcp` during staging to avoid port conflicts with legacy daemons.
    - Exposes upstream FastMCP tools (`read_note`, `write_note`, `search_notes`, `list_directory`, `delete_note`, `build_context`).
    - Tuned hybrid retrieval parameters:
      - `BASIC_MEMORY_RERANKER_CANDIDATES=5–10`
      - `BASIC_MEMORY_RERANKER_MAX_DOCUMENT_CHARS=500–1000`
      - Bounded resident memory strictly under **2–3 GB RAM** while maintaining 9/10 context accuracy.
    - Out-of-tree persistent storage at `~/.local/share/cognitive-engine/memory/`.
  - **`packages/lookup` (Companion Archive & Transcript Search, MIT)**:
    - Runs natively on `127.0.0.1:8100/mcp`.
    - Preserves the 4 standard Lookup tools (`search_archive`, `get_document`, `refresh_index`, `archive_stats`).
    - Full-text SQLite FTS5 search indexing session transcripts and external knowledge archives.
    - Enforces `docs.visibility` classification (`open`, `gated`, `unstated`, `mixed`) for strict classification inheritance.
  - **Process-Boundary AGPL Containment**:
    - `packages/memory` communicates strictly across standard process/MCP loopback boundaries, cleanly confining AGPL-3.0 to its subpackage.

#### 3.2.1 Track 2 implementation findings & refinements (2026-10-28)

Full list with evidence in [RFC 003 §4](../rfc/003-memory-lookup.md). The ones that change the plan:

- **`packages/memory` is a launcher, not a fork.** It validates settings and `exec`s the pinned upstream `basic-memory mcp`. The reranker is off by default upstream, so the tuned configuration only exists because the launcher forces it; out-of-range bounds are refused, not clamped.
- **`packages/lookup` is a port of the Docker-era Lookup**, with the transport replaced by FastMCP and the Docker-only SMB mounting removed. Python 3.12 required replacing a 3.13+ stdlib call.
- **Decided follow-up — shares declared in the manifest** (branch `feat/lookup-smb-sources`, cut from `feat/track2-memory-lookup`; not started, awaiting go). Native Lookup cannot mount network shares, so a manifest `[source.volume]` is indexed through a userspace SMB client with no host mount, root or FUSE. Decisions:
  - **Access layer:** a full filesystem seam (`walk`, `stat`, `open`) with `LocalFS` and `SmbFS` backends; manifest, pipeline, hashing and every extractor call it. The engine's `Path` becomes a small `VPath` that carries its filesystem, replacing `type(path)(d["path"])` with a lexical `src.contains_logical()`. The file digest comes from the same read the extractor performs.
  - **Path model:** `root` stays the *logical* mount path that is stored and shown (hits are marked `REMOTE`); an optional `subpath` selects a directory inside the share. Existing manifests and any migrated index keep working.
  - **Dependency:** `smbprotocol` (MIT, pure Python) is a core dependency of `cognitive-lookup`, imported lazily. **NTLM only**: Kerberos needs system krb5 libraries, which is the host setup this feature removes.
  - **Reused as-is:** the `[source.volume]` schema and `${ENV}` credential references (never inline values); an unreachable share still lets the daemon start, with rows kept and marked stale; reparse points are not followed and every document path is re-checked against its root.
  - **Streams** (S-prefixed to avoid clashing with the matrix): S1 introduce the seam with `LocalFS` only, a pure refactor that keeps all existing Lookup tests green unedited and blocks the rest; S2 `SmbFS`; S3 manifest schema (`subpath`, `type` accepts `cifs`/`smb`) and loud errors on unset credential references; S4 tests (an in-memory `FakeFS`, the ledger/escape/visibility tests parametrized over both backends, one in-process SMB server integration test); S5 docs.
  - **Acceptance:** a second pass over an unchanged share performs zero file opens; the existing core `[source.volume]` block indexes from an SMB server with no host mount; `..`, absolute paths and reparse points are refused and counted; no credential appears in any log or error string; and the server passes the §3.5 conformance runner.
- **Port policy (resolves a contradiction in RFCs 001/003/005):** the legacy `lookup-lookup-1` container holds `127.0.0.1:8100` and serves the network-share sources that native Lookup cannot read yet, and the zero-retirement policy forbids stopping it first. So native Lookup **stages on `:8101`** (`lookup mcp --http --port 8101`) and rebinds to `:8100` at Track 4 cutover, after the legacy container is stopped and per-source document-count parity is verified.

---

### 3.3 Track 3 (RFC 004): System 2 — Google Harness² Recursive Reasoning Optimizer

- **Target Branch**: `feat/track3-harness2`
- **Specification**: [RFC 004: System 2 Reasoning Optimizer](../rfc/004-system2-harness2.md)
- **Runtime & License**: Python 3.12 / uv | Apache-2.0
- **Target Directories**: `packages/harness2/`
- **Key Deliverables**:
  - **Task-Level Recursion (*Propose–Probe–Compose*)**:
    - Proposes candidate prompt/tool/context mutations across allowed dimensions without ground truth.
    - Behavioral contrast composer synthesizes candidate outputs into human-reviewable proposals.
  - **Strict Local MicroVM Execution (`sbx`)**:
    - Parallel candidate execution runs exclusively inside local Docker Sandboxes via standalone `sbx` CLI.
    - Zero cloud sandboxing; zero off-host code or credential movement.
  - **Fail-Closed Sandbox Fallback**:
    - If the `sbx` runtime is unavailable, candidate probing fails closed immediately.
    - Optional `mkdtemp` worktree mode generates diffs for inspection only and never executes untrusted code.
  - **Decoupled Probe Triggers**:
    - Probing is completely decoupled from routing confidence metrics.
    - Fires strictly on:
      - Post-task verification failure (`jev_judge` returns `FAIL` or score < 0.50).
      - Execution retry limit exhausted in the agent loop.
      - Explicit on-demand request (`/probe <task>` or `engine probe`).
  - **Daily Quota Pre-Flight Check**:
    - Pre-flights `free_model_daily_requests` from `GET /api/v1/key` before triggering candidate sweeps.
    - Throttles or skips sweeps when OpenRouter daily quota is constrained.
  - **Protected Invariant Set**:
    - Guardrails, Egress/ZDR policies, Session ceilings, Lifecycle hooks, and the human gate on composer are immune to mutation.
  - **Continual Experience Learning**:
    - Proposes reflection entries into out-of-tree domain libraries at `~/.local/share/cognitive-engine/experiences/<domain>/`.
    - Requires human sign-off before committing to permanent storage.

---

### 3.4 Track 4 (RFC 005): Cross-Runtime Egress Gates & agentic-hub Retirement Protocol

- **Target Branch**: `feat/track4-egress-retirement`
- **Specification**: [RFC 005: Egress & Retirement Protocol](../rfc/005-egress-retirement.md)
- **Runtime & License**: Polyglot (TypeScript / Python / Shell) | MIT
- **Target Directories**: Cross-repository (`cognitive-engine/`, `agentic-hub/`, `doppelganger/`)
- **Key Deliverables**:
  - **Cross-Runtime Egress Firewall**:
    - Unified outbound egress and ZDR policy validation enforced symmetrically across TypeScript (`packages/jev`) and Python (`packages/harness2`, `packages/lookup`).
    - Classification inheritance: Content derived from memory or transcripts inherits the strictest source classification; unclassified content defaults to restricted.
  - **Pre-Retirement Verification Gates**:
    - All unit, integration, schema, and latency test suites pass across Tracks 1, 2, and 3.
    - Memory footprint stays verified under 2–3 GB RAM under sustained query load.
    - Dual port binding verified on `127.0.0.1` (`:8105`, and Lookup on `:8101` staging, `:8100` after cutover).
    - Share-source parity: native Lookup indexes the same network-share sources as the legacy container before it is stopped.
    - MCP server contract verified on every daemon (`:8105`, `:8101`, `:8115`, Harness²) by the black-box conformance runner ([§3.5](#35-cross-cutting-mcp-selection--server-contract-2026-10-28)).
    - Local `sbx` probe isolation and fail-closed behavior verified.
    - Client configuration updated in `~/.gemini/config/mcp_config.json`.
  - **Phased agentic-hub Retirement Protocol**:
    - Freeze and deprecation notices added to `agentic-hub/plugins/jev/` and `agentic-hub/plugins/memory/`.
    - Consumer rebinding in `doppelganger` (`.mcp.json`, shell wrappers `jev-mode`, `memory.sh`).
    - Surgical removal of retired plugins from `agentic-hub`.
    - Reposition `agentic-hub` as the peripheral-only registry (`chrome-devtools`, `gitea`, `google-workspace`, `local-transcribe`).
    - Tag release `v2.4.0-split` in `agentic-hub`.

---

### 3.5 Cross-cutting: MCP selection & server contract (2026-10-28)

Decided with Aleksandr via `/clarify`, after a FastMCP vs official-SDK spike on Lookup ([RFC 003 §4](../rfc/003-memory-lookup.md), items 10–11) and a check of the TypeScript SDK 1.31.

| # | Decision |
|---|---|
| 1 | **Python servers use FastMCP**: Memory (upstream, pinned `fastmcp==4.1.3`), Lookup, and Harness² (RFC 004 §2.7). The official `MCPServer` was rejected because it silently ignores unknown arguments. |
| 2 | **Jev (TypeScript) moves to `McpServer.registerTool` with `z.object(...).strict()`** (typed handlers, SDK-generated JSON Schema, SDK validation). Chosen over a smaller `ajv` wrapper to reach the typed end state. It is larger and reshapes the registry tests, so it ships as its own commits on the Track 1 branch. |
| 3 | **One black-box conformance runner** (`tools/mcp_conformance.py`) enforces the contract below on every server, in both languages. Per-package tests remain as the fast inner loop. |

**Why this exists.** Two defects shipped because nothing enforced a shared contract: Lookup's HTTP daemon accepted a forged `Host` and a foreign `Origin`, and Jev accepts any tool arguments. Jev's handlers cast `args as {...}` and the low-level `Server` does not validate against `inputSchema`, so a typo'd, mistyped or missing argument is accepted, including on the `jev_execute_free*` tools that decide egress.

**The contract (every MCP daemon in this repo):**

1. Binds `127.0.0.1` only. There is no host option.
2. Refuses a forged `Host` (421/403) and a foreign `Origin` (403). `127.0.0.1` and `localhost` work.
3. Strict arguments: an unknown key, bad enum value, wrong type or missing required argument returns a tool error. Never silently ignored, never defaulted.
4. Stateless HTTP: no `Mcp-Session-Id`.
5. Liveness is `tools/list` or a TCP connect. `ping` was removed from the protocol (2026-07-28) and is not part of the contract.
6. The advertised `inputSchema` and the enforced validation come from one definition.

**Per-track consequences:**

- **Track 1 (Jev).** Migrate all 12 tools to `registerTool` with strict zod objects, via a small `defineTool()` helper that always applies `.strict()` (the raw-shape form silently drops unknown keys). Add the missing `Origin` check to the hand-written HTTP handler. Reshape the registry tests (`allTools` / `toolHandlers`) to read from the server; keep the `approvedTier`-absent assertion. RFC 002 acceptance gains "strict arguments" and "Origin refused" boxes on that branch.
- **Track 2.** Lookup already passes `host_origin_protection="auto"` and has tests pinning forged Host/Origin and misspelled-argument rejection; Memory's tests pin Host/Origin. Both are the runner's first two targets.
- **Track 3 (Harness²).** FastMCP, `host_origin_protection="auto"` from the first commit, and the runner in its tests.
- **Track 4.** The "client MCP configurations validated" gate runs the conformance runner against `:8105`, Lookup (`:8101` staging, `:8100` after cutover), `:8115` and Harness² before any `agentic-hub` retirement.

**The runner.** Python, official client plus `httpx`, runnable as `uv run tools/mcp_conformance.py <url | stdio command> --probe <spec>`. Each server supplies a small probe spec: one valid call, plus the argument and enum it should reject. It fails closed: a check it cannot perform is a failure, not a skip. Each package's test suite runs it against a daemon it starts on a free port.

**Sequencing.**

| Stream | Scope | Description | Role | Tier |
|---|---|---|---|---|
| WS5 | `tools/mcp_conformance.py`, `tools/probes/*.toml`, tests | Runner and probe specs; run against Memory and Lookup first | `qa_engineer` | `flash` |
| WS6 | `packages/jev/src/tools/*`, `packages/jev/src/server.ts`, tests | Strict `registerTool` migration plus `Origin` check; reshape registry tests | `code_engineer` | `flash` |
| WS7 | `plans/`, RFC 002 and 005 acceptance | Add the new acceptance boxes on their owning branches | `ghostwriter` | `openrouter_free` |

WS5 and WS6 are independent. **WS6 is done and committed on `feat/track1-jev-adapters` as part of PR #3** (decided by Aleksandr): `defineTool()` + `McpServer.registerTool` with strict zod for all 12 tools, the `Origin` check, a contract test that drives every tool from the registry, and RFC 002 updated; 307 tests pass (was 245). It was pushed and PR #3 is updated. **WS5 is done on `feat/mcp-conformance`** (from `feat/track2-memory-lookup`): `tools/mcp_conformance.py` (one file, inline dependencies, runs against a URL or a stdio command) with probe specs in `tools/probes/` for Memory, Lookup and Jev. It checks 12 things, fails closed (a check it cannot perform is `UNVERIFIABLE` and fails the run), requires a negative probe's unmodified call to be accepted so a rejection proves validation, and prints any documented `[waive]` exception loudly. Verified: Memory 12/12, Lookup 12/12, Jev on PR #3 12/12. Against the pre-WS6 Jev commit it fails five checks (foreign Origin, unknown argument, bad enum, argument-less tool, open schemas), and toy servers reproducing the Lookup Host/Origin bug and the silent-unknown-argument default are caught. The Jev test runs when `COGNITIVE_ENGINE_JEV_CHECKOUT` points at the Track 1 branch. Remaining: Harness² gets a probe when it exists, and Track 4's Gate 5 invokes the runner.

---

## 4. Workstream Summary & Execution Matrix

| Stream | Track | Branch | Scope / File Targets | Description | Lead Subagent | Recommended Tier |
|---|---|---|---|---|---|---|
| **WS1** | **Track 1** | `feat/track1-jev-adapters` | `packages/jev/`, `adapters/`, `cli/` | Migrate Jev core, stdio/HTTP transports, and Antigravity / Claude / OpenCode adapters | `code-engineer` | `flash_lite` |
| **WS2** | **Track 2** | `feat/track2-memory-lookup` | `packages/memory/`, `packages/lookup/` | Package native `basic-memory` on `:8105` and companion `lookup` on `:8100` with `docs.visibility` | `code-engineer` | `flash_lite` |
| **WS3** | **Track 3** | `feat/track3-harness2` | `packages/harness2/` | Package Harness² optimizer, local `sbx` microVMs, fail-closed prober, and daily quota pre-flight | `code-engineer` | `flash_lite` |
| **WS4** | **Track 4** | `feat/track4-egress-retirement` | `cognitive-engine/`, `doppelganger/`, `agentic-hub/` | Cross-runtime egress gates, consumer rebinding, and gated surgical retirement of legacy plugins | `system-operator` | `flash_lite` |
| **WS5** | **Cross-cutting** | `feat/mcp-conformance` (from `feat/track2-memory-lookup`) | `tools/` | Black-box MCP conformance runner and per-server probe specs ([§3.5](#35-cross-cutting-mcp-selection--server-contract-2026-10-28)) | `qa-engineer` | `flash` |
| **WS6** | **Track 1 follow-up** | `feat/track1-jev-adapters` | `packages/jev/` | Strict `registerTool` migration for all 12 tools plus the `Origin` check ([§3.5](#35-cross-cutting-mcp-selection--server-contract-2026-10-28)) | `code-engineer` | `flash` |

---

## 5. Review & PR Protocol for Collaboration with @Doc-Cr

To ensure seamless peer review with collaborator [@Doc-Cr](https://github.com/Doc-Cr) on GitHub:

1. **Branch Hygiene**:
   - Feature branches must branch directly from `origin/main` (`git checkout -b feat/trackX-... origin/main`).
   - Keep commits atomic and semantic (`feat:`, `fix:`, `refactor:`, `docs:`, `test:`).
2. **Pull Request Protocol**:
   - Open PRs against `main` on GitHub (`rockad/cognitive-engine`).
   - Title format: `feat(trackX): <short summary> [RFC 00X]` (e.g. `feat(track1): migrate typesafe jev and multi-harness adapters [RFC 002]`).
   - Mention `@Doc-Cr` in the PR description with:
     - Reference to the corresponding child RFC and implementation plan section.
     - Summary of changes and test execution output.
     - Specific architectural verification questions.
3. **Automated Verification Gates**:
   - Prior to requesting review or merging, ensure local tests pass:
     - Bun workspace: `bun test` (0 errors).
     - Python workspace: `uv run pytest` (0 errors).
     - Egress test suite: `bun run test:egress` and `uv run pytest tests/test_egress.py`.
4. **Merge Sequencing**:
   - Track 1 and Track 2 PRs can be reviewed and merged independently in Phase 1.
   - Track 3 merges after Phase 1 is validated on `main`.
   - Track 4 executes as the final cutover PR.

---

## 6. Dual-Harness Cross-Delegation (Antigravity + Claude Code Pro)

To maximize development velocity and conserve Gemini Pro quota, implementation leverages a **Dual-Harness Architecture** combining **Claude Code (Sonnet 5.5 & Opus 5.5 via prepaid Pro subscription)** and **Google Antigravity (Flash/Medium Orchestrator)**.

### 6.1 Division of Responsibilities

| Subsystem / Concern | Primary Harness | Model Tier | Rationale |
|---|---|---|---|
| **Deep Code Implementation** (`packages/jev`, `packages/harness2`, adapters) | **Claude Code** | Claude Sonnet 5.5 or Opus 5.5 | High-fidelity multi-file coding, algorithmic prompt synthesis; burns **zero Gemini quota** (prepaid Claude Pro). |
| **Architectural Governance & Pre-Flight Dispatch** | **Antigravity** | Gemini 3.8 Flash | Session orchestrator, multi-track dependency tracking, invariant enforcement. |
| **Verification Gates & Quality Acceptance** | **Antigravity** | Gemini 3.8 Flash + `jev_judge` | Runs test suites (`bun test`, `uv run pytest`), diff validation, and binary quality gating. |
| **Git, PRs & Review Coordination with @Doc-Cr** | **Antigravity** | Gemini 3.8 Flash | Branch checkout, semantic commits, PR management, and GitHub review updates. |
| **Session State & Cross-Harness Continuity** | **Unified Memory** | Recall (:8110) & Lookup (:8100) | Synchronizes state via `session-handoff:cognitive-engine` across both harnesses. |

### 6.2 Operating Modes

#### Mode A: Concurrent Track Ownership (Parallel Branches)
- **Claude Code**: Implements **Track 1** (`feat/track1-jev-adapters` in Bun/TS).
- **Antigravity**: Implements **Track 2** (`feat/track2-memory-lookup` in Python/uv).
- **Independence**: Both run simultaneously on disjoint feature branches without merge conflicts or context collisions.

#### Mode B: Dual-Harness Ping-Pong (Implement in Claude Code, Verify in Antigravity)
1. **Briefing Phase (Antigravity)**:
   - Antigravity scaffolds a self-contained, clipboard-ready **Harness Prompt Brief** specifying:
     - Target files and exact method/type signatures.
     - RFC invariants and constraint bounds.
     - Verification command lines (`bun test`, `pytest`).
   - Updates `session-handoff:cognitive-engine` in Recall / disk cache.
2. **Coding Phase (Claude Code)**:
   - Aleksandr launches Claude Code in `~/projects/cognitive-engine` on the target branch.
   - Pastes the briefing prompt; Claude Code (Sonnet/Opus) writes the implementation, refactors, and runs local checks.
3. **Verification & Quality Gate Phase (Antigravity)**:
   - Aleksandr returns to Antigravity.
   - Antigravity inspects `git status` and `git diff`.
   - Runs test suites (`engine test`, `bun test`, `uv run pytest`).
   - Runs `jev_judge` quality gate on the diff.
   - Verifies security invariants (ZDR rules, AGPL process boundaries, standalone `sbx` isolation).
   - Generates semantic commits and pushes the branch for [@Doc-Cr](https://github.com/Doc-Cr) review.

### 6.3 Context & Session Sharing Protocol
- **Shared Repository Disk State**: Both harnesses operate directly on `~/projects/cognitive-engine`.
- **Clipboard-Ready Prompt Templates**: Antigravity formats self-contained markdown prompts with embedded schemas so Claude Code has complete context upon session start.
- **Cross-Harness Memory**: Both harnesses query `session-handoff:cognitive-engine` and search indexed session logs via `lookup` on `127.0.0.1:8100`.

### 6.4 Dynamic Workflows & Jev Integration (Follow-Up Plan)

When using dynamic multi-agent workflows in Claude Code, Jev participates in the workflow loop to judge subagent suitability, recommend model tiers, and gate quality.
- **Overshadowing Analysis & Seam Design**: Dedicated follow-up architecture plan established in [docs/plans/2026-10-28-claude-workflows-jev-integration-plan.md](2026-10-28-claude-workflows-jev-integration-plan.md).
- **Guiding Invariant**: Jev operates as an **asynchronous advisory coprocessor** (providing probabilistic sizing, free-tier offloading, and milestone `jev_judge` gates) rather than a rigid synchronous execution interceptor, preserving Claude Code's emergent workflow adaptation and avoiding latency amplification or context pollution.
