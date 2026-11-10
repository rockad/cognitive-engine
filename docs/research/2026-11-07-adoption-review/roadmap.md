# Roadmap snapshot: agentic-hub, ax, cognitive-engine (2026-11-07)

Labels: **[V]** I read it in source or git (file:line, SHA or REST API). **[R]** It comes from a README, plan or doc only. **[U]** It is inferred.

Nothing was cloned or run. The three repos were read in place. For cognitive-engine I also read the remote branches and PRs through read-only `git ls-remote` and `gh api`. All three local checkouts are on `claude/gracious-rubin-cbvavl` with `main` alongside [V].

Repo-relative paths below are relative to `agentic-hub/`, `ax/` or `cognitive-engine/`.

**Corrections to the existing note** (`plugins/coordination/docs/research/todo-tools-evaluation-2026-11-07.md`):
- **§6, "PR #3 remediation is not in code".** This is true only on `main`. The open PR #4 (`fix/agent-guard-unknown-agents`) has `packages/jev/src/workflow-guard.ts`, four workflow-guard test files, and a `Workflow` matcher with `onFailure: block` in `adapters/claude/hooks/hooks.json` [V, gh api tree and contents].
- **§2, "Tracks 2 to 4 have no code".** This is true for `main` only. Track 2 is PR #8 and Track 3 is PRs #5 and #6, all open and unmerged [V].

---

## 1. agentic-hub (`agentic-hub`)

**Purpose.** A hub of plugins, skills and MCP integrations that works the same way in Antigravity, Claude Code and OpenCode (`README.md:3`). It is also the home of the planned A2A coordination server (`plugins/coordination`).

**What exists in code [V]**
- Plugins with a `plugin.json`:
  - chrome-devtools 1.0.0
  - gitea 1.0.0
  - google-workspace 1.0.0
  - jev 2.3.0
  - local-transcribe 1.1.0
  - memory 1.0.0
  - subagents 1.0.0
- Both marketplace files list those 7 plugins:
  - `marketplace.json` (its `updated_at` is 2026-10-07)
  - `.claude-plugin/marketplace.json`
- Skills: celi5, clarify, cplain, markdown-convert, mcp-toggle, md-to-pdf, rclone-cloud-storage, skills-doctor.
- CI: `.github/workflows/ci.yml`.
- Git: 73 commits. The latest is `3253985` (2026-11-07, the TODO evaluation).

**`plugins/coordination` is documents only [V]**
- There is no `plugin.json`, server code, Dockerfile or hook script (`plugins/coordination/README.md:7`). The directory holds only `AGENTS.md`, `README.md` and `docs/`.
- `plugins/coordination/AGENTS.md:23-24` says: planning only until the user lifts it.

**Coordination stages** (`master-tasklist.md` §4; plan §5 lines 199-317) [V as recorded]

| Stage | State | Source |
|---|---|---|
| S0 spikes | Done: scaffold, SP1 (asyncRewake wake PASS), SP2 Claude half (PASS with spec changes), SP2 C9 (`${A2A_API_KEY}` expands on every request), SP5 (PASS only with four wrappers), agy A1/A4/A6, spec fold-in on 2026-11-07. Open: SP2 agy A2, A3, A8, the A4 10-session count, A7 write-up; SP3 `PreInvocation`; SP4 (record only); the GUI-launched Claude seeing `A2A_API_KEY`; a `UserPromptSubmit` asyncRewake spike; the agy held-`Stop` park. | `master-tasklist.md:145-161`, plan:228-231 |
| S1 backlog from spikes | Key-bound `Mcp-Session-Id` (403 on mismatch); stateful-only `server/discover`; uvicorn `handle_exit` releases parked listeners with 503; the four storage wrappers; `POST /sessions` requires the key and makes handles unique; listener guard accepts `clear`, owner is `$CLAUDE_PID`; rerun SP5 D2 | `master-tasklist.md:153` |
| S1 dev on the workstation (127.0.0.1:8120) | Not started. Entry: S0 done plus user go. Exit: two-way round trip, Claude idle wake, agy surfacing, key refusal, quarantine end to end | `master-tasklist.md:162-168`, plan:234-256 |
| S2 production on the storage host over the tailnet | Not started. Tailscale is not installed on the workstation. How the tailnet identity reaches the server is unverified | plan:258-270 |
| S3 public HTTPS (Cloudflare Tunnel, the public hostname) | Not started. No route exists. Pending user choice: dedicated loopback port 8121 vs the `Host`-based design | `master-tasklist.md:173-178`, plan:393 |
| Go-live migration | After S3. The importer is built during S2/S3 | `master-tasklist.md:179-181` |
| cognitive-engine status adapter | After S1, off by default, needs user go | `master-tasklist.md:182-183` |
| BuildSlot (cognitive-engine PR #13) | Open: README not updated, spec decisions await review, adoption call sites (C2) after the user's PR go | `master-tasklist.md:128-130`, plan:308-317 |
| A1 clean-alternative survey | Open (watch list) | `master-tasklist.md:101` |

**Other open items in agentic-hub**
- **`TODO.md:1-28` [V].** It still lists Kanzo, OpenUI and OpenRig unchanged. The proposed outcome in eval §7 (lines 216-224) has not been applied: keep Kanzo as a trial after S1, drop OpenUI, reject OpenRig, and add three items (the 8120 port clash, a read-only ax A2A segment, and recording the coordination contract in cognitive-engine RFC 001/004).
- **Documentation drift [V]:**
  - The coordination README is stale:
    - `:8` says "Next step: S0 spikes", but S0 is mostly done.
    - `:9` says branch `feat/coordination`, but commit `8d678bb` moved docs work to `main`.
    - `:20` says plain `DatabaseTaskStore` is used, but SP5 found it insufficient (`master-tasklist.md:151`).
  - Plan §7 item 2 (`consolidated-bridge-v2-plan.md:397`) still calls C9 unverified. `master-tasklist.md:149-150` records it as verified.
  - The root `README.md` and `AGENTS.md` trees leave out `coordination` and `subagents`. `AGENTS.md` also leaves out `mcp-toggle`.
  - There is no root `docs/` folder yet.
- **The memory plugin binds to all interfaces [V].** `plugins/memory/docker-compose.yml:18` and `:46` publish `8110` and `8100` with no host IP, which binds 0.0.0.0. The README claims `127.0.0.1` (`plugins/memory/README.md:3`).

**Dependencies on the other repos**
- cognitive-engine:
  - BuildSlot PR #13.
  - cognitive-engine RFC 006 §3 is the fuzz-pass reference for S3 (plan:286). RFC 006 exists only on the unmerged PR #11 branch `docs/future-plans` [V]. On `main`, `docs/rfc/` has only 001 to 005 [V].
  - The status adapter is to be built inside cognitive-engine.
- ax: none in the coordination plans [V by grep]. The eval note §6 suggests a read-only ax segment [R].

---

## 2. ax (`ax`)

**Purpose.** A Rust and tmux terminal cockpit that launches agy, claude, opencode, codex and custom TOML harnesses as plain interactive processes, one per tab (`README.md:5-20`). It keeps a tab-to-conversation registry, snapshots and restores layouts, and shows per-pane harness telemetry (`README.md:165-172`).

**What exists in code [V]**
- `Cargo.toml:3` version is 0.8.4.
- CLI subcommands (`src/cli.rs:45-176`): attach, switch, project, palette, helper, new, run, preview, ls, bind, close-window, reload, hook, join, unsplit, restore, rename, git-status, install.
- Tests: `tests/` (4 suites). CI runs fmt, clippy `-D warnings`, test, and a bench build. Releases are triggered by tags.
- Git: 50 commits. The latest is `ef5ccf2` (2026-11-05).
- No `send-keys` anywhere in `src`, `lib`, `bin`, `scripts` or `config` [V by grep]. No reference to a2a, jev, cognitive-engine, 8110 or 8100 [V by grep].
- Writes outside its own config [V]:
  - The `ax-tab-sync` `Stop` hook in `~/.gemini/config/hooks.json` (`src/lib.rs:2393-2431`).
  - `custom-title.json` in session folders (`src/core/harness.rs:505-508`).

**Planned or open work**
- There is no roadmap, TODO or issue file in the repo. `docs/` holds only `configuration.md` [V].
- `CHANGELOG.md` stops at 0.7.0 (2026-11-03). The 0.8.x work from 2026-11-04 and 2026-11-05 (six feature commits) is not in it, and `git tag` returns nothing [V].
- The README "CLI Usage" (`README.md:136-161`) leaves out reload, restore, rename, hook, install and git-status [V].
- Ideas filed from outside the repo [R]:
  - Eval §4: a read-only discover-and-adopt fingerprint that maps tabs to A2A handles; an "A2A pending" tab marker.
  - Eval §6: a read-only `GET /sessions` segment.

**Dependencies on the other repos**
- None in code [V].
- Implicit: it shares `~/.gemini/config/hooks.json` with coordination decision 26 (`master-tasklist.md:72`) [V].

---

## 3. cognitive-engine (`cognitive-engine`, `rockad/cognitive-engine`)

**Purpose.** A monorepo that brings together System 1 (Jev, Bun/TS), System 2 (Harness², a Python recursive optimizer running in `sbx` sandboxes) and native memory/lookup (`README.md:3-18`). Track 4 then moves Jev and memory out of agentic-hub and makes agentic-hub a peripheral-only registry (`docs/rfc/005-egress-retirement.md:17-19`, `:118-123`).

**What exists on `main` [V]**
- 24 commits. Only Track 1 is merged: `a3af522`, PR #3, "[merge 1/10 · S14]".
- `packages/jev` and the `adapters/{antigravity,claude,opencode}`.
- `packages/harness2/pyproject.toml` and `packages/memory/pyproject.toml` are empty stubs (`dependencies = []`).
- RFC 001 is "Approved Baseline". RFCs 002 to 005 are all "Proposed" (`docs/rfc/001-*.md:252-258`; `002:5`, `003:5`, `004:5`, `005:5`).

**Open PR merge queue [V]** (`gh api repos/rockad/cognitive-engine/pulls`)

| Order | PR | Head → base | Content |
|---|---|---|---|
| 2/10 | #4 | fix/agent-guard-unknown-agents → main | Workflow and agent guard (PR #3 remediation, WS7) |
| 3/10 | #18 | fix/agent-guard-builtin-config → #4 | Guard built-ins, `CLAUDE_CONFIG_DIR` |
| 4/10 | #8 | feat/track2-memory-lookup → main | Track 2: native memory and lookup (RFC 003) |
| 5/10 | #9 | feat/mcp-conformance → #8 | Conformance runner (WS5) |
| 6/10 | #5 | feat/stage1-integration → main | Track 3: stage-1 pipeline and sbx default |
| 7/10 | #6 | feat/model-bench → #5 | Ollama benchmark |
| 8/10 | #10 | feat/lookup-smb-sources → #8 | SMB sources |
| 9/10 | #7 | docs/stage1-findings → main | Findings, RFC 004 §9; **removes the repo handoff** |
| 10/10 | #11 | docs/future-plans → #7 | **RFC 006**, checks plan, experiments plan |
| no slot | #13 | feat/buildslot-protocol → #5 | BuildSlot (coordination depends on it) |
| no slot | #12 | feat/argon-hardening → #18 | Argon hardening |

- The remote has 41 heads. Many are not in any PR: `feat/buildslot-adoption`, `feat/sbx-*`, `feat/3b-*`, `docs/bridge-turn-11` and others [V].
- What the "S14 to S17" tags mean is not stated [U].

**Planned per the plan** (`docs/plans/2026-10-26-multi-track-implementation-plan.md`) [R]
- Phase order (lines 37-39, 45-70):
  - Tracks 1 and 2 in parallel.
  - Then Track 3.
  - Then Track 4: egress gates, rebinding doppelganger, the gated split of agentic-hub, and tag `v2.4.0-split`.
- Track 3 Slice 2 (lines 220-223):
  - rootless Docker fallback
  - failure triggers
  - DeepMind citation in `NOTICE`
- Workstreams WS1 to WS9 (lines 254-264).
- Deferred:
  - the OpenCode adapter
  - the `engine install-adapters` installer
  - `bin/engine` (lines 95-96, 112-113)
- RFC 005 Phase 1 gates (`005:101-107`):
  - jev tests pass
  - memory on `:8105` under 2 to 3 GB
  - lookup on `:8100` with `docs.visibility`
  - sbx fail-closed
  - egress suites pass in TS and Python
  - `~/.gemini/config/mcp_config.json` registers the cognitive-engine endpoints

**Handoff state**
- `HANDOFF.md:30-31` and `docs/handoffs/antigravity-claude-bridge.md:139-140` both still say "Turn 4 (Claude, Track 2) pending" [V]. They are stale: Track 2 is already PR #8 [V]. PR #7's title says it removes the repo handoff [V, title only].
- The bridge doc gives the harnesses fixed roles: Antigravity is "Orchestrator & Verifier" and Claude is "Deep Builder" (`bridge.md:5-6`; plan §6.1 lines 307-313) [V].

**Dependencies on the other repos**
- On agentic-hub:
  - Recall `:8110` and Lookup `:8100` carry `session-handoff:cognitive-engine` during the transition (plan:313, 342; `bridge.md:9`) [V].
  - Lookup's source is to be taken from `agentic-hub/plugins/memory/servers/lookup` (`001:229`) [V].
  - Track 4 edits agentic-hub: retires `plugins/jev` and `plugins/memory`, rewrites the marketplace, tags `v2.4.0-split` (`005:109-123`) [V].
  - It does not reference `plugins/coordination` at all [V by grep of `docs/rfc/001`].
- On ax: none [V by grep].
- On doppelganger (a fourth repo): Phase 3 rebinds its `.mcp.json` (`005:113-116`).

---

## 4. Cross-repo dependencies, in order

1. **Decide the 8120 owner, before coordination S1 and before PR #5 merges.**
   - Coordination uses `:8120` (`plugins/coordination/AGENTS.md:19`; `master-tasklist.md:53`).
   - Harness² hard-codes `PORT = 8120` with an env override (`packages/harness2/src/cognitive_harness2/settings.py:3,14,48-50` on `feat/stage1-integration`) [V].
   - The env override makes moving Harness² cheap [U].
2. **cognitive-engine records the coordination contract in RFC 001 or 004.** That is the `status-client.md` contract and "sbx never reaches the server". It should land before Track 3's sbx allow rules settle (`plugins/coordination/AGENTS.md:91`; eval §6) [R].
3. **cognitive-engine PR #13 (BuildSlot) needs a merge slot.** Coordination lists BuildSlot as "still in force" (`master-tasklist.md:84`; plan:308-317), but #13 is marked "no slot" in the queue [V]. Order: #5, then #13, then the C2 adoption call sites.
4. **cognitive-engine PR #11 must merge before the coordination S3 fuzz pass.** That pass cites RFC 006 §3 (plan:286), which exists only on `docs/future-plans` [V].
5. **Coordination S1 must exist before the cognitive-engine status adapter** (`master-tasklist.md:182`), and the user must give a go.
6. **Coordination S1 must exist before an ax read-only A2A segment.** That segment would use the `local-harness` key and `GET /sessions` [R, eval §6]. The coordination installer must also merge, never replace, `~/.gemini/config/hooks.json` alongside ax's entry (eval §6) [R].
7. **Track 2 (PR #8) plus the RFC 005 gates must pass before agentic-hub retires `plugins/jev` and `plugins/memory`.** agentic-hub keeps running Recall and Lookup until then. Coordination must not depend on memory (`plugins/coordination/AGENTS.md:19`) [V].
8. **Coordination S3 exit, then go-live.** The importer should cover the frozen `~/.agents` channel. The cognitive-engine bridge doc should either be added to that scope or retired explicitly; PR #7 already removes it [R].
9. **doppelganger rebinding (RFC 005 Phase 3) must come before the agentic-hub tag `v2.4.0-split`** (`005:113-123`).

---

## 5. Top 10 open decisions and risks

1. **Port 8120 clash (verified in code).** See dependency 1. It needs a decision before S1. The candidates from eval §6 are 8125 and 8130.
2. **Port 8100 clash during migration.**
   - Native `packages/lookup` takes `127.0.0.1:8100` (`RFC 003:45`; plan:166).
   - The legacy lookup still publishes `8100` (`plugins/memory/docker-compose.yml:46`).
   - Memory was given a staging port (8105). Lookup was not.
   - The RFC 005 gate (`005:104`) cannot be tested while the legacy stack is up [U].
3. **RFC 005 Phase 4 scope against current agentic-hub.**
   - The rewritten marketplace keeps only chrome-devtools, gitea, google-workspace and local-transcribe (`005:121`; `001:299`). That silently drops `subagents` and leaves no place for `coordination`.
   - agentic-hub's `AGENTS.md` still documents jev and memory as first-class plugins, and neither has a deprecation banner [V].
   - The user needs to decide whether agentic-hub accepts this split.
4. **Jev is maintained in two places.** agentic-hub `ae8b851` (2026-11-03) adds Argon tiers to `plugins/jev` while cognitive-engine PR #12 does Argon hardening in `packages/jev` [V]. They can drift until the freeze in RFC 005 Phase 2.
5. **ax can wipe `~/.gemini/config/hooks.json` [V].**
   - Every `ax attach` calls `ensure_hook_installed()` (`src/lib.rs:328`).
   - If the file fails to parse, it is replaced by `{}` plus `ax-tab-sync`. Then `fs::write` writes the whole file (`src/lib.rs:2400-2429`), and it writes through a symlink.
   - Coordination decision 26 puts the agy hooks in this same file, and decision 16 requires that harness config change only through the user-run installer and chezmoi.
   - A malformed file or a chezmoi-managed file would lose coordination's entries or drift [U for the chezmoi-managed case].
6. **The cognitive-engine merge queue is long [V].** Nine PRs are open, mostly stacked (#4, #18, #8, #9, #5, #6, #10, #7, #11), plus #12 and #13 with no slot, plus about 25 heads not in any PR. Only Track 1 is on `main`. The RFC status matrix still says "Proposed" for Tracks 1 to 4 (`001:252-258`). Reviews depend on one outside reviewer, @Doc-Cr (plan §5).
7. **The agy half of S0 is user-run and stalled.** A8, A2, A3, SP3 and the 10-session count are open, and session `98480dc1` stopped before step 3. A host change still has to be put back: `trustedWorkspaces` in `~/.gemini/antigravity-cli/settings.json` (`master-tasklist.md:157-158`) [V as recorded]. This is the S1 entry gate.
8. **cognitive-engine's working model conflicts with coordination's principles.**
   - The plan and the bridge doc give fixed Orchestrator and Builder roles and a hand-relayed turn file (`bridge.md:5-6`; plan:331-337).
   - Coordination requires symmetric agency and froze the file-channel pattern (`plugins/coordination/AGENTS.md:37`, `:60-65`).
   - The handoff is stale (Turn 4 is "pending" while PR #8 is open) and is due for removal by PR #7.
   - The user should decide whether cognitive-engine coordination moves onto A2A after S1.
9. **Exposure and auth hygiene.**
   - Recall and Lookup publish on 0.0.0.0 without auth (`docker-compose.yml:18`, `:46`). This contradicts the README's `127.0.0.1` and coordination's "no non-loopback bind" norm (`plugins/coordination/AGENTS.md:92`) [V].
   - Coordination S2 and S3 still depend on unverified mechanisms: the tailnet identity, telling `cloudflared` traffic from `tailscale serve` traffic, and the Cloudflare request timeout (plan:399-400) [R].
   - Pending at S3: the dedicated 8121 public listener (plan:393).
10. **Documentation drift will mislead the consolidated document [V]:**
    - The coordination README status, branch and `DatabaseTaskStore` lines (`README.md:7-9`, `:20`).
    - Plan §7 calls C9 "unverified" against `master-tasklist.md:150`.
    - ax `CHANGELOG.md` stops at 0.7.0 while `Cargo.toml` is 0.8.4, there are no tags, and the README CLI list is incomplete.
    - `marketplace.json` `updated_at` is 2026-10-07.
    - The agentic-hub `README.md` and `AGENTS.md` trees leave out coordination and subagents.
    - The `TODO.md` outcome from eval §7 has not been applied.
    - Kanzo is the one item that still needs a user decision: trial it after S1 (stdio only) or drop it.

**For the TODO.md move.** No `docs/` folder exists at the agentic-hub root [V]. Of TODO.md, only the Kanzo item (trial after S1) and the three new items from eval §7 are still live. OpenUI and OpenRig can be closed with the reasons recorded in eval §4 and §5.
