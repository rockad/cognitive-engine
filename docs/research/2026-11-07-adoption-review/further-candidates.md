# Third-party and native options beyond the current shortlist (research only, 2026-11-07)

Nothing was installed or run. I left out everything already evaluated: Kanzo, OpenUI, OpenRig, beads, vibe-kanban, the native harness features you listed, and the projects in plan §4 and §8 and the wake audit.

**Evidence labels:**
- **V** (verified): I read the official docs or the repo page this session.
- **R** (reported): from third-party write-ups or search summaries only.
- **U** (unverified): inferred.

**Access limits:**
- `gh api` was blocked for these repos, so licences and release dates come from the GitHub pages. Several pages did not show a latest-release date.
- `docs.ntfy.sh` and `www.antigravity.google` are outside this session's network allowlist. The agy quota-field names and the ntfy action-button details therefore stay unverified until those hosts are allowed.

## Top picks (short version)

| # | Item | Project | Verdict |
|---|---|---|---|
| 1 | Claude Code `rate_limits` statusline fields | ax statusline, Jev `jev_quota` | **ADOPT** |
| 2 | Claude Code `Notification` hook (`agent_needs_input`, `terminalSequence` OSC 777) | ax waiting-state ring | **ADOPT** |
| 3 | Remote Control "Push when actions required" | human gate for Claude sessions | **ADOPT** (Claude side only) |
| 4 | ntfy (self-hosted on the storage host, over tailnet) | harness-neutral phone gate for INPUT_REQUIRED and QUARANTINED | **ADOPT** |
| 5 | `a2a-tck` and `a2a-inspector` | S1 conformance and debugging | **ADOPT** (test-only) |
| 6 | Gitea issue dependencies API, or `gh` ≥ 2.94 `--blocked-by` | the missing dependency-aware backlog | **ADOPT/BORROW** |
| 7 | smugglex, plus Cisco `mcp-scanner` in YARA static mode | S3 hardening | **ADOPT** (test-only) |
| 8 | git-native leases (`git worktree lock --reason`) and commit trailers | coordination provenance, ax worktree lifecycle | **BORROW** |

---

## 1. Human gate notifications to the phone

**Claude Code Remote Control mobile push**: **ADOPT**
- Link: https://code.claude.com/docs/en/remote-control
- Native feature, works on the subscription.
- **V**:
  - Requires a Pro, Max, Ultra, Team or Enterprise plan. API keys are not supported.
  - In `/config` there are two toggles: **"Push when Claude decides"** and **"Push when actions required"**. The second one covers permission prompts and questions.
  - Pushes are skipped while you are focused on the terminal. `CLAUDE_CLIENT_PRESENCE_FILE` extends that to "at the machine": no push while the file exists.
  - `claude --remote-control` / `/rc` keeps the normal interactive TUI.
  - It is disabled when `ANTHROPIC_BASE_URL` points to another host.
- What it gives you: phone alerts for Claude sessions that are input-required, at zero cost.
- Rule check:
  - Claude only, so it does not satisfy symmetric agency on its own.
  - It is a control channel into the session. That is fine for the user's own Claude, but it must not count as quarantine release: that stays human-only on a TTY.
  - ax already shows an `@remote_control` glyph (`src/tmux/profile.rs`), so this fits existing work.

**ntfy** (binwiederhier/ntfy): **ADOPT** as the harness-neutral gate channel
- Link: https://github.com/binwiederhier/ntfy
- Licence: Apache-2.0 / GPLv2 dual (**V**). Android app (Play and F-Droid) and iOS app (**V**).
- Maintenance: active (**R**). The repo page showed no release date.
- Subscription fit: it is independent of any model vendor.
- What it gives you:
  - The A2A server POSTs to a self-hosted ntfy topic on the storage host (tailnet) when a task enters `INPUT_REQUIRED` or `QUARANTINED`.
  - The same feed works for Claude and agy, which Remote Control cannot do.
- Caveats:
  - iOS instant delivery from a self-hosted server needs the `upstream-base-url` relay through ntfy.sh, which uses APNs/Firebase (**R**). Only a poll id leaves the tailnet, not content, but keep notification bodies to the constant stub anyway.
  - Use access tokens and deny-all by default.
- Rule check:
  - Do **not** use ntfy action buttons to release quarantine. Release stays TTY-only.
  - Notification text = constant stub, mirroring the stub-only wake rule.
- `agent-bus` (already a watch item) can emit to ntfy. That is the same channel, so no extra dependency is needed.

**Pushover**: **SKIP**. Proprietary, paid per platform (**R**), and offers nothing over ntfy.

## 2. Cockpit peers to ax: ideas to borrow

**Native Claude `Notification` hook with `terminalSequence`**: **ADOPT in ax**
- Link: https://code.claude.com/docs/en/hooks
- **V**: matchers include `permission_prompt`, `idle_prompt`, `elicitation_dialog`, `agent_needs_input`, `agent_completed` and `quota_auto_resume_*`. The docs' own example returns `{terminalSequence: "\e]777;notify;title;body\a"}`.
- What it gives ax: a real "needs input" state, separate from the generic bell/activity flags that ax already styles (`window_bell_flag` in `profile.rs`).
  - Set a tmux window option such as `@needs_input` from the hook.
  - Add a "jump to newest waiting" key.
- agy side: there is no equivalent event. Use agy's `Stop` hook, which ax already writes (`ax-tab-sync`), only for title/state, never for input.

**cmux** (manaflow-ai/cmux): **BORROW ideas, SKIP the tool**
- Link: https://github.com/manaflow-ai/cmux
- Licence: GPL-3.0-or-later for the app; the server, relay and web parts are BUSL-1.1 (**V**). macOS only (Swift/libghostty) (**V**).
- Ideas worth borrowing (**V**):
  - Notification rings driven by OSC 9/99/777 plus a `cmux notify` CLI.
  - Cmd+Shift+U jumps to the newest unread.
  - A per-workspace sidebar showing branch, PR, ports and the latest notification text.
  - Restore that auto-resumes only *trusted* commands; everything else needs a manual restore. This maps well onto `ax restore`.
- Conflict: its socket API can "send input" and read the screen. That is the driving pattern DDR-003 forbids for agy. Do not copy that part.

**agent-deck** (asheshgoplani/agent-deck): **BORROW ideas, SKIP the tool**
- Link: https://github.com/asheshgoplani/agent-deck
- Licence MIT (**V**). Go/Bubble Tea on tmux, active (v0.21.0 in Mar 2026, **R**).
- Ideas worth borrowing (**V**):
  - Hook-reported status, with pane scraping only as a fallback.
  - `agent-deck usage` reads Claude's 5-hour and 7-day windows from the statusline feed.
  - Machine-wide conversation search (`G`).
  - Worktree fork with approval-gated setup and teardown hooks, plus copying of gitignored files.
- Conflicts:
  - It has `session send` and a "conductor" agent that answers child sessions. That is master/subordinate and driving, which violates symmetric agency and DDR-003.
  - No Antigravity support (**V**).

**ccmanager** (kbwo/ccmanager): **BORROW one idea**
- MIT (**R**, README v4.1.18). Worktree create/merge/delete in a TUI without tmux.
- The idea: when a worktree is created, copy the Claude session files so the conversation continues on the new branch. This complements ax's conversation registry.

**Crystal → Nimbalyst**: **SKIP**
- Crystal was deprecated in Feb 2026 (**R**). Nimbalyst is a GUI and macOS-centric.
- The one idea worth noting is a session kanban (**R**).

**Not pursued**: Sculptor, Conductor and Zellij agent plugins. They are GUI or macOS-only, or would mean changing multiplexer, and they add nothing beyond the ideas above (**U**).

## 3. Subscription quota and usage tracking

**Claude Code statusline `rate_limits`**: **ADOPT** (native, best source)
- Link: https://code.claude.com/docs/en/statusline
- **V**:
  - `rate_limits.five_hour.used_percentage` and `rate_limits.seven_day.used_percentage` run 0–100.
  - `resets_at` is Unix epoch seconds.
  - Present only for Pro, Max and Ultra subscribers, and only after the first API response.
  - Each window can be absent independently and is dropped after its reset. Read it with `jq '... // empty'`.
- What it gives you:
  - ax statusline meters directly from the source.
  - Jev `jev_quota`, whose README claims 5-hour and 7-day buckets, can take ground truth from here: have the statusline script tee the JSON to a small cache file instead of estimating.
- No rider and no token scraping.

**ccusage** (ryoppippi/ccusage): **BORROW**, optional ADOPT as a CLI
- MIT (**V**).
- `blocks` reconstructs 5-hour windows from transcripts, and `statusline` is in beta (**V**).
- It lists Antigravity, Gemini CLI, Codex, OpenCode and Copilot CLI sources (**V**, depth not checked).
- What it gives you: historical per-day and per-block cost for Jev reporting. It reads local transcripts only.
- Keep the native `rate_limits` field as the live gauge.

**agy quota: official statusline credits and `/usage` / `/quota`**: **BORROW**
- **R** via search: the agy CLI statusline shows "AI Credits: N" and has `/usage` and `/quota`. The docs page was unreachable from this session.
- A Google forum thread asks whether a third-party utility may display quota taken from the CLI statusline. I found no official answer (**R**).

**agy-hud** (icebear0828/agy-hud): **SKIP**
- MIT (**V**).
- It reads agy's stored OAuth token and calls quota HTTP endpoints, and it writes `statusLine.command` into `~/.gemini/antigravity-cli/settings.json` (**V**).
- Third-party use of agy credentials is the DDR-001/R8 terms risk. Only parse what agy itself hands to a user-configured statusline command.

**Claude-Code-Usage-Monitor and similar**: **SKIP** (**U**, not fetched). Superseded by the native `rate_limits`.

## 4. Backlog and issue options (the "no dependency-aware backlog" gap)

**Gitea issue dependencies**: **ADOPT** as the dependency-aware backlog
- **R**, from Gitea PR #17935 mirrors:
  - `GET/POST/DELETE /repos/{o}/{r}/issues/{i}/dependencies` lists or edits the issues that block this one.
  - `/blocks` covers the reverse direction.
  - Returns 404 when dependencies are disabled on the repo.
  - Writes need the repo token scope.
- Why it fits:
  - It is self-hosted on your existing infra.
  - Dependency edges come built in.
  - It keeps the backlog out of the A2A task store, which stays the work channel. An issue becomes a task only when a human or live session submits it.
- Gap: the current gitea plugin exposes `list_issues`, `issue_read` and others, with no dependency or write-issue tools (**V**, `skills/gitea/SKILL.md`).
- Next step: add read-only `issue_dependencies` / `issue_blocks` tools plus a "ready = open and no open blockers" query.
- Confirm the version on your instance against its `/api/swagger`.

**GitHub Issues via `gh` ≥ 2.94**: **ADOPT for GitHub-hosted repos**
- Link: https://github.blog/changelog/2026-06-10-manage-sub-issues-types-and-dependencies-from-github-cli/
- `gh issue` gained `--blocked-by`/`--blocking` and sub-issue flags on 2026-06-10 (**R**, official changelog title seen).
- REST `issues/{n}/dependencies/blocked_by` and `issue_dependencies_summary` (**R**).
- Limits: no cross-repo dependencies, and 50 per issue (**R**).

**Taskmaster AI** (eyaltoledano/claude-task-master): **SKIP**
- Licence is "MIT with Commons Clause" (**V**). That is a rider, and it is not permissive.

**git-bug**: **SKIP**
- GPLv3 (**R**). Offline git-native issues, but no dependency edges are known and it would mean another tracker.

**Linear**: **SKIP**. SaaS with API keys, against the self-hosted, subscription-first stance.

## 5. A2A ecosystem items for S1

**a2a-python**: no new release since the audit
- Latest is still **v1.3.0 (2026-10-27)**. Nothing after 2026-11-04 (**V**, releases page).
- Notes useful for S1 (**V**):
  - 1.3.0 adds a configurable timeout for releasing parked interrupted tasks. That is relevant to the "offered until confirmed" pattern; check it before writing your own.
  - 1.2.3 rejects push URLs in shared address space and isolates push failures. This partly covers the SSRF gap in audit §6.
  - 1.2.1 switches ListTasks to keyset cursors.

**a2a-tck**: **ADOPT** (test-only)
- Link: https://github.com/a2aproject/a2a-tck
- Apache-2.0 (**V**). No tagged releases visible.
- `./run_tck.py --sut-host URL --transport jsonrpc,http_json --level must|should|may` (**V**).
  - MUST = fail, SHOULD = xfail, MAY = skipped unless the capability is declared.
- What it gives you: an S1 exit gate. Run `--level must` in CI against the loopback server with a test API key.
- Check: the `QUARANTINED` extension must not break the MUST tests on state values. It surfaces as `INPUT_REQUIRED`, so it should pass (**U**).

**a2a-inspector**: **ADOPT** (test-only)
- Link: https://github.com/a2aproject/a2a-inspector
- Apache-2.0 (**V**). Rewritten in Go and targets v1.0 and v0.3 (**V**).
- Features (**V**): card validation, wire-level validated logs, tasks and push config, bearer/API-key presets.
- Run it bound to 127.0.0.1 (its default).
- Do not enable its optional Gemini "host agent": it calls a model with an API key, which is outside the subscription stance.

**Registries**: none found worth adopting (**U**). Agent Cards over tailnet need no registry.

## 6. Security and testing helpers for S3

**smugglex** (hahwul/smugglex): **ADOPT** for the S3 pre-public gate
- MIT (**V**), Rust. Active per the snap listing updated Sep 2026 (**R**).
- Covers CL.TE, TE.CL, TE.TE, H2C, H2 and H2-downgrade, with opt-in CL.0/0.CL (**V**).
- Exit codes 0/1/2 and `--json` make it CI-friendly.
- No safe mode is documented. Run it only against your own staging tunnel.
- Matches the RFC 006 §3 "fuzz the proxy-class surface" item.

**defparam/smuggler**: **SKIP** as primary. A narrower Python second opinion only (**R**).

**PortSwigger HTTP Request Smuggler**: **SKIP**. Burp-dependent (**R**).

**Cisco `mcp-scanner`**: **ADOPT** (static mode only)
- Apache-2.0 (**R**).
- `--analyzers yara static --tools tools-list.json` runs offline with no API key (**R**).
- What it gives you: a pre-publish lint of the `/mcp` facade's tool descriptions for poisoning or injection patterns.

**Snyk Agent Scan** (formerly mcp-scan): **SKIP**
- Apache-2.0 (**R**), but it requires `SNYK_TOKEN`, sends tool names, descriptions and skills to Snyk, and *executes* stdio servers from your config (**R**).

**MCP Inspector** (`@modelcontextprotocol/inspector`): **BORROW as a dev tool, with care**
- CVE-2025-49596 (CVSS 9.4): proxy without auth, giving RCE/CSRF before 0.14.1 (**R**, NVD).
- Use only ≥ 0.14.1, loopback only, and never alongside the public tunnel.

**Schemathesis**: **SKIP for now**
- OpenAPI/GraphQL only; no JSON-RPC support found (**R**).
- Revisit only if you publish an OpenAPI document for the HTTP+JSON binding.

## 7. Approaches not yet considered

**Git-native coordination provenance**: **BORROW**. Standard git features, not re-read this session.
- Commit trailers (`A2A-Task: <id>`, `Session: <handle>`) written via `git interpret-trailers`.
  - This links commits to A2A tasks without touching the task store.
  - Jev's `commit.ts` can add the trailer.
- `git notes --ref=a2a` for after-the-fact annotation.
- **Worktree leases**: `git worktree lock --reason "<session> <task-id> <expiry>"` acts as a zero-daemon claim on a worktree, much like BuildSlot's `flock`.
  - ax's "New Worktree Tab" (`src/ui/palette.rs`) could lock on create and offer unlock or prune on close.
- Fits every rule: no agent driving, nothing auto-executed.

**Claude Code native `--worktree` and `WorktreeCreate`/`WorktreeRemove` hooks**: **BORROW for ax**
- **V** (hooks doc): `WorktreeCreate` replaces the default git logic and prints the path. It also fires for `isolation: "worktree"` subagents and background sessions.
- Remote Control server `--spawn worktree` (**V**).
- What it gives ax: one hook that registers every Claude-created worktree in its own DB, so worktrees made outside ax show up in its registry.

**Session-transcript search for handoffs**: **BORROW** (no new tool)
- Your own memory/lookup FTS5 (`:8100`) already indexes transcripts. Agent-deck's global search (`G`) shows the UX.
- Give A2A handoff messages a `lookup://` or `session:<id>#turn` pointer instead of pasted context.
- The receiving live session pulls the context with its own tool call. This is consistent with stub-only wake and park-never-execute.

**`claude agents --json`** (background-session inventory: pid, cwd, sessionId, status): **BORROW for ax** (**R**)
- A read-only way for ax to discover Claude sessions it did not launch.

## Rule-conflict summary

| Rejected or partly rejected | Reason |
|---|---|
| agent-deck send/conductor, cmux socket input | DDR-003 and symmetric agency |
| agy-hud | uses agy credentials (DDR-001/R8) |
| Taskmaster | Commons Clause |
| Snyk Agent Scan | data egress and runs servers |
| a2a-inspector host agent | needs an API key |
| ntfy, Remote Control | must never become a quarantine-release path (TTY-only rule) |

## Sources
- https://code.claude.com/docs/en/remote-control
- https://code.claude.com/docs/en/statusline
- https://code.claude.com/docs/en/hooks
- https://github.com/binwiederhier/ntfy
- https://dev.to/felipeelias/perfect-claude-code-notifications-setup-with-tailscale-and-ntfy-1ii1
- https://github.com/manaflow-ai/cmux
- https://github.com/asheshgoplani/agent-deck
- https://cdn.jsdelivr.net/npm/ccmanager@4.1.18/README.md
- https://github.com/stravu/crystal
- https://github.com/ryoppippi/ccusage
- https://github.com/icebear0828/agy-hud
- https://discuss.ai.google.dev/t/is-it-permitted-for-a-third-party-desktop-utility-to-display-quota-data-from-the-antigravity-cli-statusline/178823
- https://demo.gitea.com/lianguan/gitea/commit/3cab9c6b0c050bfcb9f2f067e7dc1b0242875254
- https://github.blog/changelog/2026-06-10-manage-sub-issues-types-and-dependencies-from-github-cli/
- https://github.com/eyaltoledano/claude-task-master
- https://github.com/a2aproject/a2a-python/releases
- https://github.com/a2aproject/a2a-tck
- https://github.com/a2aproject/a2a-inspector
- https://github.com/hahwul/smugglex
- https://cisco-ai-defense.github.io/docs/mcp-scanner
- https://github.com/snyk/agent-scan
- https://nvd.nist.gov/vuln/detail/cve-2025-49596
- https://agentpatterns.ai/observability/claude-agents-json-session-export/

Local files read:
- agentic-hub/plugins/coordination/docs/plans/consolidated-bridge-v2-plan.md (§4, §8)
- agentic-hub/plugins/coordination/docs/research/a2a-mcp-wake-audit-2026-11-04.md
- agentic-hub/plugins/coordination/docs/research/todo-tools-evaluation-2026-11-07.md
- ax/src/tmux/profile.rs
- ax/src/ui/palette.rs
- agentic-hub/plugins/gitea/skills/gitea/SKILL.md
- agentic-hub/plugins/jev/README.md
