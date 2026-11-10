# Claude Code agent teams: research and fit for coordination, ax and cognitive-engine (2026-11-07)

Agent teams are useful inside a single Claude Code session. They cannot serve as the coordination substrate: they break three binding rules of `plugins/coordination` and their split-pane mode uses `tmux send-keys`. The useful parts are the task model (dependencies, ready-work, atomic claim) and the quality-gate hooks. There is also one real gap in the Jev guard.

Nothing was installed or run. Sources were downloaded only into the scratchpad `/tmp/claude-0/-home-user/84e5c850-1d48-5bf0-99bf-2742d6707467/scratchpad/dl/`.

**Labels:**
- **[V]**: verified in official docs or the official CHANGELOG.
- **[R]**: reported by a third party (GitHub issues, cmux).
- **[U]**: unverified, my inference.
- **[L]**: verified by reading the local repository.

## 1. The feature

### Status, how to enable, minimum version
- Still **experimental** and off by default as of the latest changelog entry (2.1.319) **[V]**.
  - Docs: "Agent teams are experimental and disabled by default" (https://code.claude.com/docs/en/agent-teams).
  - First shipped in **v2.1.32** as a "research preview… requires setting CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1" (CHANGELOG `## 2.1.32`) **[V]**.
  - `TeammateIdle` and `TaskCompleted` hooks came in 2.1.33; `TaskCreated` came in 2.1.84 **[V]**.
- To enable it, set `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` in the shell or in `settings.json` under `env` **[V]**.
  - It needs an **interactive** session. With `-p` or the Agent SDK, no teammates are spawned **[V]**.
- Practical minimum version: 2.1.178 or later.
  - 2.1.178 removed `TeamCreate` and `TeamDelete`. Every session now has one implicit team, and "spawn teammates directly with the Agent tool's `name` parameter"; `team_name` is ignored **[V]**.
  - Many fixes landed between 2.1.290 and 2.1.317, so use a recent build **[V]**.
- Side effect: with teams enabled, "a subagent that Claude names launches as a teammate, so teams can form even when you didn't ask for one". Claude Code does not ask you to confirm the launch **[V]**.

### Topology
- **Hierarchical.** One lead, which is the main session and is "fixed for its lifetime". Only the lead spawns teammates. There are no nested teams, and you cannot transfer leadership **[V]**.
- Teammates **message each other directly by name** through `SendMessage`. There is no broadcast: "send one message per recipient". The user can also message any teammate directly **[V]**.
- **Mailbox:** one JSON file per agent at `~/.claude/teams/{team}/inboxes/{agent}.json` **[V]**.
  - Malformed entries are reported and removed.
  - A message counts as sent only when the write succeeds.
- **Team config:** `~/.claude/teams/session-<8 chars of session id>/config.json` **[V]**.
  - It holds a `members` array plus runtime session IDs and tmux pane IDs.
  - It is overwritten on each state update, so don't author or edit it. There is no project-level equivalent.
  - It is deleted when the session ends.
- **Task list:** `~/.claude/tasks/{team}/` **[V]**.
  - It stays on disk after the session ends, subject to `cleanupPeriodDays`.
  - The per-task file schema is **not documented**. The usual JSON-per-task description with `blocks`/`blockedBy` is **[R/U]**.
- **Task states** are pending, in progress and completed. A task can depend on others and shows as blocked until they complete **[V]**.
  - Teammates "don't self-claim a blocked task". The lead or the user can still assign one.
  - Self-claim: "after finishing a task, a teammate picks up the next unassigned, unblocked task on its own".
  - "Task claiming uses file locking"; completing a task auto-unblocks the tasks that depend on it.
- **Ready-work** is therefore implicit: a task that is pending, unowned and has no open blockers **[V/U]**.
- Docs-listed caveat **[V]**: "Task status can lag… teammates sometimes fail to mark tasks as completed, which blocks dependent tasks."
- **The Task tools are model-gated** **[V]** (https://code.claude.com/docs/en/tools-reference#task-tool-availability).
  - `TaskCreate`, `TaskGet`, `TaskList` and `TaskUpdate` exist by default only on Opus 4.7, Sonnet 4.6, Haiku 4.5 and earlier.
  - On newer models (for example Opus 5.5 and Sonnet 5.5) you need `CLAUDE_CODE_ENABLE_TODO_TOOLS=1`. Without the tools, "an agent coordinates with its team through messages instead of the shared task list".
- **Related outside teams** **[V]**: `CLAUDE_CODE_TASK_LIST_ID=<name>` shares one named list in `~/.claude/tasks/<name>/` across several Claude sessions on one machine (interactive-mode.md "Task list"; env-vars.md).

### How teammates run
- `teammateMode` (or the hidden flag `--teammate-mode`) takes `in-process` (the default), `auto`, `tmux` or `iterm2` **[V]**.
  - **In-process:** teammates share the lead's process and appear in an agent panel. Arrow keys and Enter select one, `x` stops it, `Ctrl+T` toggles the task list.
  - **Split-pane:** each teammate is a **separate Claude Code process** in a tmux or iTerm2 (`it2`) pane.
  - `auto` uses split panes only when already inside tmux, or in iTerm2 with `it2` installed.
  - Split panes are not supported in the VS Code terminal, Windows Terminal or Ghostty.
- **The tmux backend uses `split-window` plus `send-keys`** **[R]**.
  - Commands relied on: `split-window`, `send-keys`, `kill-pane`, `list-panes`, `display-message`, `has-session`, `list-windows` (https://github.com/anthropics/claude-code/issues/34150).
  - Teammates split the **current window**; spawning 4 or more at once garbles `send-keys` (https://github.com/anthropics/claude-code/issues/23615, open).
  - cmux replaces these calls with a tmux shim (https://cmux.com/blog/cmux-claude-teams).
  - Consistent with that, the official CHANGELOG 2.1.183 mentions "keystrokes typed during agent spawn leaking into the new tmux pane" and rc-file timing **[V]**.
- The docs suggest `tmux ls` / `kill-session` to clean up orphaned tmux sessions **[V]**.

### Hooks and wake
- **`TeammateIdle`** **[V]** (hooks.md, around lines 2919–2952 of the downloaded copy).
  - Input: `teammate_name`, `team_name` (deprecated), `agent_id` (in-process, v2.1.290 or later).
  - Exit code 2 feeds stderr back and keeps the teammate working.
  - `{"continue":false,"stopReason"}` stops the teammate.
- **`TaskCreated`** **[V]**: exit code 2 or `decision:"block"` deletes the task. `continue:false` is ignored.
- **`TaskCompleted`** **[V]**.
  - Fires on `TaskUpdate`, or when a teammate ends its turn with tasks still in progress.
  - Exit code 2 blocks the completion.
- **None of the three support matchers** **[V]**.
- `SubagentStart` also fires "each time an in-process teammate handles a new message" **[V]**.
- Plugin and settings hooks run inside subagents and in-process teammates with `agent_id`/`agent_type`. **Split-pane teammates load settings as separate sessions** **[V]**.
- **Wake** **[V]**.
  - Messages are delivered automatically; the lead doesn't poll.
  - Idle teammates send the lead an idle notification that includes their final answer.
  - Messaging an in-process teammate that is no longer running revives it with its saved conversation.
  - A message also wakes a teammate that is waiting on an API retry.

### Persistence and reach
- **A team does not survive the session** **[V]**.
  - "/resume and /rewind do not restore in-process teammates".
  - One team per session, and you cannot "share a team across sessions".
  - The team config is deleted at session end; only the task directory remains.
- **No cross-machine teams** **[V]**.
  - Separate feature: **cross-session messaging** (v2.1.224 or later) reaches your other Claude sessions on the same machine through a Unix socket, and other machines or cloud sessions through Remote Control via Anthropic servers.
  - It is plain text only: "Structured agent team protocol messages stay within a team" (https://code.claude.com/docs/en/cross-session-messaging).
- **Non-Claude harnesses (agy, Codex, OpenCode) cannot join** **[V/U]**.
  - Joining needs the Agent tool, `SendMessage` and the Task tools.
  - The mailbox and task files are plain JSON with an undocumented schema and runtime-owned config. Reading them is possible but brittle; writing to them would be an undocumented injection channel **[U]**.
  - **Correction:** our `docs/research/bridge-v2-ecosystem-analysis.md:71` says "agy can bridge into it via a wrapper skill or shared MCP server". That claim is **unverified and contradicted** by the docs (session-scoped team, overwritten config, protocol messages stay inside the team).

### Permissions
- Teammates start in the lead's permission mode, **including `bypassPermissions`**. You cannot set per-teammate modes at spawn **[V]**.
- In-process teammates follow the lead's live allow and deny rules. While the lead is in `acceptEdits`, `auto` or `bypass`, switching a teammate to Manual has no effect **[V]**.
- Split-pane teammates get `--tools` and `--disallowedTools` (v2.1.309 or later) but **not `--allowedTools`** **[V]**.
- Teammate permission prompts surface in the lead **[V]**.
- Plan approvals from teammates are **auto-approved** unless you changed that teammate's mode **[V]**.
- Messages between agents are tagged as coming from another Claude session and can never stand in for user consent. Auto mode's classifier screens each one **[V]**.

### Cost
- "Significantly more tokens"; cost scales linearly with the number of teammates **[V]**.
- Docs advice: use Sonnet for teammates, 3–5 teammates, and shut them down when done (https://code.claude.com/docs/en/costs#agent-team-token-costs) **[V]**.
- In-process teammate prompt cache defaults to a **5-minute TTL**; `subagentPromptCacheTtl: "1h"` extends it **[V]**.
- Teammate model order: the spawn prompt or call → the agent definition's `model` → `CLAUDE_CODE_SUBAGENT_MODEL` → the lead's model **[V]**.
  - `CLAUDE_CODE_SUBAGENT_MODEL_FORCE=1` (v2.1.257 or later) overrides the first two.
  - A mod's `agent.spawn` hook can also replace the model.

### Limitations listed in the docs **[V]**
- No resume for in-process teammates.
- Task status can lag.
- Shutdown can be slow.
- One team per session.
- No nested teams.
- No background subagents from in-process teammates.
- The lead is fixed.
- No per-teammate permission modes at spawn.
- Split panes need tmux or iTerm2.
- Practical guidance: same-file edits overwrite each other.

## 2. Fit with the three projects

### 2.1 `agentic-hub/plugins/coordination`
- The fit fails on three of the binding rules **[L]**:
  - **Symmetric agency** (`AGENTS.md:37`): the lead is fixed and only the lead spawns. That is the master/subordinate model the rule prohibits.
  - **Live persistent sessions** (`AGENTS.md:38`): teammates are spawned children that die with the lead and are not restored on resume.
  - **Tasks parked, never auto-executed** (`AGENTS.md:73`): teammates self-claim the next unblocked task. That is auto-execution.
- It also conflicts with **DDR-003** (`AGENTS.md:39-42`): split-pane mode uses `send-keys` **[R]**. It cannot include agy at all.
- Cross-machine reach (the workstation, mac, core) is absent **[V]**.
- **What it does show is the missing piece.** The gap is recorded at `docs/research/todo-tools-evaluation-2026-11-07.md:41`. Agent teams contain a minimal, well-tested backlog design:
  - `blocks` / `blockedBy`
  - auto-unblock when a dependency completes
  - an atomic claim of "next unowned, unblocked"
  - creation and completion gates

  These can be re-implemented server-side in A2A without the spawn model.
- The official cross-session inbox socket (`CLAUDE_CODE_MESSAGING_SOCKET`) is already being evaluated as a wake route (`docs/research/a2a-audit-2026-11-04/t1b-wake-mechanisms.md:20,359`) **[L]**. That is the right Claude-native piece to reuse; teams themselves are not.

### 2.2 `ax`
- **Collision risk is moderate, mostly in restore.** A tmux teammate splits the lead's current ax window by running `split-window` and then `send-keys` **[R]**.
- **What breaks:**
  - **Window metadata:** the window keeps the lead's window-level `@harness` / `@conversation_id` (`src/lib.rs:1373,1416`). Teammate panes get **no pane-level `@harness`**, which ax sets only through `ax run` (`src/lib.rs:1375-1390`).
  - **Border label:** `pane-border-format` therefore falls back to `#{pane_current_command}` (`config/tmux/ax.conf:38`). That is harmless, and it already acts as a rough "teammate" label.
  - **Restore:** `save_session_layout` records every pane (`src/core/persistence.rs:116-118`). `restore_panes_and_layout` recreates extra panes with a bare `split-window` and no command (`persistence.rs:325-331`).
    - A snapshot taken while teammates run restores as **empty shell panes** in the lead's tab.
    - The teammates themselves are gone after resume anyway **[V]**.
  - **Isolated socket:** ax's isolated socket (`src/config.rs:198`, `tmux -L ax`) should be transparent, because tmux inherits `$TMUX` **[U]**.
- **What looks safe:**
  - ax hooks: `pane-focus-in`, `client-detached` and `window-unlinked` → `ax reload` (`ax.conf:16-18`). These don't fire on pane split or kill, so they look safe **[U]**.
  - The Ctrl+Space prefix doesn't matter to `send-keys -t`.
- **Can ax show teammates?**
  - Only best-effort and read-only **[U]**: map tmux pane IDs from `~/.claude/teams/*/config.json`, which is undocumented and runtime-owned.
  - In-process mode, the default, is invisible to tmux by design.
- **Recommendation:** keep `teammateMode` at `in-process` (the default) under ax. Do not use `tmux`/`auto` inside ax windows.

### 2.3 `cognitive-engine` (Jev guard)
- **Spawn visibility.** Teammates are spawned by the **`Agent`** tool (a call carrying `name`) **[V]**.
  - The matcher `"Agent|Task"` (`adapters/claude/hooks/hooks.json:5`) and `evaluateClaude` (`packages/jev/src/guard.ts:163`) therefore **do see teammate spawns** **[V/U]**.
  - `tool_input.model` / `subagent_type` are read as usual (`guard.ts:164-168`).
  - Reviving a stopped in-process teammate goes through `SendMessage`, not `Agent`. That is not a new spawn, and the model is fixed at spawn **[V]**.
- **Ways teams, and subagents, can get past the tier ceiling:**
  1. **Env-selected model.** `CLAUDE_CODE_SUBAGENT_MODEL` (third in the model order) is never consulted.
     - If the call has no `model` and no definition, `requested` is null and the call passes (`guard.ts:64-66`).
     - So `CLAUDE_CODE_SUBAGENT_MODEL=opus` under a Sonnet session runs Opus teammates silently.
     - `..._FORCE=1` reverses the problem: the guard judges a `model` that will not be used **[V docs + L code]**.
  2. **Agent definitions it doesn't find.** `agentDefinitionModel` looks only in `<cwd>/.claude/agents` and `~/.claude/agents` (`guard.ts:91-93`).
     - Plugin and managed agents are missed, and so are names with `:` (for example `plugin:agent`, which fails the regex) **[L]**.
     - Teams accept plugin and managed scopes **[V]**.
  3. **Mod overrides.** A mod's `agent.spawn` hook can replace the model after PreToolUse **[V]**.
  4. **Count is not limited.** The guard caps the tier, not the number of teammates.
     - Named subagents silently become long-lived teammates when teams are enabled **[V]**, which multiplies cost linearly.
     - Whether Jev is on is decided by `readJevMode` (`hook.ts:31`), which is default-off.
- **Session-model detection.** `sessionModelFromTranscript` uses `transcript_path` (`guard.ts:130`).
  - For subagents of in-process teammates, that is the **lead's** transcript **[V: hooks.md says `transcript_path` is the main session's]**. The ceiling becomes the lead's model, not the teammate's.
  - Split-pane teammates are separate sessions with their own transcript and their own hook run **[U]**.
- **Plan §6** (`docs/plans/2026-10-26-multi-track-implementation-plan.md:299-345`): Mode A and Mode B are Claude↔agy peer work. Teams can only fan out within the Claude lane and add nothing to the agy handoff.

## 3. Comparison: Kanzo, beads, OpenRig, vibe-kanban
- **Kanzo**: a persistent local board with `waits_on` and `next_task`. It has no auth and no claim model **[R, per `todo-tools-evaluation-2026-11-07.md:71-85`]**.
- **beads**: a durable, git/Dolt-backed dependency graph with `bd ready`, usable by any harness that has a shell. Concurrent writers need server mode, and `bd init` writes `AGENTS.md` unless you pass `--skip-agents` **[L: `r2-beads-…md`]**.
- **OpenRig**: a tmux orchestrator that types into panes and rewrites config **[R]**.
- **vibe-kanban**: one worktree per card, with the agents it dispatches gated by human review **[R]**.
- **Agent teams** combine an **ephemeral, Claude-only, single-machine** version of the beads/Kanzo dependency queue with OpenRig-style pane spawning.
  - Their claim locking and auto-unblock are better than Kanzo's.
  - They give up the durability and harness neutrality that beads and Kanzo offer.
- For a backlog of record, beads (`--skip-agents`, under a flock) or a native A2A backlog stays the candidate. Teams are not one.

## 4. Verdict

### ADOPT
- **Nothing as infrastructure.**
- Optional personal use for in-session Claude research and review fan-out, with `teammateMode: in-process`. Only after the Jev fixes below land, and never inside the coordination server's design.

### BORROW
1. **coordination: a backlog with ready-work.** Add a backlog section to `docs/specs/server-api.md` and a decision in `docs/plans/consolidated-bridge-v2-plan.md`:
   - A `blocked_by[]` array on parked items, plus a computed **ready** view: pending, unowned, all blockers `COMPLETED`.
   - **Claim as a compare-and-set** in the single-writer SQLite: `UPDATE … SET owner=? WHERE owner IS NULL AND state='SUBMITTED'`, instead of file locks.
   - **Auto-unblock** dependents on `COMPLETED`.
   - **Ready is a query, never a trigger.** No self-claim on idle, which keeps "parked" intact.
   - Server-side create/complete validation, modelled on `TaskCreated` / `TaskCompleted` exit-2 semantics: reject with a reason, never auto-fix.
   - A **stale-claim sweep**, covering the documented "status can lag" failure.
   - Delivery receipts that report sent only when the write is durable, and drop-and-report for malformed entries. Both come from lessons in CHANGELOG 2.1.207 and 2.1.224.
   - Correct `docs/research/bridge-v2-ecosystem-analysis.md:71`: re-label the "agy can bridge in" claim as unverified and contradicted.
2. **coordination: wake.** Keep researching `CLAUDE_CODE_MESSAGING_SOCKET` and `crossSessionInbound` (already in `specs/server-api.md:1474`, `specs/spikes.md:522`).
   - Note `isolatePeerMachines` and the "messages can't approve" framing. They match "bodies are data".
   - The team mailbox is not a substitute.
3. **ax**
   - In `src/core/persistence.rs:116-160`, skip or flag panes that have no pane `@harness` inside a Claude window, so restore doesn't create dead shells.
   - Optionally add a read-only "N teammates" badge from `~/.claude/teams/*/config.json`, marked experimental because the schema is undocumented.
   - Document in `docs/configuration.md`: keep `teammateMode` at `in-process` under ax.
4. **cognitive-engine: Jev guard**
   - In `packages/jev/src/guard.ts:59-66,162-169`, resolve the effective model from:
     - `CLAUDE_CODE_SUBAGENT_MODEL`
     - `CLAUDE_CODE_SUBAGENT_MODEL_FORCE`
   - In `guard.ts:91`, also resolve plugin and managed agent definitions, or fail closed (`ask`) when the definition is unresolvable and the model may be strong.
   - When `tool_input.name` is set and `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`, include a "long-lived teammate" cost note in the verdict.
   - Defense in depth: recommend a user-installed native deny rule `Agent(model:opus)` (`Tool(param:value)` syntax, CHANGELOG 2.1.178 **[V]**). The user's installer would add it, not a script.
   - Optional: `TeammateIdle` / `TaskCompleted` hook calling `jev_judge` as a quality gate (RFC 002 / plan §3.1), off by default like Jev.

### SKIP
- **Agent teams as the coordination substrate, or as the plan §6 mechanism.**
  - Hierarchical: a fixed lead with no peer symmetry.
  - Session-scoped and not resumable.
  - Single machine and Claude-only, so agy cannot join.
  - Self-claim means auto-execution.
  - Split-pane spawning uses `send-keys`, which is the DDR-003 pattern.
  - Experimental, with fast-moving internals and undocumented on-disk schemas.
- **Split-pane (`tmux`/`auto`) teammate mode inside ax.**
- **Reading or writing `~/.claude/teams` / `~/.claude/tasks` from agy or the A2A server.**

## Sources
- https://code.claude.com/docs/en/agent-teams
- https://code.claude.com/docs/en/hooks (TeammateIdle, TaskCreated, TaskCompleted, common input fields)
- https://code.claude.com/docs/en/tools-reference (Agent tool behavior, task tool availability)
- https://code.claude.com/docs/en/costs#agent-team-token-costs
- https://code.claude.com/docs/en/cross-session-messaging
- https://code.claude.com/docs/en/env-vars
- https://code.claude.com/docs/en/interactive-mode (Task list)
- https://code.claude.com/docs/en/claude-directory
- https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md (2.1.32, 2.1.33, 2.1.84, 2.1.178, 2.1.183, 2.1.207, 2.1.224, 2.1.257, 2.1.290, 2.1.309–2.1.319)
- https://github.com/anthropics/claude-code/issues/23615
- https://github.com/anthropics/claude-code/issues/34150
- https://cmux.com/blog/cmux-claude-teams

## Local files cited
- `agentic-hub/plugins/coordination/AGENTS.md`
- `agentic-hub/plugins/coordination/docs/research/bridge-v2-ecosystem-analysis.md`
- `agentic-hub/plugins/coordination/docs/research/todo-tools-evaluation-2026-11-07.md`
- `ax/config/tmux/ax.conf`
- `ax/src/lib.rs`
- `ax/src/core/persistence.rs`
- `cognitive-engine/adapters/claude/hooks/hooks.json`
- `cognitive-engine/packages/jev/src/guard.ts`
- `cognitive-engine/packages/jev/src/hook.ts`
- `cognitive-engine/docs/plans/2026-10-26-multi-track-implementation-plan.md`
