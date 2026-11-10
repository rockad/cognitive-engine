
# OpenRig (mvschwarz/openrig): source deep-read, 2026-11-07

I cloned the repo with `--depth 1` into `/tmp/claude-0/-home-user/84e5c850-1d48-5bf0-99bf-2742d6707467/scratchpad/src/openrig`. HEAD is `a979ac5` (2026-11-07 13:41 -0800, PR #2696). I ran none of its code. All paths below are relative to the repo root. `D/` = `packages/daemon/src/` and `C/` = `packages/cli/src/`.

Labels: **[V]** = verified (read in source, file:line given), **[R]** = reported (README/CHANGELOG only), **[U]** = unverified (inferred).

## 1. How messages reach a seat: yes, text is typed into tmux panes

- **[V] The core transport is tmux paste plus Enter.** `D/adapters/tmux.ts:932-1005` `sendText`/`sendTextUnchecked` does this:
  1. writes the text to a 0600 temp file
  2. runs `tmux load-buffer -b <buf> <file>` (`:963`)
  3. runs `tmux paste-buffer -t <pane> -b <buf> -d -r [-p]` (`:971`)

  `-p` is bracketed paste and `-r` keeps raw line feeds. The docstring at `:916-930` explains the flags. Submission is a separate `tmux send-keys -t <pane> Enter`: `sendKeys`/`sendKeysUnchecked` at `:1074-1100`, with the send-keys call at `:1094`. If a read-only client refuses send-keys, it falls back to pasting a bare `\r` (`:1080-1085`).
- **[V] A tmux control-mode client is used as an accelerator.** `D/adapters/tmux-control.ts:55` lists `send-keys`, `paste-buffer` and `set-option` among the commands it forwards. A short `load-buffer` is rewritten as `set-buffer` (`:126-151`).
- **[V] `rig send` goes through the daemon to the same transport.** It calls `POST /api/transport/send`, which calls `transport.send` (`D/routes/transport.ts:145`). `SessionTransport.send` (`D/domain/session-transport.ts:2585-2598`) is documented as "Deliver text and Enter to the resolved recipient". The file is 4936 lines, mostly screen-scraping of the Claude and Codex composers to check for drafts, queued input and confirmed submission (`:1874` `readInputBox`, `:1137`).
- **[V] `rig broadcast` fans the same typing path out to every recipient.** `D/routes/transport.ts:390` calls `transport.broadcast`, described as "raw-to-all" (`session-transport.ts:2019-2023`).
- **[V] The chatroom does not type into panes.** `D/routes/chat.ts:50-58` stores the message (`chatRepo.send`) and emits a `chat.message` event on the event bus. Nothing in that route calls the transport. Seats read it with `rig_chatroom_watch`.
- **[V] Queue rows are durable in SQLite, but the wake is typed.** The nudge is a pointer plus a sanitized summary of at most 120 characters, and it is pasted into the pane:
  - `D/domain/queue-nudge-text.ts:1-30` says "author-supplied text typed into another seat's pane" and strips control, format and bidi characters.
  - `D/domain/queue-repository.ts:380-392` is the `QueueNudgeTransport.send` interface.
  - `D/domain/queue-wake-ladder.ts:16-25` states the invariant: the row is delivered exactly once, the wake at least once. Failed wakes get a retry ladder, then escalate to the "destination's orchestrator", then to the operator.
- **[V] Other typed senders.** All of these call `sessionTransport.send`:
  - the claim/adopt hint (`D/domain/claim-service.ts:259`)
  - the compaction enforcer (`claude-compaction-enforcer.ts:875,1340,1409`)
  - the watchdog (`watchdog-delivery.ts:43`)
  - the model monitor (`startup.ts:2579`)
- **[V] Paths that do not type:**
  - **Refocus** puts context in through a Claude `UserPromptSubmit` hook's `additionalContext` (`packages/daemon/assets/plugins/openrig-core/hooks/scripts/refocus.cjs:5-8,559`).
  - **Hosted members** (outside tools) get a webhook "ring" that carries a row pointer, never the body (`D/domain/hosted-wake.ts:1-3`).
  - **Claude `--channels`** support exists for plugin channels (`D/domain/claude-plugin-channels.ts:1-10`), but it is not the default delivery path.
- **[V] Typing guard.** The modes are `off | draft-aware | hold`, and the interruption levels are `openrig-urgent | urgent-only | sends-only` (`D/domain/seat-delivery-guard.ts:51-56`). The guard either holds automatic text or merges it into a human draft. When it delivers into a draft, it pastes at the cursor, then sends Enter (`session-transport.ts:1137`).
- **[V] No node-pty.** I found no `node-pty` import in the packages.

## 2. What `rig discover` and `rig adopt` do to an existing tmux session

- **[V] Discover is read-only.** `D/domain/tmux-discovery-scanner.ts:36-61` only calls `listSessions`, `listWindows`, `listPanes`, `getPanePid` and `getPaneCommand`.
- **[V] Adopt/bind (`ClaimService`) does six things:**
  1. Sets session user options `@rigged_node_id`, `@rigged_session_name`, `@rigged_rig_id`, `@rigged_rig_name` and `@rigged_logical_id` (`claim-service.ts:222-234`, called at `:431-436`).
  2. For a Claude runtime, provisions the context collector (`:438`). This copies `.openrig/context-collector.cjs` into the session's cwd and merges a `statusLine` into `<cwd>/.claude/settings.local.json` (`D/adapters/claude-code-adapter.ts:1170-1212`).
  3. Starts transcript capture with `tmux pipe-pane` (`:440`; `tmux.ts:1582-1593`).
  4. **Types an identity hint into the pane:** `--- OpenRig: You have been adopted into rig "<rig>" as <id>. Run: rig whoami --json ---` (`:244-262`, called at `:444`; same again at `:885-891`).
  5. Captures the resume token by file read only.
  6. Does not rename the session, kill it or restart its process. I found no `rename-session` anywhere in the daemon.
- **[V] The `rig reconcile-session` path is the only no-input adopt.** Its "safety line" (`claim-service.ts:476-485`) rules out sendText, sendKeys, the hint and launch. It may still set session options and start pipe-pane (`:633-645`).

## 3. Files it writes in $HOME and in the project

`C/commands/setup.ts:164-221` is a self-disclosed inventory of these writes **[V]**.

| File | How it writes | Merge or replace | Backup |
|---|---|---|---|
| `~/.tmux.conf` | `rig setup` (`C/commands/setup.ts:615-640`) | Writes a marker block `# OpenRig managed block … # End` with `set -g mouse on` and `set -g history-limit N`. Replaces the block in place by regex, appends it, or creates the file. Plain `writeFileSync` (`:233`), not atomic | none [V] |
| `~/.claude.json` (or `$CLAUDE_CONFIG_DIR/.claude.json`) | `claude-code-adapter.ts:1100-1120` | JSON read-modify-write. Sets `projects[<cwd>].hasTrustDialogAccepted=true` and `hasCompletedOnboarding=true`. Leaves non-object state alone. Not atomic | none [V] |
| `<cwd>/.claude/settings.local.json` | `statusLine` at `claude-code-adapter.ts:1183-1212`. Activity hooks at `:1219-1300+`: owned entries stripped and re-added for `Notification`, `PermissionRequest`, `PostCompact`, `PostToolBatch`, `PreCompact`, `SessionStart`, `Stop`, `StopFailure`, `UserPromptSubmit`. Team fragments via `mergeJsonFragment` at `:976` | Merge. Never overwrites a user's own `statusLine`. Skips a file it cannot parse. The comment at `:1168` says it "is not safe against a concurrent writer" | none [V] |
| `<cwd>/.openrig/context-collector.cjs`, `<cwd>/.mcp.json` | `claude-code-adapter.ts:1178-1180`; setup disclosure `:186-191` | Copy for the collector, merge for `.mcp.json` | — [V/R] |
| `CLAUDE.md` / `CLAUDE.local.md` (Claude seats), `AGENTS.md` (Codex/Pi seats) | `D/domain/managed-blocks.ts:43-103` | HTML-comment-delimited managed blocks: replace, append, or create the file. `managed-block-records.ts:1-20` keeps a sha256 of what it wrote so it can spot human edits. There is no symlink or `lstat` check before writing, so the write follows a symlinked `AGENTS.md` [V]. Codex ≥0.164 and Pi fresh seats now get guidance outside `AGENTS.md` (Codex as `developer_instructions`), but resume, fork, restore, handover and older Codex still use `AGENTS.md` [R: CHANGELOG.md:248-256] | `rig down` "restores your guidance files byte for byte" [R: CHANGELOG.md:2401-2402] |
| `$CODEX_HOME/config.toml` | Project trust via a line-based text splice inserting `trust_level = "trusted"` (`codex-runtime-adapter.ts:2081-2096`). Hook and plugin trust via temp file plus `renameSync` (`codex-owned-plugin-trust.ts:141`). The disclosure (`setup.ts:217-220`) says these settings reach your own Codex sessions too. It overrides your home `developer_instructions` for a session when `CODEX_HOME` is set only in your shell [R CHANGELOG:256-259] | Merge (text splice). The plugin-trust write is atomic | none seen [V] |
| `.git/info/exclude`, `.codex/plugins/<plugin>` | `setup.ts:204-215` | Append ignore entries | — [R] |
| Skills | Project scope: `<cwd>/.claude/skills/<id>` or the seat's skills root (`claude-code-adapter.ts:156,253,963`). It reads `~/.claude/skills` and `~/.agents/skills` (`profile-resolver.ts:84`). I did not confirm writes to `$HOME` skill folders [U] | copy | — |
| tmux server options | `set-option -s set-clipboard on` and a server-scope `copy-command` on whichever server the daemon uses (`D/domain/tmux-option-defaults.ts:118,125`) | Server-wide | — [V] |

`rig setup` also runs installers: brew on macOS, `npm install -g @openai/codex`, and herdr (`setup.ts:600-605,240-260`) [V]. The CHANGELOG admits read-modify-write races and gives no rollback guarantee for the global files [R].

## 4. Harness support and permission flags

- **[V] Built-in runtimes** are `claude-code`, `codex`, `pi`, `omp`, `terminal` and `stub` (`D/domain/built-in-runtimes.ts:3`).
- **[V] There is no built-in Gemini CLI, Antigravity or OpenCode runtime.** Gemini and OpenCode appear only as provider API-key names for Pi (`pi-runner-protocol.ts:125,143`). Users can add untested "community harness" YAML presets in `$OPENRIG_HOME/community-harnesses/` (`community-harness-presets.ts:1-10`), so agy could be wired in by hand.
- **[V] `@openrig/acp` runs the other direction.** It makes OpenRig an ACP agent for Zed and JetBrains (`packages/acp/README.md`); it does not drive ACP agents.
- **[V] Claude** launches with `--permission-mode <mode>` (`claude-code-adapter.ts:411,434`). The default is `acceptEdits` in every `native-permission-store.ts` branch (`:191,199,241,249`). Full bypass is opt-in ("YOLO", `:316`).
- **[V] Codex** launches with `-s workspace-write` (the "floor") by default, or `-s danger-full-access` under full bypass (`codex-runtime-adapter.ts:931-935,971`). **Seats under the "kernel" launch default get `full_bypass` for Codex** (`native-permission-store.ts:197-200`).

## 5. Team topology and shared memory

- **[V] It is a lead/orchestrator model.** Rigs have `delegates_to` edges. `defaultResolveOrchestrator` takes the delegating parent seat (`D/domain/queue-owner.ts:47-60`). "A rig's representative" is its single delegation root (`:62-67`). Escalation goes to that parent (`escalation-owner.ts:26-32`) and the wake ladder escalates to the "orchestrator" (`queue-wake-ladder.ts:18-22`).
- **[R] The shipped teams are led.** `workshop` and `factory` are led by `orch-lead@…` ("the lead runs the team") (`README.md:7,152-153,338-339`).
- **[V] Shared memory is not a memory store.** It is the daemon's SQLite database (better-sqlite3), holding queue rows, the chat repository, transcripts from pipe-pane, seat recaps and context packs (`D/domain/context-packs/*`, `seat-recap-store.ts`). There is no vector or entity store. Seats read it through `rig_context_get`, `rig_context_pack` and `rig_artifact_read`.

## 6. MCP tools, daemon bind address and auth

- **[V] The MCP server (`C/mcp-server.ts`, plus HTTP in `C/mcp-http.ts`) registers 37 tools:** `rig_add`, `rig_agent_validate`, `rig_artifact_read`, `rig_bind`, `rig_bundle_inspect`, `rig_capture`, `rig_chatroom_send`, `rig_chatroom_watch`, `rig_context_get`, `rig_context_pack`, `rig_discover`, `rig_down`, `rig_environment_export`, `rig_human_decision_room`, `rig_mission`, `rig_ps`, `rig_queue_{claim,create,decide,handoff,list,show,unclaim,update}`, `rig_restore`, `rig_restore_status`, `rig_rig_nodes`, `rig_rig_validate`, `rig_send`, `rig_slices`, `rig_snapshot_create`, `rig_snapshot_list`, `rig_status`, `rig_team`, `rig_telemetry_events`, `rig_up`, `rig_whoami`. They are gated per tool by `offered()` (`:671`).
- **[V] Daemon bind:**
  - Port 7433 by default (`D/index.ts:257`).
  - The default bind is `127.0.0.1` **plus the Tailscale interface whenever one is detected** (`D/domain/bind-plan.ts` `resolveBindPlan`; `index.ts:289-318`).
  - `OPENRIG_BIND_HOST` sets an explicit bind.
  - A guest activity server binds `127.0.0.1` (`index.ts:346`).
- **[V] Auth:**
  - `authBearerTokenMiddleware` **passes every request when no token is configured** (`middleware/auth-bearer-token.ts:102-105`).
  - The terminal token, which guards `/api/transport` (send/broadcast), `/api/terminal` and `/api/compaction` (`server.ts:819-881`), comes only from `OPENRIG_TERMINAL_BEARER_TOKEN`. It is otherwise forced only for non-loopback, non-tailnet explicit binds (`index.ts:306-314`).
  - So by default anyone on the loopback interface or the tailnet can type into panes. The only check is the `browserBoundary` Host/Origin check (`server.ts:713-719`).
  - Seat identity comes from the `X-OpenRig-Session` header, which the code itself says "isn't authentication" (`routes/require-sender-identity.ts`, header comment).
  - The activity hooks use their own `OPENRIG_ACTIVITY_HOOK_TOKEN` (`activity-relay.cjs:6-11`) and POST to `/api/activity/hooks` (`:164`).

## 7. Maturity

- **[V]** License is Apache-2.0 (`LICENSE`).
- **[V]** Workspace `package.json` files say 0.7.1 (`acp`, `cli`, `daemon`, `ui`) and 0.1.0 (`tui`). CHANGELOG's newest entry is 0.7.9, published 2026-11-06 (`CHANGELOG.md:11-14`). It has 54 release headings back to 0.2.0 on 2026-04-22 (`:8079`).
- **[V]** Last commit is 2026-11-07 (today), PR #2696.
- **[V]** 2059 `*.test.ts(x)` files, vitest, and CI in `.github/workflows/tests.yml`. The repo has about 4500 files.
- **[R]** Node 22/24.
- I could not get stars or contributors: `gh` access to this repo is not enabled for the session.
- Release cadence is very high, and pre-1.0 releases carry behaviour changes (`CHANGELOG.md:6-7`). The 0.7.8 OpenShell worker is still unusable in 0.7.9 (`:23-25`).

## 8. Verdict

**Reject as infrastructure. Mine it for ideas.** This confirms the existing note at `agentic-hub/plugins/coordination/docs/research/todo-tools-evaluation-2026-11-07.md` §4. Several of that note's "reported" claims are now verified, and three findings are new: the default Tailscale bind with no auth, the typed adopt hint, and server-wide tmux options.

| Rule | Finding |
|---|---|
| **DDR-003** (never launch, drive or wrap agy; no send-keys) | Violated by design. The primary transport is paste-buffer plus send-keys Enter (§1). Adopt types into the adopted pane (§2). A community-harness preset could launch agy. If the daemon starts inside an ax pane it inherits `$TMUX` and runs on ax's server (`D/adapters/tmux-server.ts:20-37`) [V], where `rig adopt` would reach agy panes [U] |
| **Symmetric agency** | Violated. It uses `delegates_to` edges, an orchestrator, a rig "representative" root, and escalation up the tree (§5) |
| **Decision 27** (never write `AGENTS.md`) | Violated. Managed blocks go into `AGENTS.md`/`CLAUDE.md` and the write follows symlinks (§3). The Codex ≥0.164 opt-out is partial |
| **Decision 16** (config only through chezmoi) | Violated. It writes `~/.tmux.conf`, `~/.claude.json` and `~/.codex/config.toml` directly, non-atomically for the first two, with no backup. It also runs `npm -g` and brew installers |
| **API key on every request** | Violated. Default no-token pass-through, and the network surface is widened to the tailnet (§6) |
| **Tasks parked, never auto-executed** | Violated. Queue handoffs type a nudge into the recipient's pane to wake it, and the ladder retries the nudge (§1) |
| **Jev guard** (cognitive-engine) | Unchanged from the note [U]. Settings fragments and `--permission-mode` are merged per launch, and kernel-default Codex seats run `danger-full-access` [V], which bypasses any tier ceiling enforced by sandboxing |
| **ax** | Conflicts [V for each side's config, U for runtime behaviour]: |

The ax conflicts in detail:

- ax renders global `set -g mouse on` and `set-hook -g client-detached/window-unlinked "run-shell 'ax reload'"` (`ax/src/tmux/profile.rs:20-33`). ax's `isolated_socket` defaults to false (`ax/src/config.rs:443`).
- OpenRig adds its own `~/.tmux.conf` block that duplicates `mouse on`.
- On whatever server it inherits, OpenRig sets server-wide `set-clipboard on` and `copy-command`.
- It creates a hidden control session on that server with a session-scoped `client-detached` kill hook (`tmux-control.ts:505-511`). This session would appear in ax's session enumeration [U].
- Each seat session it creates or kills would fire ax's global `window-unlinked` reload hook [U].
- It sets per-window and per-pane options (`tmux.ts:1105,1299,1327`).
- Coexistence would require ax `isolated_socket = true` and OpenRig never started from inside an ax pane.

### Ideas worth borrowing (concepts, not code)

1. **Durable row, at-least-once pointer wake.** The row carries the obligation exactly once; the wake is an idempotent pointer (`queue-wake-ladder.ts:16-25`). This maps to A2A task plus `asyncRewake` stub, with a bounded retry ladder and then operator escalation instead of a silent park.
2. **Sanitize any peer text shown to another agent.** `queue-nudge-text.ts:1-30` turns C0, C1, `\p{Cf}`, `\p{Zl}`/`\p{Zp}` and bidi characters into spaces, caps length at 120, and prevents forged envelope lines. Apply this to A2A message previews and status summaries.
3. **Refocus through `UserPromptSubmit` `additionalContext`** (`refocus.cjs:5-8,559`). This is the non-typing context-injection pattern our wake design already prefers. It is evidence that it works in practice for long sessions.
4. **Webhook ring that carries a pointer only.** The receiver reads under its own grant, and a 2xx means "accepted ring", not "picked up" (`hosted-wake.ts:1-3`). This is the same contract as A2A push notifications.
5. **Hash records for managed content.** `managed-block-records.ts` lets the tool tell "we wrote it" from "a human edited it". Useful for the chezmoi-managed config and for ax's generated `ax.conf`.
6. **A self-disclosed write inventory** (`setup.ts:164-221`). Every file, its scope and purpose. Coordination's installer should print the same.
7. **Bind intent kept separate from routing env.** `OPENRIG_BIND_HOST` is distinct from `OPENRIG_HOST` (`bind-plan.ts` header), which avoids an inherited client URL silently changing the listener. The anti-pattern to avoid is auto-binding the tailnet with no auth.
8. **Read-only discovery plus tmux user options for identity.** `@rigged_*` options (`claim-service.ts:222-234`). ax could stamp `@a2a_handle` per pane and read it, with no typing (the note's discover/adopt-read-only idea).
9. **Typing-guard and interruption-level vocabulary** (`seat-delivery-guard.ts:51-56`). Useful for ax's "A2A pending" indicator and the coordination wake budget.
10. **Activity hooks that exclude prompt text and tool arguments,** with a per-launch hook token, best effort, 1.5 s timeout (`activity-relay.cjs:1-11`). This matches our status contract.
