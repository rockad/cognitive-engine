# RFC 003: Track 2 — Cognitive Memory & Lookup Substrates

- **RFC Number**: 003
- **Track**: Track 2
- **Status**: Proposed  
- **Runtime**: Python 3.12 / uv  
- **License**: AGPL-3.0 (`packages/memory`) + MIT (`packages/lookup`)  
- **Target Subsystem**: `packages/memory/`, `packages/lookup/`  
- **Target Branch**: `feat/track2-memory-lookup`
- **Parent Blueprint**: [RFC 001: Cognitive Engine Architecture](001-harness2-jev-unified-cognitive-substrate.md)
- **Implementation Plan**: [Multi-Track Implementation Plan](../plans/2026-10-26-multi-track-implementation-plan.md)
- **Sibling Track Specifications**:
  - Track 1: [RFC 002: System 1 — TypeSafe Jev Coprocessor & Multi-Harness Adapters](002-system1-jev.md)
  - Track 3: [RFC 004: System 2 — Google Harness² Recursive Reasoning Optimizer](004-system2-harness2.md)
  - Track 4: [RFC 005: Cross-Runtime Egress Gates & agentic-hub Retirement Protocol](005-egress-retirement.md)

---

## 1. Summary & Motivation

Establishes Track 2 of Cognitive Engine: packaging the memory and lookup substrates natively in Python via `uv` without Docker containers. Preserves full hybrid semantic search (`bge-small` + `jina-reranker`), transcript FTS5 indexing, and `docs.visibility` classification.

---

## 2. Technical Scope

- **`packages/memory` (Native `basic-memory`, AGPL-3.0)**:
  - Runs upstream `basic-memory` package natively on `127.0.0.1:8105/mcp`.
  - Exposes upstream FastMCP tools (`read_note`, `write_note`, `search_notes`, `list_notes`, `delete_note`, `build_context`).
  - Tuned reranker configuration (`BASIC_MEMORY_RERANKER_CANDIDATES=5–10`, `BASIC_MEMORY_RERANKER_MAX_DOCUMENT_CHARS=500–1000`) keeping steady-state RAM comfortably under **2–3 GB**.
  - State volume housed out-of-tree at `~/.local/share/cognitive-engine/memory/`.
- **`packages/lookup` (Companion Document & Transcript Search, MIT)**:
  - Runs companion search service natively on `127.0.0.1:8100/mcp`.
  - Exposes the 4 Lookup tools (`search_archive`, `get_document`, `refresh_index`, `archive_stats`).
  - Full-text SQLite FTS5 search indexing session transcripts and external knowledge archives.
  - Enforces `docs.visibility` (`open`, `gated`, `unstated`, `mixed`) for strict classification inheritance.
- **Process-Boundary AGPL Containment**:
  - `packages/memory` communicates strictly across standard process/MCP boundaries, cleanly confining AGPL-3.0.

---

## 3. Acceptance Criteria

- [ ] `packages/memory` starts natively via `uv run` on `127.0.0.1:8105` with zero Docker dependencies.
- [ ] `packages/lookup` starts natively via `uv run` on `127.0.0.1:8100`.
- [ ] Both services respond to FastMCP ping and tool calls.
- [ ] Memory footprint stabilizes under 2–3 GB RAM during sustained searches.
- [ ] `docs.visibility` classification correctly tags and filters restricted documents.
