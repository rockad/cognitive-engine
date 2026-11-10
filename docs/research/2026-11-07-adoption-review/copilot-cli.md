
# GitHub Copilot CLI (`copilot`, github/copilot-cli): research for ax, coordination, Jev and backlog

**Sources and labels.**
- docs.github.com would not resolve from this session (it is outside the allowed network domains). I read the same pages as Markdown source in the public `github/docs` repository and give the docs.github.com URL for each.
- The other sources are the official changelog in the `github/copilot-cli` repository (latest entry **1.0.107, 2026-11-06**) and `github/site-policy`.
- Labels:
  - **[V]** verified in official docs, changelog or terms.
  - **[R]** reported.
  - **[U]** unverified, my inference.
- Nothing was installed or run.

## 1. Subscription, billing and models

- **Billing changed on 2026-06-01** **[V]**.
  - Premium requests with model multipliers are now "legacy". They only apply to annual Pro/Pro+ plans until those plans end.
  - Every other plan uses token-metered **GitHub AI Credits**. 1 credit = $0.01, priced per model, per token (input, cached, output).
  - Source: https://docs.github.com/en/copilot/reference/copilot-billing/request-based-billing-legacy/what-changed-with-billing
- **What each plan includes** **[V]**. Credits = base + flex. Credits reset on the 1st of the month (UTC) and do not roll over.

  | Plan | Price per month | Credits per month | Value at $0.01 |
  |---|---|---|---|
  | Pro | $10 | 1,000 + 500 = 1,500 | about $15 of tokens |
  | Pro+ | $39 | 3,900 + 3,100 = 7,000 | about $70 |
  | Max | $100 | 10,000 + 10,000 = 20,000 | about $200 |
  | Business | $19 per seat | 1,900 pooled | |
  | Enterprise | $39 per seat | 3,900 pooled | |

  - Above the allowance you can pay through a budget.
  - Flex "is designed to adapt as the economics of AI evolve", so it is not guaranteed.
  - Copilot CLI is included on every plan, Free included.
  - Sources:
    - https://docs.github.com/en/copilot/get-started/plans
    - https://docs.github.com/en/copilot/concepts/billing-and-usage/individuals/billing
- **Discounts** **[V]**.
  - 10% off model costs with Auto model selection.
  - 30% off with "Copilot Fusion" until 2026-11-30.
  - Same billing page as above.
- **Models on each plan** **[V]**. Source: https://docs.github.com/en/copilot/reference/ai-models/supported-models (table `model-supported-plans.yml`).
  - Pro:
    - Claude Haiku 4.5, Haiku 5.5, Sonnet 4.6, Sonnet 5 and Sonnet 5.5.
    - Gemini 3.8 Flash.
    - GPT-5.3-Codex, GPT-5.6 Luna and Terra, GPT-6 Luna.
  - Pro+ and Max add:
    - Claude Opus 4.8, 5 and 5.5, and Fable 5 and 5.1.
    - Gemini 4 Argon.
    - GPT-5.6 Sol and GPT-6 Astra.
  - Free and Student plans get Auto model selection only.
- **Price per 1M tokens, input / output** **[V]**. Source: https://docs.github.com/en/copilot/reference/copilot-billing/models-and-pricing
  - Haiku 5.5: $0.10 / $0.50
  - GPT-6 Luna: $0.10 / $0.50
  - Gemini 3.8 Flash: $0.75 / $3.75
  - Sonnet 5.5: $2 / $10
  - Gemini 4 Argon: $2 / $10
  - Opus 5.5: $4 / $20
  - Fable 5.1: $10 / $50
- **Authentication** **[V]**.
  - Interactive login is `/login`: web OAuth on a local machine, device code when headless.
  - A fine-grained PAT with the "Copilot Requests" permission works, read from `COPILOT_GITHUB_TOKEN`, then `GH_TOKEN`, then `GITHUB_TOKEN`.
  - Bring-your-own-key (BYOK) providers (`providers.json`, `COPILOT_PROVIDER_*`) and models discovered from Ollama run without a GitHub login.
  - Sources:
    - https://github.com/github/copilot-cli (README)
    - https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-llm-models
- **Terms on automation** **[V]**.
  - Individual plans are covered by GitHub ToS §J (AI Features). Business and Enterprise use the Copilot Product Specific Terms.
  - The Acceptable Use Policies ban "excessive automated bulk activity" and resale. A login may be used by one person only. Machine accounts are allowed.
  - GitHub's own docs support `-p`, ACP, the SDK, GitHub Actions, PAT auth and a server container.
  - Sources:
    - https://github.com/github/site-policy (`github-terms-of-service.md` §B and §J, `github-acceptable-use-policies.md`)
    - https://docs.github.com/en/copilot/how-tos/copilot-cli/automate-copilot-cli/run-cli-programmatically
- **My reading of the terms** **[U]**. Copilot passes the coordination plugin's restrictive-terms test. Automation is sanctioned, and because billing is per token there is no flat-rate allowance to abuse. This is unlike agy.

## 2. Features

- **Custom agents** **[V]**.
  - Agents are `.agent.md` or `.md` files in these locations, highest priority first:
    1. `~/.copilot/agents/`
    2. `.github/agents/`, from the working directory up to the Git root
    3. `.claude/agents/`, also walked upward
    4. Directories added with `--add-dir`
  - Frontmatter fields: `description`, `tools`, `model`, `models` (a priority list), `modelPolicy: required`, `reasoningEffort`, `mcp-servers`, `infer`, `include-custom-instructions`.
  - Select one with `--agent`.
  - Source: https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference#custom-agents-reference
- **Subagents** **[V]**.
  - Tools: `task`, `list_agents`, `read_agent`, `write_agent`. `write_agent` can message siblings or children of the current agent.
  - Built-in agents: `explore`, `task`, `code-review`, `security-review`, `research`, `rubber-duck`, `general-purpose`.
  - Default limits: depth 6. Concurrency depends on plan: Free 2, Pro and Pro+ 4, Max 8, Business 16, Enterprise 32.
  - `/fleet` and `--fleet` run subagents in parallel.
  - "Sidekick" agents start on session events and post into a "session inbox".
  - Same reference page as above.
- **Cross-session tools** **[V, changelog only]**.
  - 1.0.107 added "opt-in session management tools and @mentions for opted-in local sessions".
  - They are not documented yet; I found nothing for them in the docs source.
- **`/delegate`, or a prompt starting with `&`** **[V]**.
  - Hands the task to the Copilot cloud agent. It commits unstaged changes to a new branch, opens a draft PR and works remotely.
  - `--base` picks the target branch.
  - Autopilot (`--autopilot`, Shift+Tab) is the local hands-free mode.
  - Source: https://docs.github.com/en/copilot/how-tos/copilot-cli/use-copilot-cli/delegate-tasks-to-cca
- **MCP client** **[V]**.
  - Server definitions are read from:
    - `~/.copilot/mcp-config.json`
    - project `.mcp.json` or `.github/mcp.json`, walking up to the repo root
    - `--additional-mcp-config`
  - `.vscode/mcp.json` is not read.
  - Remote servers use `type: http` or `sse` with `headers`. Headers support `$VAR`, `${VAR}` and `${VAR:-default}` expansion.
  - `copilot mcp add --transport http NAME URL --header "K: V"` adds a server from the command line.
  - OAuth is supported, including `client_credentials` and device-code flows.
  - The `oidc` option injects a bearer token.
  - In `-p` mode, project MCP servers load only in trusted folders.
  - Sources:
    - https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers
    - cli-command-reference (MCP schema)
- **Hooks** **[V]**. Source: https://docs.github.com/en/copilot/reference/hooks-reference
  - **Events:**
    - `sessionStart`, `sessionEnd`
    - `userPromptSubmitted`, `userPromptTransformed`
    - `preToolUse`, `postToolUse`, `postToolUseFailure`
    - `permissionRequest`
    - `agentStop`, which can block and force another turn, up to 8 times in a row
    - `subagentStart`, `subagentStop`
    - `preCompact`, `errorOccurred`
    - `notification`, which is asynchronous and fire-and-forget, matches `agent_idle`, `shell_completed`, `agent_completed` and others, and can return `additionalContext`
  - **Hook types:** command hooks, HTTP hooks (`preToolUse` and `permissionRequest` require https), and "prompt hooks", which auto-submit text on `sessionStart` only.
  - **Config sources:**
    - policy directories
    - `.github/hooks/*.json`
    - `~/.copilot/hooks/`
    - `.github/copilot/settings*.json`
    - **the repository's `.claude/settings.json` and `.claude/settings.local.json`**
    - `~/.copilot/settings.json`
    - plugins
  - **Claude compatibility:** PascalCase event names use Claude's matcher semantics.
  - **Timeouts:** the default is 30 s. A command hook that times out fails open, even for `preToolUse`.
- **Instructions** **[V]**.
  - Files read:
    - `AGENTS.md`, `CLAUDE.md` (also `.claude/CLAUDE.md`) and `GEMINI.md`, at the Git root and the working directory
    - `.github/copilot-instructions.md`
    - `.github/instructions/**/*.instructions.md`
    - `.claude/rules/**/*.md`
    - `~/.copilot/copilot-instructions.md`
    - directories in `COPILOT_CUSTOM_INSTRUCTIONS_DIRS`
  - `@path` imports work in AGENTS.md and CLAUDE.md. This repository's `CLAUDE.md` → `@./AGENTS.md` pattern works.
  - The docs disagree on whether absolute import paths are allowed: the how-to says no, the reference says yes.
  - Subagents get no repository instructions unless `include-custom-instructions: true`.
  - Disable everything with `--no-custom-instructions`.
  - Source: https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-custom-instructions
- **Session storage and resume** **[V]**.
  - Each session is stored at `~/.copilot/session-state/<session-id>/events.jsonl`, next to plans and checkpoints. The base directory is `$COPILOT_HOME`.
  - `~/.copilot/session-store.db` is a SQLite index.
  - Resume flags:
    - `--resume[=id|prefix|name]`
    - `--continue`, which resumes the last session in this directory
    - `-n/--name`
    - **`--session-id <UUID>`**, which creates a session with that exact UUID if none exists
  - Sessions can sync to the user's GitHub account through `--sync-session` and `/chronicle reindex`.
  - Restoring a crashed session is opt-in with `COPILOT_ENABLE_INTERRUPTED_SESSION_RESTORE=1`.
  - Hook payloads carry `sessionId`.
  - Sources:
    - https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-config-dir-reference
    - cli-command-reference
  - The `events.jsonl` schema is not documented **[U]**.
- **Headless and programmatic use** **[V]**.
  - `-p` runs one prompt. Related flags: `-s`, `--output-format=json` (JSONL), `--allow-tool` and `--deny-tool` patterns, `--autopilot`, `--share`, `--trust-folder`.
  - `copilot workflow run` runs workflows.
  - `--server` and `--headless` serve SDK clients. The `ghcr.io/github/copilot-cli` container image does the same.
  - Source: https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-programmatic-reference
- **ACP** **[V]**.
  - `copilot --acp`, over stdio or `--port` (TCP, loopback, NDJSON). It is in public preview.
  - Tool filters and reasoning effort are fixed when the server starts.
  - Source: https://docs.github.com/en/copilot/reference/copilot-cli-reference/acp-server
- **Other features** **[V]**.
  - Remote control: `/remote on` lets you steer the live session from github.com or GitHub Mobile.
  - Built-in multi-session sidebar and Sessions tab.
  - `/every` and `/after` scheduled prompts. These are experimental and only fire while the interactive session is running. `/every` without an interval is self-paced.
  - Plugins and marketplaces, LSP, sandbox mode, `--worktree`.
  - Sources:
    - https://docs.github.com/en/copilot/how-tos/copilot-cli/use-copilot-cli/steer-remotely
    - https://docs.github.com/en/copilot/how-tos/copilot-cli/automate-copilot-cli/schedule-prompts

## 3. Is it a cheaper subscription path than Jev's free OpenRouter models or Claude Pro?

- **Against Jev's free pool:** no, nothing beats $0.
  - Copilot is the paid fallback with higher quality and reliability.
  - Haiku 5.5 and GPT-6 Luna cost about $0.10 / $0.50 per 1M tokens. Pro's 1,500 credits ($15) buy roughly 30M output tokens or 150M input tokens of either model **[U, arithmetic]**.
  - Fit: a guarded paid fallback when the free pool rate-limits or times out, or for tasks that need Gemini without Antigravity's terms.
  - It has to run through the CLI or SDK under the user's GitHub identity. GitHub does not offer an OpenRouter-style API key for its model pool.
- **Against Claude Pro or Max:** Copilot is plain metered API pricing plus a flex bonus of about 1.5x to 2x the plan price. Heavy Opus or Sonnet agentic work burns $15 to $70 a month quickly **[U]**.
  - Claude's subscription has rate limits but no per-token metering **[R]**, so it is probably cheaper for heavy, sustained Claude work.
  - Copilot wins for:
    - access to several vendors (Claude, Gemini, GPT) on one bill
    - cloud `/delegate` jobs that keep running with the laptop off
    - light or spiky workloads
    - PR and code-review workflows

## 4. GitHub Issues and Projects as the backlog, versus Kanzo or beads

- `gh`, the GitHub MCP server that ships with Copilot CLI by default, and `/delegate` let an issue become a cloud-agent task and then a PR. This is first-class and works across machines **[V, README]**.
- Downsides **[U]**:
  - Network-bound and tied to the GitHub host.
  - Repositories on Gitea (this hub has a gitea plugin) don't fit.
  - Not git-native or offline, unlike beads.
  - Model output written into issues becomes public or org-visible state.
- Recommendation **[U]**:
  - Keep beads or Kanzo as the local, authoritative backlog.
  - Optionally mirror selected items to GitHub Issues only for work meant for `/delegate`.

## 5. Decision table

| feature | what it does | overlaps which project need | ADOPT / BORROW / SKIP | reason |
|---|---|---|---|---|
| Interactive `copilot` as a tab | Native TUI agent on the user's subscription | ax harness | ADOPT | Fits ax's model: native CLI, user-driven. A `CustomDriver` or a built-in driver is trivial. |
| `--session-id <UUID>`, `-n`, `--resume`, `--continue` | Deterministic session identity and resume | ax tab-to-conversation registry, session restore | ADOPT | ax can assign the UUID before launch, so it needs no history scraping to bind a tab. |
| `~/.copilot/session-state/<id>/events.jsonl` | Per-session event log | ax history reader and title sync | BORROW | Read-only parse. The schema is undocumented, so version-guard the reader. Use `session-store.db` only as a hint. |
| Built-in session sidebar and multi-session | Many sessions inside one process | ax tabs | SKIP | Duplicates ax and hides sessions from ax. Use one session per ax tab. |
| Hooks (`sessionStart` additionalContext, `agentStop` block, async `notification`) | Lifecycle injection points | coordination stub wake, Jev guard | BORROW | `sessionStart` stub, plus a constant-stub `agentStop` or notification wake. Pattern is close to Claude's. |
| `.claude/settings.json` and `.claude/agents` read cross-tool | Copilot also runs repository-level Claude hooks and agents | coordination installer, Jev adapters | BORROW (with a warning) | Repository-scoped Claude hooks also fire in Copilot. Keep coordination hooks user-scoped, and give Copilot its own `~/.copilot/hooks`. |
| `subagentStart` hook plus agent `model`, `modelPolicy` | Inject context into subagents and pin their model | Jev PreToolUse tier-ceiling guard | BORROW | A Jev adapter could enforce the ceiling through `preToolUse` on `task` and `modelPolicy`. Command hooks fail open on timeout. |
| MCP `headers` with `${VAR}` expansion | Bearer header from the environment | coordination `/mcp` API key auth | ADOPT | `Authorization: Bearer ${A2A_API_KEY}` works without writing the key into config. |
| AGENTS.md, CLAUDE.md and GEMINI.md loading, `@` imports | Shared instructions | agentic-hub conventions | ADOPT | Works out of the box. Watch for duplicate loading of AGENTS.md through CLAUDE.md's import [U]. |
| `/delegate` cloud agent | Async remote PR worker | backlog execution | BORROW (optional) | Useful for GitHub-hosted repos. Not a live peer and not symmetric, so it stays outside coordination. |
| `-p`, `--server`, SDK, container | One-shot and driven sessions | Jev free-tier executor | SKIP for coordination, BORROW for Jev | Coordination forbids one-shot relays. A Jev paid fallback could use `-p --output-format json` under its guard. |
| ACP (`--acp`) | Client drives the agent | coordination | SKIP | ACP was removed from the plan, and ACP is caller/subordinate. |
| `write_agent`, `list_agents`, sidekick inbox, 1.0.107 session tools | Messaging between agents inside one process | coordination | SKIP (watch) | Only within Copilot. Watch whether the session tools add an external entry point. |
| `/every` and `/after` (self-paced) | Prompts on a timer inside the live session | coordination wake fallback | BORROW | A constant-stub `/every 10m check a2a_inbox` is a legitimate in-session wake. Each firing costs credits, and the feature is experimental. |
| Remote control (`/remote`) | Steer the live session from the web or mobile | none (human use) | SKIP | Human-only. Not an agent channel. |
| AI-credit plans and multi-vendor models | Metered Claude, Gemini and GPT on one bill | Jev free tier, Claude Pro | BORROW | Paid fallback tier. Not cheaper than free, and probably not cheaper than Claude Max for heavy use. |
| GitHub Issues and Projects | Hosted backlog wired to `/delegate` | Kanzo or beads | BORROW (mirror only) | Keep beads or Kanzo authoritative. Mirror only items meant for the cloud agent. |

## 6. Should Copilot CLI become an ax built-in harness and a coordination peer?

**ax built-in harness: yes, it is cheap and worth doing** **[U]**.

The work:
- Add a `CopilotDriver` to `ax/src/core/harness.rs`:
  - command `copilot`
  - map `conversation_id` to `--session-id <uuid>` for new sessions, or to `--resume <id>`
  - map `continue_flag` to `--continue`
  - map `remote_control` to `remoteSessions: true` or a `/remote on` hint (`--remote` is not supported interactively)
  - optionally pass `--no-banner`
  - honour `COPILOT_HOME`
- Add an `events.jsonl` reader to `ax/src/core/transcript.rs` for history and titles. It needs fixture tests because the format is undocumented.
- Until the driver exists, a `[harnesses.copilot]` TOML entry (`command = "copilot"`, `args = ["--no-banner"]`) works today.

**Coordination peer: yes in principle, and the terms are more permissive than agy's** **[U]**.

What it would take:
- **Identity binding.** Copilot runs as an independent live session. Its handle would be `copilot-cli@<host>/<session-name>`.
  - Bind it with the `sessionId` from the `sessionStart` hook, which registers with `/sessions`.
  - Or use the UUID ax assigned through `--session-id`.
- **MCP auth.** Add a user-scoped entry in `~/.copilot/mcp-config.json`:
  - `type: http`, `url: http://127.0.0.1:8120/mcp`
  - `headers: {"Authorization": "Bearer ${A2A_API_KEY}"}`
  - The API key stays in the environment, sourced from the mode-0600 file. Under decision 16 the planned installer writes this, not a hand edit.
- **Wake path.** There is no documented equivalent of Claude's asyncRewake. In order of preference:
  1. A `sessionStart` constant stub that surfaces anything pending.
  2. An `agentStop` hook that briefly checks `/wake/listen` and returns `decision: block` with the constant stub `[a2a] N pending. Call a2a_inbox.` This is limited by the 30 s timeout and the 8-block cap.
  3. A self-paced or fixed `/every` stub, which costs credits even when the inbox is empty.
  4. Possibly an agent-started background `curl` long-poll whose `shell_completed` notification re-engages an idle agent **[U, needs a spike]**.
- **Hooks layout.** Add a Copilot target to the installer (`~/.copilot/hooks/*.json`). Do not rely on Copilot picking up repository-level `.claude/settings.json`, which would double-fire Claude hooks.
- **What to avoid.** The ACP, `--server`, SDK and `-p` paths all violate the live-session and symmetric-agency rules.
- **Before any build.** Run a spike under the "borrow and put back" rule (decision 25) to confirm the wake behaviour.

The raw material I read is under `/tmp/claude-0/-home-user/84e5c850-1d48-5bf0-99bf-2742d6707467/scratchpad/` (the `ghdocs`, `cop-cli` and `sp` clones).
