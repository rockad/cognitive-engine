# Claude Code native features vs coordination, ax and the cognitive-engine bridge (as of 2026-11-07, Claude Code 2.1.319)

Scope: Claude Code only. Agent teams are left out, as asked. Your request also covers Antigravity and Codex native tools; this report does not, so those need their own pass. Nothing was installed or run. I read the official docs and the official changelog (`raw.githubusercontent.com/anthropics/claude-code/main/CHANGELOG.md`, top entry 2.1.319) and grepped the local repos.

Labels:
- **[V]**: verified in official docs or the changelog
- **[R]**: reported by a third party or by a repo doc
- **[U]**: unverified, or my own inference

## 0. Why decision 8 says "Channels not used"

Sources:
- `file://agentic-hub/plugins/coordination/docs/plans/consolidated-bridge-v2-plan.md` line 134, decision 8
- `file://agentic-hub/plugins/coordination/docs/research/a2a-audit-2026-11-04/t1b-wake-mechanisms.md`, sections 4 and 5.2
- `file://agentic-hub/plugins/coordination/docs/research/a2a-mcp-wake-audit-2026-11-04.md` line 65
- `docs/specs/server-api.md` line 1474

The audit's reasons, all labelled verified there:
- Channels are a research preview, and the docs say the "flag syntax and protocol contract may change".
- Our own server is not on the allowlist, so it would need `--dangerously-load-development-channels` plus a confirmation prompt at every launch. Both flags are hidden from `--help`.
- Team and Enterprise orgs must enable `channelsEnabled`.
- Not available on Bedrock, Vertex or Foundry.
- Issue anthropics/claude-code#61797 reports idle sessions silently dropping channel events. It was closed by the stale bot, not fixed.
- There is no receipt that Claude accepted an event.
- It was unverified whether a remote HTTP MCP server, like our Docker server, can register as a channel.

The audit's conclusion: at most an optional accelerator that is off by default.

What I found today:
- **New risk [V]:** on the v2 MCP client runtime, "a channel server that negotiates MCP protocol revision 2026-07-28 can't deliver channel messages, so Claude Code doesn't register it as a channel". Workaround: `MCP_PROTOCOL_NEGOTIATION=legacy`. This makes Channels more fragile, not less. Sources: https://code.claude.com/docs/en/mcp (Push messages with channels) and https://code.claude.com/docs/en/channels
- **Still true [V]:** research preview, `--channels` accepts only allowlisted plugins, "Being in `.mcp.json` isn't enough to push messages".
- **Remaining injection risk [V]:** a channel can declare permission relay, so "Anyone who can reply through the channel can approve or deny tool use". That conflicts with "a body is never an approval" unless the channel never declares it.
- **The skip stands.** There is also a better native alternative: cross-session messaging (section 1).

## 1. Cross-session messaging (`SendMessage`, `ListAgents`, inbox socket) vs the asyncRewake long-poll and the A2A inbox

Source: https://code.claude.com/docs/en/cross-session-messaging

What it does, all [V]:
- Needs v2.1.224 or later on Linux, macOS and WSL2.
- "When the receiving session is idle, Claude Code starts a new turn with the message." While busy, the message is read between tool calls.
- Addressing is by session name, set with `--name`/`-n` or `/rename`. When names collide, Claude adds a short ID.
- Other machines are reachable only while both sessions are connected to Remote Control. That needs a claude.ai login, and the message travels **through Anthropic servers**. A session marked `offline` gets the message when it reconnects.
- A cloud session cannot reply with `SendMessage`.
- **A container and its host cannot reach each other.**

Trust framing, [V]. The receiver is told the message came from another session, and:
- it "can't approve anything"
- it "can't change configuration"
- slash commands in it do not run

This matches the plugin's security invariants natively.

Inbound controls, [V]:
- `crossSessionInbound` is `accept`, `hold` or `refuse`.
- By default, a session that bypasses permissions holds peer messages for approval.
- A message from the session's own child process is delivered. On Linux this is checked by process evidence, even after the child exited. Otherwise it is checked with `CLAUDE_CODE_MESSAGING_TOKEN` in the first line: `{"type":"auth","token":...}`.
- `isolatePeerMachines` forces approval before a message leaves the machine.

Limits, [V]:
- plain text only
- at most 100 held messages and 50 queued
- **repeats from the same sender are rate-limited, and identical repeats within a short window are dropped**
- `notify_when_idle` works on the same machine only and expires after 12 h
- the socket closes a connection that sends no complete line within 30 s

Assessment:
- **It cannot be the A2A inbox:**
  - Claude only. agy cannot take part, which breaks symmetric agency.
  - Not durable: there is no task store, no task states, no QUARANTINED review and no ready queue.
  - Cross-machine traffic goes through Anthropic and the transcript is stored there.
  - The core Docker server cannot reach a host socket.
- **It is the best native wake transport for Claude, and this is SP6.** SP1 already confirmed that hooks on 2.1.318 receive `CLAUDE_CODE_MESSAGING_SOCKET` and `CLAUDE_CODE_MESSAGING_TOKEN` (see `spike-results-2026-11-05-sp1.md` line 71).
- **Proposed SP6 variant [U, my inference]:** arm the listener from a hook with `async: true` instead of `asyncRewake`. Docs: "Claude Code doesn't enforce timeout on async: true… still enforces timeout on asyncRewake" [V]. On a 200 from the server, the listener posts the stub to its own session's socket and does not exit, so one listener can deliver many wakes. As an own child it is delivered without approval on Linux.
  - What it removes: the 604800 s ceiling, the Esc re-arm gap for the wake path, and the "Stop hook blocking error" framing.
  - What it keeps: stub-only text, because the body is still fetched through `a2a_inbox`.
  - Caveats:
    - The stub must change between sends, for example by carrying the count N, or the native duplicate filter will drop it.
    - The frame shape after the auth line is not in the docs. Interlink's frame is [R].
    - On macOS an exited child cannot be verified by process evidence, so send the token.
- **Identity [V]:** session names are the native handles. `claude-code@<host>/<session-name>` should map one-to-one to `--name`, which `/list-agents` and `SendMessage` use.

## 2. Plugin monitors: a wake candidate the plan does not mention

Sources: https://code.claude.com/docs/en/plugins/components#monitors and https://code.claude.com/docs/en/plugins/manifest-reference#monitors

What is documented [V]:
- `monitors/monitors.json` entries take `name`, `command`, `description`, and `when: "always"` (the default) or `"on-skill-invoke:<skill>"`.
- The command is "a persistent background process" that starts at session start and on plugin reload, runs for the whole session, and "what it prints reaches Claude as notifications".
- They do not start under `-p`, which fits the interactive-only rule.
- They cannot use `${user_config.*}` and do not receive `CLAUDE_PLUGIN_OPTION_*`, so the API key must come from `~/.config/a2a-coord/api-key`, which matches decision 34.
- They run outside the sandbox.

Monitor tool vs plugin monitors [V]:
- Monitor *tool* watches now always have a deadline: 5 min by default, 30 min at most (changelog: "replacing the no-timeout `persistent` option").
- Plugin monitors are described separately, with no deadline.

Unverified points:
- **Whether a monitor notification starts a turn in an idle session [U].** The changelog gives indirect evidence: "Fixed session title being derived from plugin monitor notifications before the user's first prompt" [V text, inference U].
- Whether the monitor process sees `CLAUDE_PID` and the messaging socket variables [U].

Why it matters:
- It would remove the hook timeout, the Esc gap and the Stop re-arm entirely.
- It ships through the same marketplace as the rest of the plugin.

Worth a spike, say SP7, with the same stub-only output.

## 3. Feature-by-feature notes

**Hooks** (https://code.claude.com/docs/en/hooks) [V]
- `asyncRewake`: still wakes on exit 2; timeout enforced; exit 2 "wakes Claude immediately even when the session is idle".
- New useful fields:
  - `SessionStart` output: `sessionTitle` (sets the name, like `/rename`, on startup, resume and fork), `additionalContext`, `watchPaths` that feed `FileChanged`, `reloadSkills`.
  - `Notification` matchers: `idle_prompt`, `agent_needs_input`, `agent_completed`, `permission_prompt`.
  - `TaskCreated` and `TaskCompleted` events.
  - `PostToolBatch`.
- Use for coordination:
  - `sessionTitle` lets the installer's hook *set* the handle deterministically instead of reading it. That solves the SP2 finding that `--name` is not in the environment.
  - `Notification idle_prompt` gives a liveness and idle signal for the server and for the ax statusline.

**Subagents** (https://code.claude.com/docs/en/sub-agents) [V]
- Discovery is `.claude/agents`, `~/.claude/agents`, plugin `agents/` and the `--agents` JSON flag only. `~/.agents/agents` is not read natively, so Jev's universal shared directory remains its own convention.
- Spawned subagents run in the background by default. They can nest 3 levels (`CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH`). Frontmatter supports `model`, `background`, `isolation: worktree` and `memory`.
- They can be resumed with `SendMessage` to the agent ID.
- `SubagentStart`/`SubagentStop` hooks exist.
- cognitive-engine's PreToolUse guard on `Agent` stays valid. The `model:` frontmatter is a native per-agent ceiling it could read.

**Task list, `TaskCreate`/`TaskGet`/`TaskList`/`TaskUpdate`** (https://code.claude.com/docs/en/tools-reference and https://code.claude.com/docs/en/interactive-mode#task-list) [V]
- `TaskUpdate` "updates task status, dependencies".
- `CLAUDE_CODE_TASK_LIST_ID=<name>` shares one list across sessions in `~/.claude/tasks/<name>/`.
- On by default only up to Opus 4.7, Sonnet 4.6 and Haiku 4.5. Newer models need `CLAUDE_CODE_ENABLE_TODO_TOOLS=1`. Always on in background and cloud sessions.
- `TodoWrite` is the legacy option (`CLAUDE_CODE_ENABLE_TASKS=0`). `TaskOutput` was removed in 2.1.277.
- It is local, Claude-only, and its on-disk format is undocumented [U].
- For the ready-work gap: borrow its model (subject, description, status, dependencies) for an A2A `metadata.coord.blocked_by` plus a `ready` query. Optionally mirror Claude's tasks into the store with `TaskCreated`/`TaskCompleted` hooks. Do not make it the source of truth.

**Background bash and Monitor tool** [V]
- Background bash has no time limit in interactive sessions. Output goes to a file, and it is not restarted on resume.
- Monitor tool watches cap at 30 min, so they are not a wake path. Plugin monitors are (section 2).

**Agent view and background sessions** (https://code.claude.com/docs/en/agent-view) [V], research preview
- Commands: `claude --bg --name`, `claude agents [--json --all]`, `attach`, `logs`, `stop`, `respawn`, `/bg`.
- A supervisor daemon keeps state in `~/.claude/daemon/` and `jobs/`. Statuses: working, needs input, idle, completed, failed.
- **An idle, unattached session's process may be stopped after about 1 h.** It resumes on attach or reply, and `Ctrl+T` pins it.
- Risk for coordination [U]: a stopped process kills its asyncRewake or monitor listener, so the server's "no listener parked" badge must cover this case. Whether a cross-session message revives a stopped background session is [U].
- For ax: `claude agents --json` is a native Claude status and registry feed.

**Remote Control** (https://code.claude.com/docs/en/remote-control) [V]
- Modes: `--remote-control`/`--rc`, `/rc`, and server mode `claude remote-control --spawn same-dir|worktree --capacity N --name`. Auto-connect is a `/config` option.
- It carries cross-machine `SendMessage`.
- The transcript is stored on Anthropic servers while connected. API-key auth is not supported.
- ax already passes `--remote-control` (`ax/src/lib.rs` line 52).

**Session naming and resume** (https://code.claude.com/docs/en/sessions) [V]
- `-n` and `/rename`; `claude --resume <name>` resolves across the repo's worktrees.
- A default display name like `my-app-3f` exists but is not a resume handle.
- `--fork-session` and `/branch`; transcripts are under `~/.claude/projects/...` in an internal format.
- ax's `custom-title.json` parsing could switch to names it sets with `-n`.

**Scheduled tasks**
- `/loop` and `CronCreate` (https://code.claude.com/docs/en/scheduled-tasks) [V]:
  - session-scoped, 1 min minimum, fire only while running and idle, 7-day expiry
  - restored on resume, except self-paced loops
  - jitter up to 30 min
  - every fire costs a full turn
  - Use only as a degraded fallback poll (`/loop 15m call a2a_inbox`) if both wakes fail.
- Cloud scheduled tasks, formerly routines (https://code.claude.com/docs/en/routines) [V]:
  - a fresh cloud session per fire, 1 h minimum
  - the `/fire` API is a beta that uses an `sk-ant-oat01` bearer and is claude.ai-only
  - This violates "live persistent sessions only".

**Claude Code on the web, cloud sessions** [V]
- Cloud sessions run in a fresh sandbox with a clone. They cannot answer cross-machine `SendMessage`.
- This session's environment exposes `send_message` and `create_session` tools [R, observed in this environment only].
- These spawn and drive sessions, which is not the peer model.

**Plugins and marketplaces** (https://code.claude.com/docs/en/plugins/components) [V]
- One plugin can carry hooks, `.mcp.json` (including remote HTTP with `headers`), monitors, skills, agents, `bin/`, output styles and `userConfig` with `sensitive: true`, which is stored in the OS credential store.
- Hooks receive `CLAUDE_PLUGIN_OPTION_<KEY>`.
- agentic-hub already has `marketplace.json`.
- Tension with decision 16, the chezmoi installer: a marketplace plugin replaces hand-written hook and MCP config on the Claude side with `claude plugin install`, which the user runs.

**`claude mcp serve`** (https://code.claude.com/docs/en/mcp) [V]
- "only exposes Claude Code's tools", not the live conversation. It is a stateless tool server.
- Using it would make Claude a tool of another agent, which breaks symmetric agency and does not touch the warm session.

**statusLine** (https://code.claude.com/docs/en/statusline) [V]
- stdin carries `session_id`, `session_name`, `transcript_path`, `worktree.*`, `workspace.git_worktree` and `repo`, `rate_limits.*`, `agent.name`.
- `refreshInterval` (minimum 1 s) re-runs it even while the session is idle. `subagentStatusLine` also exists.
- A `[a2a] N pending` segment can poll `/status` and show pending work with **zero context injection**.
- It also lets a script learn the session's own name.

**Worktrees** (https://code.claude.com/docs/en/worktrees) [V]
- `claude -w <name>`, which also accepts `"#PR"`; `EnterWorktree`/`ExitWorktree`; `isolation: worktree`; `.worktreeinclude`; `WorktreeCreate`/`WorktreeRemove` hooks; `worktree.baseRef`.
- Isolation is enforced: edits and git commands into the main checkout are refused.
- The docs name cross-session messaging as the way worktree sessions coordinate.
- Matches cognitive-engine section 6.2 and the implementation plan's worktree-per-stream setup.

**Output styles** [V]
- Available as `/output-style` and from plugins.
- They do not help with coordination. Skills or hook context do that job better.

## 4. Per-project impact

**coordination**
- Wake:
  - asyncRewake still works.
  - Promote SP6 in the `async: true` + own-socket form.
  - Add an SP7 spike for plugin monitors.
  - Channels stay skipped.
- Identity:
  - Use `--name` and `sessionTitle` as the handle source.
  - The statusline can read `session_name`.
- Inbox: A2A stays. It is the only cross-harness, durable, trust-gated store.
- Backlog: borrow the Task model and its dependencies into A2A metadata plus a ready query.

**ax**
- Borrow `claude agents --json` and statusline JSON fields for Claude tabs.
- Remote Control is already adopted.
- Name tabs with `-n` and resume with `--resume <name>`.

**cognitive-engine bridge (section 6)**
- For Claude↔Claude, a hand-relayed markdown bridge is replaced natively by `SendMessage` with `notify_when_idle` plus worktrees.
- The agy↔Claude leg is still not covered natively. That leg is exactly what the A2A server is for.

## 5. Table

| feature | what it does | overlaps which project need | verdict | reason |
|---|---|---|---|---|
| Cross-session messaging (`SendMessage`/`ListAgents`) | Plain-text messages between your Claude sessions; an idle receiver starts a turn; cross-machine via Remote Control [V] | coordination inbox and wake; cognitive-engine section 6 bridge | BORROW | Claude-only, not durable, relayed through Anthropic across machines, no agy, so it cannot be the A2A inbox. Fine for Claude↔Claude notes on one machine |
| Inbox socket (`CLAUDE_CODE_MESSAGING_SOCKET` + token) | A hook or script posts into its own session; own child is auto-delivered on Linux [V] | coordination wake (SP6) | ADOPT (spike first) | With an `async: true` hook it removes the asyncRewake timeout and the Esc gap while keeping stub-only text. Vary the stub (count N) to beat the duplicate filter |
| Channels (`claude/channel`) | MCP server pushes events into a running session [V] | coordination wake | SKIP | Research preview, dev flag every launch, #61797 idle drops, new v2 protocol-revision non-registration, permission-relay risk |
| Plugin monitors | Session-long background command whose output reaches Claude as notifications [V] | coordination wake | BORROW (spike SP7) | Possibly a timeout-free, Stop-free listener; idle-turn start is [U] |
| `asyncRewake` hook | Background command hook; exit 2 wakes an idle session [V] | coordination wake | ADOPT (keep) | Proven in SP1; remains the baseline |
| SessionStart `sessionTitle`/`additionalContext`/`watchPaths` | Set name, inject context, watch files [V] | coordination identity and handles, inbox surfacing | ADOPT | Hook sets the handle deterministically; fixes SP2 `--name`-not-in-env |
| Notification hook (`idle_prompt`, `agent_needs_input`, `agent_completed`) | Fires on idle and attention events [V] | coordination liveness, ax status | ADOPT | Cheap idle and liveness signal |
| `TaskCreated`/`TaskCompleted` hooks | Fire on task-list changes [V] | coordination backlog mirror | BORROW | Optional mirror of Claude tasks into the A2A store |
| Task tools + `CLAUDE_CODE_TASK_LIST_ID` | Per-session or shared checklist with dependencies [V] | coordination ready-work gap | BORROW (model only) | Local, Claude-only, undocumented format, off by default on new models |
| Subagents (`.claude/agents`, background, `isolation`, `model`) | Delegated contexts, nesting up to depth 3 [V] | cognitive-engine tier guard, Jev discovery | ADOPT (as-is) | Jev already targets `.claude/agents`; `~/.agents/agents` is not native |
| Background bash / Monitor tool | Async shells; 30-min-capped watches [V] | wake | SKIP for wake | Deadline-capped; plugin monitors are the persistent form |
| Agent view / `claude --bg` / `claude agents --json` | Supervisor-run background sessions with status [V] | ax registry and status; coordination live sessions | BORROW | Good Claude status feed for ax; idle process may stop after about 1 h, which kills listeners |
| Remote Control | Drive or reach a local session from other devices; carries cross-machine messages [V] | ax, cross-machine Claude reach | ADOPT in ax / SKIP as transport | Already in ax; Claude-only and Anthropic-relayed |
| `--name` / `/rename` / `--resume <name>` | Named, resumable sessions [V] | coordination handles, ax restore | ADOPT | Native handle equals the `SendMessage` address |
| `/loop` / `CronCreate` | In-session polling, 1 min minimum, 7-day expiry [V] | wake fallback | BORROW (fallback only) | Each fire costs a turn; only while running and idle |
| Cloud scheduled tasks (routines, `/fire`) | Fresh cloud session per trigger [V] | delegation | SKIP | Not a live persistent session; claude.ai-only beta |
| Cloud sessions + `send_message` | Remote sandbox sessions; cannot reply cross-machine [V] | delegation | SKIP | Spawn and drive model, not peers |
| Plugins / marketplaces | Bundle hooks, MCP, monitors, skills, `userConfig` (sensitive values to keychain) [V] | coordination installer (decision 16), Jev | ADOPT for packaging | One install gives hooks, MCP and monitors; reconcile with the chezmoi installer |
| `claude mcp serve` | Exposes Claude Code's tools over stdio MCP [V] | agy→Claude delegation | SKIP | Tools only, not the warm session; breaks symmetry |
| statusLine (`session_name`, `refreshInterval`) | Scripted footer fed with session JSON [V] | ax statusline; coordination pending badge | BORROW | Zero-injection pending count; self-name discovery |
| Worktrees (`-w`, `EnterWorktree`, `isolation`) | Isolated parallel checkouts with enforced isolation [V] | cognitive-engine 6.2; coordination worktree streams | ADOPT | Native and enforced; pairs with cross-session messaging |
| Output styles | Change reply voice and format [V] | none | SKIP | No coordination value |

Local files I relied on:
- `file://agentic-hub/plugins/coordination/AGENTS.md`
- `file://agentic-hub/plugins/coordination/docs/plans/consolidated-bridge-v2-plan.md`
- `file://agentic-hub/plugins/coordination/docs/research/a2a-audit-2026-11-04/t1b-wake-mechanisms.md`
- `file://agentic-hub/plugins/coordination/docs/specs/spikes.md` (SP6, section 9)
- `file://agentic-hub/plugins/coordination/docs/research/spike-results-2026-11-05-sp1.md`
- `ax/src/lib.rs`
- `cognitive-engine/docs/plans/2026-10-26-multi-track-implementation-plan.md` (section 6)
