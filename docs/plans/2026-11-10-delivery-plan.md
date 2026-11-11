# Delivery plan: the open pull requests and what is left

- **Date**: 2026-11-10
- **Owner**: Aleksandr Korolev ([@rockad](https://github.com/rockad)); reviewer [@Doc-Cr](https://github.com/Doc-Cr)
- **Companion to**: [Multi-Track Implementation Plan](2026-10-26-multi-track-implementation-plan.md) (the design); this file is the delivery state: which PRs, in which order, what waits for what, and the decisions taken on the way.
- **Resume here:** [`../handoffs/2026-11-11-delivery-handoff.md`](../handoffs/2026-11-11-delivery-handoff.md) has the exact state, next steps and traps.
- **Moved here** from `rockad/agentic-hub` (`docs/pr-plan-2026-11-08.md` Phase 2 and the cognitive-engine sections of `docs/backlog.md`), because the PR queue is cognitive-engine's. What stays there: coordination, ax, the memory/lookup plugins and subagents, the cross-project adoption review and the [port map](https://github.com/rockad/agentic-hub/blob/main/docs/port-map.md).

## Why some work waits

Coordination (`plugins/coordination` in agentic-hub, S0 close and S1) comes before the PRs that restack or move the bridge. Its Stage-1 entry gate is not open yet. Work that does not touch the bridge, the restacked PRs or the RFC 005 amendment runs alongside it.

## Open PRs and stacks (2026-11-10)

| Stack | PRs, in merge order | State |
|---|---|---|
| **J (Jev)** | #20 guard hardening (includes advisory CI and the OpenCode V2 guard), then #22 advisor port (jev 2.4.0) | ready for review; they merge cleanly with each other. Doc-Cr reviewed both on 2026-11-10 (comment-style); the findings are fixed and answered (#20 `b971956`, #22 `4a79643`). Doc-Cr's collaborator invite is still pending, so formal review requests fail until it is accepted |
| **J follow-up** | `feat/jev-followup`, stacked on #22 | documentation done and pushed (`e431d02`); code still to write (see below) |
| **Track 2** | #8 Memory and Lookup (RFC 003), then #9 MCP conformance, then #10 SMB reads | #8 conflicts with `main`; restack after S1 |
| **Harness²** | #5 pipeline, then #6 benchmark, then #13 BuildSlot | #5 conflicts with `main`; restack after S1 |
| **Docs** | #7 stage-1 findings, then #11 RFC 006 and plans | #7 after #10; both ride the RFC 005 amendment |
| standalone | #12 Argon hardening | conflicts with `main` and with Stack J; restack once, after #20 and #22 merge |

Merged: #3, #4, #18 earlier; #19 and #21 were folded into #20 and closed. Rules for all stacks: merge commits (no squash); `git rebase --onto main` per stack with a `range-diff` comment; force-pushes are confirmed with the owner first.

## Decisions

| # | Decision | Chosen |
|---|---|---|
| 5 | Restacking conflicting PRs | `git rebase --onto main` per stack, `--force-with-lease`, a `range-diff` comment per PR; merge parents with merge commits, not squash |
| 6 | Review shape | Slice #5 and #8 for Doc-Cr with an Opus pre-pass per slice; the owner reads #7 and runs a link and anchor check |
| 8 | CI | Advisory workflow (`tsc`, `bun test`, `pytest`), now inside #20 |
| 9 | #13 BuildSlot | Merge after #5 and #6, retargeted to `main`; link the spec at agentic-hub `plugins/coordination/docs/specs/buildslot-spec.md`, do not copy it |
| 13 | Standalone `lookup` and `recall` repos | Converge on #8 `packages/lookup` and `packages/memory`; port the unique fixes; freeze as legacy only after cognitive-engine is done (issues are tracked in each standalone repo) |
| 14 | Jev version line | Continues here at 2.4.0; the agentic-hub plugin also gets 2.3.x patches until RFC 005 Phase C |
| 15 | Jev is not retired | Only the agentic-hub copy retires. The `UserPromptSubmit` advisor is ported here with the case-study improvements 5.1 to 5.11 (#22) |
| 18 | Sequencing | Independent items run alongside coordination S1; restacks, the bridge move and #7 and #11 wait for S1 |

### The 2026-11-10 /clarify pass (Jev follow-up)

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

- [ ] Review and merge Stack J (#20, #22), then tell the coordination session so it swaps its case-study copy in agentic-hub for a pointer.
- [ ] Jev follow-up code: `JEV_RECALL_URL`; the warn-only note and log field for remaps and effort; `JEV_HARNESS` in the OpenCode and Antigravity adapters. Open a draft PR stacked on #22.
- [ ] Check whether a model set in `define_subagent` front matter bypasses the Antigravity `invoke_subagent` guard.
- [ ] Any future Codex adapter must return `deny` (Codex fails open on `ask`); any Copilot guard fails open on hook timeout.
- [ ] Restack #12 after Stack J merges.

### After coordination S1

- [ ] Restack #5, #7, #8 and their children (decision 5). Move the Harness² default port to 8125 in #5. Track 2 includes the lookup staging port 8101.
- [ ] Review: slices of #5 and #8 for Doc-Cr with an Opus pre-pass (decision 6).
- [ ] Merge #13 after #5 and #6 (decision 9).
- [ ] Move the cognitive-engine bridge onto coordination. The roles question stays open (symmetric peers instead of Orchestrator and Builder). Do this before #7, which removes the hand-relayed handoff.
- [ ] #7 and #11 with the RFC 005 amendment: agentic-hub keeps coordination and subagents; `jev` relocates here instead of retiring; the GHCR lookup image and its publish workflow are interim and removed in Phase C; the standalone repos are legacy after cognitive-engine is done. Also record the coordination status-client contract and "sbx never reaches the coordination server" in RFC 001 and RFC 004.
- [ ] Converge the standalone `lookup` and `recall` repos (decision 13).
- [ ] basic-memory 1.0.0: one cutover decision, taken once for native `packages/memory` at Track 4 (`bm_` tool names, stateful sessions); `packages/memory` stays on `basic-memory==0.24.1` until then.

## Still waiting on the owner

- cognitive-engine roles: symmetric peers instead of Orchestrator and Builder.
- Doc-Cr accepting the collaborator invite (then formal review requests on #20 and #22).
- Whether to start a GitHub Project board for this repo (proposed: fields Stack and Gate, auto-add PRs; needs the `project` token scope).
