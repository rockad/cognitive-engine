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
  - Exposes upstream FastMCP tools (`read_note`, `write_note`, `search_notes`, `list_directory`, `delete_note`, `build_context`).
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

- [x] `packages/memory` starts natively via `uv run` on `127.0.0.1:8105` with zero Docker dependencies.
- [x] `packages/lookup` starts natively via `uv run` on `127.0.0.1:8100`. *(Default is 8100; verified end to end on a free port because the legacy Docker Lookup still holds 8100 — see §4.)*
- [x] Both services respond to tool calls. *(Not to MCP `ping`: both stateless HTTP servers return `Method not found`. Liveness is `tools/list` — see §4.)*
- [x] Memory footprint stabilizes under 2–3 GB RAM during sustained searches. *(728 MiB RSS after 40 searches with the reranker on.)*
- [x] `docs.visibility` classification correctly tags and filters restricted documents.

---

## 4. Implementation Findings (Track 2, 2026-10-28)

Corrections to this RFC's assumptions, found by running the code:

1. **Upstream `basic-memory` 0.24.1 does not default to the tuned configuration.** The reranker is *off* by default, `reranker_candidates` defaults to 20 and `reranker_max_document_chars` to 2000 (not 5 and 500), and `auto_update` is on. A bare `basic-memory mcp` therefore runs plain hybrid retrieval. `packages/memory` pins reranker on, 10 candidates, 1000 chars and `auto_update` off, and rejects values outside 5–10 and 500–1000 rather than clamping them.
2. **`list_notes` does not exist upstream.** The equivalent is `list_directory`. The tool list above is corrected.
3. **Upstream writes state to `$HOME` on any invocation**, including `--help` (`~/.basic-memory`, `~/basic-memory`), and its dependencies write more (onnxruntime's device-id database, Hugging Face logs, fastmcp's version-check cache). The launcher redirects `BASIC_MEMORY_CONFIG_DIR`, `BASIC_MEMORY_HOME` and both XDG dirs under the memory root and turns the fastmcp update check off; a test runs the daemon under a fake `$HOME` and asserts it stays empty.
4. **Neither server answers MCP `ping`, and that is the protocol, not a quirk.** The official SDK (mcp 2.3.0) warns "ping is removed as of 2026-07-28; the method only works under mode='legacy'", so the "responds to ping" criterion cannot be met by a current server. Use `tools/list` or a TCP connect for liveness. (An earlier version of this note blamed stateless HTTP; that was wrong.)
5. **Port 8100 is occupied** by the legacy Docker Lookup (`lookup-lookup-1`, `127.0.0.1:8100`) for as long as it runs. Memory's staging port (8105) avoids the legacy `:8110` container; Lookup's spec port does not avoid its legacy twin. The new daemon fails to bind and exits until the container is stopped or `--port` is given.
6. **Lookup's legacy code was 3.14-only** (`PurePath.full_match` is 3.13+). `lookup.manifest.full_match` replaces it and is checked against the stdlib on 304 pattern/path pairs.
7. **Network shares are no longer mounted by Lookup.** The Docker version mounted SMB shares from inside its container (`shares.py`, `lookup mounts`, `CAP_SYS_ADMIN`). Native Lookup reads only roots that already resolve on the host; a manifest `[source.volume]` is metadata, and an absent root that declares one is reported as `UNMOUNTED`. Declaring shares in the manifest and indexing them with no host setup is a follow-up (see the bridge document, Turn 4).
8. **A carried-over filter bug was fixed.** `search_archive(visibility="unstated")` did not return a document that states nothing inside a `mixed` source, although the hit is labelled `unstated`. The filter now resolves through the same `effective_visibility` as the label.
9. **An unrecognised `visibility` filter is now a tool error** rather than silently answering unfiltered.
10. **Lookup's HTTP daemon had no DNS-rebinding guard, and it is fixed.** FastMCP's `host_origin_protection` is off unless asked for; the first version accepted a forged `Host` and a foreign `Origin` (HTTP 200) on a loopback server that returns `gated` documents. It now passes `host_origin_protection="auto"`: forged Host gets 421, foreign Origin gets 403, and `127.0.0.1` / `localhost` still work. Tests pin it for both daemons (Memory's upstream already passes the same value).
11. **FastMCP vs the official SDK was spiked; Lookup stays on FastMCP.** FastMCP 4 sits on the official `mcp` SDK, which is installed regardless (`basic-memory` requires it). Behind the same four handlers the two are equivalent for Lookup: identical tools and results, loopback-only, stateless, both reject out-of-range arguments as tool errors. The official `MCPServer` is slightly lighter (87 vs 98 MiB idle, 660 vs 813 ms import, 33 vs 39 lines to register the tools) and refuses forged Host/Origin by default. But it **silently ignores unknown arguments**: a misspelled `visiblity="open"` returned `gated` documents with no error, which is the failure this server exists to prevent. FastMCP rejects them. Each library has one unsafe default; FastMCP's is a one-argument fix, the official server's needs custom validation. A test now pins the strict-argument property so a future library swap cannot lose it.
