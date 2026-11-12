# Delivery plan: the open pull requests and what is left

- **Date**: 2026-11-10
- **Owner**: Aleksandr Korolev ([@rockad](https://github.com/rockad)); reviewer [@Doc-Cr](https://github.com/Doc-Cr)
- **Companion to**: [Multi-Track Implementation Plan](2026-10-26-multi-track-implementation-plan.md) (the design); this file is the delivery state: which PRs, in which order, what waits for what, and the decisions taken on the way.
- **Resume here:** [`../handoffs/2026-11-11-delivery-handoff.md`](../handoffs/2026-11-11-delivery-handoff.md) has the exact state, next steps and traps.
- **Moved here** from `rockad/agentic-hub` (`docs/pr-plan-2026-11-08.md` Phase 2 and the cognitive-engine sections of `docs/backlog.md`), because the PR queue is cognitive-engine's. What stays there: coordination, ax, the memory/lookup plugins and subagents, and the cross-project adoption review (both of which now live here too, since agentic-hub's `docs/` was removed on 2026-11-10). The [port map](../port-map.md) is here.

## Why some work waits

Coordination (`plugins/coordination` in agentic-hub, S0 close and S1) comes before the PRs that restack or move the bridge. Its Stage-1 entry gate closed on 2026-11-11 (agentic-hub `1213787`, SP3 and SP4 recorded; the S1 kickoff still needs the owner's go) and S1-PG is in review as agentic-hub PR #37. What the restacks actually depend on in coordination is unproven: the evidence audit ([2026-11-12-evidence-audit.md](2026-11-12-evidence-audit.md)) found only docs-file conflicts, so "after S1" needs a definition before it gates anything (audit decision 3). Work that does not touch the bridge, the restacked PRs or the RFC 005 amendment runs alongside it.

## Open PRs and stacks (2026-11-10)

| Stack | PRs, in merge order | State |
|---|---|---|
| **J (Jev)** | #20 guard hardening (includes advisory CI and the OpenCode V2 guard), then #22 advisor port (jev 2.4.0) | **merged 2026-11-12** (`0f58554`, `c260bf5`; CI green; Doc-Cr approved both on 2026-11-11). Earlier state: ready for review; they merge cleanly with each other. Doc-Cr reviewed both on 2026-11-10 (comment-style); the findings are fixed and answered (#20 `b971956`, #22 `4a79643`). Doc-Cr's collaborator invite is still pending, so formal review requests fail until it is accepted |
| **J follow-up** | `feat/jev-followup`, stacked on #22 | **Code dropped here (2026-11-12, decision 19):** its items move to the agentic-hub plugin as 2.3.x work. The three documentation commits (`e431d02` and earlier) are kept on the branch; their "planned" wording is to be repointed at agentic-hub before any docs PR |
| **Track 2** | #8 Memory and Lookup (RFC 003), then #9 MCP conformance, then #10 SMB reads | #8 conflicts with `main`; restack after S1 |
| **Harness²** | #5 pipeline, then #6 benchmark, then #13 BuildSlot | #5 conflicts with `main`; restack after S1 |
| **Docs** | #7 stage-1 findings, then #11 RFC 006 and plans | #7 after #10; both ride the RFC 005 amendment |
| standalone | #12 Argon hardening | merges cleanly with `main` (trial merge 2026-11-12, GitHub MERGEABLE; the earlier "conflicts" note was wrong). It is Jev code, so decision 19 applies: move it to the agentic-hub plugin work or close it, do not restack it here |

Merged: #3, #4, #18 earlier; #19 and #21 were folded into #20 and closed. Rules for all stacks: merge commits (no squash); `git rebase --onto main` per stack with a `range-diff` comment; force-pushes are confirmed with the owner first.

## Decisions

| # | Decision | Chosen |
|---|---|---|
| 5 | Restacking conflicting PRs | `git rebase --onto main` per stack, `--force-with-lease`, a `range-diff` comment per PR; merge parents with merge commits, not squash |
| 6 | Review shape | Slice #5 and #8 for Doc-Cr with an Opus pre-pass per slice; the owner reads #7 and runs a link and anchor check |
| 8 | CI | Advisory workflow (`tsc`, `bun test`, `pytest`), now inside #20 |
| 9 | #13 BuildSlot | Merge after #5 and #6, retargeted to `main`; link the spec at agentic-hub `plugins/coordination/docs/specs/buildslot-spec.md`, do not copy it |
| 13 | Standalone `lookup` and `recall` repos | Converge on #8 `packages/lookup` and `packages/memory`; port the unique fixes; freeze as legacy only after cognitive-engine is done (issues are tracked in each standalone repo) |
| 14 | Jev version line | **Revised 2026-11-12 (owner):** the live line is the agentic-hub plugin (2.3.x patches); all Jev fixes and changes go there. `packages/jev` 2.4.0 here (merged as #20 and #22 on 2026-11-12) stays as it is and gets no further code until cognitive-engine work resumes |
| 15 | Jev is not retired | **Revised 2026-11-12 (owner):** cognitive-engine keeps the Jev docs only (RFCs, case-study copies, pointers) until then. The advisor port 5.1 to 5.11 (#22) stays merged here as the reference implementation; improvements are sent to the agentic-hub session as text with evidence and land as agentic-hub 2.3.x patches |
| 19 | Plugins first | Owner, 2026-11-12: cognitive-engine does not go ahead of the plugins and hosts fixes only for its own project (Harness², Memory, Lookup). Plugin fixes (Jev, coordination, memory, subagents) are made in agentic-hub. Real cognitive-engine work restarts from a grounded, evidence-based plan |
| 18 | Sequencing | Independent items run alongside coordination S1; restacks, the bridge move and #7 and #11 wait for S1 |

### The 2026-11-10 /clarify pass (Jev follow-up)

Status 2026-11-12: these decisions stand, but the code they describe is built in the agentic-hub plugin, not here (decision 19).

| Item | Decision |
|---|---|
| PR shape | one PR stacked on #22 (base `feat/jev-advisor-port`, retarget to `main` after #22 merges) |
| Recall URL (`core/telemetry.ts`) | `JEV_RECALL_URL` env override, default `http://127.0.0.1:8110/mcp`, empty disables; a test pins the `read_note` tool name (basic-memory 1.0.0 renames it `bm_read`) |
| OpenCode tier map | keep the pay-per-token default, document it, override under `harnesses.opencode.models` (documented) |
| Native `Agent(model:opus)` deny rule | opt-in snippet in the Claude adapter README, no installer (documented) |
| `ANTHROPIC_DEFAULT_*_MODEL` remaps, `CLAUDE_CODE_EFFORT_LEVEL` | warn only, never block. The bypass is documented. Still to build: one guard-note line naming the variable (never its value) and the variable names in the decisions-log entry |
| `JEV_HARNESS` | each adapter sets it (OpenCode plugin at init, Antigravity launcher or MCP env), one test each |
| Log rotation | deferred; measured about 20 KB a day; revisit past about 10 MB (documented) |
| #12 restack | once, after #20 and #22 merge |

## Work left

### Alongside coordination S1

- [x] Review and merge Stack J (#20, #22), then tell the coordination session so it swaps its case-study copy in agentic-hub for a pointer. Done 2026-11-12; the agentic-hub copy of the 2026-11-09 study is already a pointer.
- [x] Jev follow-up code (`JEV_RECALL_URL`; the warn-only note and log field for remaps and effort; `JEV_HARNESS` in the OpenCode and Antigravity adapters): moved to agentic-hub plugin work on 2026-11-12 (decision 19), sent to its session as text.
- [ ] Check whether a model set in `define_subagent` front matter bypasses the Antigravity `invoke_subagent` guard. Code read 2026-11-12: the hook matcher is `invoke_subagent` only and the guard reads only `Subagents[].Model`, so `define_subagent` is never seen. Whether Antigravity applies the definition's model when `Model` is empty or `inherit` is **unverified** and needs a live run. Sent to the agentic-hub session.
- [x] Copy the 2026-11-11 case study (PreToolUse dialog on a pinned model) into `packages/jev/docs/case-studies/` (decision 19: docs only). Done 2026-11-12.
- [ ] Any future Codex adapter must return `deny` (Codex fails open on `ask`); any Copilot guard fails open on hook timeout.
- [ ] #12 (Jev code, mergeable as is): decide whether it moves to the agentic-hub plugin work or closes (decision 19). No restack needed.
- [ ] Jev in the agentic-hub plugin, state 2026-11-12 (reported by its session, not checked here): 2.3.3 is open as agentic-hub PR #38 (backports of 5.1, 5.2, 5.5, 5.6, 5.7, 5.9, 5.10, the whole-word sites and more; an executable parity audit ran this repo's #22 tests against 2.3.2 and 114 of 289 failed), then 2.3.4 (elevation clamp) and 2.3.5 (5.3, 5.4, 5.11). Four findings it raised were checked against `packages/jev` the same day and three apply here, with no code change (decision 19): the intent parser reads "Users who use opus see errors" and "We use haiku for the classifier" as instructions; `core/telemetry.ts` creates its files with default modes (0644 in 0755 dirs), and the other state files get 0600/0700 only when created; `taskNeedsEdit` treats remove, modify, patch and amend as non-editing verbs, which matters outside `jev_batch_dispatch`. The fourth (the "one approval prompt" sentence) holds in #22 only because its guard has no intent bypass.

### After coordination S1

- [ ] Restack #5, #7, #8 and their children (decision 5). Move the Harness² default port to 8125 in #5. Track 2 includes the lookup staging port 8101.
- [ ] Review: slices of #5 and #8 for Doc-Cr with an Opus pre-pass (decision 6).
- [ ] Merge #13 after #5 and #6 (decision 9).
- [ ] Move the cognitive-engine bridge onto coordination. The roles question stays open (symmetric peers instead of Orchestrator and Builder). Do this before #7, which removes the hand-relayed handoff.
- [ ] #7 and #11 with the RFC 005 amendment: agentic-hub keeps coordination and subagents; Jev is **not retired** and the live line stays in the agentic-hub plugin while `packages/jev` is the frozen reference (decisions 14, 15, 19; this supersedes the earlier "`jev` relocates here"); the GHCR lookup image and its publish workflow are interim and removed in Phase C; the standalone repos are legacy after cognitive-engine is done. Also record the coordination status-client contract and "sbx never reaches the coordination server" in RFC 001 and RFC 004.
- [ ] Converge the standalone `lookup` and `recall` repos (decision 13).
- [ ] basic-memory 1.0.0: one cutover decision, taken once for native `packages/memory` at Track 4 (`bm_` tool names, stateful sessions); `packages/memory` stays on `basic-memory==0.24.1` until then.

## Still waiting on the owner

- cognitive-engine roles: symmetric peers instead of Orchestrator and Builder.
- Doc-Cr accepting the collaborator invite (then formal review requests on #20 and #22).
- Whether to start a GitHub Project board for this repo (proposed: fields Stack and Gate, auto-add PRs; needs the `project` token scope).
