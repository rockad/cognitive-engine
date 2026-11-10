# Gemini CLI (google-gemini/gemini-cli): research for ax, coordination and Jev

**Sources and labels**
- Main source: a shallow clone of `main` at commit `62bab5b`, dated 2026-11-06, version `0.69.0-nightly.20261104`. It is at `/tmp/claude-0/-home-user/84e5c850-1d48-5bf0-99bf-2742d6707467/scratchpad/gcli/src`. Latest stable is v0.67.0, released 2026-11-04.
- **verified** means I read it in the official repo's docs or source, or in Google's own GitHub announcement.
- **reported** means a search result or third-party write-up.
- **unverified** means I did not read a primary source.
- Three hosts are blocked by this session's network settings: `geminicli.com`, `antigravity.google` and `thenewstack.io`. They need to be allowed before the Plans page, the agy migration guide and agy's own docs can be checked first-hand.
- Copilot CLI is not covered here. This step was Gemini CLI only.

## 0. Most important finding: the user's subscription no longer works in Gemini CLI

- **verified.** The official announcement is GitHub Discussion #27274, "Transitioning Gemini CLI to Antigravity CLI", 2026-05-19, by maintainer @LyalinDotCom: https://github.com/google-gemini/gemini-cli/discussions/27274
  - It says: "On June 18, 2026, Gemini CLI will stop serving requests for Google AI Pro and Ultra, as well as those using it free of charge. These tiers are now supported via Antigravity CLI."
  - Who keeps access: Gemini Code Assist Standard and Enterprise licences, Google Cloud, and "paid Gemini and Gemini Enterprise Agent Platform API keys".
- **verified.** The repo `README.md` banner says the same:
  - "Enterprise Code Assist only".
  - "No free tier or Google AI plans … moved to Antigravity CLI on June 18, 2026".
  - "Not optimized for Gemini 4+".
  - It also points to a new stateless `gemini-api` CLI (https://github.com/google-gemini/gemini-api-cli) as the tool other agents should call.
- **verified, but stale.** `docs/resources/quota-and-pricing.md` still lists per-day limits:
  - Code Assist Individual: 1,000 requests per day.
  - AI Pro: 1,500 requests per day.
  - AI Ultra: 2,000 requests per day.
  - API key free tier: 250 requests per day, Flash only.
  - Workspace Standard: 1,500 requests per day. Enterprise and AI Ultra: 2,000.
  - The README and the announcement override this page. Since 2026-06-18 the consumer rows are dead.
- **Result.** On Google AI Pro the user cannot run Gemini CLI at all. It only works with a paid API key, Vertex AI, or a Code Assist Standard or Enterprise seat. That is pay-per-token or org-licensed, which goes against the user's subscription-first premise.

## 1. Authentication and its link to the Antigravity subscription

- **verified.** `docs/get-started/authentication.mdx` lists four ways to sign in:
  - Sign in with Google. A Google Cloud project is needed for Workspace and Code Assist licences.
  - `GEMINI_API_KEY` from AI Studio.
  - Vertex AI, using ADC, a service-account JSON or a Google Cloud API key.
  - Headless mode, which the doc says should use an API key or Vertex AI.
- **verified.** OAuth credentials are cached at `~/.gemini/oauth_creds.json` (`Storage.getOAuthCredsPath`).
- **Shared quota.** Since June 2026 there is nothing left to share: the consumer plan serves agy only.
  - **reported, conflicting:** older third-party guides claimed Gemini CLI and Antigravity drew on one Pro pool. Forum threads report separate limits, and that agy's Pro quota resets every 5 hours up to a weekly cap. Examples:
    - https://discuss.ai.google.dev/t/usage-quotas-as-of-last-month-or-so-has-made-gemini-ai-pro-with-cli-and-antigraivity-worthless/144049
    - https://discuss.ai.google.dev/t/antigravitys-usage-quota-is-very-restrictive/146716
- **Linked enforcement.**
  - **verified locally:** our r1 research quotes the Antigravity Additional Terms item 6: "suspension or termination of your Antigravity and/or Gemini CLI accounts" (`agentic-hub/plugins/coordination/docs/research/r1-antigravity-terms-and-acp-gate-2026-11-03.md`). Google treats the two as one account family for enforcement.

## 2. How Gemini CLI relates to agy, and whether `~/.gemini` is shared

This answers the open question at `consolidated-bridge-v2-plan.md:449`.
- **The parent folder is shared; the files inside it are separate.**
  - **verified (Gemini CLI side, from source):** `packages/core/src/config/storage.ts` and `docs/hooks/index.md`. Gemini CLI uses:
    - `~/.gemini/settings.json`, which holds hooks, `mcpServers` and policy settings.
    - `~/.gemini/GEMINI.md`, the global context file.
    - `~/.gemini/tmp/<project-short-id>/chats/` for sessions.
    - `~/.gemini/history/<id>`, a shadow git repo for checkpoints.
    - `~/.gemini/agents/*.md` for subagents.
    - `~/.gemini/extensions/`.
    - `~/.gemini/oauth_creds.json`.
    - `projects.json`, a registry mapping project roots to short ids.
    - Project files under `.gemini/settings.json` and `.gemini/agents/`, plus a system file at `/etc/gemini-cli/settings.json`.
  - **verified (agy side, our own spikes):** agy uses `~/.gemini/config/{hooks.json,mcp_config.json,AGENTS.md,plugins/}` and `~/.gemini/antigravity-cli/{brain,conversation_summaries.db,mcp/,builtin/}`.
  - I found no reference to `mcp_config.json` or `hooks.json` in Gemini CLI's `packages/core/src` or `packages/cli/src` (grep). So Gemini CLI does not read agy's `hooks.json` or `mcp_config.json`, and agy does not read `settings.json`. That second half is **reported** in the migration guide: agy moved MCP servers out of inline `settings.json` into a separate `mcp_config.json`. Source: https://antigravity.google/docs/cli/gcli-migration (search snippet only, page blocked here).
- **One real overlap: skills.**
  - **verified:** `Storage.getUserAgentSkillsDir()` is `~/.agents/skills`, and `getProjectAgentSkillsDir()` is `<ws>/.agents/skills`.
  - **reported:** agy uses workspace `.agents/skills/` too.
  - Result: a skill placed there loads in both tools.
- **Context files.**
  - **verified:** Gemini CLI reads `GEMINI.md` by default. `context.fileName` can be set to `["AGENTS.md","CONTEXT.md","GEMINI.md"]` (`docs/cli/gemini-md.md:105`).
  - It does not read agy's `~/.gemini/config/AGENTS.md`, which is a symlink, unless someone points it there. Decision 27 (no `AGENTS.md` writes) holds for both tools.
- **Migration.**
  - **reported:** `agy plugin import gemini`, or a first-run checklist, converts extensions to "Antigravity plugins" and moves skills. The announcement itself says extensions become plugins.
  - **verified:** Gemini CLI ships a built-in skill, `packages/core/src/skills/builtin/antigravity-support/SKILL.md`, that tells users how to migrate to agy.
- **ax bug to fix.** **verified:** `ax/src/core/harness.rs:38` maps `"gemini"` to `Antigravity` in `from_str_loose`. A Gemini CLI harness would collide with that alias.

## 3. Hooks

- **verified** (`docs/hooks/reference.md`, `packages/core/src/hooks/types.ts`). There are 11 events:
  - `BeforeTool` and `AfterTool`
  - `BeforeAgent` and `AfterAgent`
  - `BeforeModel`, `AfterModel` and `BeforeToolSelection`
  - `SessionStart` and `SessionEnd`
  - `Notification`
  - `PreCompress`
- How they run:
  - JSON in on stdin, JSON out on stdout.
  - Exit code 2 blocks.
  - The only type is `command`.
  - Default timeout is 60 s.
  - `sequential` runs a group in order; the default is parallel.
  - Input includes `session_id`, `transcript_path`, `cwd` and `hook_event_name`.
  - Hooks get the env vars `GEMINI_PROJECT_DIR` and `CLAUDE_PROJECT_DIR`. The second is an alias kept for compatibility.
  - There is a `gemini hooks migrate` command (`packages/cli/src/commands/hooks/migrate.ts`). It imports Claude Code hooks; v0.67 fixed a seconds-to-milliseconds timeout conversion in it.
- **No async or rewake.**
  - **verified:** grep finds no `async` or `rewake` field in the hook types.
  - The nearest thing to a wake:
    - `AfterAgent` with `decision:"deny"` plus a `reason` makes the model run again, with the reason sent as a new prompt. That is a stop-hook continuation at the end of a turn, not an idle wake.
    - `BeforeAgent` and `SessionStart` can add `additionalContext`.
  - So Gemini CLI wakes the same way agy does: it only notices at its next turn. It has nothing like Claude's long-poll `asyncRewake`.
- **Other ways to signal.**
  - **verified:** "model steering" (experimental, off by default) lets the user steer a turn that is already running.
  - **verified:** notifications use OSC 9 and are experimental.
  - Neither one is an external injection point.

## 4. Extensions, subagents, MCP client

- **Extensions (verified, `docs/extensions/reference.md`).**
  - Each has a `gemini-extension.json` with `mcpServers`, `contextFileName` and settings.
  - Optional folders: `commands/*.toml`, `hooks/hooks.json`, `skills/` and `agents/`. Subagents in extensions are a preview feature.
  - Extensions are frozen for consumers, because agy turns them into plugins.
- **Subagents (verified, `docs/core/subagents.md`).**
  - Defined as Markdown with YAML frontmatter in `.gemini/agents/` or `~/.gemini/agents/`.
  - Each is exposed to the main agent as a tool of the same name, and can be forced with `@name`.
  - Each has its own context loop, a tool allowlist, recursion protection and its own policies.
  - Built-in subagents: `codebase_investigator`, `cli_help`, `generalist` and a browser agent.
  - A subagent's transcript is nested under `chats/<parentSessionId>/`.
  - **unverified:** whether a per-subagent `model` field exists in the schema; I did not read that section.
  - Jev could put its tier-ceiling guard in a `BeforeTool` hook matched on subagent tool names, because subagents are plain tools here.
- **MCP client (verified).**
  - Settings keys: `url`, `httpUrl`, `headers`, `env`, `trust`, `includeTools`, `excludeTools`, OAuth with automatic discovery, and Google service-account impersonation (`docs/tools/mcp-server.md`).
- **Header auth with env expansion works**, unlike agy (our A6 spike showed agy does not expand variables in headers).
  - String values in `settings.json` and `gemini-extension.json` resolve `$VAR`, `${VAR}` and `${VAR:-default}` against `process.env` when settings load (`packages/cli/src/config/settings.ts:914`, `envVarResolver.ts`, `docs/reference/configuration.md:63`).
  - `mcp-client.ts:1015` runs a second expansion on headers, against an environment with `*KEY*` and `*TOKEN*` redacted.
  - Because the first pass already expanded the value, `"Authorization":"Bearer ${A2A_API_KEY}"` should work.
    - **verified in source, not run.** If someone puts the variable only in the header and relies on the second pass alone, it would come out empty after redaction.
  - That fits decision 24: a non-secret managed line, with the key coming from the environment.

## 5. Sessions, checkpointing, resume (for ax history)

- **Storage (verified, `chatRecordingService.ts`, `storage.ts`, `docs/cli/session-management.md`).**
  - Path: `~/.gemini/tmp/<project-short-id>/chats/session-<timestamp>-<shortId>.jsonl`. Older files were `.json`; the code normalizes them to `.jsonl`.
  - The short id comes from `~/.gemini/projects.json`. The docs call it a "project_hash", but the code uses a registry short id.
  - Sessions are per project. A `general.sessionRetention.maxAge` setting controls how long they are kept.
- **Resume (verified).**
  - `gemini --resume latest|<index>|<uuid>`, `--list-sessions`, `--delete-session`.
  - `/resume` opens a browser over sessions. `/resume save|list|resume <tag>` manages named branch points.
  - Hook input carries `session_id` and `transcript_path`, so a `SessionStart` hook can register the tab-to-session mapping in ax.
- **Checkpointing (verified).**
  - Off by default; turned on with `general.checkpointing.enabled`. The `--checkpointing` flag was removed in v0.11.
  - It keeps a shadow git repo at `~/.gemini/history/<id>`, and `/restore` reverts.
- **Titles.** **unverified:** I found no session-title store like agy's `conversation_summaries.db`. ax title sync would need a new writer, or would have to skip this harness.

## 6. Headless, non-interactive policy, ACP, A2A

- **Headless (verified, `docs/cli/headless.md`).**
  - Triggered by `-p` or by running without a TTY.
  - `--output-format text|json|stream-json`.
  - Exit codes: 0, 1, and 42 for bad input.
  - `-i/--prompt-interactive` runs a prompt and then stays interactive.
- **Non-interactive policy (verified).**
  - Policy-engine rules carry an `interactive` flag. When `ask_user` would be needed in non-interactive mode, it is treated as `deny` (`docs/reference/policy-engine.md:102-117`).
  - `--approval-mode default|auto_edit|yolo|plan`.
  - In headless runs the trusted-folders gate behaves differently (`docs/cli/trusted-folders.md:103`).
- **Terms.**
  - **verified:** `docs/resources/tos-privacy.md` says: "Directly accessing the services powering Gemini CLI … using third-party software … (e.g. OpenClaw with Gemini CLI OAuth) is a violation."
  - **verified:** the FAQ says third-party tools that "harvest or piggyback on Gemini CLI's OAuth" are prohibited, names Claude Code, OpenClaw and OpenCode, and recommends a Vertex or AI Studio API key.
  - Unlike agy's FAQ, the Gemini CLI docs do not forbid wrapping `gemini` itself. Their ban is on reusing its OAuth token. With API-key or Vertex auth, scripting `gemini -p` falls under the Gemini API or Google Cloud terms, not consumer terms.
- **ACP (verified).**
  - `gemini --acp` is current; `--experimental-acp` is deprecated (`packages/cli/src/config/config.ts:370-377`). `docs/cli/cli-reference.md:59` still lists the old flag, so the docs are inconsistent.
  - Runs JSON-RPC over stdio. Gemini CLI is in the ACP registry and works with Zed and JetBrains.
  - ACP permission requests include `_meta['gemini-cli'].mcp`.
  - ACP is excluded by the coordination plan, so it is irrelevant here.
- **A2A client (verified, `docs/core/remote-agents.md`, `packages/core/src/agents/a2a-client-manager.ts`).**
  - Gemini CLI can use remote A2A agents as subagents: `kind: remote` plus `agent_card_url` or `agent_card_json`.
  - Auth types: `apiKey`, `http` (bearer), `google-credentials` and `oauth` with PKCE. Secrets resolve from `$ENV` or `!command`.
  - It uses `@a2a-js/sdk 0.3.11` with REST, JSON-RPC and gRPC transports. A shim (`a2aUtils.ts:255-285`) maps 1.0-style `supportedInterfaces` and `protocolBinding` onto the 0.3 fields.
  - No `A2A-Version` header is sent; grep finds none. Our server's strict `A2A-Version` check (decision 33) would reject it unless relaxed or bridged. **unverified on the wire.**
- **A2A server (verified, `packages/a2a-server`).**
  - npm package `@google/gemini-cli-a2a-server`, binary `gemini-cli-a2a-server`, marked "experimental".
  - Its card says `protocolVersion: '0.3.0'`, `streaming: true`, `pushNotifications: false`.
  - It has an `autoExecute` option.
  - It is a separate headless coder-agent process, not a bridge into the user's live TUI session. The changelog shows it is still maintained.

## 7. Does DDR-003 need to extend to Gemini CLI?

- **Under the user's subscription, the question is moot.** Gemini CLI cannot use AI Pro since 2026-06-18, so there is no consumer session to protect or wrap.
- **Under API-key or Code Assist Enterprise auth**, the written terms ban OAuth piggybacking, not wrapping the binary. The literal DDR-003 trigger (an FAQ that forbids wrapping) is therefore not met.
- **Still, I recommend a rule on principle.** Add a DDR-003a: "never launch, drive or wrap a Google-account-authenticated `gemini` process, and never reuse `~/.gemini/oauth_creds.json`".
  - Reason one: the Antigravity terms penalise "Antigravity and/or Gemini CLI accounts" together.
  - Reason two: the coordination rules ban one-shot relays anyway (`gemini -p`, ACP, and spawning `gemini-cli-a2a-server`).
- Peers stay independent interactive sessions, as for agy.

## Table

| feature | what it does | overlaps which project need | ADOPT / BORROW / SKIP | reason |
|---|---|---|---|---|
| Subscription login (AI Pro/Ultra) | Google OAuth to Code Assist | ax (subscription harness) | SKIP | Consumer tiers stopped 2026-06-18 [verified, Discussion #27274 and README]; they moved to agy |
| API key / Vertex / Code Assist Ent auth | Pay-as-you-go or org seat | ax custom harness | SKIP (unless the user has a seat) | Goes against the subscription-first premise |
| `~/.gemini` layout | settings.json, GEMINI.md, tmp/, agents/ | coordination installer and agy paths | BORROW (knowledge) | Same parent folder as agy, separate files; `~/.agents/skills` is loaded by both [verified] |
| Hooks (11 events, sync, exit 2 blocks) | Lifecycle, tool and model interception | coordination wake; Jev guard | BORROW | `SessionStart`/`BeforeAgent` additionalContext matches agy injectSteps; no async rewake [verified] |
| `AfterAgent` deny + reason | Re-prompts the model at turn end | coordination stub wake | BORROW (pattern) | Could carry a constant "N pending" stub; only at turn end |
| `hooks migrate` (from Claude) | Converts Claude hooks to Gemini format | coordination dual installer | BORROW | Event-mapping reference for a multi-harness installer |
| Subagents as tools (`.gemini/agents/*.md`) | Specialist agents in isolated loops | Jev subagent discovery and ceiling guard | BORROW | `BeforeTool` matcher on the subagent name equals Jev's PreToolUse guard |
| Remote A2A subagents | Calls A2A agents by card URL with bearer or `$ENV` | coordination A2A | BORROW (test client) | A2A 0.3 SDK with 1.0 card shim, no `A2A-Version` header; useful for interop tests |
| `gemini-cli-a2a-server` | Headless coder agent served over A2A 0.3 | coordination | SKIP | A spawned headless worker, not a live session; uses `autoExecute`; breaks the live-session and parked-task rules |
| MCP headers with `${VAR}` | Settings-wide env expansion | coordination MCP header auth (D24) | BORROW | Works where agy's does not; keeps the key out of chezmoi files |
| Sessions jsonl + `--resume <uuid>` | Per-project history and resume | ax history reader and restore | BORROW | Simple jsonl path with a projects.json short id; hook gives the session id |
| Checkpointing (shadow git) | Snapshot before edits, `/restore` | none directly | SKIP | Off by default; no matching need |
| Headless `-p` / stream-json | Scripted single runs | Jev free execution | SKIP | One-shot runs are banned for peers; not subscription-backed |
| ACP (`--acp`) | Editor control over JSON-RPC stdio | coordination (removed) | SKIP | ACP was dropped from the plan; it drives the agent |
| Policy engine (`interactive` flag) | Per-mode allow, deny, ask; ask becomes deny when headless | Jev quality gate | BORROW (design) | Clean model for split interactive and headless rules |

## Should Gemini CLI become an ax built-in harness or a coordination peer?

**Recommendation: not built in, and not a peer by default.**

**As an ax harness**
- Since 2026-06-18 Gemini CLI does not run on the user's Google AI Pro plan. A built-in harness would only help people with a paid API key, Vertex or a Code Assist Standard/Enterprise seat. That is pay-per-token or org-licensed, which is not the user's setup.
- Today it can run as a TOML harness (`[harnesses.gemini-cli] command="gemini"`) with no code change.
- Making it a first-class built-in would take:
  - Removing the `"gemini" => Antigravity` alias in `src/core/harness.rs:38`.
  - A history reader for `~/.gemini/tmp/<short-id>/chats/*.jsonl`, resolving the short id through `projects.json`.
  - Restore through `gemini --resume <uuid>`.
  - A `SessionStart` hook in `~/.gemini/settings.json` that reports `session_id` to ax for the tab registry.
  - Title sync stays an open question; no title store was found.

**As a coordination peer**
- Only if the user runs it with Enterprise or API-key auth.
- Identity: a separate `local-harness` handle such as `gemini-cli@the workstation/<session>`, bound in a `SessionStart` hook from `session_id`.
- Wake: works like agy's, with no async rewake.
  - A `SessionStart` or `BeforeAgent` hook returns `additionalContext`, carrying a constant stub such as "[a2a] N pending. Call a2a_inbox."
  - An optional `AfterAgent` deny can carry the same stub as a nudge at turn end.
  - Bodies arrive only through `a2a_inbox` tool results.
- MCP auth: a `"headers":{"Authorization":"Bearer ${A2A_API_KEY}"}` entry in `settings.json`. Env expansion supports this, so no literal key is needed.
- Hard limits:
  - Never use `gemini -p`, `--acp` or `gemini-cli-a2a-server` as a relay or driver.
  - Treat its built-in A2A client as a 0.3-era test client, because it does not send `A2A-Version`.
  - Add the DDR-003a rule above, so Google-account-authenticated `gemini` gets the same never-launch, never-drive treatment as agy.

**Sources**
- Gemini CLI repo, cloned 2026-11-07: https://github.com/google-gemini/gemini-cli
- Transition announcement: https://github.com/google-gemini/gemini-cli/discussions/27274
- Transition blog post (linked from the README, not fetched): https://developers.googleblog.com/an-important-update-transitioning-gemini-cli-to-antigravity-cli/
- agy migration guide (search snippet only, host blocked): https://antigravity.google/docs/cli/gcli-migration
- Migration write-up (reported): https://agentpedia.codes/blog/gemini-cli-to-antigravity-cli-migration
- Migration write-up (reported): https://inventivehq.com/blog/antigravity-cli-install-migrate-config
- Migration write-up (reported): https://www.digitalapplied.com/blog/gemini-cli-to-antigravity-cli-migration-june-18-2026-guide
- Forum threads on quota (reported):
  - https://discuss.ai.google.dev/t/usage-quotas-as-of-last-month-or-so-has-made-gemini-ai-pro-with-cli-and-antigraivity-worthless/144049
  - https://discuss.ai.google.dev/t/antigravitys-usage-quota-is-very-restrictive/146716
- ACP registry: https://agentclientprotocol.com/get-started/registry
- A2A SecurityScheme spec: https://a2a-protocol.org/latest/specification/#451-securityscheme
