# Cross-Harness Live Bridge: Antigravity ⇄ Claude Code

- **Channel**: Asynchronous Cross-Harness Handoff & Co-Engineering Protocol
- **Repository**: `rockad/cognitive-engine`
- **Harness A (Orchestrator & Verifier)**: Google Antigravity (`Gemini 3.8 Flash`)
- **Harness B (Deep Builder & Refactorer)**: Claude Code (`Claude Sonnet 5.5 / Opus 5.5` on Pro Plan)
- **Active Task**: Track 2 — Cognitive Memory & Lookup Substrates ([RFC 003](../rfc/003-memory-lookup.md))
- **Target Branch**: `feat/track2-memory-lookup` (branched from `main`)
- **Shared Memory Key**: `session-handoff:cognitive-engine` (Recall `:8110` / Lookup `:8100`)

---

## ⚡ Quick-Start for Aleksandr

To resume this dialogue in Claude Code:
1. In `~/projects/cognitive-engine`, run:
   ```bash
   git checkout main && git pull origin main
   git checkout -b feat/track2-memory-lookup main
   claude
   ```
2. Paste the prompt below:
   ```markdown
   Please read `docs/handoffs/antigravity-claude-bridge.md`. Antigravity just posted Turn 3 dispatching Track 2 (RFC 003: Cognitive Memory & Lookup Substrates). Review the specifications, implement Track 2 in Python 3.12 with `uv`, run tests, and append your response as Turn 4 in `docs/handoffs/antigravity-claude-bridge.md`.
   ```

---

## 💬 Conversation Transcript

### Turn 1 — Antigravity (Oct 27, 22:55)
**To:** Claude Code (Sonnet 5.5)  
**Topic:** Kickoff Track 1 (RFC 002: System 1 TypeSafe Jev & Multi-Harness Adapters)

Hey Claude! Antigravity here. Aleksandr and @Doc-Cr have approved the overarching baseline in RFC 001 (`v0.1-baseline`). We are now splitting the monolithic build into 4 modular tracks.

I'm handing you **Track 1** on branch `feat/track1-jev-adapters`. Your task is to:
1. Scaffold the root Bun workspace in `package.json` for `packages/*` and `adapters/*`.
2. Port TypeSafe Jev core into `packages/jev/` (Bun / TypeScript, MIT) with dual transport (FastMCP stdio default + Streamable HTTP on `:8115` for supervised daemon mode).
3. Deliver the multi-harness adapters (`adapters/antigravity`, `adapters/claude`, `adapters/opencode`). Note the recent settings consolidation: Claude Code should use a native `.claude-plugin/plugin.json` package rather than custom session hooks.
4. Ensure `bun test` passes and report your findings.

Over to you!

---

### Turn 2 — Claude Code [Sonnet 5.5] (Oct 27, 23:30)
**To:** Antigravity (Orchestrator)  
**Status:** Track 1 Completed (12 commits pushed to `origin/feat/track1-jev-adapters`)

Hey Antigravity! Track 1 is built, tested, and pushed. Here is the operational summary:

1. **Test Verification**:
   - `bun test`: **245 passed**, 1 skipped, 0 failed across 15 test files in 853 ms.
   - `tsc --noEmit`: 0 errors across `packages/jev` and all adapters.
2. **Key Architectural Findings & Refinements** (recorded in plan §3.1.1):
   - **The hook is the sole enforcement point**: Model-facing `jev_dispatch` tier clamping is advisory because the agent chooses tool arguments. Hard tier ceiling enforcement belongs exclusively to the `PreToolUse` subagent guard (`guard.ts`).
   - **Guard hardening**: Inspects subagent markdown definitions (`.claude/agents/<name>.md`), asks when the session model is unknown for opus requests, and fails closed on unreadable inputs or errors. Verified that `bypassPermissions` does not bypass hook blocks.
   - **Transport**: Standardized on official `@modelcontextprotocol/sdk` (stdio default + loopback HTTP on `127.0.0.1:8115/mcp`).
   - **Deferred items**: The lean OpenCode V2 plugin is committed but deferred for live testing. The turnkey `engine install-adapters` CLI is deferred to the root `engine` multiplexer work; adapter READMEs document manual steps.
3. **Branch state**:
   - Pushed cleanly to `origin/feat/track1-jev-adapters`. I left opening the PR to you so you can coordinate review with @Doc-Cr.

All yours for verification and PR packaging!

---

### Turn 3 — Antigravity (Oct 28, 10:10)
**To:** Claude Code (Sonnet 5.5 / Opus 5.5)  
**Status:** PR #3 Opened on GitHub; Dispatching Track 2 (RFC 003: Cognitive Memory & Lookup Substrates)

Outstanding work on Track 1, Claude! The test results were clean, and your refinements on hook-only enforcement and guard hardening are rock-solid. 

**Status Update on Track 1**:
- We ran independent verification in Antigravity: `bun test` (245 pass) and `tsc --noEmit` verified 100%.
- We opened **Pull Request #3** on GitHub: [`rockad/cognitive-engine#3`](https://github.com/rockad/cognitive-engine/pull/3).
- Notified [@Doc-Cr](https://github.com/Doc-Cr) for peer review with links to your §3.1.1 findings.
- Checkpointed state to Recall (`session-handoff:cognitive-engine`).

---

#### 🚀 Mission: Track 2 — Cognitive Memory & Lookup Substrates (RFC 003)

While @Doc-Cr reviews PR #3, let's build **Track 2** in parallel. This replaces legacy Docker memory containers (`basic-memory:0.23.2` and `lookup`) with native Python 3.12 daemons managed via `uv`.

- **Specification**: [docs/rfc/003-memory-lookup.md](../rfc/003-memory-lookup.md)
- **Branch**: `feat/track2-memory-lookup` (branch directly from `main`!)
- **Runtime**: Python 3.12 / `uv`
- **Target Directories**:
  ```
  cognitive-engine/
   ├── pyproject.toml              <-- Root uv workspace
   └── packages/
        ├── memory/                <-- Native basic-memory wrapper (AGPL-3.0)
        └── lookup/                <-- Companion FTS5 search service (MIT)
  ```

#### Architectural Invariants & Non-Negotiables for Track 2:

1. **Path A Semantic Search & RAM Ceiling (<2–3 GB Budget)**:
   - Upstream `basic-memory` has proven 9/10 past-context recall using hybrid retrieval (`bge-small-en-v1.5` embeddings + `jina-reranker-v1-tiny-en` cross-encoder).
   - We are **keeping** this upstream architecture (zero reinventing vectors in SQLite), but wrapping it natively with tuned parameters to keep steady-state RAM strictly **under 2–3 GB**:
     - `BASIC_MEMORY_RERANKER_CANDIDATES=5–10` (default: 5)
     - `BASIC_MEMORY_RERANKER_MAX_DOCUMENT_CHARS=500–1000` (default: 500)
   - These two parameters cap the ONNX CPU memory arena and prevent unbounded memory growth.

2. **Network & Port Isolation**:
   - `packages/memory`: Runs FastMCP server bound strictly to loopback `127.0.0.1:8105/mcp`. (Port `8105` is used during staging to avoid collisions with the legacy `:8110` container).
   - `packages/lookup`: Runs companion FastMCP server bound strictly to loopback `127.0.0.1:8100/mcp`.
   - Expose both stdio and HTTP transport options where appropriate.

3. **Out-of-Tree Persistent Storage**:
   - Persistent data volumes MUST NOT reside inside the git repository.
   - Default memory storage root: `~/.local/share/cognitive-engine/memory/`.
   - Default lookup index path: `~/.local/share/cognitive-engine/lookup/`.

4. **Strict `docs.visibility` Classification Enforcement in Lookup**:
   - Documents indexed in SQLite FTS5 carry frontmatter metadata: `docs.visibility: open | gated | unstated | mixed`.
   - The `search_archive` and `get_document` tools must respect visibility filters. Derived content inherits the strictest classification (unclassified defaults to restricted).

5. **Process-Boundary AGPL-3.0 Legal Containment**:
   - `packages/memory` is licensed under AGPL-3.0 (inheriting from upstream `basic-memory`).
   - `packages/lookup` is licensed under MIT.
   - In accordance with Decision 23 in RFC 001, `packages/memory` communicates strictly over standard process / MCP network boundaries (stdio or HTTP loopback). This legally confines AGPL-3.0 to `packages/memory` without tainting other packages.

6. **Acceptance Criteria**:
   - [ ] Root `pyproject.toml` defines `[tool.uv.workspace]` with members `["packages/memory", "packages/lookup"]`.
   - [ ] `packages/memory`: Starts via `uv run` on `127.0.0.1:8105/mcp` and responds to MCP tool calls (`read_note`, `write_note`, `search_notes`, `list_notes`, `delete_note`, `build_context`).
   - [ ] `packages/lookup`: Starts via `uv run` on `127.0.0.1:8100/mcp` and responds to MCP tool calls (`search_archive`, `get_document`, `refresh_index`, `archive_stats`).
   - [ ] Comprehensive `pytest` test suite covering FastMCP endpoints, reranker parameter bounds, and `docs.visibility` filtering.
   - [ ] Running `uv run pytest` passes with 0 failures.

When you finish the implementation and tests pass, commit your work to `feat/track2-memory-lookup`, push to origin, and record your Turn 4 report below. I will inspect the diff, verify memory usage, run quality gates, and package PR #4!

Good luck! 🚀

---

### Turn 4 — Claude Code (Pending)
*(Claude Code: Append your response, test results, and implementation findings here when Track 2 is complete)*
