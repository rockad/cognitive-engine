
# vibe-kanban evaluation (BloopAI/vibe-kanban)

**Verdict:** skip it as a tool, but borrow its ideas. The README says the project is shutting down. It runs every agent as a one-shot headless subprocess (`claude -p` with stream-json). It writes `CLAUDE.md`/`AGENTS.md` and `~/.claude.json`. Telemetry is on by default. Its "blocking" dependency tracking works only on the cloud or self-hosted Postgres tier. It has no Antigravity (agy) support.

Source: a `--depth 1` clone at `/tmp/claude-0/-home-user/84e5c850-1d48-5bf0-99bf-2742d6707467/scratchpad/src/vk-1794088945/vibe-kanban`. HEAD is `08064e04`, dated 2026-11-03, version 0.1.45. No code was run. All paths below are relative to that root.

Labels:
- **V** = verified, read in the source at file:line.
- **R** = reported in the README or docs only.
- **U** = unverified.

## 1. Maturity, license, recency

- **Sunsetting.** README.md:19-20 says "Vibe Kanban is sunsetting." and links vibekanban.com/blog/shutdown. I did not read the announcement, so its timeline is U. (V for the banner)
- **Still committing as of 2026-11-03.** The head commit is "chore: update Cargo.lock versions for relay-tunnel and remote crates (#3490)". That is about 3,490 PRs, so the history is large. (V)
- **License:** Apache-2.0 (LICENSE:1-3). (V)
- **GitHub API:** the session has no GitHub API access to this repo, so stars and the archived flag are U.
- **Bottom line:** it is mature but end-of-life. Do not take a runtime dependency on it.

## 2. Architecture

- **Backend:** a Rust Cargo workspace of 33 crates (`crates/`). It includes:
  - `server`, `executors`, `worktree-manager`, `workspace-manager`, `git`, `mcp`, `review`
  - the `relay-*` crates, `remote`, `tauri-app`, `embedded-ssh`, `preview-proxy`
  - (V, `ls crates`)
- **Local database:** SQLite through sqlx (crates/db/Cargo.toml:14), with 76 migrations. (V)
- **Remote database:** the `remote` crate (cloud or self-host tier) uses Postgres (crates/remote/Cargo.toml:33). (V)
- **Web UI:** a React/TS frontend under `packages/web-core`, plus a Tauri desktop app. It is distributed with `npx vibe-kanban` (README.md:48). (V/R)
- **Network bind:** defaults to `127.0.0.1`, overridable with `HOST` (crates/server/src/main.rs:78). (V)
- **Remote access:** there is a relay/tunnel mode (`VK_TUNNEL`, `VK_SHARED_RELAY_API_BASE`) and a hosted relay at relay.vibekanban.com. (R, docs)

**Data model**
- Local `Task` has these fields: id, project_id, title, description, status, parent_workspace_id (crates/db/src/models/task.rs:24-33). (V)
- `TaskStatus` values are Todo, InProgress, InReview, Done and Cancelled (task.rs:16-20). (V)
- **Workspace** = one task attempt. It has one branch per repo, a terminal, a dev server and many sessions. (V: models `workspace.rs`, `workspace_repo.rs`, `session.rs`, `execution_process.rs`)
- **Issues and dependencies are remote-only.** The relationship enum is `('blocking','related','has_duplicate')` in Postgres (crates/remote/migrations/20260112000000_remote-projects.sql:141-150). (V)
- **The "hide blocked" ready view is client-side only.** A blocker counts as resolved when it sits in the last visible column or any hidden status (packages/web-core/src/features/kanban/ui/KanbanContainer.tsx:271-286). (V)
- There is no server-side "ready work" query. I found no `ready` or `unblocked` in crates/remote/src (V, grep).

## 3. Task to git worktree lifecycle

- **Branch name** = `{prefix}/{short_uuid(workspace)}-{slug(task title)}` (crates/services/src/services/container.rs:785-795). The default prefix is `"vk"` (crates/services/src/services/config/versions/v8.rs:12-14). (V)
- **Worktree base directory** is `/var/tmp/vibe-kanban/worktrees` on Linux. /var/tmp is used to avoid tmpfs RAM use (crates/worktree-manager/src/worktree_manager.rs:521-522; crates/utils/src/path.rs:108-120). (V)
- **Worktree creation:**
  - `create_worktree` creates a branch from the base branch, then runs `ensure_worktree_exists` (worktree_manager.rs:59-83). (V)
  - It uses the git CLI `worktree add` so sparse-checkout still applies. On failure it cleans the `.git/worktrees` metadata and retries (worktree_manager.rs:303-357). (V)
  - It recreates the worktree if it is missing or broken (worktree_manager.rs:88-156). (V)
- **Multi-repo workspaces:** it writes workspace-level `CLAUDE.md` and `AGENTS.md` files containing `@<repo>/CLAUDE.md` import lines. It skips the file if it already exists (crates/local-deployment/src/container.rs:991-1040). (V)
- **Automatic cleanup:** a worktree is deleted 72 h after its last activity, or 1 h if the workspace is archived. Workspaces with a running process are excluded (crates/db/src/models/workspace.rs:~266-295; local-deployment/src/container.rs:276-316). (V)
- **After merge:** the workspace is auto-archived unless it is pinned (crates/server/src/routes/workspaces/git.rs:~249-252). (V)

## 4. How it launches and drives agents

The pattern is one subprocess per turn, headless, with structured stdio. There is no tmux. A PTY exists only for the user's own terminal, which runs `$SHELL` (crates/local-deployment/src/pty.rs:9,56-87). (V)

| Agent | Command (pinned versions) | Evidence |
|---|---|---|
| Claude Code | `npx -y @anthropic-ai/claude-code@2.1.119 -p --verbose --output-format=stream-json --input-format=stream-json --include-partial-messages --replay-user-messages` | claude.rs:61-67, 158-195 (V) |
| Claude (CCR) | `npx -y @musistudio/claude-code-router@1.0.66 code` | claude.rs:63 (V) |
| Gemini CLI | `@google/gemini-cli@0.29.3 --experimental-acp` | gemini.rs:49,60 (V) |
| Qwen | `--acp` | qwen.rs:45,54 (V) |
| Copilot | `@github/copilot@1.0.83 --acp` | copilot.rs:52,82 (V) |
| Codex | `@openai/codex@0.124.0 app-server` | codex.rs:447-453 (V) |
| OpenCode | `opencode-ai@1.4.7 serve --hostname 127.0.0.1 --port 0` | opencode.rs:92-95 (V) |
| Cursor | `-p --output-format=stream-json` | cursor.rs:152 (V) |
| Droid | `droid exec --output-format stream-json` | droid.rs:91 (V) |
| Amp | `@sourcegraph/amp@latest`, unpinned | amp.rs:37 (V) |
| **Antigravity / agy** | **Not supported.** No match in crates/executors; not in docs/agents/ | (V) |

All paths in this table are under `crates/executors/src/executors/`.

- **Follow-up turns** spawn a new process with `--resume <session_id>` and optionally `--resume-session-at` (claude.rs:360-375). (V)
- **Spawn settings:** `kill_on_drop`, piped stdio, process group (claude.rs:630-650). (V)
- **Claude approvals:**
  - Claude runs with `--permission-prompt-tool=stdio` plus `--permission-mode=bypassPermissions`. Otherwise it gets `--disallowedTools=AskUserQuestion` and optionally `--dangerously-skip-permissions` (claude.rs:162-178). (V)
  - Approvals go to the UI through a pending map with a timeout. A timeout returns `TimedOut` (crates/services/src/services/approvals.rs:21-26,92-125). (V)
  - The Noop approval service auto-approves (crates/executors/src/approvals.rs:57-81). (V)
- **Credential handling:** it reads `~/.claude.json` for auth detection (claude.rs:598-602). It can strip `ANTHROPIC_API_KEY` (claude.rs:644-647). (V)

## 5. Review and merge gates

- **Diff review:** inline comments are serialised to markdown as a `## Review Comments (N)` block with `**path** (Line n)`, the code line, and a `> comment` quote. That block is sent to the agent as a follow-up prompt (packages/web-core/src/shared/hooks/ReviewProvider.tsx:59-88). (V)
- **Direct merge is a squash merge into the local base branch.** It is blocked when:
  - the base branch is ahead of the task branch (`BranchesDiverged`, crates/git/src/lib.rs:587-596) (V)
  - a PR is open for the repo (git.rs:195-203) (V)
  - the target is a remote branch (git.rs:205-213) (V)
- **No required-approval state:** there is no test-pass or required-reviewer gate in code. The InReview status is a column, not an enforced gate. The merge is a human UI action. (V, no such checks in merge_workspace)
- **Squash bookkeeping:** after the squash, the task branch is reset to the squash commit (lib.rs:649-670). (V)
- **PR path:** it can create a GitHub PR with an AI-written description. A `pr_monitor` service tracks it (crates/services/src/services/pr_monitor.rs exists). (R/V)
- **`crates/review`:** a separate CLI that talks to `https://api.vibekanban.com` (crates/review/src/main.rs, `DEFAULT_API_URL`). This is cloud review. (V)

## 6. MCP server

- **Transport:** stdio only, binary `vibe-kanban-mcp --mode global|orchestrator`. It reaches the backend over HTTP using `VIBE_BACKEND_URL` or a port file (crates/mcp/src/bin/vibe_kanban_mcp.rs:2,34-42,60-114). (V)
- **Auth:** I found no Authorization header in crates/mcp/src/task_server/mod.rs. MCP calls go to the local HTTP API without a key. (V, absence by grep)
- **Global-mode tools** (tools/mod.rs:52-64) (V):
  - context
  - workspaces: list, update, delete
  - organizations, repos, remote projects
  - remote issues: create, list, get, update, delete, priorities
  - issue assignees and tags
  - issue relationships: create or delete `blocking`/`related`/`has_duplicate`
  - task_attempts: "Create a new workspace and start its first session", link a workspace to an issue
  - sessions: create, list, rename, `run_session_prompt` ("Run a coding agent turn ... return immediately"), get execution status
- **Orchestrator mode:** context, workspaces and sessions only, without list/delete (mod.rs:66-73). This lets an agent spawn and drive other agents. (V)
- **Default MCP config ships `npx -y vibe-kanban@latest --mcp`,** an unpinned `@latest` (crates/executors/default_mcp.json:2-8). (V)
- **Harness config writes:**
  - The UI's POST `/mcp-config` writes MCP servers into each agent's user-level config, for example `~/.claude.json` and `~/.config/amp/settings.json`, with JSONC that keeps comments (crates/server/src/routes/config.rs:48,327-365; crates/executors/src/mcp_config.rs:88-106; amp.rs:150). This happens on a user action, not automatically. (V)
- **AGENTS.md writes:** `CLAUDE.md` and `AGENTS.md` in workspace directories (section 3). (V)

## 7. Telemetry

- **PostHog:** `track_event` posts to the PostHog endpoint. The key and endpoint are baked in at build time, with an env fallback (crates/services/src/services/analytics.rs:18-33,54-90; crates/server/build.rs:10-21). (V)
- **On by default:** `analytics_enabled: true` for a fresh config, and `unwrap_or(true)` when migrating an older config (v8.rs:76,144). (V)
- **Login re-enables it:** an OAuth login forces `analytics_enabled = true` and saves it, even if the user turned it off (crates/server/src/routes/oauth.rs:350-354). (V)
- **Sentry:** initialised in the backend with a build-time DSN (crates/server/src/main.rs:39,49; crates/utils/src/sentry.rs:26-54). (V)
- **Other outbound endpoints:** relay.vibekanban.com and api.vibekanban.com (docs/responsible-disclosure.mdx:40; review/main.rs). (V/R)

## 8. Conflicts with coordination rules

- **One-shot transports are prohibited.** Claude runs as `-p` stream-json, Cursor as `-p`, Droid as `exec`. These are exactly the `claude -p` class. **Hard fail.** (V)
- **DDR-003 (never drive agy):** agy is not supported today. But the design is "the tool launches and drives the agent". Any agy executor added later, or the ACP path (Gemini's `--experimental-acp` is the agy lineage), would drive agy. Orchestrator-mode `run_session_prompt` gives an agent the power to drive others. **Fail by design.** (V)
- **Symmetric agency:** the UI or orchestrator is the master and agents are subordinate workers. **Fail.** (V)
- **Decision 27 (never write AGENTS.md):** it writes `CLAUDE.md`/`AGENTS.md`, though only inside its own workspace dirs under /var/tmp and only if absent. **Low direct risk, but it violates the rule.** (V)
- **Decision 16 (harness config is written only by the installer):** the UI writes `~/.claude.json` and other agent configs. In this setup those are chezmoi-managed. **Fail if used.** (V)
- **API key on every request:** local MCP to HTTP calls have no auth (V). Remote mode relies on OAuth or a relay (R). **Fail.**
- **Tasks parked, never auto-executed:** "Create a new workspace and start its first session" both creates and executes in one call. **Fail.** (V)
- **DDR-001 (restrictive terms):** it runs Claude Code headless with `--dangerously-skip-permissions` as an option. Under Anthropic's terms, Claude Code use is allowed. The concern is the "third-party wrapper of the Claude subscription" posture, which is U. Not an automatic DDR-001 exclusion. **Unverified.**

## 9. Comparison: vibe-kanban, Kanzo, beads, OpenRig, ax

Sources and labels:
- **Kanzo and OpenRig:** from the existing evaluation, todo-tools-evaluation-2026-11-07.md lines 69-166. Those labels carry over.
- **beads:** I did not clone it in this run, so every beads row is **R/U** from public descriptions (`bd` CLI, git-backed JSONL issues, `bd ready`). Verify it in a separate pass before you rely on it.
- **ax:** verified in the local ax checkout.

| Dimension | vibe-kanban | Kanzo | beads | OpenRig | ax |
|---|---|---|---|---|---|
| Primary role | Board + agent runner + worktrees + review/merge | Human/agent backlog board | Agent-first issue tracker | tmux seat/team orchestrator | tmux cockpit and launcher |
| Dependency-aware "ready" | Remote tier only, client-side filter (V) | Yes, "ready work" (per existing doc) | Yes, `bd ready` with `blocks`/`parent-child`/`discovered-from` (R/U) | No | No |
| Storage | SQLite local; Postgres remote (V) | SQLite `~/kanzo/kanzo.db` | JSONL in the repo, synced through git, plus a SQLite cache (R/U) | files + tmux | `~/.config/ax/ax.db` |
| Drives agents | Yes, `-p`/ACP/app-server subprocesses (V) | No | No; agents call the CLI (R/U) | Yes, types into panes | Launches agents in tmux windows; does not type into them (per existing doc) |
| Writes AGENTS.md / harness config | Yes (V) | No (per existing doc) | Reportedly can add an AGENTS.md section via an onboarding command (U) | Yes | One agy `Stop` hook + titles |
| Auth | None on local MCP (V) | None on HTTP MCP | N/A, local CLI | N/A | N/A |
| Telemetry | PostHog + Sentry, default on (V) | U | U | U | No |
| agy support | No (V) | N/A | Agent-agnostic CLI (R) | No | Yes (launch) |
| Status | Sunsetting (V) | Active (per existing doc) | U | U | Active (v0.8.4) |
| Fit with coordination | Fails 5 or more rules | Trial later, stdio only | Best "ready work" model to borrow; possible trial (U) | Rejected | Natural UI surface |

**ax worktree tabs:**
- The palette runs `git worktree add -b %% .worktrees/%% && cd .worktrees/%% && ax run` in a new tmux window (ax/src/ui/palette.rs:709-717). (V)
- There is no remove, prune, merge or expiry step. `rg "worktree remove|prune"` in ax/src finds nothing. (V)
- The branch name from the prompt is interpolated unquoted into a shell string. A name containing shell metacharacters would run as code. This is low risk because the input is user-typed, but worth quoting. (V)

## ADOPT

- **None.** Do not install or run vibe-kanban.
- Reasons:
  - It is sunsetting.
  - Its one-shot headless transports are prohibited.
  - It is a master/worker design.
  - It writes AGENTS.md and agent configs.
  - Telemetry is on by default, and an OAuth login re-enables it.
  - Its local MCP is unauthenticated.
  - The `@latest` npx MCP entry is a supply-chain risk.
  - Apache-2.0 would allow copying code, but there is nothing worth vendoring.

## BORROW

### ax

1. **Worktree lifecycle for worktree tabs** (palette.rs:709-717):
   - Add `ax wt close`. It should refuse while the tab's process is alive, then run `git worktree remove` and `git worktree prune`.
   - Add an optional expiry: 72 h idle, or 1 h after marked done, skipping pinned worktrees. Model: workspace.rs:~266-295 and container.rs:276-316.
   - Store `pinned`/`archived` flags on the tab row in `ax.db`.
2. **Self-healing worktree creation:** if `worktree add` fails, prune stale `.git/worktrees/<name>` metadata and retry once (worktree_manager.rs:303-357).
3. **Unique branch naming:** `{prefix}/{short-id}-{slug}` keeps branches unique per attempt (container.rs:785-795). Also shell-quote `%%` in ax's own command.
4. **Divergence and merge-readiness badge:** a read-only tab segment showing "base +N ahead / task +M". This mirrors `merge_changes`' ahead check (git/src/lib.rs:587-596). It reports only and never merges.
5. **Review-comments-to-markdown format:** reuse the `## Review Comments (N)` shape from ReviewProvider.tsx:59-88 as the structure for hand-written review notes the human pastes into a session. Pasting by hand is fine. ax must never type it into an agy pane (DDR-003).

### cognitive-engine

1. **Plan section 6.2 (Dedicated Worktree Protocol)** (2026-10-26-multi-track-implementation-plan.md:315-322) and **section 5.1:**
   - Formalise the worktree path and branch rule as `<repo>-<track>` and `<prefix>/<track>-<slug>`.
   - Add a teardown step: remove after merge unless pinned.
   - Add a merge precondition: refuse a squash or merge when the base is ahead of the task branch, so rebase first. This is lib.rs:587-596.
   - Add "no direct merge while a PR is open" (git.rs:195-203).
2. **Harness2 `mkdtemp` diff-only mode** (RFC 004:58, RFC 001 row 5):
   - Put the diff worktree under a persistent non-tmpfs root, like /var/tmp, keyed by `run_id`.
   - Add a TTL sweep modelled on the 72 h / 1 h rule.
   - Keep fail-closed and no-execute unchanged.
3. **BatchDispatchPlan DAG:** borrow the `blocking` / `related` / `has_duplicate` relation vocabulary. Also borrow the resolved-blocker rule: a blocker is resolved in a terminal-done status or a hidden/cancelled status (KanbanContainer.tsx:271-286). This makes DAG nodes exportable to a backlog later.

### coordination (task metadata)

1. **Optional task `metadata` keys:**
   - `repo`
   - `base_branch`
   - `branch`
   - `worktree_path`
   - `blocked_by: [task_id]`
   - `relation: blocking|related|duplicate`
   - Purpose: a `ready` filter, `SUBMITTED` with every `blocked_by` in `COMPLETED`/`CANCELED`. This closes the known "ready work" gap without a second store.
   - It is a server-side query, not a client filter as in vibe-kanban.
2. **Two-step create:** keep "create task" separate from "start work". This is the opposite of vibe-kanban's `start_workspace_session`, and it confirms "parked, never auto-executed".
3. **Merge status as data:** `branch_ahead`/`behind` and `pr_state` can be status fields reported by peers. They are never an approval path.

## SKIP

- **vibe-kanban as a tool or plugin:**
  - It is sunsetting.
  - It violates the no-one-shot-transport rule (claude.rs:158-195).
  - It violates symmetric agency (orchestrator MCP, tools/mod.rs:66-73).
  - It writes AGENTS.md (container.rs:991-1040) and harness configs (config.rs:327-365).
  - The local MCP is unauthenticated.
  - Telemetry is on by default and forced on at login (oauth.rs:350-354).
  - It has no agy support.
- **Its MCP server, even read-only:** it is unauthenticated, uses unpinned `npx @latest`, and exposes create-and-run tools.
- **Its relay/tunnel and cloud review:** these are third-party endpoints and would form a second control path.
