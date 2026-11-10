> **Moved from `rockad/agentic-hub` on 2026-11-10**, scrubbed of machine-specific details. Current state: [`plans/2026-11-10-delivery-plan.md`](plans/2026-11-10-delivery-plan.md). Links to `plugins/...` refer to the `agentic-hub` repository.

# Adoption review: third-party tools and native harness features

*Date: 2026-11-07. Scope: `agentic-hub` (mainly `plugins/coordination`), `ax` and `cognitive-engine`.*

This document answers one question: **which third-party tools should be adopted, and which of their ideas or approaches should be borrowed, for the goals these three projects already have?** It began as the three-item `TODO.md` (Kanzo, OpenUI, OpenRig) and grew to cover:

- the backlog candidates beads and vibe-kanban
- the native multi-agent features of every harness in use or under consideration: Claude Code (including agent teams), Antigravity, Codex, OpenCode, Gemini CLI and Copilot CLI
- further tools found by a separate sweep
- problems across the three projects found along the way

Nothing was installed or run. External repositories were read from shallow clones; harness features were read from official docs, changelogs and source. Every claim in the evidence files carries a label:

- **verified**: read in source or official docs, with file and line
- **reported**: from a README or a third party only
- **unverified**: inferred

This document keeps the labels where a conclusion depends on them. The full evidence, one file per subject, is in [`research/2026-11-07-adoption-review/`](research/2026-11-07-adoption-review/README.md).

---

## Contents

1. [The answer in one page](#1-the-answer-in-one-page)
2. [The yardstick: your goals and rules](#2-the-yardstick-your-goals-and-rules)
3. [Where the three projects stand](#3-where-the-three-projects-stand)
4. [Goal by goal](#4-goal-by-goal)
5. [Harness matrix](#5-harness-matrix)
6. [Tool by tool](#6-tool-by-tool)
7. [Problems found across the projects](#7-problems-found-across-the-projects)
8. [Recommended plan](#8-recommended-plan)
9. [Decisions only you can make](#9-decisions-only-you-can-make)
10. [Corrections to earlier documents](#10-corrections-to-earlier-documents)
11. [Method and evidence](#11-method-and-evidence)

---

## 1. The answer in one page

**Nothing third-party should become infrastructure.** Every orchestrator examined (OpenRig, vibe-kanban, Claude agent teams, agent-deck, Antigravity teamwork) runs a lead that drives subordinate agents. That breaks symmetric agency, and most of them type into panes or run one-shot headless calls, which breaks DDR-003 and the subscription-first premise behind ax. The A2A coordination plan holds up. What the review adds is a set of concrete ideas that make it better, plus a few small tools that fit around it.

### Adopt (use as-is)

| What | For | Why |
|---|---|---|
| Claude Code statusline `rate_limits` (5-hour and 7-day usage) | ax statusline, Jev `jev_quota` | Native, subscription-only, ground truth for quota |
| Claude Code `Notification` hook (`agent_needs_input`, `idle_prompt`, …) | ax "needs input" tab state | Native signal; no scraping |
| Claude Code `--name` / `sessionTitle` as the session handle | coordination identity | The native name is also the `SendMessage` address |
| Remote Control push (Claude) and `--remote-control` (agy) | your phone as the human gate and agy's wake | Both vendors' own features on your own account. Human use only. |
| ntfy, self-hosted on the storage host over the tailnet | phone alerts for `INPUT_REQUIRED` and `QUARANTINED` tasks, both harnesses | Harness-neutral; stub text only; never a release path |
| `a2a-tck` and `a2a-inspector` | S1 exit gate and debugging | Official A2A conformance kit and inspector; test-only |
| smugglex and Cisco `mcp-scanner` (static mode) | S3 pre-public gate | Request-smuggling fuzzing; offline MCP tool-description lint |
| Copilot CLI as an ax harness | ax | Native interactive CLI on a subscription; `--session-id` makes tab binding trivial |

### Spike next (promising, unproven)

| What | For | Why |
|---|---|---|
| Claude inbox socket wake (`CLAUDE_CODE_MESSAGING_SOCKET`) from an `async: true` hook (the plan's SP6) | coordination Claude wake | Removes the `asyncRewake` timeout and the Esc re-arm gap; keeps stub-only text |
| Claude plugin monitors (new SP7) | coordination Claude wake | A session-long background process whose output reaches Claude; no hook timeout at all. Unverified: whether its output starts a turn in an idle session. SP7 must test that first. |
| `codex queue` stub wake | Codex as a peer | A durable native queue that runs only when the thread is idle; drives no TUI |
| OpenCode V2 plugin `session.synthetic` with `delivery: "queue"` | OpenCode as a peer | Documented, durable, in-process self-wake |

### Borrow (ideas, not code)

- **A backlog with "ready work" inside the A2A server.** Use beads' model (dependency edges, a ready query, an atomic claim, a lease with heartbeat and reclaim), the same model Claude agent teams use. Add it as a post-S1 stage ("S1b") instead of adding a second tool.
- **Delivery rules** from OpenRig: durable row plus at-least-once pointer wake; sanitize any peer text shown to another agent; a printed inventory of every file the installer writes.
- **Worktree lifecycle** from vibe-kanban and agent-deck for ax: close, prune, expiry, divergence badge, safe branch naming.
- **Cockpit ideas** from cmux and agent-deck: notification rings, jump to newest waiting, restore that auto-resumes only trusted commands.
- **Git-native provenance**: commit trailers (`A2A-Task:`, `Session:`) and `git worktree lock --reason` as a zero-daemon worktree lease.
- **Pointers instead of pasted context** in handoffs: link to Lookup or session transcripts and let the receiving session fetch them.

### Trial (optional, personal, after coordination S1)

- **beads** as a backlog only, in stealth mode, outside any repo. It has the better multi-agent model.
- **Kanzo** as a backlog only, over stdio, with HTTP turned off. It is the lighter board for a human.

Run one, not both. The trial tells you whether a separate backlog earns its keep before the A2A backlog (S1b) is built.

### Skip

OpenRig, vibe-kanban (shutting down), OpenUI, Claude agent teams as a substrate, Claude Channels, Antigravity Automations, `agy agentapi`, Gemini CLI on your plan (it no longer runs on Google AI Pro), OpenCode's "Claude Agent" provider, Taskmaster AI (Commons Clause), agy-hud, Snyk Agent Scan, Pushover and Linear.

### Top problems found (details in section 7)

1. **Port 8120 is claimed twice**: by the coordination server and by the Harness² daemon in cognitive-engine.
2. **ax can wipe `~/.gemini/config/hooks.json`**: on any parse failure it writes `{}` plus its own entry, on every attach, without a backup. Coordination's agy hooks will live in that file.
3. **The Jev subagent guard has gaps**:
   - it ignores `CLAUDE_CODE_SUBAGENT_MODEL`
   - it misses plugin-scoped agents
   - the OpenCode adapter never fires (wrong tool name and wrong argument field in V2)

   A future Codex adapter must return `deny`, because Codex fails open on `ask`.
4. **The handle format is unreconciled**: the spike used `name#sid8`, the spec forbids `#`.
5. **`server-api.md` section 11.2 has the agy hooks.json shape wrong.**
6. **Jev and memory exist twice** (agentic-hub and cognitive-engine) with the same names and the same `~/.config/jev/` files. Retirement (RFC 005) has not started.

---

## 2. The yardstick: your goals and rules

Every candidate was measured against these. They come from your own documents (`plugins/coordination/AGENTS.md`, the consolidated plan, cognitive-engine's RFCs) and from this conversation.

### Goals

| # | Goal | Home |
|---|---|---|
| G1 | Live, independent agent sessions on several machines exchange tasks as peers | `plugins/coordination` |
| G2 | A dependency-aware backlog: "what is ready to work on" | gap in `plugins/coordination`; hand-edited tasklists today |
| G3 | One cockpit to run many subscription-backed agent CLIs side by side | `ax` |
| G4 | The human is reachable wherever they are when an agent needs them | ax, coordination |
| G5 | Model cost and quality governance: route cheap work cheaply, cap subagent tiers | Jev (cognitive-engine) |
| G6 | Claude and Antigravity collaborate on the cognitive-engine build | cognitive-engine plan §6 |
| G7 | The coordination server is safe to expose in stages (loopback, tailnet, public) | `plugins/coordination` S1 to S3 |
| G8 | Reports and status are easy to read | coordination `/status`, Harness² reports |

### Rules a candidate must not break

1. **Subscription first.** Agents run on consumer plans (Claude Pro/Max, Google AI Pro) inside each vendor's own interactive client. That is why ax exists: it runs the native CLIs side by side in tmux instead of routing work through an API-key orchestrator. Anything that needs API keys, or that drives agents through one-shot headless calls (`claude -p`, `agy -p`, `codex exec`), fails this test or puts the plan at risk.
2. **Symmetric agency.** Peers are equals. No lead, orchestrator or subordinate roles.
3. **DDR-003.** Never launch, drive or wrap agy: no `tmux send-keys`, no paste-buffer, no PTY, no `agy -p`, no `agentapi`.
4. **Restrictive terms, applied symmetrically (DDR-001).** Skip anything whose terms restrict Claude Code or Antigravity. Apply the same test to every vendor.
5. **Tasks are parked, never auto-executed.** A session takes a task with an explicit tool call.
6. **Stub-only wake.** Wake text is a constant ("N pending, call a2a_inbox"); bodies arrive only as tool results.
7. **An API key on every request**, loopback included; scoped keys; no public bind without TLS.
8. **Never write `AGENTS.md`**, never write through a symlink (decision 27).
9. **Harness config changes only through the user-run installer and chezmoi** (decision 16). No tool edits `~/.claude.json`, `~/.tmux.conf` or `~/.codex/config.toml` behind your back. Two existing or recommended items sit in tension with this and must go through the user-run installer: ax rewriting `~/.gemini/config/hooks.json` on every attach, and the Claude `Notification` and `WorktreeCreate` hooks recommended for ax in section 4 (G3).
10. **Permissive licences.** No vendor riders, no Commons Clause. AGPL is acceptable only as a separate, unmodified process.

---

## 3. Where the three projects stand

### agentic-hub

- Seven plugins ship: chrome-devtools, gitea, google-workspace, jev, local-transcribe, memory and subagents.
- `plugins/coordination` is documents only. Planning is complete. S0 spikes are mostly done:
  - SP1 `asyncRewake` wake passes.
  - SP2 Claude half passes, with spec changes.
  - SP5 a2a-python on SQLite passes with four wrappers.
  - The agy half (A2, A3, A8, SP3) is open and run by you.
  - The S1 backlog from the spikes is written down. S1 has not started.
- Documentation has drifted:
  - The coordination README still says "next step: S0".
  - The root README and AGENTS.md trees list coordination and subagents (fixed 2026-11-07). At review time the README omitted celi5 and cplain, and AGENTS.md omitted celi5, cplain and mcp-toggle (fixed 2026-11-08, uncommitted).
  - README and AGENTS.md pointed at a `plugins/jev/opencode/` that does not exist (references removed 2026-11-08, uncommitted).
  - `marketplace.json` `updated_at` is 2026-10-07.

### ax (v0.8.4)

- Rust and tmux.
- Launches agy, claude, opencode, codex and custom TOML harnesses as plain interactive processes, one per tab.
- Keeps a tab-to-conversation registry, layout snapshots, restore, two-way title sync and a statusline.
- No messaging, no MCP, no `send-keys`: verified by grep, so it is DDR-003-clean.
- The CHANGELOG stops at 0.7.0. Tags exist (15, up to v0.8.4).

### cognitive-engine

- Only Track 1 (Jev and the adapters) is on `main`.
- Eleven PRs are open. Nine are in the merge queue, mostly stacked:
  - #4 and #18: guard fixes, including the PR #3 remediation
  - #8: Track 2
  - #9: conformance runner
  - #10: SMB sources
  - #5 and #6: Track 3
  - #7: removes the hand-relayed handoff
  - #11: RFC 006

  Two more have no slot yet: #12 (Argon hardening) and #13 (BuildSlot).
- The bridge between Claude and Antigravity is a hand-relayed markdown file. It gives fixed "Orchestrator" and "Builder" roles, which conflicts with symmetric agency, and its Turn 4 is stale.
- RFC 005 plans to retire agentic-hub's `plugins/jev` and `plugins/memory`. Its target marketplace keeps four plugins and drops coordination and subagents.

Full snapshot with sources: [`roadmap.md`](research/2026-11-07-adoption-review/roadmap.md).

---

## 4. Goal by goal

### G1. Live peer coordination: wake, inbox, identity

**Verdict: keep the A2A server as the one cross-harness inbox. Improve the Claude wake with native features. Give each additional harness the best wake its own documented hooks allow.**

Why no native feature replaces it:

- **Claude cross-session messaging** (`SendMessage`, v2.1.224 and later) is real and well framed. The receiver is told a peer message "can't approve anything". But:
  - it is Claude-only
  - it is plain text, not durable, and has no task states or quarantine
  - across machines it is relayed through Anthropic and needs both sessions on Remote Control
  - a container cannot reach the host socket
- **Claude agent teams** are a fixed lead with teammates that die with the session. Teammates self-claim work, which is auto-execution. The tmux mode uses `send-keys` (reported in GitHub issues, and consistent with CHANGELOG 2.1.183).
- **Claude Channels** are still a research preview:
  - each launch needs a development-channel flag
  - idle sessions reportedly drop events
  - new: a channel server on MCP protocol revision 2026-07-28 is not registered at all
  - a channel can declare permission relay, which would let replies approve tool use
- **Antigravity** has no documented idle wake. Subagents and `/teamwork-preview` are parent and child inside one process. Automations start new conversations.
- **OpenRig, vibe-kanban, agent-deck and cmux** all deliver by typing into panes or by running headless subprocesses.

What to take:

| Harness | Best compliant wake | Status |
|---|---|---|
| Claude Code | today: `asyncRewake` long-poll (SP1 passed). Better: an `async: true` hook that keeps one listener alive and posts the stub to the session's own inbox socket (SP6). Alternative: a plugin monitor (SP7). | SP6 and SP7 are spikes. Vary the stub (carry the count) or the native duplicate filter drops repeats. |
| Antigravity (agy) | none while idle. `SessionStart` and `PreInvocation` `injectSteps` surface the count at the next model call. The human is the wake: ntfy tells them an A2A task arrived, and `--remote-control` lets them answer from a phone. Remote Control does not notify on external events itself. | As planned. The held `Stop` park stays spike-only. |
| Codex | `codex queue --thread <id>` with the constant stub. The daemon parks it and runs it only when the thread is idle. | Spike, then your decision: it is an external process enqueueing into the thread, though it drives no TUI. |
| OpenCode | an in-process V2 plugin holding one long-poll for the whole OpenCode service, then `session.synthetic(..., delivery: "queue")` with the stub; or `instructions.entry.put` for a no-turn notice | Spike, pinned version (`experimental.idle` may change). Only worth it on a ChatGPT or Copilot plan (section 5). |
| Copilot CLI | `sessionStart` stub; `agentStop` block with the stub (30 s timeout, 8-block cap); `/every` as a paid fallback | Feasible; terms are permissive. |
| Gemini CLI | `SessionStart`/`BeforeAgent` `additionalContext`; `AfterAgent` deny as a turn-end nudge | Moot on your plan (section 5). |

**Identity.** Use the harness's own session name:

- Claude: `--name` / `sessionTitle`.
- Copilot: `--session-id`, a UUID that ax can assign before launch.
- Codex: thread name, with the id as A2A `contextId`.
- OpenCode: sessionID.

Reconcile the handle grammar first (section 7, item 4).

**MCP auth from the environment.** Each of these can send `Authorization: Bearer ${A2A_API_KEY}` without a literal key in config, which keeps decision 24 intact:

- Claude Code (verified in SP2)
- Gemini CLI
- Copilot CLI
- Codex (`bearer_token_env_var`)

OpenCode supports headers, but whether it expands variables in them is unverified; read the key from a 0600 file. agy cannot expand variables, as already planned around.

**Listener caveat (Claude).** An idle, unattached `claude --bg` session's process may be stopped after about an hour, which kills its `asyncRewake` or monitor listener. The server's "recipient not listening" badge must cover this.

**Borrow from OpenRig** (concepts only):

- the row carries the obligation exactly once, and the wake is an idempotent pointer with a bounded retry ladder that escalates to the human
- sanitize peer text before showing it to another agent: control, format and bidi characters stripped, length capped
- hash records so the installer can tell "we wrote it" from "you edited it"
- a printed write inventory

Evidence: [`claude-native.md`](research/2026-11-07-adoption-review/claude-native.md), [`claude-agent-teams.md`](research/2026-11-07-adoption-review/claude-agent-teams.md), [`antigravity-native.md`](research/2026-11-07-adoption-review/antigravity-native.md), [`codex-native.md`](research/2026-11-07-adoption-review/codex-native.md), [`opencode.md`](research/2026-11-07-adoption-review/opencode.md), [`copilot-cli.md`](research/2026-11-07-adoption-review/copilot-cli.md), [`openrig.md`](research/2026-11-07-adoption-review/openrig.md).

### G2. A backlog with "ready work"

**Verdict: build it into the A2A server after S1 ("S1b"), using beads' model. Optionally trial one external backlog first to learn whether you use one.**

History: beads was evaluated on 2026-11-03 as the coordination ledger and **superseded** on 2026-11-04 by the A2A task store (Dolt dependency, single writer by default, edits host config). That decision stands. The gap it leaves, "what is ready", was noted at the time and is still open. Here beads is considered **only as a backlog**.

| Option | Ready query | Atomic claim | Auth | Host writes | Licence | Verdict |
|---|---|---|---|---|---|---|
| beads (`bd`) | yes, in SQL, with explain | compare-and-set plus 5-minute lease, heartbeat, reclaim. Strictly atomic only in server mode; in embedded mode run one writer at a time | MCP: stdio, none. HTTP: shared secret, no scopes | plain `bd init` writes `AGENTS.md` through symlinks and auto-commits with `--no-verify`; `--stealth` avoids it | MIT | **trial (stealth) or borrow the model** |
| Kanzo | via filter (`status:todo -is:waiting -is:human`); `next_task` is atomic | only via `next_task`; no assignee, no lease | **none**; HTTP `/mcp` and `/api/*` are on by default | none | AGPL-3.0 | **trial (stdio, HTTP off) or skip** |
| Claude agent teams' task list | implicit (pending, unowned, unblocked) | file lock | n/a | runtime-owned, undocumented schema | n/a | **borrow the model** |
| Gitea issue dependencies | `/issues/{i}/dependencies` and `/blocks` (reported); ready computed client-side | no | your Gitea tokens | none | your own instance | **adopt as read-only tools** in the gitea plugin. The sweep rated it "adopt as the backlog". Here it stays a source, not the coordination backlog, because it has no claims or leases. |
| GitHub Issues (`gh` ≥ 2.94 `--blocked-by`) | none native; computed client-side (reported) | no | GitHub | none | n/a | mirror only, for `/delegate` work |
| vibe-kanban | client-side only | n/a | none | writes `AGENTS.md` and agent configs | Apache-2.0 | **skip** (sunsetting) |
| Taskmaster AI | yes | n/a | n/a | n/a | MIT + Commons Clause | **skip** (rider) |
| Codex message board, agent graph | in development | n/a | n/a | n/a | n/a | watch |

**The S1b design, borrowed:**

- Tables `backlog_item` and `backlog_dep` (`blocks`, `parent-child`, `waits-for` all or any; plus non-blocking `discovered-from` and `supersedes`).
- A **ready** view: open, not deferred, no unclosed blocking edge.
- MCP tools:
  - `a2a_ready` (read)
  - `a2a_claim` and `a2a_release` (write), with the claim as one `BEGIN IMMEDIATE` compare-and-set
- Lease TTL plus heartbeat, using session registration or listener liveness as the heartbeat. A stale-claim sweep.
- Auto-unblock on `COMPLETED`.
- **Ready is a query, never a trigger.** Nothing self-claims on idle, which preserves "parked".
- Create and complete validation that rejects with a reason, modelled on Claude's `TaskCreated` and `TaskCompleted` hook semantics.

**The cognitive-engine fit.** Jev's `BatchDispatchPlan` already has this shape: `SubTask.dependencies` become `blocks` edges, and Kahn's stages are the ready query over time. The multi-track plan's WS1 to WS9 become epics. Add an optional exporter, off by default, with no network client in core.

Evidence: [`beads.md`](research/2026-11-07-adoption-review/beads.md), [`kanzo.md`](research/2026-11-07-adoption-review/kanzo.md), [`vibe-kanban.md`](research/2026-11-07-adoption-review/vibe-kanban.md), [`further-candidates.md`](research/2026-11-07-adoption-review/further-candidates.md).

### G3. The cockpit (ax)

**Verdict: ax remains the right tool, and nothing examined replaces it.** OpenRig is the only tool with the same shape, and it is an orchestrator that types into panes and rewrites `~/.tmux.conf`, `~/.claude.json` and `AGENTS.md`. Running both on one tmux server would fight over global options and hooks. Take these ideas into ax instead:

| Idea | Source | Change in ax |
|---|---|---|
| "Needs input" state and jump to newest waiting | Claude `Notification` hook; cmux; agent-deck | Hook sets `@needs_input` on the pane or window; key binding jumps to it |
| Subscription quota meters | Claude statusline `rate_limits`; agy statusline "AI Credits" (reported) | Segment fed by the Claude statusline JSON; never read agy's OAuth token (agy-hud does; skip it) |
| Coordination pending count | coordination S1 `GET /sessions` | Read-only segment; pane options `@a2a_handle`, `@a2a_pending` set by the harness's own `SessionStart` hook, never by typing |
| Worktree lifecycle | vibe-kanban, agent-deck, ccmanager | `ax wt close` (refuse while busy, then remove and prune); optional expiry; pinned flag; self-healing create; shell-quote the branch name (it is interpolated unquoted today) |
| Divergence badge | vibe-kanban merge preconditions | "base +N / branch +M" per worktree tab; display only |
| Worktree lease | `git worktree lock --reason` | Lock on create with session and task id; unlock on close |
| Register worktrees made outside ax | Claude `WorktreeCreate`/`WorktreeRemove` hooks | One hook writes to ax's registry |
| Discover sessions ax did not launch | `claude agents --json` (reported); OpenRig's read-only discover | Read-only listing; never adopt by typing |
| Restore only trusted commands | cmux | Restore asks before re-running anything not on an allowlist |
| Copilot CLI harness | Copilot | `CopilotDriver`: `--session-id <uuid>` for new tabs, `--resume`, `--continue`; history from `~/.copilot/session-state/<id>/events.jsonl` (undocumented; version-guard it) |
| Codex history and restore | Codex | Read `$CODEX_HOME/state_5.sqlite` `threads` (glob the name); restore with `codex resume <id>` |
| OpenCode title sync | OpenCode V2 | Use `PATCH /api/session/:id` (or `opencode api session.update`) instead of writing `session_v2` in an event-sourced store; honour `OPENCODE_DB` |
| Teammate panes | Claude agent teams | Keep `teammateMode: in-process` under ax; skip panes without `@harness` when snapshotting, or restore creates empty shells |

Also fix in ax:

- the hooks.json writer (section 7, item 2)
- the `"gemini"` alias that maps to Antigravity (`src/core/harness.rs:38`), which would collide with a Gemini CLI harness
- the picker's `--resume <id>` for Claude: the argument parser does not recognise it, so the id lands in the initial prompt
- Claude session ids never bind to a tab through the hook, which only looks ids up in agy's database

Evidence: [`openrig.md`](research/2026-11-07-adoption-review/openrig.md), [`vibe-kanban.md`](research/2026-11-07-adoption-review/vibe-kanban.md), [`further-candidates.md`](research/2026-11-07-adoption-review/further-candidates.md), [`verify.md`](research/2026-11-07-adoption-review/verify.md).

### G4. Reaching the human

**Verdict:**

- Use each vendor's own push for its own sessions (Claude Remote Control "push when actions required"; agy `--remote-control`).
- Add ntfy for a harness-neutral feed from the coordination server.

Rules:

- **Notifications carry the constant stub only.** Never task bodies.
- **No notification is a release path.** Quarantine release stays on a TTY, one typed decision per task (decision 19). No ntfy action buttons, no Remote Control approvals for coordination.
- Self-hosted ntfy on the storage host, over the tailnet, with access tokens and deny-all by default. iOS instant delivery needs the upstream relay through ntfy.sh, which sees only a poll id.
- Skip Pushover (proprietary, nothing over ntfy).

### G5. Model governance and cost (Jev)

**Verdict: under subscription-first, Jev's job is guarding tiers and offloading cheap work, not being the main execution path. Fix the guard gaps. Use native quota data.**

**Guard gaps (Claude):**

- `CLAUDE_CODE_SUBAGENT_MODEL` (and `_FORCE`) are never read, so a subagent with no explicit model passes even when the environment selects Opus.
- Agent definitions from plugins or managed scopes, and names with `:`, are not found.
- A mod's `agent.spawn` hook can replace the model after the check.
- There is no count limit. With agent teams enabled, named subagents silently become long-lived teammates.
- Add a native deny rule (`Agent(model:opus)`) as defence in depth. Your installer adds it, not a script.

**Guard gaps (other harnesses):**

- **OpenCode adapter: never fires.** The V2 delegation tool is `subagent`, not `task`. The argument is `event.input.prompt`, not `out.args`.
- **Codex.** Match `^(spawn_agent|Agent)$`. Return `deny`, never `ask`: Codex fails open on `ask`. The hook input already carries the session model.
- **agy.** Check whether a model set in `define_subagent` front matter bypasses the `invoke_subagent` guard (unverified).

**Quota truth:**

- Claude's statusline JSON has `rate_limits.five_hour` and `rate_limits.seven_day` for subscribers. Tee it to a cache for `jev_quota`.
- ccusage can reconstruct history from transcripts.

**Model slugs:**

- Jev's OpenCode tier map uses `~anthropic/...-latest` OpenRouter slugs, which are pay-per-token.
- If OpenCode is used on a subscription, map tiers to `openai/...` (ChatGPT sign-in) or `github-copilot/...` models.
- Never use `claude-agent/*` (section 5).

**Copilot as a paid fallback:**

- Copilot plans now meter tokens: Pro is $10 for 1,500 credits, about $15 of tokens.
- Cheap models such as Haiku 5.5 and GPT-6 Luna cost about $0.10 per 1M input tokens.
- It is a reasonable guarded fallback when the free OpenRouter pool rate-limits. It is not cheaper than free, and probably not cheaper than Claude Max for heavy work.

**Borrow from Gemini CLI's policy engine:** an `interactive` flag per rule, where "ask" becomes "deny" when headless. It is a clean way to split interactive and headless rules.

### G6. The Claude–Antigravity collaboration in cognitive-engine

**Verdict: move the cognitive-engine bridge onto coordination after S1, with symmetric roles. Until then, use native features for the Claude-to-Claude leg only.**

- The hand-relayed bridge file repeats the frozen file-channel pattern. It assigns fixed Orchestrator and Builder roles. Its plan section 6 contradicts itself: §6.2 says isolated worktrees, a second §6.3 says both harnesses work in the main checkout.
- For Claude to Claude on one machine, `SendMessage` with `notify_when_idle` plus native worktrees (`-w`, `isolation: worktree`) covers it today.
- The agy leg is exactly what the A2A server is for.
- Borrow vibe-kanban's merge preconditions into plan §6.2:
  - refuse a merge when the base is ahead
  - no direct merge while a PR is open
  - a teardown step after merge
  - a branch naming rule

### G7. Safe staged exposure (S1 to S3)

| Stage | Add | Notes |
|---|---|---|
| S1 | `a2a-tck --level must` against loopback with a test key; `a2a-inspector` on 127.0.0.1 | Do not enable the inspector's Gemini host agent (needs an API key). Check that `QUARANTINED` (shown as `INPUT_REQUIRED`) passes MUST tests. |
| S1 | Read a2a-python 1.3.0 notes | 1.3.0 adds a configurable timeout for releasing parked interrupted tasks; 1.2.3 rejects push URLs in shared address space; 1.2.1 uses keyset cursors for ListTasks. Still the latest release. |
| S1 | Sanitize peer text in previews and status | OpenRig's character classes |
| S2/S3 | smugglex | CL.TE, TE.CL, H2 downgrade; run only against your own staging tunnel |
| S3 | `mcp-scanner --analyzers yara static` | Offline lint of `/mcp` tool descriptions |
| dev | MCP Inspector ≥ 0.14.1 only, loopback only | CVE-2025-49596 (unauthenticated proxy RCE) in older versions |
| skip | Snyk Agent Scan | Uploads tool descriptions and runs servers from your config |
| skip | Schemathesis | No JSON-RPC support |

**Note on the Gemini CLI A2A client.** It can call A2A agents, but it speaks the 0.3 SDK and sends no `A2A-Version` header (found by grep). Decision 33's strict version check would therefore likely reject it; this is unverified on the wire. It is usable as an interop test client only with that check relaxed for tests.

### G8. Reports and status pages

**Verdict: OpenUI does not fit.**

- ax is a terminal UI, and the harnesses render in terminals.
- A coordination `/status` page has a fixed schema. Static HTML plus `fetch()` is simpler than generative UI with a 900 KB runtime and default-on install telemetry.
- Decision 19 rules out its interactive actions for quarantine.
- Its `@openuidev/a2ui` package is A2UI, not A2A.
- Do not install its skill globally: its trigger ("dashboard") is broad and it steers toward its own gateway.

---

## 5. Harness matrix

| | Claude Code | Antigravity (agy) | Codex CLI | OpenCode | Gemini CLI | Copilot CLI |
|---|---|---|---|---|---|---|
| Runs on your subscription | yes (Pro/Max) | yes (Google AI Pro) | yes, on a ChatGPT plan | only on ChatGPT or Copilot sign-in (not Claude, not Google) | **no**: consumer tiers moved to agy on 2026-06-18 | yes (Copilot plans, now token-metered credits) |
| Terms on automating it | native features fine; third-party use of the plan credentials forbidden | FAQ: third-party wrapping unsupported, names Claude Code and OpenCode | no restriction found (unverified) | depends on the provider; Claude Agent provider collides with Anthropic's Agent SDK note | OAuth piggybacking banned; wrapping with an API key allowed | automation sanctioned (`-p`, SDK, ACP) |
| Idle wake | `asyncRewake` (proven); inbox socket and plugin monitors (spikes) | none | `codex queue` (spike) | V2 plugin synthetic (spike) | none | `agentStop` block, `/every` |
| Surfacing hooks | `SessionStart`, `UserPromptSubmit` | `SessionStart`, `PreInvocation` `injectSteps` | `SessionStart`, `UserPromptSubmit` | `instructions.entry.put` | `SessionStart`, `BeforeAgent` | `sessionStart` |
| MCP bearer from env | yes | no (literal in a 0600 file) | yes | headers yes; env expansion unverified | yes | yes |
| Native multi-agent | subagents, agent teams (lead-based) | subagents, teamwork preview (lead-based) | subagents, agent roles; message board in development | subagents, background | subagents, remote A2A subagents | custom agents, `/delegate` to cloud |
| In ax today | yes | yes (best support) | launch only | launch, history, title (direct DB write) | via TOML; alias bug | via TOML |
| Recommended role | peer (now) | peer (now) | candidate peer, if you hold a ChatGPT plan | candidate peer, only on ChatGPT or Copilot | skip on your plan | ax harness; candidate peer |

**DDR-003 extension proposal ("DDR-003a").** Give Google-account-authenticated `gemini` the same never-launch, never-drive treatment as agy, and never reuse `~/.gemini/oauth_creds.json`. Antigravity's terms penalise "Antigravity and/or Gemini CLI accounts" together.

**Shared `~/.gemini`.** This was an open question in the plan. The answer: the parent folder is shared, the files are not.

- agy uses `~/.gemini/config/` and `~/.gemini/antigravity-cli/`.
- Gemini CLI uses `~/.gemini/settings.json`, `GEMINI.md`, `tmp/` and `agents/`.
- Gemini CLI loads skills from `~/.agents/skills` and `<workspace>/.agents/skills` (verified). agy reportedly loads the workspace `.agents/skills`, so a workspace skill probably loads in both.

---

## 6. Tool by tool

Each card states the verdict and the decisive facts. The full reasoning is in the evidence file.

### Kanzo: trial only, or skip

*AGPL-3.0, v0.4.1, about two weeks old, effectively one maintainer.* [`kanzo.md`](research/2026-11-07-adoption-review/kanzo.md)

- Ten MCP tools. `next_task` atomically moves the top ready card to doing. `waits_on` dependencies: only `done` unblocks.
- There is no assignee or lease. Comment authorship comes from the MCP client name, so two Claude sessions look identical.
- HTTP `/mcp` and `/api/*` are **on by default**, with no auth. Any local process that sends no `Origin` can read and write.
- `POST /api/update` downloads an installer and pipes it to `sh`, also without auth. The stdio server re-execs itself when its binary changes.
- The server instructions tell agents to update the board unprompted. That is in tension with "parked".
- Projects are keyed by `owner/repo` with the host dropped. A Gitea repo and a GitHub repo with the same owner and name collide, which matters because your repos are on Gitea.
- If trialled:
  - stdio only, `mcp_http = false`, no share, join or service
  - deny `delete_task`
  - a skill that frames card text as data
  - a pinned binary outside auto-updated paths

### beads: backlog trial (stealth), and the model to borrow

*MIT, canonical home `gastownhall/beads`.* [`beads.md`](research/2026-11-07-adoption-review/beads.md)

- The best "ready work" design examined:
  - 19 dependency types, 4 of them blocking
  - `bd ready --claim` is one transaction
  - leases with heartbeat and reclaim
  - `row_lock` stops silent merges of conflicting claims
  - SSE event stream with a cursor contract
- Dolt is now the only backend; SQLite survives only as a migration bridge.
- Embedded mode is single-writer, and `bd serve` refuses it because claims there are not strictly atomic. Across machines, conflicting claims surface later as Dolt merge conflicts.
- Plain `bd init` writes `AGENTS.md` **through symlinks**, installs Claude, Codex and Cursor config, and auto-commits with `--no-verify`. `bd setup gemini` writes `~/.gemini/settings.json` globally. Never run either.
- A compliant trial:

  ```
  BEADS_DIR=<outside any repo> bd init --stealth --skip-agents --skip-hooks --non-interactive
  bd config set dolt.local-only true
  ```

  Use the CLI only. Skip beads-mcp: its own README puts it at 10k to 50k tokens, and its `admin` tool can delete issues.

### vibe-kanban: skip, borrow the worktree lifecycle

*Apache-2.0, **sunsetting** per its README.* [`vibe-kanban.md`](research/2026-11-07-adoption-review/vibe-kanban.md)

- Drives agents as one-shot headless subprocesses: `claude -p` stream-json, ACP for Gemini and Copilot, `codex app-server`, `opencode serve`.
- Supports `--dangerously-skip-permissions`. No agy support.
- Writes `AGENTS.md` and `CLAUDE.md` into workspaces, and agent MCP configs into `~/.claude.json`.
- PostHog and Sentry telemetry are on by default, and an OAuth login switches analytics back on.
- Its local MCP has no auth and is installed as `npx vibe-kanban@latest`.
- The orchestrator mode lets one agent drive others.
- Worth borrowing:
  - the worktree lifecycle
  - merge preconditions
  - the review-comments-to-markdown format
  - the "create task" and "start work" kept as separate steps

### OpenRig: skip, borrow delivery ideas

*Apache-2.0, v0.7.9, very fast release cadence.* [`openrig.md`](research/2026-11-07-adoption-review/openrig.md)

- **Verified** that messages are typed into panes: `tmux load-buffer`, `paste-buffer`, then `send-keys Enter`.
- `rig adopt` types an identity hint into the adopted pane.
- Lead and orchestrator topology with escalation up the tree.
- Writes:
  - `~/.tmux.conf`
  - `~/.claude.json` (trust and onboarding)
  - project `.claude/settings.local.json`
  - `~/.codex/config.toml` (`trust_level = "trusted"`)
  - managed blocks in `AGENTS.md` and `CLAUDE.md`, following symlinks
- Runs `npm -g` and brew installers.
- Daemon on port 7433 binds loopback **plus the Tailscale interface** by default. No token is required by default, so anyone on the tailnet can type into panes.
- No agy, Gemini or OpenCode runtime.
- The ten ideas worth borrowing are listed in the evidence file. The best are the durable-row and pointer-wake split, peer-text sanitizing, and the write inventory.

### OpenUI: skip

*MIT.* [`openui.md`](research/2026-11-07-adoption-review/openui.md). See G8.

### Claude Code native features

[`claude-native.md`](research/2026-11-07-adoption-review/claude-native.md)

**Adopt:**

- `--name` and `sessionTitle` as handles
- `SessionStart` context
- the `Notification` hook
- worktrees
- plugins as the packaging unit
- `asyncRewake` as the baseline wake

**Spike:**

- the inbox socket (SP6)
- plugin monitors (SP7)

**Borrow:**

- cross-session messaging for Claude-only notes
- `TaskCreated` and `TaskCompleted` hooks
- `claude agents --json`
- the statusline fields

**Skip:**

- Channels
- `claude mcp serve` (tools only; breaks symmetry)
- cloud routines and cloud sessions (not live peers)
- the Monitor tool for wake (30-minute cap)

### Claude Code agent teams: skip as substrate, borrow the task model

[`claude-agent-teams.md`](research/2026-11-07-adoption-review/claude-agent-teams.md)

- Experimental. There is one implicit team per session since 2.1.178.
- A fixed lead. Teammates message each other directly. A task list with dependencies, file-locked claims and auto-unblock.
- Teammates inherit the lead's permission mode, including bypass. Plan approvals are auto-approved by default.
- Not resumable, one machine, Claude-only.
- An earlier research note claimed agy could bridge into a team. **The official docs contradict this.**
- Under ax, keep `teammateMode: in-process`.

### Antigravity native features

[`antigravity-native.md`](research/2026-11-07-adoption-review/antigravity-native.md)

- **Adopt:**
  - `SessionStart` and `PreInvocation` `injectSteps`
  - the plugin bundle as the install unit (gated on SP2 A8)
  - the MCP client with a header
  - skills
  - `--remote-control` for the human
- **Borrow:**
  - the subagent lifecycle states (Running, Idle, Killed; "a message wakes an idle subagent") as a model for A2A sessions
  - teamwork roles: a dispatch-only orchestrator and a forced final auditor, for Jev batch dispatch
  - workflows as a one-word human wake (`/a2a`)
- **Skip:**
  - Automations
  - `agentapi`
  - `conversation_summaries.db` for coordination
  - rules and `AGENTS.md`
- **Caveat:** the official pages could not be fetched from this session. Re-read the live FAQ before S1, since plan Risk 5 is still open.
- **Two reported risks to check in A8 and SP3**, either of which would break a plugin-only install:
  - plugin hooks' inject and veto outputs may be ignored
  - external hook commands may need listing in `trusted_hooks.json`

### Codex

[`codex-native.md`](research/2026-11-07-adoption-review/codex-native.md)

- **Adopt:**
  - MCP over HTTP with `bearer_token_env_var`
  - `SessionStart` and `UserPromptSubmit` context
  - `PreToolUse` on `spawn_agent`/`Agent` for Jev, with deny not ask
  - read-only history from `state_5.sqlite`
  - `codex resume`
- **Spike:** `codex queue`.
- **Watch:** `wake_on_agent_messages` and the agent message board.
- **Skip:**
  - `codex exec`
  - `codex mcp-server` (appears removed at HEAD)
  - the `sleep` tool
  - legacy `notify`
- The installer must never set `trust_level` itself, which is the OpenRig mistake.

### OpenCode

[`opencode.md`](research/2026-11-07-adoption-review/opencode.md)

- **Subscriptions.**
  - Anthropic OAuth was removed in OpenCode 1.3.0.
  - The new "Claude Agent" provider drives your `claude` binary through the Agent SDK. Anthropic's Agent SDK page says third parties may not offer claude.ai login "unless previously approved". **Skip.**
  - Google's Antigravity FAQ names OpenCode as prohibited with an Antigravity login.
  - What remains is ChatGPT sign-in, Copilot, and OpenCode's own plan.
- **Technically the richest wake surface** of all the harnesses:
  - a durable session inbox with `queue` or `steer`, and `resume:false`
  - no-turn instruction entries
  - a password-protected local API
- **If it becomes a peer**, take tasks under tighter permissions (`ask` on shell and edit), or through a dedicated restricted agent. The default Build agent has broad tool access. Use `delivery: "queue"`, never `steer`, and keep OpenTunnel remote pairing off.
- **Fix ax's title sync.** It writes a projection in an event-sourced store; use the API instead.
- **Fix the Jev adapter** (G5).

### Gemini CLI: skip on your plan

[`gemini-cli.md`](research/2026-11-07-adoption-review/gemini-cli.md)

- **Since 2026-06-18 it no longer serves Google AI Pro or Ultra.** It is Code Assist Standard/Enterprise, Vertex or API key only.
- Useful knowledge to borrow:
  - the 11 hook events and `hooks migrate`
  - the policy engine
  - the remote A2A subagent client
- `gemini-cli-a2a-server` is a spawned headless worker with `autoExecute`. Skip it.

### Copilot CLI: adopt as an ax harness; candidate peer

[`copilot-cli.md`](research/2026-11-07-adoption-review/copilot-cli.md)

- **Billing.** Token-metered "AI Credits" since 2026-06-01.
- **Models.** Claude, Gemini and GPT on one bill.
- **Session control.** `--session-id`, `--resume` and `--continue`.
- **Hooks** close to Claude's.
- **Watch out:** it reads repository-level `.claude/settings.json` hooks and `.claude/agents`, so repository-scoped Claude hooks fire twice. Keep coordination hooks user-scoped, with a separate `~/.copilot/hooks` target.
- **Fail-open risk:** a command hook that times out fails open, even `preToolUse` (30 s default). A Jev guard on Copilot would inherit that.
- Spike the wake before building a peer.
- GitHub Issues and `/delegate` suit GitHub-hosted repos only. Your Gitea repos do not fit.

### Other tools from the sweep

[`further-candidates.md`](research/2026-11-07-adoption-review/further-candidates.md)

| Tool | Verdict | One line |
|---|---|---|
| ntfy | adopt | harness-neutral phone gate; stub text; tokens; no action buttons for release |
| a2a-tck, a2a-inspector | adopt (test) | S1 gate and debugging |
| smugglex, mcp-scanner (static) | adopt (test) | S3 gate |
| ccusage | borrow / optional | historical usage per 5-hour block from local transcripts |
| cmux | borrow ideas | notification rings, jump to unread, trusted-only restore; GPL and BUSL, macOS only; its socket can send input (don't copy) |
| agent-deck | borrow ideas | hook-reported status, usage windows, global conversation search, worktree fork hooks; its "conductor" is master/subordinate |
| ccmanager | borrow one idea | copy session files into a new worktree so the conversation continues |
| Gitea issue dependencies | adopt (read-only) | extend the gitea plugin with read-only dependency tools and a ready query; a source, not the coordination backlog |
| MCP Inspector | dev only | ≥ 0.14.1, loopback only |
| Crystal/Nimbalyst, Sculptor, Conductor, Zellij plugins | skip | GUI or macOS-only, or a different multiplexer |
| agy-hud | skip | reads agy's OAuth token (terms risk) |
| Claude-Code-Usage-Monitor | skip | superseded by native `rate_limits` |
| Taskmaster AI | skip | Commons Clause |
| git-bug, Linear, Pushover | skip | no dependency edges, SaaS, proprietary |
| Snyk Agent Scan, Schemathesis | skip | data egress and executes servers; no JSON-RPC |

---

## 7. Problems found across the projects

All verified in source unless marked.

1. **Port 8120 is claimed twice.**
   - Coordination decision 5 uses it.
   - The cognitive-engine multi-track plan (Track 3 Slice 1, WS3) puts Harness² on `127.0.0.1:8120`, and `settings.py` on `feat/stage1-integration` hard-codes it, with an env override.
   - RFC 001 itself names no Harness² port; an earlier note said it did.
   - Decide before S1 and before PR #5 merges. Keep 8121 free if coordination keeps its proposed public listener. 8125 and 8130 are unused.
2. **ax can wipe `~/.gemini/config/hooks.json`.**
   - `ensure_hook_installed` runs on every attach and keeps other top-level keys.
   - But on an unreadable or invalid file it starts from `{}`, writes non-atomically and through symlinks, keeps no backup and re-sorts keys.
   - Coordination's agy hooks (decision 26) and possibly Jev's will live in this file.
   - Fix: refuse to touch invalid JSON, back up, write atomically, preserve order.
   - Spike restore checks must compare with `jq -S`, not bytes.
3. **Jev guard gaps.** See G5. On cognitive-engine `main` the PR #3 remediation is missing, and `hook.ts` only activates in mode `on` while RFC 002 says `on` and `on-demand`. The open PR #4 contains the `Workflow` matcher and `workflow-guard.ts`, and also fixes the `on-demand` activation and `plugin:agent` lookup (verified 2026-11-08). It does not fix `CLAUDE_CODE_SUBAGENT_MODEL` or the OpenCode adapter.
4. **The handle grammar has no recorded decision.**
   - The SP2 spike adopted `claude-code@<host>/<name>#<sid8>`.
   - `server-api.md` (line 472) takes a different position: `^[a-z0-9][a-z0-9._-]{0,47}$`, which forbids `#`, with the server appending `-` plus 4 hex on collision.
   - The 2026-11-07 spec fold-in did not record why the `#sid8` form was rejected. Either record that or adopt it.
5. **`server-api.md` section 11.2 shows the agy hooks.json shape wrong.**
   - It shows a top-level `"hooks"` key with Claude-style nesting.
   - agy uses named groups in flat form for `SessionStart`, `PreInvocation` and `Stop`.
   - Also, `implementation-plan.md:602`'s "no `Stop` entry" scan refers to the shipped `hooks.json`. Say explicitly that this means coordination's own group, because ax adds a `Stop` entry to the user-level file.
6. **Jev and memory exist twice.**
   - Same plugin and server names, the same 12 tool names, the same `~/.config/jev/` files.
   - Installing both gives two `PreToolUse` guards and a name clash.
   - RFC 005 Phase A (deprecation notices) has not started.
   - The two copies are drifting. Argon tiers landed in the hub (`ae8b851`), and Argon hardening is open in cognitive-engine PR #12.
   - RFC 005's target marketplace drops coordination and subagents.
7. **Jev hard-codes Recall on `:8110`** (`telemetry.ts:65`). RFC 003 moves memory to `:8105` and RFC 005 retires `:8110`, so Jev's recall read will fail silently after migration.
8. **Memory binds to all interfaces.** `plugins/memory/docker-compose.yml` publishes 8110 and 8100 with no host IP, so they bind 0.0.0.0 without auth. The README says 127.0.0.1.
9. **Port 8100 clash during migration** (unverified). Native lookup and the legacy lookup both want 8100; only memory got a staging port.
10. **cognitive-engine doesn't know coordination exists.** RFC 001 and 004 should record the status-client contract and "sbx sessions never reach the coordination server", before Track 3 settles its sandbox allow rules. `status-client.md` cites `harness2` files that do not exist on `main`.
11. **The cognitive-engine bridge** assigns fixed roles (Orchestrator, Builder) and is stale. PR #7 removes it.
12. **Stale documentation:**
    - the coordination README status, branch and `DatabaseTaskStore` lines
    - plan §7 still calls C9 unverified
    - the README missed celi5 and cplain, and AGENTS.md also missed mcp-toggle (coordination and subagents were added 2026-11-07)
    - `plugins/jev/opencode/` does not exist
    - the jev README says the hooks are at `hooks/hooks.json`; the real path is `./claude/hooks.json`
    - the jev version mismatch (2.3.0 against 2.0.0 in `package.json`)
    - ax CHANGELOG and tags
13. **Doc links in coordination point at absolute home-directory paths.** They resolve only on the workstation.

---

## 8. Recommended plan

Ordered by dependency. Nothing here needs to happen urgently.

### Now (documentation and small fixes; no new services)

1. **Decide the 8120 owner.** Move Harness² (it has an env override), or move coordination. Record it in both repos.
2. **ax: make the hooks.json writer safe.** Also fix the `"gemini"` alias, quote the worktree branch name, and fix Claude `--resume` parsing.
3. **coordination spec fixes:**
   - reconcile the handle grammar
   - fix `server-api.md` 11.2 to agy's named-group flat form
   - scope the `Stop` scan
   - refresh the README status
   - correct `bridge-v2-ecosystem-analysis.md`: line 71 (agent teams), and lines 59 to 61 and 87 to 96 (beads storage, the `mcp_agent_mail_rust` pairing)
4. **cognitive-engine:**
   - record the coordination contract and the sbx exclusion in RFC 001/004
   - land PR #4 (guard; it also fixes `on-demand` activation and `plugin:agent` lookup)
   - fix the OpenCode adapter (`subagent`, `event.input.prompt`)
   - make the guard read `CLAUDE_CODE_SUBAGENT_MODEL`
5. **agentic-hub housekeeping:**
   - list the missing skills in README and AGENTS.md
   - jev README path and version
   - remove the `plugins/jev/opencode/` references
   - bind memory to 127.0.0.1
   - start RFC 005 Phase A notices, or decide not to (section 9)

### Next (with coordination S1)

6. Add spikes **SP6** (inbox socket from an `async: true` hook) and **SP7** (plugin monitor) to `spikes.md`. Keep `asyncRewake` as the baseline.
7. Add `a2a-tck --level must` and peer-text sanitizing to the S1 exit gate.
8. Add **ntfy** on the storage host as the stub-only human gate for `INPUT_REQUIRED` and `QUARANTINED`.
9. **ax:**
   - "needs input" state from the Claude `Notification` hook
   - Claude `rate_limits` meters
   - a `CopilotDriver`
   - Codex history and restore
   - OpenCode title sync through the API

### Later (after S1 is running)

10. **S1b backlog** in the A2A server (G2 design). Before that, optionally run a two-week beads (stealth) or Kanzo (stdio) trial to learn whether you use a backlog at all.
11. **ax read-only coordination segment**: handle and pending count per pane.
12. **Third peer.** Codex (`codex queue` spike) if you hold a ChatGPT plan, or OpenCode (V2 plugin spike) on ChatGPT or Copilot. Copilot CLI is the third option.
13. **Move the cognitive-engine bridge onto coordination**, with symmetric roles.
14. **S3 gate additions**: smugglex and `mcp-scanner` static.

The live backlog for these items is [`backlog.md`](backlog.md).

---

## 9. Decisions only you can make

1. **Who owns port 8120:** coordination or Harness²?
2. **Backlog:**
   - build S1b in the A2A server only, or trial beads or Kanzo first?
   - which one to trial?
3. **Third peer:**
   - do you hold, or want, a ChatGPT plan (Codex, and OpenCode on ChatGPT) or a Copilot plan?
   - Without one, stay at two peers.
4. **`codex queue` stub wake:** an external process enqueues a constant stub into a Codex thread. It drives no TUI, but the audit called this pattern "external driver, out of scope". Allowed for Codex or not?
5. **DDR-003a:** extend never-drive to Google-account-authenticated Gemini CLI?
6. **RFC 005 scope:** accept that agentic-hub keeps only four plugins after the split? This drops coordination and subagents from the marketplace. If not, amend RFC 005 to keep them.
7. **cognitive-engine roles:** replace the Orchestrator and Builder split with symmetric peers when the bridge moves onto A2A?
8. **Network access for future research.** These hosts were blocked in this session, so a few points stay unverified:
   - `antigravity.google`
   - `opencode.ai`
   - `github.blog`
   - `docs.ntfy.sh`
   - `geminicli.com`
   - `discuss.ai.google.dev`

   Allow them if you want those points re-checked.

---

## 10. Corrections to earlier documents

To [`plugins/coordination/docs/research/todo-tools-evaluation-2026-11-07.md`](../plugins/coordination/docs/research/todo-tools-evaluation-2026-11-07.md), which this document supersedes:

- Kanzo has tagged releases (v0.4.1). It has no assignee model. HTTP `/mcp` is on by default, so "use stdio" also needs `mcp_http = false`. It has an unauthenticated self-update route.
- cognitive-engine RFC 001 does not name a Harness² port. Only the plan and the stage-1 code do.
- The PR #3 remediation exists in open PR #4. Tracks 2 and 3 exist as open PRs, not on `main`.
- ax installs only the agy `Stop` hook. Claude session ids do not bind through it.
- ax's title writes are in `src/core/harness.rs:474-530`, not `lib.rs:2393`.
- The port list missed 8121 (proposed S3 listener). 8101 appears only in an old note.
- OpenRig's messaging is verified, not just implied, to type into panes. It also binds the tailnet by default without a token.
- OpenUI has more packages (a2ui, react-email, assistant-ui, langchain) and default-on install telemetry.

To other coordination research:

- `bridge-v2-ecosystem-analysis.md` line 71: "agy can bridge into [Claude agent teams]" is contradicted by the official docs.
- `bridge-v2-ecosystem-analysis.md` lines 59 to 61 and 87 to 96:
  - beads is Dolt-backed, not SQLite and JSONL
  - the suggested pairing with `mcp_agent_mail_rust` is excluded by DDR-001

---

## 11. Method and evidence

**How the review ran.** Eighteen research agents ran, mostly in parallel through workflows:

- Two early surveys of ax and cognitive-engine fed the first note and are not kept separately.
- Fifteen produced the kept evidence reports.
- A final agent fact-checked this document against them.

Each answered one question:

- source deep-reads of Kanzo, OpenRig, OpenUI, beads and vibe-kanban
- official docs, changelog and source for each harness
- an adversarial re-check of local claims
- a roadmap snapshot of the three repos
- an open sweep for other tools

External repositories were cloned shallow into a scratch area and never built or executed. One agent attached `openai/codex` to the session and cloned it to `openai/codex` for reading.

**Evidence index.** See [`research/2026-11-07-adoption-review/README.md`](research/2026-11-07-adoption-review/README.md). Those files are the agents' reports, kept close to verbatim. They cite file and line in the external repos at the commits named in each file. Paths under `/tmp/...` refer to scratch clones that no longer exist.

**Limits.**

- Several vendor sites were unreachable (section 9, item 8). Claims that rely on them are marked reported or unverified.
- GitHub API access was limited to the three attached repositories, so star counts and some release pages were not checked.
- No tool was run, so behavioural claims (wake latency, races) remain to be measured in spikes.

**Re-verification 2026-11-08.** An independent Opus pass re-checked the locally verifiable claims in section 7 and the documentation-drift claims against source (ax at `eabe619`). Every high-stakes problem held. It corrected the ax tags, the mcp-toggle scope, the PR #4 coverage, the handle-grammar framing, the Gitea-dependencies label, the agent count and the `Stop` scan wording, all applied above.
