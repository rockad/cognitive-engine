
# beads (`bd`) evaluation for coordination, cognitive-engine and ax (2026-11-07)

**Source.** I made a shallow clone of `https://github.com/steveyegge/beads` at HEAD `6dc95bc7d1335771e763c29d60934d58bc920c78` (2026-11-07 21:28 UTC). It is in `/tmp/claude-0/-home-user/84e5c850-1d48-5bf0-99bf-2742d6707467/scratchpad/src/beads-1794088954/repo`. I installed, ran and built nothing. The earlier R2 note read HEAD `dbe6699` (2026-11-03), docs only.

**Labels.**
- **verified**: I read it in the source. Paths below are relative to the clone root and cite file:line.
- **reported**: README or docs only.
- **unverified**: not checked.

## 0. Where the canonical repo lives

- `git ls-remote` gives the same HEAD SHA `6dc95bc…` for both `github.com/steveyegge/beads` and `github.com/gastownhall/beads` (verified, run today).
- The README badges and the install script point at `gastownhall/beads` (verified, `README.md:7,9,31`). So does the beads-mcp project metadata (verified, `integrations/beads-mcp/pyproject.toml:33-36`).
- The Go module path is still `github.com/steveyegge/beads` (verified, `go.mod:1`).
- In-code comments credit `gastownhall/beads#4682` (verified, `internal/storage/issueops/lease.go` around line 80).
- Conclusion: `gastownhall/beads` is the canonical home, and `steveyegge/beads` serves the same repo, probably as a transfer redirect. The redirect itself is unverified, because `gh api` was blocked in this session.
- `bd doctor` still queries `api.github.com/repos/steveyegge/beads/releases/latest` (verified, `cmd/bd/doctor/version.go:230`).

## 1. Data model

**Statuses** (verified, `internal/types/types.go:598-604`):
- `open`
- `in_progress`
- `blocked`
- `deferred`
- `closed`
- `pinned`
- `hooked` ("work actively claimed by a worker")
- Custom statuses with categories also exist. Category `active` counts as ready (verified, `internal/storage/sqlbuild/ready.go:166-181`).

**Issue types** (verified, `types.go:799-817`):
- `bug`, `feature`, `task`, `epic`, `chore`
- `decision`, `message`
- `molecule`, `gate`, `event`
- `spike`, `story`, `milestone`

**Dependency types.** There are 19 built-in types (verified, `types.go:1463-1507`), and any string up to 32 characters is accepted (verified, `types.go:1518-1521`).
- Only 4 types affect ready work: `blocks`, `parent-child`, `conditional-blocks` ("B runs only if A fails") and `waits-for` (verified, `types.go:1549-1559`).
- `waits-for` carries gate metadata, `all-children` or `any-children` (verified, `types.go:~1568-1575`).
- The other types do not block. Of note:
  - `discovered-from`
  - `supersedes`
  - `duplicates`
  - `replies-to`
  - `assigned-to`
  - `approved-by`
  - `delegated-from` ("completion cascades up")
  - `until`, `caused-by`, `validates`, `tracks`

**Ready-work query** (verified, `sqlbuild/ready.go:207+` and `issueops/ready_work.go:108`). One SQL WHERE clause, shared by the CLI and the server, requires:
- status `open` or a custom status in the active category
- `is_blocked = 0`, a projected flag that is ignored when stale (`ready.go:87-127`)
- not pinned, not ephemeral
- `defer_until` null or in the past
- the parent is not deferred
- the type is not `merge-request`, `gate`, `molecule`, `rig` or an infra type (`ready.go:16-22`)

Default sort is priority. The other policies are `hybrid` and `oldest` (verified, `cmd/bd/ready.go:844`). `--explain` shows why an issue is ready or blocked (verified, `ready.go:861`).

**Claim atomicity** (verified, `internal/storage/issueops/claim.go`):
- `ClaimIssueInTx` is a conditional `UPDATE … SET assignee=?, status='in_progress' … WHERE id=? AND row_lock=? AND status IN (claimable)` (lines 105-139).
- Repeating your own claim is idempotent (line 159). Losing a race gives `ErrAlreadyClaimed` naming the holder (lines 174-198).
- `ClaimReadyIssueInTx` computes readiness and claims in the same transaction. It skips rows a racing agent already took (lines 266-319). The CLI form is `bd ready --claim`; the MCP tool is `claim`.
- Pool pseudo-assignees are supported, for example a crew alias (lines 84-103, 321-349).

**Leases** (verified, `issueops/lease.go`):
- `DefaultLeaseTTL = 5 * time.Minute` (line 28). The CLI has `bd heartbeat <id>` (`cmd/bd/heartbeat.go:14`), `bd unclaim` and `bd reclaim`.
- `ReclaimExpiredLeasesInTx` reverts a stale claim to ready (line 937). A replica guard keeps one machine from reclaiming another machine's leases (lines 864+, 714+).
- `row_lock` is a random value rewritten by every status or ownership write. It forces a conflict where Dolt's cell-by-cell merge would otherwise silently merge a reclaim with a close (lines 40-95).

## 2. Storage, concurrency, daemons, footprint

**Storage.** Dolt is the only supported backend.
- PostgreSQL and MySQL adapters were rolled back. SQLite remains only as a legacy migration bridge (verified, `cmd/bd/init.go` `--backend` flag help; `PROPOSAL-pluggable-storage-backends.md:3-17`; reported, `docs/getting-started/upgrading.md:681-759`).
- **Embedded mode is the default.** Dolt runs in-process and the data lives in `.beads/embeddeddolt/`. The docs call it "single writer, file-locked" (reported, `docs/architecture/dolt.md:104-128`).
- The code is softer than the docs. Each call opens a short-lived connection "to reduce contention when multiple processes access the same database", and "the driver handles its own concurrency internally" (verified, `internal/storage/embeddeddolt/store.go:42-48`).
- `bd serve` refuses embedded mode permanently: "Its commit protocol runs outside the SQL transaction on a separate connection, so the per-request atomicity … would be a lie there" (verified, `cmd/bd/serve.go:578-600`). Treat embedded-mode claims as not strictly atomic across processes. This is my reading of that comment and is unverified by test.
- A cooperative workspace gate takes a shared flock for normal commands and an exclusive one for maintenance. The gate file is `<dir>/.beads.gate.lock` (verified, `internal/workspacegate/gate.go:1-36`).
- **Server mode** (`--server`) uses an external `dolt sql-server` on 127.0.0.1:3307 or a socket. **Proxied-server mode** starts a server on demand. Both need the `dolt` CLI at 2.6.0 or newer (reported, `dolt.md:18-110`; flags verified in `init.go`).
- **JSONL** (`.beads/issues.jsonl`) is an export only. It is upsert-only on import, not a sync channel. Auto-export is off by default (reported, `docs/core-concepts/sync-concepts.md:28-40`; the prompt default is verified in `init.go:2360-2380`).
- **Git.** Dolt history is pushed to `refs/dolt/data` on the same git `origin`. `bd init` auto-wires `origin` as the Dolt remote unless `--stealth` or `dolt.local-only` is set (reported, `sync-concepts.md:18-26`; verified, `init.go:1136, 1538`).
- Config defaults (reported, `docs/reference/configuration.md:115-119`):
  - `dolt.auto-commit=on`
  - `dolt.auto-push=false`
  - `dolt.local-only=false`

**N agents across worktrees.** All worktrees share one `.beads` through the shared git directory, or `BEADS_DIR` can point at an external workspace (reported, `docs/reference/worktrees.md:13-93`). So it is the same database and the same writer rules. For sustained concurrent agents the docs say to use server or proxied-server mode (reported, `dolt.md:~128`).

**N agents across machines** (reported, `worktrees.md:148-153`):
- Each machine has its own Dolt copy. You `bd dolt pull` and `bd dolt push`, and the docs say to "coordinate through the tracker to avoid working the same issue".
- So a claim is atomic only within one database. Across machines, a double claim shows up later as a merge conflict through `row_lock` (my reading of `lease.go`).
- A shared server, `bd serve`, or federation and bucket federation are the alternatives (reported, `docs/multi-agent/*.md`).

**Footprint.**
- One Go binary (Go 1.27.3, verified, `go.mod:3`). Embedded mode links the Dolt engine.
- Server and proxied modes add the `dolt` CLI at 2.6.0 or newer.
- Each embedded `bd` call starts the engine (reported).
- Binary size, RAM and per-call latency: unverified.
- Telemetry is opt-in only, through `BD_OTEL_ENABLED` (verified, `internal/telemetry/telemetry.go:3-5`).

## 3. MCP server and HTTP API

**beads-mcp 1.3.4** is Python, built on `fastmcp==4.1.2`, MIT (verified, `integrations/beads-mcp/pyproject.toml:1-11`).
- Stdio only (verified, `server.py:1431`). There is no auth, which is inherent to stdio.
- It shells out to the `bd` CLI for every call (verified, `bd_client.py:322-347`).
- The actor is `BEADS_ACTOR`, defaulting to `$USER`. It is a provenance label, not an identity (verified, `config.py:37,166`).
- Tools (verified, `server.py:364-1386`):
  - `discover_tools`
  - `get_tool_info`
  - `context`
  - `ready`
  - `list`
  - `show`
  - `create`
  - `claim`
  - `update`
  - `close`
  - `reopen`
  - `dep`
  - `comment`
  - `comments`
  - `note`
  - `stats`
  - `blocked`
  - `admin`
- `admin` has validate, repair, schema, debug, migration and `pollution` (`clean=True` deletes issues). `debug` returns HOME, USER and the working directory.
- Its own README says that where a shell exists, the CLI plus hooks is recommended: about 1-2k tokens against 10-50k for the MCP schemas (reported, `integrations/beads-mcp/README.md:6`).

**`bd serve`** is an HTTP API described by OpenAPI under `/v0` (verified, `cmd/bd/serve.go:60-150`).
- It binds 127.0.0.1 by default. Auth is off on loopback.
- `--auth-token-file` accepts bearer tokens, one per line, re-read every second. They are stored as SHA-256 digests and compared in constant time. There is **no identity and there are no scopes** (verified, `internal/httpapi/auth.go:16-60`).
- `--allow-non-loopback` requires a token file. `--insecure-no-auth` exists.
- No TLS. Hooks do not fire. The actor is caller-asserted.
- `POST /v0/beads/issues:sweep` deletes in bulk.
- Routes include (verified, grep of `internal/httpapi`):
  - `/v0/beads/ready`
  - `/v0/beads/ready:count`
  - `/issues/{id}:claim`
  - `:heartbeat`
  - `:release`
  - `/issues:reclaim`
  - `/events:watch`
- `/events:watch` is SSE over a journal cursor. `Last-Event-ID` equals the `since` cursor, and the server keeps no per-consumer state (verified, `internal/httpapi/events_watch.go:1-14`).

**Statusline use.**
- The CLI has no dedicated count command for ready work.
- `bd ready --json` returns the list, and the counted path computes a total (verified, `cmd/bd/ready.go:186-210`). The exact JSON envelope shape is unverified.
- A global `--readonly` flag exists (verified, `cmd/bd/main.go:1228`).
- A practical segment is `bd --readonly ready --json -n 0 | jq length`, cached. This is unverified, and each call pays the embedded engine start.
- In server mode, `GET /v0/beads/ready:count` is cheaper.

## 4. What `bd init` and `bd setup` write, and conflicts with decisions 27 and 16

**`bd init` with no flags** (verified, `cmd/bd/init.go`):
- Creates or updates `AGENTS.md`, or the file named by `--agents-file`, with a `BEGIN BEADS INTEGRATION` section (lines 2393-2427).
- `updateAgentFile` uses plain `os.ReadFile`/`os.WriteFile` with **no symlink check**, so it **writes through symlinks** (verified, `cmd/bd/init_agent.go:37-119`; grep finds no `Lstat`). By contrast, `bd setup`'s agents writer does refuse symlinks (verified, `cmd/bd/setup/agents.go:125-137`).
- Installs the Claude project integration: `.claude/settings.json` gets a `SessionStart` hook running `bd prime --hook-json`, plus a `CLAUDE.md` section. It also installs Codex (`.agents/skills/beads/`, `.codex/`) and Cursor (`.cursor/`) project integrations (lines 2428-2449; `setup/claude.go:440-470, 489-567`).
- Installs git hooks unless `--skip-hooks`: pre-commit, post-merge, pre-push and post-checkout shims. It refuses a symlinked hooks directory (reported, `docs/reference/git-integration.md:38-75`).
- **Makes a git commit by itself.** It stages `.beads/`, the agents file, `.claude/settings.json`, `CLAUDE.md`, the Codex and Cursor files and `.gitignore`, then runs `git -c core.hooksPath= commit --no-verify -m "bd init: initialize beads issue tracking"` (verified, `init.go:4471-4520`). It skips this under `--stealth` (line 2454).
- Wires the Dolt remote to git `origin` (lines 1136, 1538).

**Escapes.**
- `--skip-agents` skips the `AGENTS.md` file and the Claude, Codex and Cursor setup. It does not skip the hooks or the auto-commit.
- `--stealth` (verified, `init.go:1288-1294, 2115-2121, 2393-2454`; `init_stealth.go:52-62`):
  - adds `.beads/` and `.claude/settings.local.json` to `.git/info/exclude`
  - sets `no-git-ops: true`
  - skips git hooks
  - skips the agents files and the Claude, Codex and Cursor setup
  - skips the auto-commit and the origin remote wiring
- `--skip-hooks`.
- `dolt.local-only=true` keeps Dolt data off the network.
- `BEADS_DIR` points at an external workspace.

**`bd setup <tool>`** (verified):
- Flags are `--global`, `--project` (gemini and mux only) and `--stealth` (`cmd/bd/setup.go:511-513`).
- `bd setup claude --global` writes `~/.claude/settings.json` and `~/.claude/CLAUDE.md` (`setup/claude.go:181-189`).
- **`bd setup gemini` defaults to global.** It writes `~/.gemini/settings.json` (a `SessionStart` hook running `bd prime --hook-json`) and `~/.gemini/GEMINI.md` (`setup/gemini.go:76-98, 207-235`).
  - These are Gemini CLI paths, not agy's `~/.gemini/config/`. Whether agy reads them is unverified.
  - There is no first-party agy or Antigravity integration. The only mention is the community `beads-pm-kit` (verified, grep; `docs/community-tools.md:106`).

**Conflicts.**
- **Decision 27 (never write `AGENTS.md`, never write through a symlink).**
  - Plain `bd init` writes `AGENTS.md` through symlinks and commits it.
  - In agentic-hub, `CLAUDE.md` imports `AGENTS.md`, and on the workstation `~/.gemini/config/AGENTS.md` is a symlink into a tracked file in another repository.
  - Only `--stealth` or `--skip-agents` avoids this.
- **Decision 16 (harness config only through the user-run installer and chezmoi).**
  - Every `bd setup` command, and the default `bd init`, writes Claude, Codex, Cursor or Gemini config directly, including chezmoi-managed `~/.claude/settings.json`.
  - Never run `bd setup`.
- **"Do not commit unless asked."** Plain `bd init` auto-commits with `--no-verify`.
- A compliant invocation (derived, not run):
  ```
  BEADS_DIR=<dir outside repos> bd init --stealth --skip-agents --skip-hooks --non-interactive
  bd config set dolt.local-only true
  ```

## 5. License

- MIT, "Copyright (c) 2025 Beads Contributors", 21 lines, standard text (verified, `LICENSE:1-21`).
- beads-mcp is also MIT (verified, `pyproject.toml:7`).
- I found no rider restricting AI vendors (verified, grep of `LICENSE` and `THIRD_PARTY_LICENSES` for anthropic, openai, google and vendor). DDR-001 passes.
- DDR-003 passes: nothing launches, drives or wraps agy (verified, grep).

## 6. Comparison

| | beads | Kanzo (prior local research, reported) | coordination A2A task store (planned) | vibe-kanban |
|---|---|---|---|---|
| Purpose | Backlog plus dependency graph plus claims | Kanban backlog | Messages and requests between live sessions | Worktree and task-card runner |
| Store | Dolt only. Embedded (single writer) or server | SQLite, one Rust binary | SQLite owned by one server process | not checked |
| Dependencies | 19 types, 4 blocking; `waits-for` all/any gates | `waits_on`; only `done` unblocks | none | not checked |
| Ready query | yes, in SQL, with explain, sort, parent and label filters | `next_task` | none (the known gap) | not checked |
| Claims | atomic CAS plus 5-minute lease, heartbeat, reclaim, replica guard | none | task states only, no claim | not checked |
| Wake | `events:watch` SSE cursor (`bd serve`) | none | `asyncRewake`, `/wake/listen` | not checked |
| Auth | MCP: stdio, none. HTTP: optional shared-secret token, no scopes, no TLS | HTTP `/mcp` has none | scoped API keys on every request, loopback included | not checked |
| Writes to host | `AGENTS.md`, `.claude`, `.codex`, `.cursor`, git hooks, auto-commit, `~/.gemini` (unless escaped) | none described | only the user-run installer | not checked |
| License | MIT | AGPL-3.0 | ours | not checked |
| Cross-machine | Dolt push/pull merge; claims atomic only within one database | git sync, "which board wins" | tailnet server on the storage host | not checked |

**Note on vibe-kanban.** I did not evaluate vibe-kanban in this step. The only local description is `bridge-v2-ecosystem-analysis.md:65-67` (reported): it starts a git worktree and a tmux pane for each task card and dispatches each card to Claude Code or Gemini/Antigravity CLI. Dispatching agy would conflict with DDR-003. It needs its own source check.

## 7. What changed against the earlier local research

**Corrections and additions to R2, verified in source today:**
- The canonical repo is gastownhall, served at both URLs with an identical SHA.
- The embedded writer is "driver-managed, short connections" in the code, and `bd serve` calls embedded atomicity unsound.
- Leases, heartbeat and reclaim exist, along with `row_lock` and the replica guard.
- `bd serve` provides the HTTP API, token-file auth, `ready:count` and SSE events.
- The `AGENTS.md` writer in `bd init` follows symlinks.
- Plain `bd init` auto-commits with `--no-verify`.
- `--stealth` also skips git hooks, the agents files and the auto-commit.
- `bd setup gemini` is global by default.
- The SQLite, PostgreSQL and MySQL backends are gone.

**`bridge-v2-ecosystem-analysis.md:59-61,87-96` is stale (verified against today's source):**
- It describes the storage as "SQLite + Git-versioned JSONL". The store is now Dolt, and JSONL is only an export.
- It proposes integrating with `mcp_agent_mail_rust`, which is excluded under DDR-001.

**`a2a-deep-dive-verification…:29` still holds:** A2A provides no ready queue.

## ADOPT / BORROW / SKIP

### ADOPT

**coordination: do not adopt beads as a dependency.**
- It would add a second source of truth beside the A2A task store.
- It adds a Dolt toolchain.
- Its HTTP auth (a shared secret with no scopes and no TLS) does not meet the scoped-API-key invariant.

**Optional: a personal trial, if the user wants a backlog before coordination grows one. The user installs `bd`.**
- Use the CLI only. No beads-mcp and no `bd setup`.
- Use an external workspace: `BEADS_DIR=~/beads/cognitive-engine/.beads`, initialised with `--stealth --skip-agents --skip-hooks --non-interactive`, and set `dolt.local-only=true`.
- Use embedded mode on the workstation with one writer at a time.
- Scope: cognitive-engine Tracks 2-4 only.
- Agents use `bd ready --claim --json`, `bd heartbeat` and `bd close`.
- Success test: whether agents call `bd ready` without being told.
- I rank this above a Kanzo trial for multi-agent work (claims, leases, MIT). Kanzo stays simpler and lighter for a human board.

### BORROW

**coordination**, in [`specs/server-api.md`](https://github.com/rockad/agentic-hub/blob/main/plugins/coordination/docs/specs/server-api.md):

- **§1.3 Tables.** Add a `backlog_item` table: `id`, `title`, `body` (untrusted), `status ∈ {open, in_progress, deferred, closed}`, `assignee_handle`, `lease_expires_at`, `row_version`, `defer_until`, `priority`. Add a `backlog_dep` table: `from`, `to`, `type ∈ {blocks, parent-child, waits-for(all|any)}`, plus non-blocking `discovered-from` and `supersedes`.
  - Ready = open, not deferred, and no unclosed blocking edge. This is the logic in `sqlbuild/ready.go:166-260`, reduced to one SQLite view.
- **§4 MCP tools.** Add `a2a_ready` (read), `a2a_claim` and `a2a_release` (write), with claims in one `BEGIN IMMEDIATE` transaction. Port `ClaimReadyIssueInTx` (`claim.go:266-319`): pick and claim in the same transaction, and skip lost races.
  - "Parked, never auto-executed" still holds. A claim is an explicit act by the session, and item bodies are framed as data (§4.1).
- **§3.5 `/sessions` and liveness.** Use lease TTL plus heartbeat plus reclaim (`lease.go:28, 937`). The heartbeat can be the existing session registration or listener liveness, so there is no new heartbeat sidecar. Port the replica guard as "only the owning host's server reclaims". With a single server on the storage host this is trivial.
- **§3.4 `/wake/listen` and events.** Use the cursor-is-the-contract SSE design (`events_watch.go:1-14`): a journal sequence where `Last-Event-ID` equals `since` and the server keeps no per-consumer state. Wake text stays a constant stub.
- **Installer (decision 27).** Copy beads' own `setup/agents.go:125-137` symlink refusal pattern, and its negative example, `init_agent.go`, into the installer test plan.
- **Plan placement.** Put this in [`implementation-plan.md`](https://github.com/rockad/agentic-hub/blob/main/plugins/coordination/docs/plans/implementation-plan.md) as a post-S1 "S1b backlog" item. A new decision row is needed, because it extends the store's scope.

**cognitive-engine:**

- `packages/jev/src/core/graph.ts:3-37` maps `SubTask` to an issue as follows:
  - `taskId` becomes the id
  - each entry in `dependencies[]` becomes a `blocks` edge
  - `requiresUserApproval` becomes a gate or human label, used as a display marker only, never as approval
  - `stages` (Kahn's waves) equals what the "ready" query returns at each step
- Use an optional exporter (`BatchDispatchPlan` to a JSONL or `bd batch` file), off by default, with no network client in core.
- In `docs/plans/2026-10-26-multi-track-implementation-plan.md`:
  - §4 (WS1-WS9) becomes epics with `parent-child` edges.
  - §2's dependency graph supplies the `blocks` edges.
  - Use `bd ready --parent <WS>` as the per-harness queue.
  - This replaces the task portion of the §6 bridge doc and `~/.agents/cognitive-engine-tasklist.md`.

**ax:**

- Add an `ax ready-count [path]` subcommand mirroring `ax git-status` (`src/cli.rs:170`, `src/lib.rs:2471-2526`). It prints a tmux segment such as `#[fg=…]●N`. Source order:
  1. coordination `/status` or a ready endpoint, once it exists
  2. `bd --readonly ready --json -n 0 | jq length` when `.beads` or `BEADS_DIR` is present
- Cache it for 30-60 s to amortise the embedded engine start and stay off the writer's lock. This is optional and does not touch agy, so DDR-003 is safe.

### SKIP

- **beads-mcp.** It is stdio-only, shells out to the CLI for every call, costs 10-50k tokens by its own README, and its `admin` tool can delete issues and dump environment details.
- **`bd setup` (any recipe), and plain `bd init`.** They break decisions 27 and 16, auto-commit with `--no-verify`, and write through symlinks.
- **`bd serve` beyond loopback.** Its tokens are shared secrets with no scopes, no identity and no TLS, the actor is caller-asserted, and the sweep endpoint is destructive. That is weaker than coordination's API keys.
- **Dolt remote on git `origin`.** It pushes `refs/dolt/data` into code repositories.
- **Molecules, formulas, federation, bucket federation, merge slots and Gas Town.** These are scope creep. BuildSlot already covers resource contention.
- **The `mcp_agent_mail_rust` pairing** suggested in `bridge-v2-ecosystem-analysis.md`. It is excluded under DDR-001.
