# Checks Plan: Merge Checklist, Item-6 Leftovers, Recurring Checks and Item-4 Acceptance

- **Document Type**: Verification and engineering-hygiene plan (every row has a pass or fail answer; no hypothesis)
- **Date**: 2026-11-02
- **Authors**: Aleksandr Korolev ([@rockad](https://github.com/rockad)), Claude Code (Sonnet 5.5)
- **Target Reviewers**: [@Doc-Cr](https://github.com/Doc-Cr), Aleksandr Korolev
- **Baseline**: [Multi-Track Implementation Plan](2026-10-26-multi-track-implementation-plan.md), sections 3.3.3, 4 and 5
- **Companions**: [RFC 006](../rfc/006-subscription-channels-and-sbx-model-sessions.md), [Experiments plan](2026-11-02-experiments-plan.md)
- **Specifications referenced**: [RFC 004](../rfc/004-system2-harness2.md) sections 6 to 9, [RFC 005](../rfc/005-egress-retirement.md)
- **Facts verified**: 2026-11-02 with `git`, `gh pr view` and file reads. Anything not checked is marked UNVERIFIED. Experiments (things that can come out either way) are in the Experiments plan, not here.

---

## 1. How to read this plan

- **A check** has a command or a piece of evidence and a pass or fail answer. A hypothesis does not belong here.
- **Owner** is who runs it. `User` means the person at the terminal (the approval gate and merge authority are the user's). `Claude Code` can run read-only and local checks. `Antigravity` is listed only where it was asked to review; its quota is out with no date, so nothing here depends on it unless stated.
- **Status** is one of: `open` (can run now), `blocked` (names what blocks it), `done` (verified on 2026-11-02, evidence given), `UNVERIFIED` (a claim that could not be checked).
- **Never** run a command here with `rm -rf` on a variable path. Deletions of run directories and branches are listed as user actions with the exact names to review first.
- **No check here calls a real model.** Any run that does is an experiment and needs the user's typed approval (Experiments plan, section 2).

### 1.1 Common commands

| Id | What | Command | Notes |
| --- | --- | --- | --- |
| K-1 | Jev and adapters suite | `bun test packages/jev adapters` (root script: `bun run test`) | Last recorded: 1204 pass for PR #4 (user fact); PR #4's body says 1203 pass, 1 skip, 0 fail. Re-run, do not quote |
| K-2 | Jev type check | `bun run lint` (runs `tsc --noEmit` per package) | Must be clean |
| K-3 | Python suite | `uv run pytest` from the repository root | `testpaths` = `packages/memory/tests`, `packages/lookup/tests`, `packages/harness2/tests`, `tools/tests` (checked in `pyproject.toml` at `9f12381`). `tools/bench/tests` is NOT in `testpaths` |
| K-4 | Docker-backed tests under the shared lock | `flock <shared lock file> uv run pytest -q <paths>` | Real-daemon tests share one rootless Docker daemon with other agents, so they run under one `flock` lock. The lock file path is not recorded in the repository: UNVERIFIED as a durable convention (the agents used a session-scoped file). Agree one path before the merge run. `mkdir`-based locks are not exclusive on this host (RFC 004 section 9.9); `flock` is |
| K-5 | Real opencode against a stub model | `COGNITIVE_HARNESS2_REAL_CLI=1 uv run pytest -q packages/harness2/tests/test_real_cli.py` | Skipped without the variable or `~/.opencode/bin/opencode`. Run on its own |
| K-6 | sbx live tests | `COGNITIVE_HARNESS2_SBX_LIVE=1 uv run pytest -q packages/harness2/tests/test_sbx_session.py -k test_live -s` | Under K-4's lock; one at a time; starts throwaway `h2m-*` sandboxes and a stub model. Needs both sbx host-reach settings off |
| K-7 | MCP conformance | `uv run tools/mcp_conformance.py <url \| stdio command> --probe tools/probes/<spec>.toml` | Specs at PR #5: `harness2`, `jev`, `lookup`, `memory`, `toy`. Fails closed: a check it cannot perform is a failure. Expected 12/12 per daemon |
| K-8 | Skip audit | `uv run pytest -rs` and compare the skip reasons with the expected list | PR #5 reported 5446 passed and 7 skipped (user fact, not re-run here). Tests marked `requires_rootless_docker` must run, never skip |
| K-9 | Merge dry run | `git fetch origin && git merge-tree --write-tree --name-only origin/main origin/<branch>` | Exit 0 = clean; exit 1 lists conflicted files. Writes objects only, no refs, no working tree |
| K-10 | PR state | `gh pr view N --json state,baseRefName,headRefName,headRefOid,mergeable,mergeStateStatus` | Record the head SHA you tested |
| K-11 | Merge style | `gh pr merge N --merge` | Merge commits only, never squash (plan section 5). Run by the user or on the user's go |

---

## 2. State snapshot (2026-11-02)

| PR | Branch | Base | Head | Mergeable (GitHub) |
| --- | --- | --- | --- | --- |
| #3 | `feat/track1-jev-adapters` | `main` | `61fd4a3` | MERGEABLE |
| #4 | `fix/agent-guard-unknown-agents` | `feat/track1-jev-adapters` | `fb9fb8d` | MERGEABLE |
| #5 | `feat/stage1-integration` | `main` | `9f12381` | UNKNOWN |
| #6 | `feat/model-bench` | `feat/stage1-integration` | `888a4d4` | UNKNOWN |
| #7 | `docs/stage1-findings` | `main` | `19de968` | MERGEABLE |
| #8 | `feat/track2-memory-lookup` | `main` | `1e7ff7a` | MERGEABLE |
| #9 | `feat/mcp-conformance` | `feat/track2-memory-lookup` | `d7c4f2e` | MERGEABLE |
| #10 | `feat/lookup-smb-sources` | `feat/track2-memory-lookup` | `205a332` | MERGEABLE |

Agreed merge order (user): **#3, #4, #8, #9, #5, #6, #10, #7**, then this docs PR (`docs/future-plans`, stacked on #7). Merge commits only. Children are retargeted to `main` after their parent merges.

**Ancestry facts (checked with `git merge-base --is-ancestor`).**

- #5's head contains the heads of #8 and #9, so #5's diff shows Track 2 and the conformance runner until those merge. It does not contain #10.
- #6 is 69 commits behind #5's head and adds only `tools/bench` (23 files).
- #7's branch contains #10's head, which is why #7 is docs-only only after #10 lands.
- #3 and #4 touch `packages/jev`, `adapters/` and `tools/probes`. #5, #6, #8, #9 and #10 do not touch `packages/jev`.

**Dry-run conflicts (K-9, recorded 2026-11-02 against the heads above).**

| Sequence | Result |
| --- | --- |
| `main`, then #3, then #4 | clean |
| `main + #3 + #4`, then #8 | conflicts in `docs/plans/2026-10-26-multi-track-implementation-plan.md` and `docs/rfc/002-system1-jev.md`. The same two files conflict for #9, #5, #6, #10 and #7 against that tip, because all of them carry the same docs history |
| `main`, then #8, then #9 (without #3, #4) | clean |
| the above, then #10 | conflicts in `docs/handoffs/antigravity-claude-bridge.md` and `docs/rfc/003-memory-lookup.md` |
| the above (without #10), then #5, then #6 | clean |
| the above (without #10), then #7 | conflicts in `HANDOFF.md`, `docs/handoffs/antigravity-claude-bridge.md`, `docs/rfc/003-memory-lookup.md`, `docs/rfc/004-system2-harness2.md` |
| #3 alone against #5, and #3 alone against #7 | conflicts in the plan and RFC 002 only |

The dry runs cannot predict what happens after a conflict is resolved by hand. After each resolved merge, re-run K-9 for the next PR in the order; do not rely on this table past the first conflict.

---

## 3. Merge checklist per PR (in the agreed order)

Every PR gets the same envelope before and after (rows M-x.1 to M-x.3), then its own rows.

**Envelope, for every PR N.**

| Id | When | What | Command or evidence | Owner | Blocked by | Status |
| --- | --- | --- | --- | --- | --- | --- |
| M-N.1 | before | Head SHA is the one tested; no new commits since | K-10, compare `headRefOid` with the recorded SHA | Claude Code | the previous merge in the order | open |
| M-N.2 | before | Dry run is clean, or the conflict list equals the known list for this PR | K-9 against current `origin/main` | Claude Code | the previous merge in the order | open |
| M-N.3 | before | PR body states the merge order block and no stale claim (see R-1 to R-4) | `gh pr view N --json body --jq .body` and `rg` the stale-phrase list (C-4) | Claude Code | none | open |
| M-N.4 | merge | Merge commit, no squash | K-11 | User | M-N.1 to M-N.3 | open |
| M-N.5 | after | `git log --first-parent origin/main -3` shows the merge commit of N | `git fetch origin && git log --first-parent --oneline origin/main -3` | Claude Code | M-N.4 | open |

### 3.1 PR #3 — Jev adapters (`feat/track1-jev-adapters` → `main`)

| Id | When | What | Command or evidence | Owner | Blocked by | Status |
| --- | --- | --- | --- | --- | --- | --- |
| M-3.a | before | Jev suite green on the head | K-1, K-2 | Claude Code | none | open |
| M-3.b | before | Adapter packaging: Claude plugin manifest, hooks file and manifest test present. The turnkey `engine install-adapters` is NOT in #3 (deferred to the `engine` CLI work, per #3's README and plan text); see R-11 | `git ls-tree -r --name-only origin/feat/track1-jev-adapters adapters/claude` lists `.claude-plugin/plugin.json`, `hooks/hooks.json`, `tests/manifest.test.ts` | Claude Code | none | open |
| M-3.c | after | #4 retargeted to `main` | `gh pr edit 4 --base main`, then K-10 shows `baseRefName` = `main` | Claude Code | M-3.4 | open |
| M-3.d | after | #4's diff now shows only guard commits | `gh pr diff 4 --name-only \| wc -l` is smaller than before the merge | Claude Code | M-3.c | open |
| M-3.e | after | Jev suite on `main` | K-1, K-2 | Claude Code | M-3.5 | open |

### 3.2 PR #4 — Jev guard (stacked on #3, then → `main`)

| Id | When | What | Command or evidence | Owner | Blocked by | Status |
| --- | --- | --- | --- | --- | --- | --- |
| M-4.a | before | Jev suite on the head: all pass, 0 fail, `tsc` clean | K-1, K-2 | Claude Code | M-3.5 | open |
| M-4.b | before | Backlog items G-1 to G-5 (section 4.1) are either closed or recorded as tickets | read PR #4 body, "Known limits" | Claude Code | none | open |
| M-4.c | before | The guard refuses an unknown agent (the HIGH finding fixed in `3e37ce7`) | run the named test in `packages/jev/tests` for unknown agents and read its result | Claude Code | none | open |
| M-4.d | after | Jev suite on `main`; hooks file still has `onFailure: "block"` on the guard hooks | K-1; `jq '.' adapters/claude/hooks/hooks.json` and read the guard entries | Claude Code | M-4.5 | open |
| M-4.e | after | Conformance for Jev (`:8115`) after #9 lands | K-7 with `tools/probes/jev.toml` | Claude Code | M-9.5 | blocked |

### 3.3 PR #8 — Track 2 Memory and Lookup (→ `main`)

| Id | When | What | Command or evidence | Owner | Blocked by | Status |
| --- | --- | --- | --- | --- | --- | --- |
| M-8.a | before | Memory and Lookup tests | `uv run pytest packages/memory packages/lookup` | Claude Code | none | open |
| M-8.b | before | Docs conflict list equals {plan, RFC 002} after #3 and #4 | K-9 | Claude Code | M-4.5 | open |
| M-8.c | merge | Resolve the docs conflict by ownership (section 3.9, rule table) | read both sides; apply the rule; K-9 then reports clean | Claude Code (resolves) with User (approves) | M-8.b | open |
| M-8.d | after | #9 and #10 retargeted to `main` | `gh pr edit 9 --base main`; `gh pr edit 10 --base main`; K-10 on both | Claude Code | M-8.5 | open |
| M-8.e | after | Port policy intact: native Lookup on `:8101`, legacy container still on `:8100` | read `packages/lookup` defaults; `ss -ltn` on the host (user) | User | M-8.5 | open |

### 3.4 PR #9 — MCP conformance runner (stacked on #8, then → `main`)

| Id | When | What | Command or evidence | Owner | Blocked by | Status |
| --- | --- | --- | --- | --- | --- | --- |
| M-9.a | before | Runner tests | `uv run pytest tools/tests` | Claude Code | M-8.5 | open |
| M-9.b | before | Conformance 12/12 on Memory and Lookup daemons | K-7 with `tools/probes/memory.toml`, `tools/probes/lookup.toml` (start each daemon on a free port) | Claude Code | M-8.5 | open |
| M-9.c | after | Jev probe is a required check from now on | K-7 with `tools/probes/jev.toml` against `bun run start:http` | Claude Code | M-9.5 | open |

### 3.5 PR #5 — Harness2 pipeline (→ `main`)

| Id | When | What | Command or evidence | Owner | Blocked by | Status |
| --- | --- | --- | --- | --- | --- | --- |
| M-5.a | before | #5's diff no longer shows Track 2 | `gh pr diff 5 --name-only \| rg '^packages/(memory\|lookup)/'` prints nothing | Claude Code | M-9.5 | open |
| M-5.b | before | Full suite on the head under the lock | K-4 with K-3; record passed/skipped counts; expected 5446 passed / 7 skipped (user fact, to be re-measured) | Claude Code | M-5.a | open |
| M-5.c | before | The skip list is the expected seven and no `requires_rootless_docker` test skipped | K-8 | Claude Code | M-5.b | open |
| M-5.d | before | Real-opencode stub probes | K-5 | Claude Code | M-5.b | open |
| M-5.e | before | Selftest rehearsal with fakes on the real daemon | `uv run python -m cognitive_harness2.stage1 selftest` under K-4 | Claude Code | M-5.b | open |
| M-5.f | before | Proposer image resolves by id and the tag matches the source | `uv run python -m cognitive_harness2.image status` | Claude Code | none | open |
| M-5.g | before | `stage1 plan` is READY on the host (read-only) | `uv run python -m cognitive_harness2.stage1 plan --opencode ~/.opencode/bin/opencode` | User | none | open |
| M-5.h | before | sbx live tests, if sbx is to be trusted at merge | K-6 | Claude Code | host settings off | open |
| M-5.i | before | The independent look on post-round-3 fixes has been decided (L-b3) | decision recorded | User | none | open |
| M-5.j | before | PR #5 body corrected: "the base harness passes about half the time at k=1" is superseded by the measured 0 of 8 (R-1) | `gh pr view 5 --json body --jq .body \| rg 'about half'` prints nothing | Claude Code | none | open |
| M-5.k | after | #6 retargeted to `main` | `gh pr edit 6 --base main`; K-10 | Claude Code | M-5.5 | open |
| M-5.l | after | Suite and `stage1 plan` on `main` | K-3 under K-4; `stage1 plan` | Claude Code | M-5.5 | open |
| M-5.m | after | Conformance on the Harness2 daemon (`:8120`) | K-7 with `tools/probes/harness2.toml` | Claude Code | M-5.5 | open |

### 3.6 PR #6 — Model benchmark (stacked on #5, then → `main`)

| Id | When | What | Command or evidence | Owner | Blocked by | Status |
| --- | --- | --- | --- | --- | --- | --- |
| M-6.a | before | Bench tests (not in `testpaths`) | `uv run pytest tools/bench/tests` | Claude Code | M-5.5 | open |
| M-6.b | before | The bench imports exist at `main` (`cognitive_harness2.sum_signed`) | `rg -n 'sum_signed' tools/bench packages/harness2/src` | Claude Code | M-5.5 | open |
| M-6.c | before | #6 is 69 commits behind #5's old head; after #5 merges it must merge cleanly | K-9 | Claude Code | M-5.5 | open |
| M-6.d | before | `tools/bench/PREREGISTRATION.md` is committed before any benchmark result in the same PR | `git log --follow --format='%h %ad' --date=short -- tools/bench/PREREGISTRATION.md` precedes the first report commit; the file text says so | Claude Code | none | open |

### 3.7 PR #10 — Lookup SMB sources (stacked on #8, then → `main`)

| Id | When | What | Command or evidence | Owner | Blocked by | Status |
| --- | --- | --- | --- | --- | --- | --- |
| M-10.a | before | Lookup tests incl. SMB (fake client plus in-process server) | `uv run pytest packages/lookup` | Claude Code | M-6.5 | open |
| M-10.b | before | Docs conflict list: `docs/handoffs/antigravity-claude-bridge.md` and `docs/rfc/003-memory-lookup.md` (observed in the dry run); resolve the handoff by deletion, as PR #5's body states | K-9 | Claude Code | M-6.5 | open |
| M-10.c | before | Real-Samba share check on `core` still passes (7 live tests passed on 2026-10-29) | the SMB live test suite against `//core/knowledge-archive` | User | host `core` reachable | open |
| M-10.d | before | No credential in any log or error string | `rg -n -i 'password\|NTLM' packages/lookup/src` read for literal values; tests that pin the redaction run in M-10.a | Claude Code | none | open |
| M-10.e | after | Share-source parity gate for Track 4 is still open (it is not closed by merging) | RFC 005 Phase 1 box stays unticked until per-source document counts match | User | Track 4 | blocked |

### 3.8 PR #7 — Docs (→ `main`, last)

| Id | When | What | Command or evidence | Owner | Blocked by | Status |
| --- | --- | --- | --- | --- | --- | --- |
| M-7.a | before | #7 is docs-only now that #10 landed | `gh pr diff 7 --name-only \| rg -v '^docs/\|^HANDOFF\.md$\|^\.gitignore$'` prints nothing | Claude Code | M-10.5 | open |
| M-7.b | before | Conflict list equals {`HANDOFF.md`, `docs/handoffs/antigravity-claude-bridge.md`, RFC 003, RFC 004} or smaller; handoff files resolved by deletion | K-9 | Claude Code | M-10.5 | open |
| M-7.c | before | Docs reality sync (C-5) run on the merged result | C-5 | Claude Code | M-7.b | open |
| M-7.d | before | Stale-phrase search clean (C-4) | C-4 | Claude Code | M-7.b | open |
| M-7.e | after | This docs PR (`docs/future-plans`) retargeted to `main` | `gh pr edit <number> --base main` | Claude Code | M-7.5 | open |
| M-7.f | after | Branch cleanup (section 4.6) | list, then user deletes | User | M-7.5 | open |

### 3.9 Docs conflict resolution list

Applies at the first docs-carrying merge after #3 and #4 (expected to be #8), and again wherever K-9 reports a docs conflict.

| File | Owner of the text | Rule |
| --- | --- | --- |
| `docs/rfc/002-system1-jev.md` | #3 and #4 (Track 1) | Keep main's (post-#3, post-#4) acceptance boxes and Option A dual-hook text; add only the docs side's cross-references that name a PR or a section that exists after the merge |
| `docs/plans/2026-10-26-multi-track-implementation-plan.md` sections 3.1 and 3.1.3 | #3 and #4 | Keep main's text |
| Same plan, sections 3.2 to 3.5 and 4 to 6 | the docs side | Keep the docs side's text |
| Same plan, the "PR stack" bullet in 3.3.3 | neither | Rewrite to the true state at merge time: which of #3 to #10 are merged, which are open, and the order. Do not copy either side |
| `docs/rfc/003-memory-lookup.md` | #8 and #10 | Union of the SMB status lines; keep the later date on each status line |
| `docs/rfc/004-system2-harness2.md` | the docs side (#7) | Take #7's section 9; keep #5's code-adjacent facts only if they are still true; re-run C-5 |
| `HANDOFF.md`, `docs/handoffs/antigravity-claude-bridge.md` | none (removed by user decision 2026-10-31) | Resolve by deletion. The only channel is `~/.agents` |

After resolving, K-9 must report clean for the next PR, and C-4 and C-5 must pass.

---

## 4. Item-6 leftovers

### 4.1 Jev guard backlog as tickets

PR #4's body lists the open backlog as: click-through false positives on `this` and on variable-key lookups, the resume model gap, and project-level configs. Several commits in #4 address parts of these (`f44fe12` bare `this` in a class body, `d2ad318`, `4860f65` and `1faec29` for project config, a resume marker in the README). Whether the remaining part of each is still open is UNVERIFIED until the tests below are written or found.

| Id | Ticket | Acceptance test (fails before, passes after) | Owner | Blocked by | Status |
| --- | --- | --- | --- | --- | --- |
| G-1 | Workflow guard asks for a built-in `agentType` (`Explore`, `Plan`, ...) while the Agent guard allows built-ins (verified in `packages/jev/README.md`) | A Workflow call with `agentType: "Explore"` and no `model` is allowed; an unknown or plugin agent without a definition still asks; the Agent and Workflow guards give the same verdict for the same built-in | Claude Code | #3, #4 merged | open |
| G-2 | `PostToolUse` payload for `jev_judge` was unclear and got no code. The harness2 push hook counts tool failures only and cannot see a `jev_judge` verdict (README, "What is not done") | Step 1: capture a real payload with a log-only canary and commit it as a fixture under `packages/jev/tests/fixtures/payloads/`. Step 2, only if the verdict is readable: a hook test that a `FAIL` or a score under 0.50 advises. If it is not readable: document the limit in RFC 004 section 2.3 | Claude Code | #4 merged; canary run needs the user's go | open |
| G-3 | Resume model gap: after `--resume --model X` the first subagent call asks until the first assistant turn lands (documented in `packages/jev/README.md`) | Decide: accepted limit (close with the documentation) or reduce false asks. If reduce: a test that a resumed session with a readable transcript tail does not ask | User decides; Claude Code | #4 merged | open |
| G-4 | Click-through false positives on `this` and variable keys | Read `workflow-guard-this.test.ts`; list the forms that still ask and decide per form: keep (fail closed) or allow with a test. Close the ticket when the list is empty or each remaining form is documented | Claude Code | #4 merged | UNVERIFIED |
| G-5 | Project-level configs | Tests `ceiling-inherit.test.ts` and the project-ceiling tests in #4 pass; the README states that a project config can tighten but never loosen | Claude Code | #4 merged | UNVERIFIED |
| G-6 | Plugin lookup ignores `enabledPlugins` (PR #4 "Known limits"; errs toward asking) | No test needed; record as an accepted limit in the README | Claude Code | none | open |

### 4.2 Antigravity's optional review

| Id | What | Scope | Evidence | Owner | Blocked by | Status |
| --- | --- | --- | --- | --- | --- | --- |
| L-b1 | Independent review of `374b790..9f12381` on `feat/stage1-integration` | Read-only. Files: `session_runner.py`, `egress_proxy.py`, `sbx_session.py`, `stage1.py`, `substrate.py`; the approval gate; the route filter; teardown. Output: findings with file and line, severity, and the failing scenario | A review note in `~/.agents` with the head SHA reviewed (`git rev-parse` of the reviewed commit) | Antigravity | Antigravity quota (out, no date) | blocked |

### 4.3 The earlier "independent look" and the post-round-3 options

Facts to keep straight (RFC 004 section 9.10): the first review was requested on Fable 5.1 and served by Sonnet 5.5 (the transcript records `claude-sonnet-5-5`; the subscription has no Fable). Two later read-only passes ran on Opus 5.5 (verified from their transcripts): one on the fix diffs and the guard backlog, one on the `sbx` model route. A third round of fixes was applied. Fixes after the Opus passes have no review by a stronger model.

| Id | Option | Command or evidence | Cost | Owner | Blocked by | Status |
| --- | --- | --- | --- | --- | --- | --- |
| L-b2 | One more read-only Opus 5.5 pass on the diff after the last Opus pass | `git diff <last-reviewed-sha>..9f12381 --stat` to size it; the pass writes findings with file and line | Subscription quota (shared with interactive use) | User decides; Claude Code runs | none | open |
| L-b3 | Accept the residual and record it as an accepted limit in the PR #5 body | PR body line naming the head SHA and what was not re-reviewed | none | User | none | open |
| L-b4 | Wait for Antigravity (L-b1) and treat it as the independent look | L-b1 evidence | Antigravity quota | Antigravity | quota | blocked |

### 4.4 Marker assumption check

| Id | What | Command or evidence | Owner | Blocked by | Status |
| --- | --- | --- | --- | --- | --- |
| L-c1 | `CLAUDE_PID` and `CLAUDE_JOB_DIR` are assumed absent from the user's own terminal. The README at #5 calls this an ASSUMPTION; both are present in the agent sessions it was built in | In a plain terminal the user opens themselves, outside any agent: `env \| rg -c '^(CLAUDE_PID\|CLAUDE_JOB_DIR)='` prints `0` (the count only, never the values) | User | none | UNVERIFIED |
| L-c2 | The refusal never prints a value | `rg -n 'agent_env\|names found' packages/harness2/src/cognitive_harness2/stage1.py` and read: only names are recorded | Claude Code | none | open |

### 4.5 Run-directory cleanup

The stage-1 run directories under `~/.local/state/cognitive-engine/harness2-stage1` hold synthetic prompts and model text and are the user's to delete. On 2026-11-02 there were 11 directories (9.3 MB in total): `stage1-20261030T115823Z-4ee1`, `stage1-20261030T164154Z-f8de`, `stage1-20261030T205030Z-334d`, `stage1-20261031T063810Z-58c3`, `stage1-20261031T140554Z-5a59`, `stage1-20261031T183543Z-a81f`, `stage1-20261031T183855Z-1493`, `stage1-20261031T194343Z-82cd`, `stage1-20261031T200113Z-72a1`, `stage1-20261031T213637Z-5ead`, `stage1-20261101T185011Z-d08d`.

| Id | What | Command or evidence | Owner | Blocked by | Status |
| --- | --- | --- | --- | --- | --- |
| L-d1 | Decide which runs to keep as evidence. `d08d` is the 8-rollout baseline cited by RFC 004 section 9.11; `58c3` is run #4; the Experiments plan (EXP-4 replay) may want the proposals in `d08d` | List: `fd -t d -d 1 'stage1-' ~/.local/state/cognitive-engine/harness2-stage1` and `du -sh` each | User | none | open |
| L-d2 | Copy the report numbers worth keeping into the docs before deleting (counts only) | `jq` of `report.json` fields named in the Experiments plan | Claude Code | L-d1 | open |
| L-d3 | Delete the runs not kept | The user removes the directories by name after L-d1; nothing in this plan automates it | User | L-d1, L-d2 | open |

### 4.6 Branch cleanup after merges

The audit found these branches fully contained in #5: `feat/3b-session-runner`, `feat/3b-slot-table`, `feat/3b-stage1-runner`, `feat/allow-api-show`, `feat/ollama-route-filter`, `feat/route-deny-diagnostics`, `feat/sbx-default`, `feat/stage1b-hard-task`, `feat/test-isolation`, `fix/fable-harness2`.

| Id | What | Command or evidence | Owner | Blocked by | Status |
| --- | --- | --- | --- | --- | --- |
| L-e1 | Re-verify containment at cleanup time, per branch | `git fetch origin && git merge-base --is-ancestor origin/<branch> origin/main && echo contained` (after #5 merges; before that, against `origin/feat/stage1-integration`). Checked today: `feat/sbx-model-route`, `feat/proposer-hint-rollouts` and `fix/review-round3` are ancestors of #5's head | Claude Code | M-5.5 | open |
| L-e2 | Delete remote branches that are contained and whose PR is merged | `git push origin --delete <branch>` for each name on the verified list, run by the user | User | L-e1 | open |
| L-e3 | Other branches not in the audit list that are not contained anywhere | `git branch -r --no-merged origin/main` after all merges; each remaining name needs an owner or a deletion decision. Observed today but not in the audit list: `feat/guard-backlog` (an ancestor of #4), `feat/jev-workflow-guard`, `feat/worktree-protocol`, `docs/3b-decisions`, `docs/3b-spike-findings`, `docs/bridge-turn-11`, `docs/plan-rfc-decisions`, `docs/rfc-reality-sync`, `rfc/001-harness2-jev-unified-cognitive-substrate` | Claude Code lists, User decides | all merges | open |
| L-e4 | `feat/worktree-protocol` (`tools/wt`, `.githooks`, `AGENTS.md`) is not in any of #3 to #10; decide whether it gets a PR | `gh pr list --head feat/worktree-protocol` | User | none | open |

---

## 5. Recurring checks

| Id | What | Command or evidence | When | Owner | Status |
| --- | --- | --- | --- | --- | --- |
| C-1 | Full Python suite on the integration branch (and on `main` after PR #5) | K-3 under K-4; record passed, skipped, failed | Before each merge in section 3 and weekly while PRs are open | Claude Code | open |
| C-2 | Jev suite and type check | K-1, K-2 | Before each Track 1 merge and after | Claude Code | open |
| C-3 | MCP conformance across daemons | K-7 for `memory` (`:8105`), `lookup` (`:8101` staging), `jev` (`:8115`), `harness2` (`:8120`); 12/12 each | After #9 and at Track 4 gate 5 | Claude Code | blocked until #9 merges |
| C-4 | Stale-phrase search | `rg -n 'HANDOFF\|docs/handoffs\|about half\|Both harnesses operate directly\|Fable 5\.1' docs README.md` and read each hit: a hit is allowed only inside a correction note (for example RFC 004 section 9.10) | Before each docs merge | Claude Code | open. Hits found 2026-11-02: see R-1 to R-6 |
| C-5 | Docs reality sync | For every number, SHA, port, flag, env var and test count written in `docs/`, name the source (a file, a command output or a PR) and re-check it; list the ones that moved | Before each docs merge and after each code merge | Claude Code | open |
| C-6 | Claims-versus-transcript check for model attribution | For every sentence of the form "reviewed by <model>" in docs or PR bodies, open the reviewer's transcript and count the model names: `jq -r 'select(.message.model!=null) \| .message.model' <transcript>.jsonl \| sort \| uniq -c`. The names must match the claim. The Fable mix-up (a "Fable 5.1" pass that the transcript shows as `claude-sonnet-5-5`) is the reason this check exists. Location of workflow subagent transcripts: UNVERIFIED | Before any review claim is written, and for every existing claim once | Claude Code | open |
| C-7 | Audit report consistency | For every stage-1 run quoted in docs, compare the quoted counts with `report.json` and `audit.jsonl` (`jq`), counts only | Before quoting a run | Claude Code | open. First finding: R-1 |
| C-8 | Image tag equals source | `uv run python -m cognitive_harness2.image status` | After any change to `egress_proxy.py` or the Dockerfile | Claude Code | open |
| C-9 | Leftover scan after any real or live run | `docker -H unix:///run/user/1000/docker.sock ps -a --filter label=cognitive-harness2.session`, `sbx ls -q`, `sbx policy ls --json` against the saved snapshot, and the run directory's `report.json` `teardown` block | After every run | Whoever ran it | open |

---

## 6. Reconciliation findings against the global plan and RFC 004

Found while writing this plan and RFC 006. Each is a place where the global text and the repository, a run record, or another document disagree. The global plan and RFC index are NOT edited in this phase; these rows are the work list for the reconciliation step.

| Id | Where | What it says | What was verified | Proposed action | Status |
| --- | --- | --- | --- | --- | --- |
| R-1 | RFC 004 section 9.11; plan 3.3.3 | Stage 1c: "no denied requests" | Run `d08d` `report.json`: `sessions_summary.denied_reasons` = `["unix-route-model-denied"]`, `denied_hosts` = `{"harness2-proxy.internal:11434": 14}`, and each `session_end` event in `audit.jsonl` shows `egress_denied` of 8 (for example 7 allowed and 8 denied in one solver session). Totals over the 14 sessions: proposers 20 denied, solvers 56 denied. Earlier runs `a81f`, `1493`, `82cd`, `72a1` show `unix-route-path-denied` (the `/api/show` denials before it was allowed). The cause of the model denials is UNVERIFIED: the proxy denied requests whose `model` field differs from the pinned model. They did not stop the sessions (all exit 0) | Find out what the denied requests were (name and count only) and whether they change behaviour; then correct the sentence to what the audit shows. Cheap and read-only | open |
| R-2 | Plan 3.3.3 "PR stack" | Lists #3 to #6 and "Docs PR -> main after #5" | Eight PRs are open (#3 to #10) with the order #3, #4, #8, #9, #5, #6, #10, #7 and this docs PR last | Rewrite the bullet at merge time (section 3.9) | open |
| R-3 | Plan section 5.1 | Stacked delivery list puts `feat/track3-harness2` on `feat/mcp-conformance` and omits `feat/stage1-integration` | PR #5 is `feat/stage1-integration` → `main` and carries Track 2 until #8 and #9 merge | Rewrite to the true stack | open |
| R-4 | Plan section 6.3 | "Both harnesses operate directly on `~/projects/cognitive-engine`" | Section 6.2 and the repository rule say never work in the hub; one agent, one worktree | Delete or reword section 6.3's first bullet | open |
| R-5 | RFC 004 section 9.6 | Lists seven Claude Code runtime variables as approval markers | The harness2 README at #5 lists nine: it adds `CLAUDE_PID` and `CLAUDE_JOB_DIR`, marked as an ASSUMPTION | Add the two names with the same ASSUMPTION label, or remove them from the code; tie to L-c1 | open |
| R-6 | RFC 004 section 7 (stage 3 acceptance) | "execute without leaking credentials outside container" | Design note and RFC 006: the credential must never be inside the session container | Reword after RFC 006 is accepted | blocked by RFC 006 acceptance |
| R-7 | RFC 004 section 6.2 stage 2; plan "Stage 2 / 3" row | Stage 2 is "user-gated, not started" | Also true but incomplete: at #5's head `stage1.py` pins `ZDR = "strict"` and its preflight accepts only an `opencode` channel with `retention = "local"` on a loopback endpoint; the solver runner refuses a non-local channel. Stage 2 therefore needs code (RFC 006 section 2.3, phase A4) and a key-delivery decision (D9) before the user's go can be used | Add the code prerequisites to the Stage 2 row | open |
| R-8 | PR #5 body | "On the hard task the base harness passes about half the time at k=1" | Superseded by the measured 0 of 8 (RFC 004 section 9.11) | Edit the PR body (M-5.j) | open |
| R-9 | Plan 3.3.3, "Independent read-only verification" | Says the pass was "requested as Fable 5.1, actually served by Sonnet 5.5" | Matches RFC 004 section 9.10 and the transcript (C-6) | No change; keep as the model-attribution reference case | done |
| R-11 | Plan section 3.1 (on the docs base) | Lists "Turnkey installer command: `engine install-adapters`" as a Track 1 deliverable | PR #3's own plan and RFC 002 text say the installer is deferred to the `engine` CLI work and no code for it exists on #3's branch | Resolved by the section 3.9 rule (Track 1 text is owned by #3); re-check after the merge | open |
| R-10 | RFC 004 section 4 acceptance, `GET /api/v1/key` | Ticked, "tested against mocked responses; the live field names are unverified" | Still unverified; Stage 2 needs it live | Keep the caveat; EXP-1 prerequisite in the Experiments plan | open |

---

## 7. Acceptance checks defined by RFC 006

The rows below are the RFC 006 acceptance criteria as checks, so they appear in one tracker. IDs match the RFC.

### 7.1 Part A (Stage 3), all testable with a stub endpoint and a random sentinel

| Id | What | Evidence | Owner | Blocked by | Status |
| --- | --- | --- | --- | --- | --- |
| A-1 | Stub upstream receives the real bearer, never the placeholder; inbound auth headers are dropped | Test name and result | Claude Code | phase A2 | blocked |
| A-2 | Hostile fake CLI cannot find the sentinel anywhere | Test result; `docker inspect` of both containers scanned | Claude Code | phase A3 | blocked |
| A-3 | Route limits: path, model pin, caps, splitting, duplicate headers | Test result | Claude Code | phase A2 | blocked |
| A-4 | SSE passes byte for byte | Test result | Claude Code | phase A2 | blocked |
| A-5 | Host credential file gone after READY; no leftovers | Per-run id leak test | Claude Code | phase A3 | blocked |
| A-6 | `zdr` policy: strict refuses, `--zdr off` needs `open` and the changed phrase; audit fields present, no sentinel | Test result and audit lines | Claude Code | phase A4 | blocked |
| A-7 | Negative control finds the sentinel under option A | Test result | Claude Code | phase A3 | blocked |
| A-8 | Non-tmpfs base refused | Test result | Claude Code | phase A3 | blocked |
| A-9 | Proxy source change changes the image tag | Test result | Claude Code | phase A2 | blocked |
| A-10 | No real secret in any test file | `rg` for token-shaped strings in new tests finds only the sentinel pattern | Claude Code | phase A4 | blocked |
| A-11 | Spike record: hosts, paths, auth form, beta headers, `TMP_EXEC=False` start, OAuth with plain-HTTP base URL, no user settings or hooks loaded | A written record with the stub's request log | Claude Code, after the user's go (D2) | D2 | blocked |

### 7.2 Part B (sbx for model sessions), measured in EXP-3

| Id | What | Evidence | Owner | Blocked by | Status |
| --- | --- | --- | --- | --- | --- |
| B-1 | Real sweep on the hard set completes on sbx: `status ok`, all exit 0, no 600 s timeout | `report.json` | User runs; Claude Code reads | user's typed approval | blocked |
| B-2 | Teardown clean; policy identical to the snapshot | C-9 | User | B-1 | blocked |
| B-3 | Median session wall time within the agreed factor of the Docker reference | `jq` over `audit.jsonl` `session_end` events | Claude Code | B-1 | blocked |
| B-4 | No stream truncation, EOF or proxy-timeout errors | `report.json` `errors` and per-rollout `metrics.json` `errors` | Claude Code | B-1 | blocked |
| B-5 | Every denial is of a pre-registered kind | `sessions_summary` and `egress_denied` counts | Claude Code | B-1 | blocked |
| B-6 | Preflight no longer requires Docker under sbx, or the dual requirement is accepted in writing | Code diff, or a decision note | Claude Code, User | none | open |
| B-7 | One review pass on the code changed since the last Opus pass, no open HIGH finding | L-b2 or L-b3 outcome | User | none | open |
| B-8 | The user accepts the isolation shape (RFC 006 section 3.3) | A written decision | User | none | open |
| B-9 | Both host-reach settings off at run time; restart-pending heuristic passes | `sbx settings ls` read; `stage1 plan` shows the `model_runtime` check ok | User | none | open |
