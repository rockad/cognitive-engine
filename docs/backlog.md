> **Moved from `rockad/agentic-hub` on 2026-11-10**, scrubbed of machine-specific details. Current state: [`plans/2026-11-10-delivery-plan.md`](plans/2026-11-10-delivery-plan.md). Links to `plugins/...` refer to the `agentic-hub` repository.

# Backlog: tools to evaluate and actions from reviews

This file replaces the old root `TODO.md`. It holds two things:

- tools still waiting for an evaluation or a decision
- actions that came out of a review and have not landed yet

Finished evaluations move to a review document under `docs/`, with their evidence in `docs/research/`. Keep each item to a few lines, and link to where the reasoning lives.

Status markers:

- `[ ]` open
- `[~]` in progress
- `[x]` done (keep for one review cycle, then delete)
- `[?]` waiting on a decision by the user

## Waiting on a decision

From [adoption review 2026-11](adoption-review-2026-11.md), section 9.

- [?] **basic-memory 1.0.0 migration scope** (2026-11-10). The migration plan targets the legacy `plugins/memory` stack (phases 2 to 5: `memory.sh` dual-compatibility, skill docs, volume snapshots, cutover), but RFC 005 retires that stack and cognitive-engine's native `packages/memory` (RFC 003) is the successor. Recommendation: do only the cheap, live-consumer part (phase 2, `memory.sh` detecting `bm_read` versus `read_note`) and the skill docs now; keep both stacks on 0.24.1; make the 1.0.0 cutover one decision, taken once for the native `packages/memory` at Track 4 cutover (it owns the same `recall` tool names), not twice. Flips if you want 1.0.0's features in the live stack before cutover.

- [x] **Port 8120 owner.** Decided 2026-11-08: Harness² moves to 8125; coordination keeps 8120; 8121 stays free for the S3 listener. Record it in the coordination docs, and change the default in cognitive-engine #5 when it is restacked. See [pr-plan-2026-11-08.md](pr-plan-2026-11-08.md).
- [x] **Handle grammar.** Decided 2026-11-09 as `name#sid8`; **revised 2026-11-10** by the owner in the coordination session to `card/name~sid8` (`~` separator, `sid8` always present; `#` is out because it starts a URL fragment). Unnamed-session form and imported author handles stay open in the coordination docs.
- [x] **R8 (8121 listener).** Approved 2026-11-09: the 8121 listener binds `127.0.0.1` from stage s2 (task S2-LISTEN). The tunnel route is the exposure gate, and no route exists before the S3 gate. The AGENTS.md rule changes in the same commit (by agentic-hub-cp).
- [x] **Lookup staging port.** Decided 2026-11-09: native lookup staging is 8101 on cognitive-engine #8. Recorded in the port map in [`port-map.md`](port-map.md).
- [?] **Backlog approach.** Build "S1b" (a ready-work backlog inside the A2A server) directly, or first trial beads (stealth mode) or Kanzo (stdio only) for two weeks.
- [?] **Third coordination peer.** Codex or OpenCode needs a ChatGPT plan; OpenCode or Copilot CLI needs a Copilot plan. Without one, stay at Claude Code plus agy.
- [?] **`codex queue` stub wake for Codex.** Is it allowed under the "no external driver" stance?
- [?] **DDR-003a.** Extend never-launch, never-drive to Google-account-authenticated Gemini CLI?
- [x] **RFC 005 scope.** Decided 2026-11-08, refined 2026-11-09: agentic-hub keeps coordination and subagents; `memory` retires; `jev` is not retired (decision 15 in pr-plan-2026-11-08.md). **Revised 2026-11-12:** the live line stays in the agentic-hub plugin and `packages/jev` is the frozen reference (delivery plan, decisions 14, 15, 19); the earlier "relocates to cognitive-engine" wording is superseded. The edit rides on a cognitive-engine docs PR (#7 or #11).
- [?] **cognitive-engine roles.** Replace the fixed Orchestrator and Builder roles with symmetric peers once the bridge moves onto A2A?
- [?] **Blocked research hosts.** Allow these in the environment's network settings to re-check the remaining unverified points:
  - `antigravity.google`
  - `opencode.ai`
  - `github.blog`
  - `docs.ntfy.sh`
  - `geminicli.com`
  - `discuss.ai.google.dev`

## Order of work

**Coordination (`plugins/coordination`, S0 close and S1) comes before the cognitive-engine restacks.** The full sequence, decisions and workstreams are in [pr-plan-2026-11-08.md](pr-plan-2026-11-08.md). Decided 2026-11-09 (sequencing):

- independent cognitive-engine items run alongside coordination S1 (see "Next: alongside coordination S1")
- restacks of #5, #7, #8 and their children, moving the bridge onto coordination, and the #7 / #11 docs PR with the RFC 005 amendment wait for S1 (see "After coordination S1")

Plan Phase 0 items 1 and 5 are done (agentic-hub PR #19, below). `agentic-hub-cp` (another Claude session) owns `plugins/coordination/docs`: re-read those files and coordinate with it before touching them.

## Now: documentation and small fixes

### ax

- [ ] Make `ensure_hook_installed` safe. **Prerequisite for coordination's agy hooks**, which live in the file it can wipe:
  - refuse invalid JSON instead of starting from `{}`
  - back up before writing
  - write atomically
  - do not write through symlinks
  - preserve key order
- [ ] Remove or rename the `"gemini"` → Antigravity alias in `src/core/harness.rs:38`.
- [ ] Shell-quote the branch name in the "New Worktree Tab" palette command.
- [ ] Parse `--resume <id>` for Claude tabs. Today the id lands in the initial prompt.
- [ ] Bind Claude session ids to tabs. The `Stop` hook only looks ids up in agy's database, so Claude tabs never get `@conversation_id`.
- [ ] Update CHANGELOG (stops at 0.7.0; tags already exist up to v0.8.4).

### plugins/coordination

- [x] Handle grammar: `card/name~sid8` (revised 2026-11-10); the spec, SP2 docs and SP5b are updated by `agentic-hub-cp` (round 4, commit 74f6c0e, on agentic-hub `main`). Nothing in ax or cognitive-engine builds or parses coordination handles yet (checked 2026-11-10); any future code must use `~`, not `#`.
  - S1 entry gate: closed 2026-11-11 (SP3 and SP4 recorded in agentic-hub `1213787`; the S1 kickoff needs the owner's go; S1-PG is agentic-hub PR #37). Earlier text: "not open: SP5b passed, SP3 and SP4 still open"
  - R8 is approved: the 8121 listener binds `127.0.0.1` from s2 (task S2-LISTEN); the tunnel route is the exposure gate, with no route before the S3 gate; the AGENTS.md rule is updated in the same commit
- [ ] Coordination S1 grew: Postgres from S1, the SP5b gate, a `pg_dump` sidecar to a backup directory on the storage host, and S2-LISTEN. Track in the coordination plan, not here.
- [ ] Fix `server-api.md` 11.2 to agy's named-group flat hooks.json form.
- [ ] State explicitly that the "no `Stop` entry" scan (`implementation-plan.md:602`) covers coordination's own group only, since ax adds a `Stop` entry to the user-level file.
- [ ] Refresh the README status, the branch name and the `DatabaseTaskStore` line. Fix the plan §7 C9 label.
- [ ] Correct `bridge-v2-ecosystem-analysis.md`:
  - line 71: agy cannot join Claude agent teams
  - beads storage is Dolt
  - the `mcp_agent_mail_rust` pairing is excluded

### cognitive-engine: merged

Order and decisions in [pr-plan-2026-11-08.md](pr-plan-2026-11-08.md). The remaining items are split under "Next: alongside coordination S1" and "After coordination S1".

- [x] PR #4 (Workflow guard) merged 2026-11-09 (merge commit 3bbda64). It also fixes `hook.ts` activation in `on-demand` and `plugin:agent` lookup (verified 2026-11-08).
- [x] PR #18 merged 2026-11-09 (merge commit 9fd9af1).

### plugins/coordination (cross-repo)

- [ ] `status-client.md` cites `packages/harness2/README.md`, `session_runner.py` and `sbx_session.py`, which do not exist on cognitive-engine `main`. Re-point the citations once Track 3 merges.

### agentic-hub

- [x] Root README and AGENTS.md trees: add `docs/`, coordination and subagents (2026-11-07).
- [x] Root README and AGENTS.md: add the celi5, cplain and mcp-toggle skills (README already had mcp-toggle). Remove the references to `plugins/jev/opencode/`, which does not exist (2026-11-08, uncommitted).
- [x] Jev README: hooks path is `./claude/hooks.json`; removed the missing `opencode/` tree entry. `package.json` version aligned to `plugin.json` (2.3.0). Done in PR #19 (rebase-merged 2026-11-09, main 58420e1).
- [x] Jev 2.3.0 released 2026-11-09 (tag `jev-v2.3.0`): <https://github.com/rockad/agentic-hub/releases/tag/jev-v2.3.0>. Decision 14 was revised on 2026-11-09: the live plugin keeps getting 2.3.x patch releases (see the item below). The version line continues in cognitive-engine at 2.4.0 (see "Next: alongside coordination S1").
- [ ] Coordination docs link to absolute home-directory paths, which resolves only on the workstation. Consider repo-relative links.
- [ ] Subagents plugin for Claude Code, Antigravity and Codex (decided 2026-11-08; closes the subagents half of issue #13):
  - canonical source in `plugins/subagents/src/`
  - `scripts/build-agents.ts` with a `--check` drift test
  - verify the Codex agent TOML schema before emitting `codex/agents/*.toml`
  - update the plugin README and the Claude marketplace entry
  - reply to `@Doc-Cr` on issue #13
- [x] **basic-memory 0.24.1** (be32593, 2026-11-09, owner): the `plugins/memory` compose pin moved from 0.23.2 to 0.24.1 (`BASIC_MEMORY_TAG`, default 0.24.1); drop-in compatible, tool names unchanged, adds POSIX verbs, FastMCP 4.1.3 stateless (needs `FASTMCP_STATELESS_HTTP=true`, already set). It matches cognitive-engine `packages/memory`, which pins `basic-memory==0.24.1` (RFC 003). Plan: [`plugins/memory/docs/v1-migration-plan.md`](../plugins/memory/docs/v1-migration-plan.md) (phase 1 = this upgrade, done).
- [ ] **Installed memory plugin refresh.** The Claude plugin cache keeps `memory@agentic-hub` at 1.0.0 and the marketplace entry still says 1.0.0, so installed copies will not pick up the 0.24.1 compose pin until the version is bumped (1.0.1). The loopback bind needs no bump: the installed cache already binds `127.0.0.1` (that half of the idea was refuted, see the memory compose item below). Owner's call; the stack is not running on the workstation.
- [ ] **basic-memory 1.0.0 breaking contract** (from the migration plan): all MCP tools renamed with a `bm_` prefix (`read_note` to `bm_read`, `write_note` to `bm_write`, `search_notes` to `bm_search`, `edit_note` to `bm_edit`, `delete_note` to `bm_delete`, `list_directory` to `bm_list`, `build_context` to `bm_context`), changed argument schemas (path-based reads and writes), and FastMCP 4 session statefulness by default. Consumers to adapt: `scripts/memory.sh` (dual-compatibility: detect `bm_read` versus `read_note` via `tools/list`), the `recall` skill docs, and cognitive-engine's `packages/memory`, whose RFC 001 promises the upstream tool names the `recall` skill uses. Do not move `packages/memory` past `==0.24.1` until the decision below is made.
- [x] Memory compose: bind 8110 and 8100 to `127.0.0.1`. Done in PR #19 (main 58420e1), which also shipped plan PR 1, so plan Phase 0 items 1 and 5 are done. The memory 1.0.1 bump idea was refuted: the installed cache already binds `127.0.0.1`. Takes effect after `docker compose up -d`.
- [ ] RFC 005 Phase A deprecation notices on `plugins/jev` and `plugins/memory`. The scope decision is made (see above); do not word the `jev` notice as "retired". As of 2026-11-12 it is also not "relocating": the agentic-hub plugin stays the live line (decision 19), so it needs no deprecation notice until the owner decides Jev's single home (evidence audit, decision 1).
- [x] **Port map doc** [`port-map.md`](port-map.md) (single doc, production ports authoritative), added 2026-11-09. Production ports:
  - 8100 lookup (legacy, then native after cutover)
  - 8101 native lookup staging (cognitive-engine RFC 003)
  - 8105 native memory
  - 8110 legacy recall
  - 8115 jev
  - 8120 coordination (tailnet-only)
  - 8121 coordination remote-peer listener
  - 8125 Harness²

  The recall eval stack ports (Graphiti 8101, mcp-memory-service 8120, Basic Memory 8110) are eval-only and overridable at run time. This closes the lookup 8100 staging port item.
- [ ] **Harden the GHCR lookup workflow** (`.github/workflows/publish-plugins.yml`, `ghcr.io/rockad/lookup`). Both are an INTERIM artifact (decision 2026-11-09): RFC 005 Phase C removes them together with `plugins/memory`. Until then:
  - pin the tag plus digest
  - drop the `:latest` default
  - trigger only on `lookup-v*` tags and manual dispatch
  - package visibility: **decided public** (owner, 2026-11-10; the image pulls anonymously). Amend the private-registry-first line in RFC 001 section 8 item 2 in the RFC 005 amendment PRs (#7, #11); consider deleting the old `:latest` tag in the package settings
- [ ] **RFC 005 Phase C checklist:** add an entry to remove `.github/workflows/publish-plugins.yml` and `ghcr.io/rockad/lookup` together with `plugins/memory`.
- [x] No sudo step on the storage host: round 3 of the coordination plan moved the dumps to a backup directory on the storage host (user-writable), created at S2-DEPLOY.
- [ ] **Close the stale remote branches** superseded by main:
  - `feat/jev-modular-sqlite-ram-cache`
  - `fix/jev-small-tails`
  - `rfc/harness2-jev-unified-cognitive-substrate`: check its content against cognitive-engine before deleting

## Next: alongside coordination S1

- [ ] Add spike **SP6**: Claude inbox-socket wake from an `async: true` hook. Vary the stub so the duplicate filter does not drop it.
- [ ] Add spike **SP7**: Claude plugin monitor as a session-long listener.
- [ ] S1 exit gate:
  - `a2a-tck --level must` against loopback
  - `a2a-inspector` on 127.0.0.1 for debugging
- [ ] Sanitize peer text in previews and status: strip control, format and bidi characters, and cap the length.
- [ ] Self-hosted ntfy on the storage host (tailnet) for `INPUT_REQUIRED` and `QUARANTINED`:
  - stub text only
  - no action buttons
  - never a release path
- [ ] ax: "needs input" tab state from the Claude `Notification` hook, plus jump to newest waiting.
- [ ] ax: Claude `rate_limits` meters from the statusline JSON. Optionally feed `jev_quota`.
- [ ] ax: `CopilotDriver` (`--session-id`, `--resume`, `--continue`).
- [ ] ax: Codex history (`state_5.sqlite`) and `codex resume`.
- [ ] ax: OpenCode title sync through `PATCH /api/session/:id`. Honour `OPENCODE_DB`.
- [ ] ax: keep Claude `teammateMode` in-process. Skip panes without `@harness` when snapshotting.
- [ ] ax: register worktrees made outside ax through Claude's `WorktreeCreate` and `WorktreeRemove` hooks.
- [ ] ax: restore re-runs only allowlisted commands and asks before anything else (the cmux idea).
- [ ] ax: read-only discovery of Claude sessions it did not launch (`claude agents --json`, reported).

### cognitive-engine (independent of S1; decision 18, 2026-11-09)

Jev is not retired: only the agentic-hub copy retires (RFC 005). The engine lives on as cognitive-engine `packages/jev` and its adapters, acting as the main decision engine when plugged in.

Status 2026-11-10: **Stack J (Jev) is two PRs on cognitive-engine, both open and ready for `@Doc-Cr`** (formal review requests wait for the collaborator invite to be accepted): #20 `[jev 1/2]` guard hardening, then #22 `[jev 2/2]` advisor port (jev 2.4.0). #19 (advisory CI) and #21 (OpenCode V2 guard) were folded into #20 (merge commit dd6ae2a) and closed. All of them merge cleanly with each other. #12 (Argon hardening) conflicts with `main` and with Stack J and restacks once after #20 and #22 merge.

- [ ] **#20, guard hardening** (open). Advisory CI (`tsc`, `bun test`, `pytest`, from #19); Claude guard reads `CLAUDE_CODE_SUBAGENT_MODEL` and `_FORCE`, resolves agent definitions or fails closed, bounds subagent effort (RFC 007); OpenCode guard for the V2 `subagent` tool reading `event.input` (from #21). Still to check: whether a model set in `define_subagent` front matter bypasses the Antigravity `invoke_subagent` guard.
- [ ] **#22, advisor port** (open). The Claude `UserPromptSubmit` advisor ported to the cognitive-engine Claude adapter with the case-study improvements 5.1 to 5.11, packaged as `packages/jev` 2.4.0, case study moved into cognitive-engine docs (agentic-hub keeps a pointer). Includes the 2.3.1 open items (5.3, 5.4, 5.5, 5.8, session ceiling for 5.11). When it merges, tell the coordination session so it swaps its case-study copy for a pointer.
- [x] **Jev 2.3.1 released 2026-11-10** (`57d2f88`, tag `jev-v2.3.1`): 4.11, 5.1, 5.2, 5.6, 5.7, 5.9, 5.10 and the wording half of 5.11. Further 2.3.x patches continue until RFC 005 Phase C, keeping the live plugin aligned with the cognitive-engine port.
- [x] **Jev follow-up: moved to the agentic-hub plugin, 2026-11-12** (owner; delivery plan decision 19). #20 and #22 merged 2026-11-12; the code items below are built as agentic-hub 2.3.x patches, not in a cognitive-engine PR. The decisions stand. Original plan (stacked PR on #22), decided 2026-11-10 (/clarify, 8 forks):

  | Item | Decision |
  |---|---|
  | PR shape | one PR stacked on #22 |
  | Recall URL (`telemetry.ts:70`) | `JEV_RECALL_URL` env override, default `127.0.0.1:8110/mcp`, empty disables; a test pins the `read_note` tool name (1.0.0 renames it to `bm_read`) |
  | OpenCode tier map | keep the pay-per-token package default; document it and override under `harnesses.opencode.models` in your own config. Never `claude-agent/*`. **Docs done** (cognitive-engine branch `feat/jev-followup`, commit 6521679) |
  | Native `Agent(model:opus)` deny rule | documented as an opt-in snippet, no installer (the guard already fails closed on Claude Code 2.1.295+). **Docs done**, same commit |
  | `ANTHROPIC_DEFAULT_*_MODEL` remaps and `CLAUDE_CODE_EFFORT_LEVEL` | **warn only, never block**; the bypass is documented (**docs done**), and the non-silent warning is still to build: one guard-note line naming the variable (never the value) plus the variable names in the decisions-log entry |
  | `JEV_HARNESS` | each adapter sets it (OpenCode plugin at init, Antigravity launcher or MCP env), one test each |
  | Log rotation | deferred: about 20 KB a day measured, documentation and a manual prune only (**docs done**); revisit past about 10 MB |
  | #12 restack | **superseded 2026-11-12:** #12 merges cleanly with `main` and is Jev code, so it moves to the agentic-hub plugin work or closes (delivery plan, decision 19) |

  Code still to write in the follow-up: `JEV_RECALL_URL`, the warn-only note and log field, `JEV_HARNESS` in the adapters. The documentation commit does not depend on them.
- [ ] Any future Codex adapter must return `deny` (Codex fails open on `ask`). Any Copilot guard fails open on hook timeout.

## After coordination S1

Decision 18, 2026-11-09. Order in [pr-plan-2026-11-08.md](pr-plan-2026-11-08.md).

- [ ] Restack #5, #7, #8 and their children with `rebase --onto main` and a `range-diff` comment. Merge parents without squash.
- [ ] Change the Harness² port default to 8125 in #5 when it is restacked (decision 2026-11-08).
- [ ] Review: slice #5 and #8 for `@Doc-Cr` with an Opus pre-pass; the owner reads #7.
- [ ] Merge #13 (BuildSlot) after #5 and #6. Link the spec at `plugins/coordination/docs/specs/buildslot-spec.md`.
- [ ] Move the cognitive-engine bridge onto coordination.
- [ ] **#7 and #11 with the RFC 005 amendment.** Content:
  - agentic-hub keeps coordination and subagents (decision 2026-11-08)
  - `jev` is not retired; 2026-11-12: the live line stays in the agentic-hub plugin and cognitive-engine `packages/jev` is the frozen reference (decisions 14, 15, 19; was "relocates to cognitive-engine")
  - the GHCR lookup image and `publish-plugins.yml` are interim, removed in Phase C (decision 12)
  - standalone `lookup` and `recall` are legacy after cognitive-engine is done (decision 13)
- [ ] Record the coordination status-client contract and "sbx never reaches the coordination server" in RFC 001/004.
- [ ] **Converge the standalone repos.** `lookup` and `recall` converge on cognitive-engine #8 `packages/lookup`: port their unique fixes there. Freeze them as legacy ONLY AFTER cognitive-engine is done. The the workstation memory MCP registrations stay untouched for now.

## Later: after S1 runs

- [ ] **S1b backlog** in the A2A server, using the beads model:
  - dependency edges
  - a ready view
  - compare-and-set claim
  - lease and heartbeat
  - auto-unblock

  Ready is a query, never a trigger.
- [ ] ax read-only coordination segment (`@a2a_handle`, `@a2a_pending` per pane, set by the harness's own hook).
- [ ] ax worktree lifecycle:
  - close and prune
  - optional expiry
  - pinned flag
  - divergence badge
  - `git worktree lock --reason` as the lease
- [ ] Commit trailers `A2A-Task:` and `Session:` (Jev `commit.ts`).
- [ ] Gitea plugin: read-only issue dependency tools plus a ready query.
- [ ] Third peer spike, depending on the decision above, one of:
  - `codex queue` stub wake
  - OpenCode V2 plugin wake (restricted task-taking agent, `delivery: "queue"`)
  - Copilot `agentStop` stub wake
- [ ] S3 gate: smugglex against the staging tunnel; `mcp-scanner` static on the `/mcp` tool list.

## Tools to evaluate

Watch items. Each was looked at briefly, and none needs action now.

- **Codex `wake_on_agent_messages` and the agent message board** (in development). Could become a native mailbox with idle wake.
- **agent-bus** (already a coordination watch item). Hash-chained event log; can emit to ntfy.
- **ccusage.** Historical per-block usage from local transcripts, if Jev reporting needs history.
- **Copilot `write_agent` / session tools.** Watch whether they gain an external entry point.

## Closed

- [x] **Kanzo, OpenUI, OpenRig** (the original TODO, 2026-11-07). Evaluated in [adoption review 2026-11](adoption-review-2026-11.md):
  - Kanzo: optional trial only.
  - OpenUI: skip.
  - OpenRig: skip, with ideas borrowed.
- [x] **beads, vibe-kanban, Claude agent teams, native harness features** (Claude Code, Antigravity, Codex, OpenCode, Gemini CLI, Copilot CLI). Same review.
