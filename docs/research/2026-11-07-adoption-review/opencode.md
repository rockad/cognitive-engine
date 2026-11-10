# OpenCode research: multi-session coordination, delegation, backlog, cockpit (2026-11-07)

## How I researched this, and limits

- **opencode.ai and github.blog would not resolve from this session**, so I could not fetch them. They need to be allowed in the session's network settings before anyone re-checks those pages.
- **Where I read the official material instead:** I made a read-only shallow clone of `anomalyco/opencode` (`sst/opencode` moved there) at commit `e45af15`, dated 2026-11-07, into my scratchpad. The opencode.ai docs are built from that repo:
  - `services/site/src/docs/content/` is the **V2 docs**.
  - `packages/web/src/content/docs/` is the **V1 docs**. Its index says "See the OpenCode 2 docs to migrate to V2."
- I installed and ran nothing.
- **Labels used below:**
  - **verified-in-official-docs (VOD):** I read it in the vendor's own docs, docs source, changelog or code.
  - **reported:** secondary sources, or official pages I only saw through a search summary.
  - **unverified:** I found no source either way.
- **Version history (VOD, `CHANGELOG.md`):** V2 is current. Release v2.2.5 is dated 2026-10-30, and the changelog covers v2.0.26 (2026-10-08) onward.

## 1. Subscription constraint: what OpenCode can legitimately use

| Plan | Status in OpenCode today | Terms position | Label and source |
|---|---|---|---|
| **Claude Pro/Max through Anthropic OAuth (the direct provider)** | Removed. The V1 docs say: "There are plugins that allow you to use your Claude Pro/Max models with OpenCode. **Anthropic explicitly prohibits this.** … no longer the case as of 1.3.0." | Anthropic's legal page: OAuth is "intended exclusively for … subscription plans … ordinary use of Claude Code and other native Anthropic applications." It also says: "Anthropic does not permit third-party developers to offer Claude.ai login into their own applications, or to route requests through Free, Pro, Max, or Ultra plan credentials." | VOD: [code.claude.com/docs/en/legal-and-compliance](https://code.claude.com/docs/en/legal-and-compliance) and the [V1 providers.mdx](https://github.com/anomalyco/opencode/blob/e45af15/packages/web/src/content/docs/providers.mdx). History (reported): server-side block around 2026-01-09; Anthropic terms text in 2026-02; OpenCode removed the OAuth code on 2026-02-19 after a legal request. Sources: [zbuild](https://www.zbuild.io/resources/news/opencode-blocked-anthropic-2026), [alternativeto](https://alternativeto.net/news/2026/2/anthropic-officially-bans-using-subscription-authentication-for-third-party-claude-use), [paddo.dev](https://paddo.dev/blog/anthropic-walled-garden-crackdown/), [issue #18329](https://github.com/anomalyco/opencode/issues/18329). |
| **Claude plan through the new "Claude Agent" provider** (v2.2.3, 2026-10-30) | Built in. OpenCode runs the user's own `claude` binary through `@anthropic-ai/claude-agent-sdk`. It turns off CLAUDE.md files, hooks and Claude Code's built-in tools, and gives Claude OpenCode's tools. A subscription login "shows $0". | Anthropic's Agent SDK page: "**Unless previously approved, Anthropic does not allow third party developers to offer claude.ai login or rate limits for their products, including agents built on the Claude Agent SDK.**" OpenCode's own spec is a **Draft** and lists as open questions "Do we need to discuss anything here with anthropic or any kind of approval?" and "reuse cc credentials for usage endpoints? Also not allowed but everyone else does it." The docs page is `noindex: true`. The counter-reading: the legal page does not prevent "an end user from signing in to the **unmodified Claude Code binary** with their own Claude subscription." Here the binary is driven through the SDK with its features stripped. Whether Anthropic approved this is unverified. | VOD: [CHANGELOG v2.2.3](https://github.com/anomalyco/opencode/blob/e45af15/CHANGELOG.md), [claude-agent.mdx](https://github.com/anomalyco/opencode/blob/e45af15/services/site/src/docs/content/claude-agent.mdx), [specs/v2/claude-agent-provider.md](https://github.com/anomalyco/opencode/blob/e45af15/specs/v2/claude-agent-provider.md), [Agent SDK overview](https://code.claude.com/docs/en/agent-sdk/overview). **Result: this falls under the restrictive-terms rule, so SKIP.** |
| **Google AI Pro, Gemini or Antigravity login** | No consumer OAuth provider exists. Only Gemini API keys (`GEMINI_API_KEY` and similar) and Vertex Application Default Credentials. | The Antigravity FAQ **names OpenCode**: "Why can't I use third-party software (such as Claude Code, OpenClaw, or OpenCode) with my Antigravity login?" It calls this a ToS violation that can lead to suspension. Community `opencode-gemini-auth` style plugins fall under that. | VOD through your R1 note (`docs/research/r1-antigravity-terms-and-acp-gate-2026-11-03.md`, citing [antigravity.google/docs/faq](https://antigravity.google/docs/faq)). The V2 providers doc lists Vertex and Google only as API or ADC: [providers.mdx](https://github.com/anomalyco/opencode/blob/e45af15/services/site/src/docs/content/providers.mdx). **Pay-per-token only.** |
| **ChatGPT Plus/Pro (Codex OAuth)** | Built in. **OpenAI → "Sign in with ChatGPT"** or legacy Codex sign-in. "Usage counts against your plan." Context caps match Codex defaults by default. | OpenCode documents it as first-class. I found no OpenAI primary statement endorsing third-party use; a partnership is reported. OpenAI's help center only says the ChatGPT ToS applies. | VOD (OpenCode side): [V2 providers.mdx](https://github.com/anomalyco/opencode/blob/e45af15/services/site/src/docs/content/providers.mdx). Reported (OpenAI side): [besthub](https://www.besthub.dev/articles/anthropic-s-model-lockdown-vs-openai-s-opencode-integration-what-it-means-for-developers-74830a2c24d8), [help.openai.com](https://help.openai.com/articles/11369540). |
| **GitHub Copilot (Pro, Pro+, Business, Enterprise)** | Built in through device-code login. Provider code is `packages/core/src/plugin/provider/github-copilot.ts`. | GitHub announced official support in its changelog on 2026-01-16 (one listing says 01-15). | Reported (an official GitHub page, but seen only through search because github.blog was blocked): [github.blog changelog](https://github.blog/changelog/2026-01-16-github-copilot-now-supports-opencode/). V1 OpenCode docs (VOD) list "ChatGPT Plus, Github Copilot, Gitlab Duo" as zero-setup subscriptions. |
| OpenCode Go and OpenCode Models | OpenCode's own subscription and pay-as-you-go, through an API key. | Clean, but it is a separate paid plan, not one of your existing subscriptions. | VOD |
| Everything else (Anthropic API, OpenRouter, Vertex, Bedrock and so on) | API key or cloud credentials. | Pay-per-token. | VOD |

**Net:** OpenCode can run **legitimately on a subscription only through ChatGPT or Copilot** (also GitLab Duo, SuperGrok, or OpenCode's own Go plan). It **cannot** legitimately use your Claude Pro/Max or Google AI Pro plans. The only Claude-plan path, Claude Agent, collides with Anthropic's written Agent SDK restriction.

## 2. Native features

All items are VOD in the V2 docs or code unless marked otherwise.

- **Agents and subagents.**
  - Modes are `primary`, `subagent` and `all`.
  - Built-ins: Build, Plan, General, Explore. Hidden: compaction, title, summary.
  - Agents load from `~/.config/opencode/agents/*.md` and `.opencode/agents/` (nested paths become IDs such as `team/reviewer`).
  - **The delegation tool is now `subagent`, not `task`.** The V1-to-V2 migration renames tools: `RENAMED_TOOLS = { bash: "shell", task: "subagent", apply_patch: "patch" }`.
  - The `subagent` tool supports `background: true` ("you are notified when it finishes"), follow-ups to an existing child, and per-agent `subagent` permissions.
  - `@agent` mentions work in prompts.
  - Commands with `subagent: true` run in a background child session.
  - Sources: [agents.mdx](https://github.com/anomalyco/opencode/blob/e45af15/services/site/src/docs/content/agents.mdx), [commands.mdx](https://github.com/anomalyco/opencode/blob/e45af15/services/site/src/docs/content/commands.mdx), `packages/core/src/tool/plugin/subagent.ts`.
- **V2 plugins** (`Plugin.define({id, setup})`, Promise or Effect API).
  - Transform hooks: agent, command, mcp, model, provider, tool and others.
  - Runtime hooks:
    - `ctx.session.hook("prompt" | "context" | "retry" | "title" | "compaction" | "experimental.idle" | …)`
    - `ctx.tool.hook("execute.before" | "execute.after")`. This takes **one mutable event `{tool, sessionID, agent, input}`**, not V1's `(input, output)` pair.
    - `ctx.event.subscribe({types})`
  - Session control from inside a plugin: `ctx.session.prompt`, `synthetic`, `interrupt`, `wait`, `update`, and `instructions.entry.put` ("render as `<context key>` blocks … announced at the next step boundary **without queuing a message turn**").
  - The V1 `session.idle` event maps to V2 **`experimental.idle`**, which is marked "name and shape may still change."
  - Sources: [plugin README](https://github.com/anomalyco/opencode/blob/e45af15/packages/plugin/src/README.md), [build/plugins/server.mdx](https://github.com/anomalyco/opencode/blob/e45af15/services/site/src/docs/content/build/plugins/server.mdx), [migrate-v1.mdx](https://github.com/anomalyco/opencode/blob/e45af15/services/site/src/docs/content/build/plugins/migrate-v1.mdx).
- **Server, SDK and the native inbox.**
  - By default every TUI, desktop or web client talks to **one shared background service** on `127.0.0.1:49374` (Linux and macOS).
  - That service is **password-protected by default**. `opencode pair` prints one-time links; `opencode serve` gives a standalone foreground server; `--standalone` runs a private server.
  - The V2 HTTP API is `/api/...`. The relevant endpoints:
    - `POST /api/session/:id/prompt` and `POST /api/session/:id/synthetic`, both taking `delivery: "steer" | "queue"` and `resume: boolean`
    - `GET /api/session/:id/inbox`
    - `GET /api/session/active`
    - `GET /api/event` (server-sent events; live only, no replay)
    - `PATCH /api/session/:id` (title)
  - The session contract describes a **durable `session_inbox`**: input is admitted first, then delivered at a safe step boundary. `resume:false` records the input without waking execution.
  - `@opencode/client` is the TypeScript SDK, and `opencode api <operationId>` is the CLI equivalent.
  - **So yes: an external process can put a message into a running session, through a documented, durable inbox path.**
  - `opencode pair --remote` exposes the server to the internet through OpenTunnel, behind a prompt.
  - Sources: [cli/web.mdx](https://github.com/anomalyco/opencode/blob/e45af15/services/site/src/docs/content/cli/web.mdx), [specs/v2/session.md](https://github.com/anomalyco/opencode/blob/e45af15/specs/v2/session.md), [protocol/groups/session.ts](https://github.com/anomalyco/opencode/blob/e45af15/packages/protocol/src/groups/session.ts), [build/client/index.mdx](https://github.com/anomalyco/opencode/blob/e45af15/services/site/src/docs/content/build/client/index.mdx).
- **ACP.** `opencode acp` makes OpenCode an ACP agent for Zed and similar clients, using stored credentials. Source: [cli/acp.mdx](https://github.com/anomalyco/opencode/blob/e45af15/services/site/src/docs/content/cli/acp.mdx).
- **MCP client.**
  - Configured under `mcp.servers.<name>` with `type: local|remote`, headers and OAuth.
  - It supports remote streamable HTTP with headers, so it can reach the coordination server's `/mcp` with a bearer API key.
  - `opencode mcp add`.
  - Source: [mcp-servers.mdx](https://github.com/anomalyco/opencode/blob/e45af15/services/site/src/docs/content/mcp-servers.mdx).
- **Sessions.**
  - Resume with `opencode --continue` or `--session <id>` ("continue, or create if it does not exist").
  - Fork, export and import, `opencode session list|delete|export|import`.
  - Child sessions per subagent, `@` links to other sessions (changelog), and restart recovery of claimed sessions.
  - **Share:** the V1 docs had `/share`. The V2 table has a `share_url` column, but I found no V2 share endpoint (unverified).
- **Custom commands.** `.opencode/commands/*.md` with `$ARGUMENTS`, shell, attachments and background (VOD).
- **Rules.**
  - Loads `~/.config/opencode/AGENTS.md` plus the hierarchical `AGENTS.md` files, with lazy discovery below the workspace.
  - **V2 does not fall back to CLAUDE.md.** That is fine for agentic-hub, where `CLAUDE.md` imports `AGENTS.md`.
  - Source: [instructions.mdx](https://github.com/anomalyco/opencode/blob/e45af15/services/site/src/docs/content/instructions.mdx).
- **Worktrees and parallel work.**
  - `worktree.directory` config, a worktree and base-branch picker (v2.2.4 and v2.2.5), and a plugin `worktree` domain.
  - Separate sessions run concurrently on the one server.
  - Experimental **schedules**: `/schedule`, `experimental.schedules`, and `client.experimental.schedule.*` with `if`/`until` checks and custom wake messages. Since v2.2.4 a schedule fires once across processes. Source: CHANGELOG.
- **Clients.** TUI, Desktop (with GUI plugins), the web UI from the same server, the IDE, and the ACP agent.
- **GitHub agent.**
  - V1 had `opencode github install`, the `anomalyco/opencode/github@latest` Action and `/oc` comments.
  - **No `github` command exists in the V2 CLI** (`packages/cli/src/commands/commands.ts`). The Action's V2 status is unverified.
  - It runs in CI on API keys, so it is out of scope for a subscription peer.

## 3. Your projects

### `plugins/coordination`: OpenCode as an A2A peer

**Native wake is possible, and it can be at least as good as the Claude wake.**

- **Recommended pattern (in-process).**
  - A V2 plugin loaded by the background service holds **one** long-poll to `/wake/listen` for card `opencode@<host>`. One server hosts every OpenCode session, so one listener can route `opencode@the workstation/<session>` to a sessionID.
  - On a wake it calls `ctx.session.synthetic({sessionID, text: "[a2a] N pending. Call a2a_inbox.", delivery: "queue"})`. That is the constant stub, so it meets the stub-only invariant.
  - Task bodies reach the model only as `a2a_inbox` results from MCP tool calls, so the invariant holds. Tasks stay parked because the model must call a tool to take one.
- **Non-waking mode, matching agy's asymmetry.**
  - Option one: `instructions.entry.put({key:"a2a", value:"N pending"})`, which surfaces at the next step without a turn.
  - Option two: `synthetic` with `resume:false`.
- **Lifecycle and registration.** `ctx.event.subscribe` on session created, updated and deleted can do `POST /sessions` registration.
- **The external alternative.** `POST /api/session/:id/synthetic` against the password-protected service works and is documented, but it is an outside process writing into a session.
- **Is it "driving"?**
  - DDR-003 is agy-specific, and this is your own tool on its documented plugin API. A constant stub that only prompts a tool call is what Claude's `asyncRewake` already does.
  - **Prefer the in-process plugin.** The session then wakes itself, which keeps symmetric agency.
  - Keep `delivery: "queue"`, not `steer`, so a wake never cuts into a running turn.
  - Never call `prompt` with task text.
- **Injection risk.**
  - The default Build agent allows broad tool access. Read access outside the workspace is allowed, and only sensitive env-file reads ask.
  - Anything that follows an inbox read should run under tighter `permissions`, for example `ask` on `shell` and `edit` for a coordination agent. Alternatively, take tasks with a dedicated restricted agent.
- **Other cautions.**
  - `experimental.idle` re-entry needs a loop bound.
  - Restart recovery resumes claimed sessions; persist state.
  - **Keep OpenTunnel remote pairing off.** It conflicts with the "never expose" rule.
  - The plugin needs the `local-harness` API key from a 0600 file, not from `opencode.json`.

### `cognitive-engine/adapters/opencode` (marked "deferred / unverified")

Checked against the V2 code. Two of its assumptions no longer hold.

1. **The guard never fires: wrong tool name.** The default `delegationTool: 'task'` matches nothing, because the V2 tool is `subagent`.
2. **The guard never fires: wrong argument field.** `execute.before` passes one event whose payload is `event.input`. The adapter reads `out.args ?? event.args`, which are always undefined, so `task` is `""`. `dispatch` then returns null under `MIN_TASK_CHARS`, and enforce mode silently never blocks. The fix is `event.input.prompt`, with `delegationTool` set to `'subagent'`.
3. **What still holds.**
   - `ctx.session.hook('context')` exists. `event.messages[].content[].text` and `event.system.push({type:'text',text})` match the V2 `SystemPart` and message shapes.
   - The `{id, setup}` export matches `Plugin.define`, which is an identity function.
   - The MCP registration format `mcp.servers.jev {type:"local", command:[…]}` matches V2.
   - Also consider `ctx.session.instructions.entry.put` instead of re-pushing the system prompt on every step.
4. **Restart guidance is stale.** "`pkill -f "opencode serve"`" is V1 thinking. V2 uses `opencode reload` or `opencode service restart`.
5. **API-key reliance.** The adapter only advises and blocks; it never picks models. But the Jev OpenCode tier map (`agentic-hub/plugins/jev/src/core/config.ts:160-167`, and the same in `cognitive-engine/packages/jev/src/core/config.ts`) uses `~anthropic/claude-haiku|sonnet|opus-latest`.
   - Those are OpenRouter alias slugs, so they are **pay-per-token through OpenRouter**.
   - Under your subscription constraint, the tiers should map to `openai/…` (ChatGPT sign-in) or `github-copilot/…` models.
   - Not `claude-agent/*`, which is terms-restricted.
6. **`agentic-hub/plugins/jev/opencode/` does not exist.** `AGENTS.md` ("OpenCode V2 plugin (`plugins/jev/opencode/`)") and the README tree both reference it, so that text is stale. The only implementation is the cognitive-engine adapter.

### `ax`

- **Launching** `opencode` in tmux tabs is fine. In V2 every tab is a client of the shared background service. For resume, prefer `--session <id>` or `--continue`.
- **Title sync is fragile.** `UPDATE session_v2 SET title …` (`src/core/harness.rs:517-528`) writes a projection directly in V2's event-sourced store (`event` table plus projections; the server owns the DB):
  - Live clients get no `session.updated` event.
  - A projection rebuild could revert the title (my inference).
  - It can hit `SQLITE_BUSY` while the server holds the database.
  - Use `PATCH /api/session/:id` or `opencode api session.update` instead.
- **The database path is hardcoded.** `~/.local/share/opencode/opencode.db` is only the default. `databasePath` honours `OPENCODE_DB` and non-default channels (`opencode-<channel>.db`), as `packages/cli/src/database-path.ts` shows.
- **Read-only history queries still work.** The columns `id, title, directory, project_id, parent_id, time_updated` exist.
- **Cockpit status.** `time_busy`, `time_idle` and `idle_outcome` exist in the table. Better still, use `/api/session/active` plus the `/api/event` server-sent events.

## 4. Feature table

| Feature | What it does | Overlaps which project need | Verdict | Reason |
|---|---|---|---|---|
| ChatGPT and Copilot sign-in | Subscription-billed models in OpenCode | Subscription constraint | **ADOPT** (if OpenCode is used) | The only first-class subscription paths. Copilot is officially supported by GitHub (reported); ChatGPT is first-class in OpenCode's docs. |
| Claude Agent provider | Claude plan through the user's `claude` and the Agent SDK | Subscription constraint | **SKIP** | Anthropic: no claude.ai login in third-party products, Agent SDK included, "unless previously approved". OpenCode's own spec leaves approval open. |
| Gemini or Antigravity login | none (community plugins only) | Subscription constraint | **SKIP** | The Antigravity FAQ names OpenCode. The symmetric terms rule applies. |
| V2 plugin `experimental.idle`, `event.subscribe` and `session.synthetic` | In-process idle hook and inbox injection | Coordination wake | **ADOPT** (spike) | A native, documented self-wake with a constant stub. Experimental API, so pin the version. |
| Session inbox (`delivery` queue/steer, `resume:false`) | Durable admission; delivery at step boundaries | Coordination wake, no turn hijack | **ADOPT** | Matches "parked, stub-only". Use `queue`. |
| `instructions.entry.put` | Context note with no turn | Coordination non-wake notice; Jev advisory | **BORROW** | A cleaner notice channel than system-prompt pushes. |
| `opencode serve` / service HTTP API and `@opencode/client` | External control of sessions | Wake fallback; ax title and status | **BORROW** | Use for ax title (`PATCH`) and status (`active`, server-sent events). External injection only as a fallback. |
| OpenTunnel `pair --remote` | Internet exposure of the server | none | **SKIP** | Conflicts with the "never expose" rule. |
| ACP (`opencode acp`) | Agent over ACP | (old Phase 4) | **SKIP** | ACP is removed from the plan. It is also a driver/client pattern. |
| MCP client (remote HTTP plus headers) | Connects to `/mcp` | Coordination tools; Jev MCP | **ADOPT** | Standard. Keep the key out of chezmoi and config files. |
| `subagent` tool and agent modes | Delegation to child sessions, background | Jev guard | **BORROW** | Fix the adapter: tool name `subagent`, field `event.input`. |
| `tool.hook("execute.before")` | Block or modify tool calls | Jev Pro gate | **ADOPT** | Works once the adapter is fixed. |
| Custom commands (`subagent: true`) | Slash workflows, background child | Backlog and delegation | **BORROW** | A useful pattern. Not needed for coordination. |
| Experimental schedules | Timed prompts with checks and wake messages | Backlog / cockpit | **SKIP** (for now) | Experimental. Auto-running prompts conflicts with "never auto-execute". |
| AGENTS.md rules | Hierarchical instructions | Shared repo rules | **ADOPT** | Already compatible. No CLAUDE.md fallback. |
| Worktrees | Worktree picker, `worktree.directory` | ax worktree tabs | **BORROW** | Align ax's `.worktrees/<branch>` with `worktree.directory`. |
| Direct `session_v2` SQLite writes (current ax behaviour) | Title sync | ax | **SKIP** | Event-sourced store. Use the API. |
| Share, GitHub agent | Public links; CI agent | none | **SKIP** | Not peer-relevant. API keys in CI. V2 status unclear. |

## 5. Should OpenCode be a first-class peer?

**Technically, yes. It has the richest documented wake and inbox surface of the three harnesses:**

- an in-process idle hook
- a durable, step-boundary inbox with `resume:false`
- no-turn instruction entries
- a password-protected local API

It could match or beat Claude's `asyncRewake` without any terminal or "drive" hacks.

**The subscription constraint changes the case.** OpenCode can only run legitimately on **ChatGPT or Copilot** (or OpenCode Go or API keys), never on your Claude Max or Google AI Pro plans.

- Claude Agent is terms-restricted by Anthropic's Agent SDK note.
- Gemini consumer login is explicitly barred by Google, which names OpenCode.

So OpenCode adds value only as a peer that uses **your ChatGPT or Copilot quota**. That overlaps Codex, if you adopt a ChatGPT plan.

**Recommendation:**

- Keep OpenCode a **candidate third peer**, as the plan already says.
- Promote it only if you hold a ChatGPT or Copilot subscription you want to use.
- If you do, build the wake as a pinned-version V2 plugin: queued constant-stub synthetic plus `instructions.entry`, plus restricted permissions for task-taking.
- **Never configure it with Claude Agent or any Gemini OAuth plugin.**

## Local paths referenced

- agentic-hub/plugins/coordination/AGENTS.md
- agentic-hub/plugins/coordination/README.md
- agentic-hub/plugins/coordination/docs/research/r1-antigravity-terms-and-acp-gate-2026-11-03.md
- cognitive-engine/adapters/opencode/src/index.ts (guard bug at the `execute.before` handler: `args = out.args ?? event.args`, `delegationTool` default `'task'`)
- cognitive-engine/adapters/opencode/README.md (stale `pkill` restart guidance)
- agentic-hub/plugins/jev/src/core/config.ts lines 160-167 (OpenRouter `~anthropic/*` tier map)
- agentic-hub/AGENTS.md and agentic-hub/plugins/jev/README.md (reference the missing `plugins/jev/opencode/`)
- ax/src/core/harness.rs lines 517-528 (direct `session_v2` UPDATE) and 861-960 (history query)
- Read-only clone of OpenCode: /tmp/claude-0/-home-user/84e5c850-1d48-5bf0-99bf-2742d6707467/scratchpad/oc

## Other sources (search results, reported)

- [kersai](https://kersai.com/anthropic-killed-third-party-claude-access-heres-every-workaround-that-still-works/) gives dates that conflict with the others; treat with caution.
- [ridakaddir](https://ridakaddir.com/blog/post/did-anthropic-kill-opencode-claude-subscription-ban)
- [shareuhack](https://www.shareuhack.com/en/posts/opencode-anthropic-legal-controversy-2026)
- [openchamber/opencode-claude](https://github.com/openchamber/opencode-claude) is a community plugin using Pro/Max OAuth, not sanctioned.
- [numman-ali codex auth plugin](https://numman-ali.github.io/opencode-openai-codex-auth) is a community plugin.

