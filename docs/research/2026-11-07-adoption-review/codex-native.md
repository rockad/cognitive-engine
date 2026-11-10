# Codex native features compared with coordination, ax and Jev

**Scope and how I checked.** I could not fetch the official docs: `developers.openai.com` is outside this session's allowed network domains and has to be allowed in the session's network settings before those pages can be read. Instead I read OpenAI's own source code (`openai/codex`, shallow clone of `main` at `aa9b9cc`, committed 2026-11-07) in `openai/codex`. Labels used below:
- **verified-in-source**: I read the first-party code at that commit. This is not the docs, and a feature on `main` may not be in a release yet.
- **official-docs (search snippet)**: from a search result showing a developers.openai.com page; the page itself was not opened.
- **reported**: a third-party source.
- **unverified**: no source found.

I installed and ran nothing; the clone was read only.

## Local mentions (rg)

- **coordination docs** have no Codex peer design yet. Codex comes up only in rejected bridges:
  - `codex exec` per-turn spawns in `toshiotomikawa/a2a-bridge`, which fails the live-session rule.
  - `codex app-server --stdio` in a third-party example (`t1c-bridges-and-sdks.md:47`).
  - switchboard wakes Codex with a held Stop `mcp_tool` hook returning `decision: block` (`t1b-wake-mechanisms.md:37`).
  - interlink and agent-bus wake Codex through `codex queue` (`t1b:107,175,233`). The audit marked this "out of scope", not "rejected".
  - OpenRig writes `trust_level = "trusted"` and hooks into `~/.codex/config.toml` (`todo-tools-evaluation-2026-11-07.md:136`). That breaks decision 16.
- **ax**: `CodexDriver` in `ax/src/core/harness.rs:251-290` runs a bare `codex` with args, workdir and env. Unlike agy, it reads no history or title. agy gets this from `~/.gemini/antigravity-cli/conversation_summaries.db` (`ax/src/core/session.rs:180`). Codex has no hotkey.
- **cognitive-engine docs**: no Codex mentions. The guard adapters cover `claude` (matcher `Agent|Task`, returns `ask`), `antigravity` (`invoke_subagent`) and `opencode`.

## Features

### 1. AGENTS.md handling (verified-in-source, `core/src/agents_md.rs`)
- Codex collects every `AGENTS.md` from the project root down to the cwd and joins them.
  - The project root is found with `project_root_markers` (default `.git`). Codex does not walk above it.
- `AGENTS.override.md` is the local override file.
- `project_doc_fallback_filenames` and `project_doc_max_bytes` (default 32 KiB, `core/src/config/mod.rs:269`) control what is read.
- The global file is `$CODEX_HOME/AGENTS.md`.
- There is a migration helper that imports Claude configuration and writes `AGENTS.md`, including `$CODEX_HOME/AGENTS.md` (`external-agent-migration/src/service.rs:775-783`). Its feature flag `external_migration` is `Removed`, but the code is still there.
- Relevance: the "never write AGENTS.md" rule applies to Codex too. The inbox instruction must come from a hook (see 5), not `AGENTS.md`. Check whether `~/.codex/AGENTS.md` is a symlink into the dotfiles, the same way `~/.gemini/config/AGENTS.md` is.

### 2. `codex exec` (verified-in-source)
- Runs one task non-interactively, plus `codex exec resume --last|<id>` (`exec/src/cli.rs:179`).
- This is a one-shot subprocess, the same pattern as `claude -p`. **It is prohibited as a coordination transport.**
- It is fine for Jev free-tier or offline batch work that has nothing to do with live peers.

### 3. `codex mcp-server` (Codex as an MCP server)
- Older docs and mirrors describe `codex mcp-server` exposing two tools, `codex` (new session) and `codex-reply` (`conversationId` + prompt), with approvals sent as MCP elicitations (reported: [PR #4811](https://github.com/openai/codex/pull/4811/files), [mirror of codex_mcp_interface.md](https://git.halfakop.ru/rad/codex/src/commit/5728bd67d03aae737ca6c47df85d1148c9b0cc7b/codex-rs/docs/codex_mcp_interface.md)).
- **At `aa9b9cc` there is no `McpServer` subcommand** in `cli/src/main.rs` (verified-in-source by absence). Only a stale comment remains (`codex-mcp/src/elicitation.rs:434`). The app-server has taken its place. Whether a release removed it is unverified.
- Either way, it makes Codex a subordinate tool of another agent, starting its own sessions. That breaks symmetric agency and the live-session rule. **SKIP for coordination.**

### 4. MCP client config (verified-in-source, `config/src/mcp_types.rs:281-451`)
- `[mcp_servers.<name>]` accepts `command` / `args`, or `url` + `bearer_token_env_var`, plus `startup_timeout_sec`, `tool_timeout_sec`, `enabled_tools` and a deny-list.
- `codex mcp add|list|login` manages them.
- Relevance: `url = "http://<host>:8120/mcp"` with `bearer_token_env_var = "A2A_API_KEY"` reuses the env-var key handling already chosen for Claude (decision 24). No key goes into `config.toml`.

### 5. Hooks (verified-in-source; official-docs (search snippet) [hooks](https://developers.openai.com/codex/hooks))
- **Status:** stable and on by default (`features/src/lib.rs:1513`, key `hooks`; the old key is `codex_hooks`).
- **Config:** `hooks.json` or inline `[hooks]` in `config.toml`, at every config layer.
- **Review:** new or changed hooks need trust review in `/hooks`.
- **Managed-only mode:** admins can set `allow_managed_hooks_only` in `requirements.toml` (`docs/config.md`).
- **Events** (`protocol/src/protocol.rs:1808`):
  - `PreToolUse`
  - `PermissionRequest`
  - `PostToolUse`
  - `PreCompact`
  - `PostCompact`
  - `SessionStart`
  - `SessionEnd`
  - `UserPromptSubmit`
  - `SubagentStart`
  - `SubagentStop`
  - `Stop`
  - `Interrupt`
  - `PreModelRequest`
  - `PostModelRequest`
- **Handler types:** `command`, `mcp_tool`, `prompt`, `agent`.
- **Execution modes:** `sync` and `async`.
- **Output shapes** (`hooks/src/schema.rs`) are close to Claude's:
  - `SessionStart` returns `hookSpecificOutput.additionalContext`.
  - `Stop` returns `decision: "block"` with a required `reason`.
  - `PreToolUse` returns `permissionDecision` (`allow` or `deny`), `updatedInput` or `additionalContext`.
  - **`permissionDecision: "ask"` is parsed but unsupported.** The hook run is marked failed and the call goes ahead (fails open) (`hooks/src/events/pre_tool_use.rs:602`).
- **Default timeout** is 600 s. Only `SessionEnd` has a maximum (`hooks/src/engine/discovery.rs:950-968`).
- **Async hooks:**
  - At most 8 run at once per session.
  - Their `additionalContext` is drained only at the next turn boundary (`core/src/hook_runtime.rs:812-858`).
  - Their `Stop` and feedback outputs are dropped (`command_runner.rs:160`).
  - **So an async hook cannot wake an idle session. There is no `asyncRewake` equivalent** (verified-in-source).

### 6. Subagents and multi-agent (verified-in-source)
- **Tools:** `spawn_agent` (args include `model`, `reasoning_effort`, `agent_type`), `send_message`, `followup_task`, `wait_agent`, `close_agent` and `resume_agent`.
  - `multi_agent` v1 is deprecated but on by default.
  - `multi_agent_v2` is `Stable` but off by default (`features/src/lib.rs:1641-1650`).
- **Agent roles:**
  - `[agents.<name>]` in `config.toml`.
  - `*.toml` files in `<config layer>/agents/`, meaning `~/.codex/agents/` and the project `.codex/agents/` (`agent-roles/src/loader.rs:78`).
  - `[agents] max_threads` and `max_depth` limits.
  - Roles are TOML. They are not the markdown persona files Jev's discovery reads for Claude and agy.
- **Under development, all off by default:**
  - `wake_on_agent_messages`
  - `agent_message_board` and `agent_message_board_mcp` (`message-board-mcp/src/lib.rs`: "MCP adapters and HTTP transport for existing board tools")
  - `agent-graph-store` ("parent/child topology for thread-spawned agents")
  - These are Codex-internal mailboxes and boards between threads. Worth watching as native competitors to a ready-work board, but not usable now.
- **`sleep` tool:** stable and on by default, up to 12 h. It "ends early when new input arrives for the active turn" (`core/src/tools/handlers/sleep.rs:28,51`).

### 7. `codex queue` and the app-server thread queue (verified-in-source; reported dates)
- `codex queue --thread <uuid|name> --message <text> [--resume] [--remote]` (`cli/src/queue_cmd.rs:20-39`) calls `thread/queue/add` on the shared app-server daemon.
- `daemon_auto_start` is stable and on by default (`features/src/lib.rs:1136`).
- `--no-daemon` is refused, so the message goes through the same server the TUI uses (`tui/src/session_queue_commands.rs:39`).
- The queue is durable and per thread, with add, list, update, delete, reorder and start.
- **When the thread goes idle, it starts the next queued message as a turn by itself** (`ext/queue/src/service.rs:440-519,588-601`, "Durable, storage-neutral user-message queue and idle dispatch").
- Dates are reported, from third-party trackers: app-server thread queue in 0.148.0-alpha.14 ([techdevnotes](https://techdevnotes.com/releases/codex-cli/0.148.0-alpha.14)), `codex queue` in 0.148.0-alpha.22 ([techdevnotes](https://techdevnotes.com/releases/codex-cli/0.148.0-alpha.22)) and a write-up for v0.149 ([danielvaughan](https://codex.danielvaughan.com/2026/08/29/codex-cli-v0149-multi-session-agents-dashboard-codex-queue-working-directory/)).
- A third party reports that a successful queue call means the message was accepted, not that it was consumed ([codex-long-jobs v0.4.0](https://github.com/zycdev/codex-long-jobs/releases/tag/v0.4.0)).
- `codex agents` is a command center for browsing and switching sessions (`cli/src/main.rs:146`).

### 8. Session storage and resume (verified-in-source)
- Transcripts: `$CODEX_HOME/sessions/` and `archived_sessions/` (`rollout/src/lib.rs:86-87`).
- Names: `session_index.jsonl` holds `{id, thread_name, updated_at}`. It is append-only and the newest entry wins (`rollout/src/session_index.rs:23-50`).
- Metadata: SQLite `state_5.sqlite` (`state/src/sqlite.rs:53`). Its `threads` table has `id`, `rollout_path`, `cwd`, `title`, `updated_at`, `archived`, `git_branch`, `source` and more (`state/migrations/0001_threads.sql`).
  - "5" is a schema generation, so expect the filename and later migrations to change.
- Commands: `codex resume [--last|--all|<id or name>]`, `fork`, `archive`, `unarchive`, `delete`, `migrate-rollouts`.

### 9. Legacy `notify` (verified-in-source, `hooks/src/legacy_notify.rs`)
- `notify = [argv...]` gets a JSON argument: `{"type":"agent-turn-complete", thread_id, turn_id, cwd, client, input_messages, last_assistant_message}`.
- It fires per turn only and replaces nothing that the `Stop` hook doesn't already cover.

### 10. Approvals, sandbox and trust (verified-in-source)
- **Sandbox modes:** `read-only` (default), `workspace-write`, `danger-full-access`.
- **Approval policies:** `untrusted`, `on-request` (default; `on-failure` is an alias), and a granular per-category mode.
- **Project trust:** `[projects."<path>"] trust_level = "trusted"|"untrusted"` (`config/src/config_toml.rs:618-625`). A CLI flag "save trust for this project ... including MCP commands outside the sandbox" persists it (`main.rs:322`).
- **Exec policy:** rules are checked with `codex execpolicy check`.

### 11. Codex app worktrees and Codex cloud (official-docs (search snippet); reported)
- **Worktrees** ([worktrees](https://developers.openai.com/codex/app/worktrees), [features](https://developers.openai.com/codex/app/features)):
  - App threads default to disposable Codex-managed worktrees. "Handoff" moves a thread between local (foreground) and worktree (background).
  - Local environments run setup scripts.
  - Thread automations are "recurring wake-up calls that preserve the thread's context".
- **CLI side:** the CLI `worktrees` feature flag is `Removed` (verified-in-source); there is a `worktree` crate.
- **Cloud:** `codex cloud` is experimental: browse cloud tasks and apply their diffs locally (`main.rs:228`).
- Cloud tasks run remotely in OpenAI's environment, not as live sessions on the workstation, mac or core.

## Assessment 1: Codex as a live A2A peer

Options for an idle wake, best first.

1. **`codex queue` stub wake: the closest thing to Claude's `asyncRewake`.**
   - A listener for the Codex session, a plain process, long-polls `/wake/listen`. When tasks are pending it runs `codex queue --thread <id> --message "[a2a] N pending. Call a2a_inbox."`.
   - The daemon parks the stub durably and starts it only when the thread is idle. It never interrupts a running turn and never launches or wraps Codex. The TUI and the queue share one app-server.
   - Satisfies stub-only wake, parked tasks and the live-session rule.
   - Costs:
     - The stub arrives as a **user-role** message. That is fine because it is a constant with no body, but the framing ("stub ≠ approval") must be stated in the `SessionStart` context.
     - Something has to run the listener. An async `SessionStart` hook cannot wake the session, but it can start the listener. Whether a background child outlives the hook is unverified.
     - The audit marked this pattern "external driver, out of scope". DDR-003 names only agy, so using `codex queue` for Codex is a user decision under the symmetric restrictive-terms rule. I found no OpenAI terms that forbid it (unverified).
   - Needs a spike: does the queue fire while the TUI sits idle at the prompt, and does a queued item survive a TUI restart?
2. **Held `Stop` hook park** (switchboard's Codex pattern).
   - A sync command hook long-polls with a large `timeout`, then returns `{"decision":"block","reason":"[a2a] N pending. Call a2a_inbox."}`. Timeouts are not capped.
   - Cost: the turn never finishes while parked, so the TUI shows "working". Third parties report that user input is deferred during long Stop hooks (reported). This is worse UX than option 1.
3. **Surfacing only, the agy equivalent.**
   - `SessionStart` and `UserPromptSubmit` return `additionalContext` with the stub, and async hooks feed in at turn boundaries.
   - No idle wake. This is the minimum peer tier.
4. **Watch, not usable yet:** `wake_on_agent_messages` plus `agent_message_board_mcp` (under development) could become a native mailbox with real idle wake.

Transport: `[mcp_servers.a2a]` over HTTP with `bearer_token_env_var`. Handle: `codex@<host>/<thread_name>`, with `thread_id` as the A2A `contextId` mapping.

The installer would have to add `--codex`. It must write `~/.codex/config.toml` and `hooks.json` through the chezmoi source (decision 16), never set `trust_level` on its own, and expect a `/hooks` trust review on first run.

## Assessment 2: ax history and title sync for Codex

Possible and cheap, read-only:
- Read `$CODEX_HOME/state_5.sqlite` `threads` (filter `cwd = <project>`, `archived = 0`, order by `updated_at DESC`) for id, title and branch. This mirrors `latest_conversation_id` for agy.
- Use `session_index.jsonl` as a fallback, newest entry wins.
- Restore by changing `build_command` to run `codex resume <id>` (or `--last`), which the CLI supports natively.
- Risks:
  - The `state_5` name and schema version will change, so find the file with a glob and check columns, as `table_has_column` already does.
  - `migrate-rollouts` (paginated history) may change the rollout layout.

## Assessment 3: Jev tier-ceiling guard on Codex

Yes, with two changes:
- **Matcher:** `^(spawn_agent|Agent)$`. `Agent` is a built-in alias (`core/src/tools/hook_names.rs:46-50`). `tool_input.model` and `agent_type` are present.
- **Session model:** the hook input carries `model` directly (`hooks/src/schema.rs:290`), so no transcript scraping is needed. This is simpler than the Claude adapter.
- **Required change: no `ask`.** Codex fails open on `ask`. A Codex adapter must return `permissionDecision: "deny"` with a reason, or rewrite `updatedInput.model` down to the ceiling. Deny is safer. The Claude adapter's fail-closed `ask` becomes fail-closed `deny`.
- For `agent_type` with no model, the role TOML in `~/.codex/agents/*.toml` or `[agents.<role>]` has to be resolved.
- Codex has no in-TUI "ask" for this path, so overriding means the user runs `/jev off` or edits the role. That is a UX regression against Claude.
- `PermissionRequest` hooks cover sandbox escalations only; their use for `spawn_agent` is unverified.

## Summary table

| feature | what it does | overlaps which project need | ADOPT / BORROW / SKIP | reason |
|---|---|---|---|---|
| AGENTS.md hierarchy + `AGENTS.override.md` (source) | Joins AGENTS.md from root to cwd, 32 KiB cap | coordination: inbox instruction | SKIP for inbox; BORROW override idea | "Never write AGENTS.md" rule; put the instruction in `SessionStart` `additionalContext`; claude-migration code can write `$CODEX_HOME/AGENTS.md` |
| `codex exec` / `exec resume` (source) | One-shot non-interactive run | Jev free-tier execution | SKIP for coordination; optional for Jev | One-shot transport is prohibited for live peers |
| `codex mcp-server` (reported; absent at HEAD) | Codex as an MCP tool (`codex`, `codex-reply`) | Jev dispatch | SKIP | Subordinate pattern, starts its own sessions, appears removed |
| MCP client `url` + `bearer_token_env_var` (source) | HTTP MCP servers with key from env | coordination `/mcp` transport | ADOPT | Fits decision 24; no key in config |
| Hooks: `SessionStart`, `UserPromptSubmit` `additionalContext` (source) | Context injection at start and per prompt | coordination: inbox surfacing | ADOPT | Same shape as Claude; constant stub |
| Async hooks (source) | Background hooks, context at next turn | coordination wake | SKIP as wake | Cannot start a turn when idle; no `asyncRewake` equivalent |
| Held `Stop` hook `decision: block` (source; switchboard reported) | Parks the turn end, continues with a reason | coordination wake | BORROW only as fallback | Works, but TUI shows "working" and input is deferred |
| `codex queue` / `thread/queue/*` + daemon (source) | Durable per-thread queue, runs when idle | coordination: Codex idle wake; Jev ready-work queue idea | ADOPT (after spike and user decision) | Native idle wake that drives no TUI; stub arrives as user role |
| `PreToolUse` on `spawn_agent` / `Agent` with `model` in input (source) | Pre-spawn deny or rewrite | Jev tier-ceiling guard | ADOPT, `deny` not `ask` | `ask` fails open in Codex |
| Agent roles `~/.codex/agents/*.toml`, `[agents]` (source) | Named subagent roles, depth and thread limits | Jev subagent discovery | ADOPT in discovery | Add a Codex path; parse TOML, not markdown |
| `wake_on_agent_messages`, message board MCP, agent graph (source, in development) | Native inter-thread mail, wake, topology | coordination ready-work board | BORROW ideas, watch | Off by default and unstable |
| `sleep` tool (source) | Up to 12 h wait, ends on new input | coordination: "wait for task" | SKIP | Keeps the turn open; burns turn context |
| `sessions/`, `session_index.jsonl`, `state_5.sqlite` (source) | Transcripts, names, thread metadata | ax history, title, restore | ADOPT (read-only) | Same as the agy DB reader; glob for the DB name |
| `codex resume` / `fork` / `codex agents` (source) | Native restore and session browser | ax restore | ADOPT `resume <id>`; BORROW `agents` UI ideas | Removes the need for ax to track Codex sessions |
| Legacy `notify` (source) | argv JSON on turn complete | ax statusline / Jev telemetry | SKIP | `Stop` hook is a superset |
| Sandbox / approval / `trust_level` (source) | Permission levels, project trust | coordination installer, ax launch flags | BORROW (document only) | Installer must not set `trust_level` (OpenRig mistake) |
| Codex app worktrees, automations, cloud tasks (official-docs snippet) | Parallel isolated threads; recurring thread wakes; remote tasks | ax tabs; coordination ready-work | BORROW ideas (handoff, wake-up automations) | App or cloud only; cloud tasks are not live local peers |

Sources: OpenAI source at `openai/codex` (commit `aa9b9cc`); [Codex hooks](https://developers.openai.com/codex/hooks); [worktrees](https://developers.openai.com/codex/app/worktrees); [app features](https://developers.openai.com/codex/app/features); [techdevnotes alpha.14](https://techdevnotes.com/releases/codex-cli/0.148.0-alpha.14); [techdevnotes alpha.22](https://techdevnotes.com/releases/codex-cli/0.148.0-alpha.22); [danielvaughan v0.149](https://codex.danielvaughan.com/2026/08/29/codex-cli-v0149-multi-session-agents-dashboard-codex-queue-working-directory/); [codex-long-jobs](https://github.com/zycdev/codex-long-jobs/releases/tag/v0.4.0); [PR #4811](https://github.com/openai/codex/pull/4811/files); [codex_mcp_interface mirror](https://git.halfakop.ru/rad/codex/src/commit/5728bd67d03aae737ca6c47df85d1148c9b0cc7b/codex-rs/docs/codex_mcp_interface.md).
