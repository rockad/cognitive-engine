# Google Antigravity native features: what can be adopted for coordination, ax and cognitive-engine (2026-11-07)

## Bottom line

- **No native Antigravity feature gives an idle agy session a wake that is compliant with DDR-003 and Google's terms.** The plan's current agy path is still the right one:
  - a `SessionStart` hook that injects the handle and pending count (`injectSteps`)
  - a `PreInvocation` hook that shows the pending count before the next model call
  - the `a2a_wait` tool
  - an `a2a_wait` that the agy session starts itself as a background command, kept as the fallback
- **`--remote-control` opens a compliant path for the human only, not for a program.**
  - It is Google's own relay between the user's own session and a Google-hosted dashboard, signed in with the same Google account. It sends push notifications when an agent finishes a turn or needs input.
  - I found no documented API for it. Driving the dashboard from software would be the GUI automation that the consumer FAQ calls unsupported (reported, see the terms section).
  - What it does give: the human gets a phone channel to type "check a2a_inbox" into the live agy session. That makes the accepted rule "the human is agy's wake" work from a phone. It carries no A2A traffic.
- **Native Antigravity features do not provide a backlog or a team of peer agents.**
  - Subagents and `/teamwork-preview` are parent-child structures inside one agy process. They are not peers.
  - Task-list artifacts belong to one conversation and track that agent's own progress.
  - Automations (formerly Scheduled Tasks) start new conversations on a cron schedule. They do not reach a live one.
- **Worth adopting:**
  - the plugin as the install unit (`.agents/plugins` or `~/.gemini/config/plugins`)
  - hooks and MCP config, already planned
  - a skill shipped with the plugin
- **Ideas worth borrowing:**
  - the subagent lifecycle (Running, Idle, Killed, and "a message wakes an idle subagent")
  - the teamwork roles (an orchestrator that only dispatches, and an auditor that runs a forced final audit)
  - the human-facing Agent Manager inbox

## Limits of this pass (read first)

- **I could not fetch any official page.** `antigravity.google`, `www.antigravity.google`, `discuss.ai.google.dev`, `agentsroom.dev` and `gigazine.net` all fail DNS in this cloud session. They have to be allowed in the session's network settings before the official pages can be re-read.
- Official facts therefore come from two places:
  - **verified-in-official-docs (local):** pages read by earlier local runs. These are the audit's two passes over `antigravity.google/docs/hooks`, the FAQ and Terms read in R1 on 2026-11-03, and agy's bundled `hooks.md` read for the runbook on 2026-11-05.
  - **official-URL snippet:** a search engine's summary of an official URL that I did not open myself. Treat it as better than "reported" but below verified.
- Wire facts come from spikes A4 and A6 (verified on the wire, agy 1.5.0).
- I did not install or run anything.

## Local evidence read

- Coordination rules: `agentic-hub/plugins/coordination/AGENTS.md`
- Research and spikes, all in `agentic-hub/plugins/coordination/docs/research/`:
  - `r1-antigravity-terms-and-acp-gate-2026-11-03.md`
  - `a2a-audit-2026-11-04/t1b-wake-mechanisms.md` (agy hooks section, about lines 286 to 296 and 370 to 400)
  - `spike-runbooks-2026-11-05.md` section 0.3 (`hooks.json` nesting, plugin roots)
  - `spike-results-2026-11-04-sp2-a4.md` and `spike-results-2026-11-04-sp2-a6.md`
- Specs: `agentic-hub/plugins/coordination/docs/specs/spikes.md` (SP3 and SP4) and `agentic-hub/plugins/coordination/docs/specs/server-api.md` (sections 11.2 and 12)
- ax:
  - `ax/src/lib.rs:1222` `build_agy_args`. It forwards or appends `--remote-control` and resumes with `--conversation <id>` or `-c`.
  - `ax/src/lib.rs:2393` `ensure_hook_installed`. It writes an `ax-tab-sync` `Stop` hook with a 5 s timeout. It merges by group key, but it rewrites the whole file and silently replaces an invalid file with `{}`.
  - `ax/src/core/session.rs:180`. It reads `~/.gemini/antigravity-cli/conversation_summaries.db` directly with SQL.
  - `ax/src/config.rs`: `single_remote_control_instance` defaults to true.
- cognitive-engine:
  - `cognitive-engine/adapters/antigravity/README.md` and `src/index.ts`. A `PreToolUse` matcher on `invoke_subagent` returns `force_ask` for a Pro-class model, and the MCP entry is merged into `~/.gemini/config/mcp_config.json`.
  - `cognitive-engine/docs/plans/2026-10-26-multi-track-implementation-plan.md` section 6. Hand-off between the two harnesses goes through the bridge document `docs/handoffs/antigravity-claude-bridge.md` and memory.

## Feature by feature

### Hooks (`SessionStart`, `PreToolUse`, `PostToolUse`, `PreInvocation`, `PostInvocation`, `Stop`)

- **What is documented (verified-in-official-docs, local, audit 2026-11-04):**
  - Six events.
  - Handlers can only be `command`. The default timeout is 30 s.
  - Input is camelCase JSON: `conversationId`, `workspacePaths`, `transcriptPath`, `artifactDirectoryPath`, `modelName`.
  - `SessionStart` and `PreInvocation` return `injectSteps`, whose items can be `systemMessage`, `userMessage`, `ephemeralMessage` or `toolCall`.
  - `PostInvocation` can return `terminationBehavior: force_continue` or `terminate`.
  - `Stop` receives `fullyIdle` and can return `{"decision":"continue","reason":...}`, which re-enters the loop.
  - Hooks are loaded from `.agents/hooks.json`, `~/.gemini/config/hooks.json`, and a plugin's `hooks.json`.
  - The pages say nothing about async hooks, background hooks, rewake, or a maximum timeout.
  - An official-URL snippet in this pass confirms the same file locations and the 30 s default. I found no `async` option.
- **Nesting (verified, local bundled `hooks.md`):** the file is a map of named groups. Most events use the flat form; only the tool events use matchers. `systemMessage` needs a nested object.
- **Two new claims, both reported (third party, unverified):**
  - The crickets wiki says that on Antigravity, inject and veto from **plugin** hooks are ignored, so plugin hooks only observe ([github.com/alexherrero/crickets/wiki/Hooks](https://github.com/alexherrero/crickets/wiki/Hooks)).
  - An ithome article says external commands must be listed in a global `trusted_hooks.json`, or they are silently blocked ([ithelp.ithome.com.tw/articles/10407665](https://ithelp.ithome.com.tw/articles/10407665)).
  - Either one, if true, breaks a plugin-only install of the `SessionStart` and `PreInvocation` hooks. Add both checks to SP2 A8 and to SP3.
- **Idle wake:**
  - Only a held `Stop` hook (a `continue` after a long-poll) or a `PostInvocation` `force_continue` could keep a loop alive.
  - Both run inside agy's own session as agy's own configured hook, so they do not launch or drive agy. DDR-003 holds.
  - They do hold the user's session, which may block typing (unverified, SP4). They are also third-party code attached to the product, the grey area of plan Risk 5.
  - Keep SP4 as record-only.
- **ax interaction:** ax's `Stop` hook and the coordination hooks share the user-level `hooks.json`. ax's writer replaces an invalid file with `{}`. The coordination installer must merge entries, and so must ax.

### MCP client config

- Verified on the wire (A6): `agy mcp add --header` writes `~/.gemini/config/mcp_config.json`, and agy sends the `Authorization` header.
- Verified (A4): `_meta["antigravity.google/conversation_id"]` and `artifacts_dir` arrive with every call. That is the handle key.
- Adopt, as already planned. Note that cognitive-engine writes the same file, so both writers must merge.

### Plugins (`.agents/plugins/`, `~/.gemini/config/plugins/`)

- Official-URL snippets ([antigravity.google/docs/cli/plugins](https://antigravity.google/docs/cli/plugins), [antigravity.google/docs/build-plugins/](https://antigravity.google/docs/build-plugins/)):
  - A plugin can bundle skills, MCP servers, rules, hooks and custom agents.
  - `plugin.json` sits at the plugin root or in `.antigravity-plugin/`.
- The sources disagree, so A8 has to settle three points:
  - the MCP file name: `mcp_config.json` or `mcp.json`
  - the global root: one CLI page shows `~/.gemini/antigravity-cli/plugins/<name>/`
  - an underscore variant of the workspace folder: `_agents/plugins/`
- Adopting a plugin as the agy install unit makes decisions 26 to 28 cleaner. One folder holds the MCP entry, the hooks and the skill, and no `AGENTS.md` is written.
- It is a good fit for cognitive-engine's antigravity adapter too. That adapter currently prints a guard entry that you merge by hand.

### Skills and workflows

- Skills (official-URL snippet): a folder with `SKILL.md` under `.agents/skills`. The global path differs between sources (reported).
- Workflows: slash-invoked markdown files, at most 12,000 characters each, and one workflow can call another ([antigravity.google/docs/ide/workflows](https://www.antigravity.google/docs/ide/workflows), official-URL snippet).
- Adopt a skill named something like `a2a-inbox` inside the plugin. It tells agy how to read the inbox and how to treat bodies as untrusted data.
- A workflow such as `/a2a` gives the human a one-word way to wake agy, from the keyboard or from the Remote Control dashboard. The snippets do not list workflows among the things a plugin can bundle, so where it would live is unverified.

### Rules and `AGENTS.md`

- Native. `GEMINI.md` and `AGENTS.md` precedence is not published (reported, [thepromptshelf.dev](https://thepromptshelf.dev/blog/google-antigravity-agents-md-rules-guide-2026)).
- Skip for coordination: decision 27 forbids writing any `AGENTS.md`, and `~/.gemini/config/AGENTS.md` is a symlink into another repository. Plugin-bundled rules are an alternative that writes no `AGENTS.md`. Raise it as an option for the user to decide; it is not adopted.

### Subagents (`invoke_subagent`, `define_subagent`, `/agents` panel)

- Official-URL snippets ([antigravity.google/docs/subagents?tab=cli](https://antigravity.google/docs/subagents?tab=cli)):
  - Subagents run asynchronously and can be nested.
  - States are Running, Idle and Killed. "Sending a message to an idle subagent wakes it up again."
  - Custom subagents are markdown files with frontmatter. The frontmatter has a `subagent` flag and `model: inherit|flash|pro`.
  - `define_subagent` creates a subagent during a session.
  - Approvals from a subagent bubble up to the parent.
- I found no documentation of a tool named `manage_subagents` (unverified name).
- This is a parent-child structure inside one agy, so it cannot serve as a peer transport (symmetric agency).
- Borrow the lifecycle states and the rule that a message wakes an Idle session as the model for A2A session liveness.
- **Gap for cognitive-engine (unverified):** the guard matches only `invoke_subagent`. A subagent created with `define_subagent` that has `model: pro` in its frontmatter, then invoked without a `Model` argument, may skip the `force_ask`. Test this and consider adding a `define_subagent` matcher.
- Jev looks for agy agents in `.agents/agents/`. Check that against the official location for custom agents, which the snippets did not give.

### `/teamwork-preview` and Agent Manager (2.0 desktop app)

- Official-URL snippets plus third-party reports:
  - A dynamic team with Orchestrator (dispatch only), Explorer, Worker, Reviewer, Critic, Auditor and Sentinel roles. A final audit is forced before the user is notified.
  - It is a preview. Plan access is reported inconsistently: Ultra only, or all paid plans.
  - Subagents get worktree isolation ([antigravity.google/blog/google-io-2026-feature-deep-dive](https://www.antigravity.google/blog/google-io-2026-feature-deep-dive)).
- Agent Manager has an inbox for **the human**: it shows threads and agents blocked waiting for approval (reported, [aifire.co](https://www.aifire.co/p/google-antigravity-review-the-ai-coding-tool-that-orchestrates-agents)). It is not an inbox between agents, and I found no API for it.
- Borrow ideas for Jev's `batch_dispatch`:
  - a dispatcher that never writes code
  - a mandatory auditor or `jev_judge` before the workstream is reported done
  - worktree isolation per worker, which matches plan section 6.2
- Skip adopting it: it is single-harness, hierarchical, a preview, and tied to a plan tier.

### Artifacts (task list, implementation plan, walkthrough)

- Official blog: these are deliverables, and the user can comment on them ([developers.googleblog.com](https://developers.googleblog.com/build-with-google-antigravity-our-new-agentic-development-platform/)).
- Verified on the wire: they live under `~/.gemini/antigravity-cli/brain/<conversation_id>`.
- The task list is the agent's own progress tracker for one conversation. It has no dependencies and no "ready work" query.
- Borrow:
  - the plan-then-approve pattern for `TASK_STATE_INPUT_REQUIRED` hand-offs
  - letting an A2A task carry `artifacts_dir` as metadata, so a peer's human can open the plan
- The server must never read the brain directory: forum posts say polling local transcripts is restricted (reported), and transcripts are on the never-expose list.
- This does not close the backlog gap.

### Automations (formerly Scheduled Tasks)

- Official-URL snippets:
  - Cron-triggered. "Time-based triggers to start conversations periodically."
  - Fixed to a Flash model (3.5 or 3.8 depending on the page).
  - Has a "Run Now" action ([antigravity.google/docs/changelog](https://antigravity.google/docs/changelog?tab=ide), [antigravity.google/product/antigravity-2](https://antigravity.google/product/antigravity-2)).
- Each run is a **new** conversation, inferred from the docs wording (not stated outright). That breaks "live persistent sessions only".
- An automation that reads the inbox and then acts would come close to auto-executing parked tasks, or to an agent pre-screen that decision 19 forbids.
- Skip as a wake. A read-only automation that only reports the pending count to the human is possible, but it costs quota and the `PreInvocation` hook already covers it.

### `--remote-control`, `/remote-control`, `agy remote-control start|status|stop`

- Official-URL snippet ([antigravity.google/docs/remote-control](https://antigravity.google/docs/remote-control?tab=cli)):
  - Interactive mode uses the `--remote-control` flag or `/remote-control`.
  - The headless daemon is installed with `agy remote-control start`.
  - You reach it through the instance switcher on antigravity.google.com.
- Reported, from third-party sources ([agentsroom.dev](https://agentsroom.dev/blog/antigravity-remote-control-from-your-phone), [productcompass.pm](https://www.productcompass.pm/p/antigravity-remote-control), [sakutto.ai](https://sakutto.ai/en/articles/antigravity-remote-control), [ye-yu.com](https://ye-yu.com/en/blog/antigravity-cli-headless-remote-control/)):
  - One daemon per machine. This matches ax's `single_remote_control_instance`.
  - Settings can only be changed on the host machine.
  - The dashboard can start conversations and send prompts.
  - Launched 2026-08-21.
- Official blog snippet: notifications when agents finish a turn or need input ([antigravity.google/blog/remote-control-for-antigravity](https://antigravity.google/blog/remote-control-for-antigravity)).
- **Is it compliant? Yes, as a human channel.** It is Google's feature, used on the user's own account. ax passing the flag at launch only enables a Google feature in a session the user started.
- **It is not a machine path.**
  - No API is documented.
  - The `/r/` conversation link and the dashboard are for a person.
  - Scripting them (Playwright, or the third-party "Antigravity Link" or "Antigravity Automation" extensions) is the GUI automation that the forum summary of the FAQ calls unsupported and liable to suspension (reported). An unanswered forum thread on exactly this question exists ([188220](https://discuss.ai.google.dev/t/supported-automation-into-an-existing-antigravity-conversation-on-a-consumer-plan/188220)).
  - Any server-to-dashboard bridge would also break DDR-003 ("drive").
- It does not notify on external events, so an A2A arrival does not reach the phone unless something else tells the human.

### `agy agentapi`

- An undocumented subcommand (reported, [hermes-agent docs](https://hermes-agent.nousresearch.com/docs/user-guide/skills/optional/autonomous-ai-agents/autonomous-ai-agents-antigravity-cli)).
- zb-ss uses `agentapi send-message` with a CSRF token harvested from the session.
- Skip. It is explicitly rejected by DDR-003 and the audit.

### `conversation_summaries.db`

- An undocumented internal SQLite database. ax reads it, read-only, to map tabs to conversations.
- For coordination: skip, and never read it. The handle comes from `_meta.conversation_id` and the hook stdin.
- For ax: there is no official alternative; keep it fail-soft. The forum's "polling local transcripts is restricted" note is a reported risk for a summary database that is read regularly.

### Browser agent

- Not relevant to any of the three projects' coordination needs. Skip.

### Headless `agy -p`, `--print`, ACP wrappers

- Prohibited by DDR-003 and by FAQ entry 2 (verified-in-official-docs, R1, 2026-11-03). Skip.

## Terms

- **Verified-in-official-docs, R1, 2026-11-03:**
  - Additional Terms item 6: third-party software accessing the Service is a breach.
  - FAQ entry 1 names Claude Code.
  - FAQ entry 2: `agy -p` is for direct scripting and CI. Wrapping agy in third-party agents is unsupported on consumer plans.
- **Reported (forum summary 2026-10/11):** GUI or computer-use automation that sends tasks from Codex or Claude into an Antigravity conversation is unsupported and can lead to suspension. For integrations, use a Gemini Enterprise or AI Studio API key.
- **Still open:** whether our own `SessionStart` and `PreInvocation` hooks plus an MCP entry count as "third-party software accessing" the service (plan Risk 5). Nothing in this pass settles it. The pages are undated, so re-read the live FAQ before S1.

## Answers to the per-feature question

Can a feature give agy an idle wake, an inbox, a backlog or team orchestration within DDR-003 and the terms?

- **Idle wake:** no documented option.
  - The closest are a held `Stop` `continue` and `PostInvocation` `force_continue`. Both are agy's own hooks and remain spike-only (SP4).
  - Also open: whether a background command that agy started itself (2.0 says long-running operations are offloaded to background processes) restarts the loop when it finishes. That is SP4 K6, unverified.
  - Remote Control gives the **human** a phone wake.
- **Task inbox:** only through our MCP tools, surfaced by the `SessionStart` and `PreInvocation` hooks. The native inbox is for humans.
- **Backlog:** nothing native. Task lists and plans belong to one conversation. Track this gap on the coordination side.
- **Team orchestration:** native, but only inside one agy (subagents, teamwork). Borrow the patterns; it is not a peer mechanism.

| feature | what it does | overlaps which project need | ADOPT / BORROW / SKIP | reason |
|---|---|---|---|---|
| Hooks `SessionStart` and `PreInvocation` `injectSteps` | inject steps at conversation start or before each model call | coordination: agy handle and inbox surfacing (decisions 9 and 27) | ADOPT | verified-in-official-docs (local); agy's own hook in its own session; already planned (SP3) |
| Hook `Stop` `continue` and `PostInvocation` `force_continue` | re-enter the loop after the agent stops | coordination: agy idle wake | BORROW (spike only) | may hold the user's session; maximum timeout and typing behaviour undocumented; SP4 record-only |
| Hook `PreToolUse` matcher | veto or ask before a tool call | cognitive-engine: tier-ceiling guard on `invoke_subagent` | ADOPT (in use); check `define_subagent` | guard may miss a Pro model set in `define_subagent` frontmatter (unverified) |
| Plugin bundle (`plugin.json`, `hooks.json`, MCP file, skills) | one folder installs MCP, hooks, skills, rules, agents | coordination installer (decisions 26 to 28); cognitive-engine adapter | ADOPT, gated on SP2 A8 | removes hand merges and needs no `AGENTS.md`; path and MCP file name disagree between sources; plugin hooks reported observe-only and `trusted_hooks.json` reported, both unverified |
| MCP client (`mcp_config.json`, `--header`, `_meta.conversation_id`) | agy calls HTTP MCP servers with a bearer header | coordination `/mcp`; Jev MCP | ADOPT | verified on the wire (A4, A6) |
| Skills (`.agents/skills`, `SKILL.md`) | on-demand instructions | coordination: inbox handling and untrusted-data framing | ADOPT (inside the plugin) | native and passive; no `AGENTS.md` write |
| Workflows (slash markdown) | stored prompt chains invoked with `/name` | coordination: one-word human wake `/a2a`; cognitive-engine hand-off briefs | BORROW | useful for the human; whether a plugin can ship one is unverified |
| Rules / `AGENTS.md` / `GEMINI.md` | always-on instructions | coordination inbox instruction | SKIP | decision 27; symlinked file in another repository |
| Subagents (`invoke_subagent`, `define_subagent`, `/agents`) | async child agents; Idle until messaged | cognitive-engine dispatch; coordination liveness model | BORROW | parent-child inside one process, not peers; lifecycle states make a good A2A session model |
| `/teamwork-preview` / Agent Manager | orchestrated role team; human inbox of threads | Jev `batch_dispatch` DAG; cognitive-engine section 6 | BORROW (dispatch-only orchestrator, forced auditor, worktree per worker) | preview, plan-gated, single harness, hierarchical; inbox is human-facing with no API |
| Artifacts (task list, implementation plan, walkthrough) | per-conversation plan, progress, proof | coordination backlog gap; `INPUT_REQUIRED` review | BORROW (plan-approve pattern; `artifacts_dir` as task metadata) | no dependencies or ready queue; server must not read the brain directory |
| Automations (Scheduled Tasks) | cron-started new conversations on Flash | agy wake; periodic inbox check | SKIP | new conversation, not the live session; risk of auto-execution or pre-screen (decision 19) |
| `--remote-control`, `agy remote-control` daemon | Google-relayed browser and phone control plus turn notifications | human wake for agy; ax tab badge | ADOPT for the human only (ax already passes the flag) | compliant as Google's own feature on own account; no API; automating it is unsupported GUI automation and breaks DDR-003 |
| `agy agentapi send-message` | undocumented local session control | agy wake (zb-ss) | SKIP | DDR-003; undocumented; CSRF harvesting |
| `conversation_summaries.db` | internal conversation index | ax tab-to-conversation registry; coordination identity | SKIP for coordination; ax keep fail-soft | undocumented; transcript polling reported restricted; `_meta` and hook stdin give the id |
| Headless `agy -p` / ACP wrappers | one-shot or wrapped runs | relays between harnesses | SKIP | DDR-003; FAQ entry 2 (verified-in-official-docs, R1) |
| Browser agent | web automation by agy | none | SKIP | out of scope |

Sources:
- [antigravity.google/docs/hooks](https://antigravity.google/docs/hooks)
- [antigravity.google/docs/subagents?tab=cli](https://antigravity.google/docs/subagents?tab=cli)
- [antigravity.google/docs/ide/workflows](https://www.antigravity.google/docs/ide/workflows)
- [antigravity.google/docs/cli/plugins](https://antigravity.google/docs/cli/plugins)
- [antigravity.google/docs/build-plugins/](https://antigravity.google/docs/build-plugins/)
- [antigravity.google/docs/remote-control](https://antigravity.google/docs/remote-control/)
- [antigravity.google/blog/remote-control-for-antigravity](https://antigravity.google/blog/remote-control-for-antigravity)
- [antigravity.google/docs/changelog](https://antigravity.google/docs/changelog?tab=ide)
- [antigravity.google/product/antigravity-2](https://antigravity.google/product/antigravity-2)
- [antigravity.google/blog/google-io-2026-feature-deep-dive](https://www.antigravity.google/blog/google-io-2026-feature-deep-dive)
- [antigravity.google/docs/faq](https://antigravity.google/docs/faq)
- [antigravity.google/terms](https://www.antigravity.google/terms)
- [developers.googleblog.com: Build with Google Antigravity](https://developers.googleblog.com/build-with-google-antigravity-our-new-agentic-development-platform/)
- [discuss.ai.google.dev thread 188220](https://discuss.ai.google.dev/t/supported-automation-into-an-existing-antigravity-conversation-on-a-consumer-plan/188220)
- [agentsroom.dev: Antigravity Remote Control](https://agentsroom.dev/blog/antigravity-remote-control-from-your-phone)
- [productcompass.pm: Antigravity Remote Control](https://www.productcompass.pm/p/antigravity-remote-control)
- [sakutto.ai: Antigravity Remote Control](https://sakutto.ai/en/articles/antigravity-remote-control)
- [ye-yu.com: Headless Remote Control in Antigravity CLI](https://ye-yu.com/en/blog/antigravity-cli-headless-remote-control/)
- [github.com/alexherrero/crickets/wiki/Hooks](https://github.com/alexherrero/crickets/wiki/Hooks)
- [ithelp.ithome.com.tw/articles/10407665](https://ithelp.ithome.com.tw/articles/10407665)
- [hermes-agent docs: Antigravity CLI](https://hermes-agent.nousresearch.com/docs/user-guide/skills/optional/autonomous-ai-agents/autonomous-ai-agents-antigravity-cli)
- [aifire.co: Google Antigravity review](https://www.aifire.co/p/google-antigravity-review-the-ai-coding-tool-that-orchestrates-agents)
- [thepromptshelf.dev: Antigravity AGENTS.md rules guide](https://thepromptshelf.dev/blog/google-antigravity-agents-md-rules-guide-2026)
