# @cognitive-engine/jev

System 1 coprocessor for Cognitive Engine (RFC 002). A Bun/TypeScript MCP server that gives agent harnesses fast, calibrated routing decisions, quality gates and free-tier offloading. MIT licensed.

## Tools

| Group | Tools |
| --- | --- |
| Routing and dispatch | `jev_dispatch`, `jev_batch_dispatch`, `jev_route`, `jev_select_model` |
| Quality gates | `jev_judge`, `jev_score` |
| Mode and quota | `jev_set_mode`, `jev_get_mode`, `jev_quota` |
| Free-tier offloading | `jev_execute_free`, `jev_execute_free_guarded`, `jev_agent_free` |

## Transports

- **stdio (default)**: `bun run src/index.ts`. One process per harness; this is what the adapters register.
- **Streamable HTTP**: `bun run src/index.ts --http` (or `JEV_TRANSPORT=http`) serves `http://127.0.0.1:8115/mcp`. Override the port with `JEV_HTTP_PORT`. The server is stateless, binds loopback only, and rejects requests whose `Host` header is not loopback on the bound port. `engine up` supervises this mode.

## Invariants

- **ZDR**: under `zdr: strict`, `openrouter/free` offloading is refused, and every outbound request is gated by the egress policy in `~/.config/jev/egress.json`.
- **Tier ceiling**: subagent tiers are clamped to the parent session's model; Pro tier requires manual user approval, enforced by the PreToolUse guard.

## Configuration

`~/.config/jev/config.json` is the single store for the global default mode (`defaults.mode`) and per-harness overrides (`harnesses.<harness>.mode`). The OpenRouter key is read from `JEV_OPENROUTER_API_KEY`, a dedicated key separate from `OPENROUTER_API_KEY`.

The expected shape is `{"defaults":{"mode":"on-demand"}}`, with `mode` one of `off`, `on-demand`, `on`. `validateJevConfig` (`src/core/config.ts`) is the single check for whether that file can be trusted. A missing file (no entry at the default path), `{}`, or a file with no mode set is unset and takes the loader default (`off`). A file that exists but is unusable is malformed:

- unreadable, not a regular file, or a symlink whose target is missing or unreadable ("broken symlink"); a symlink to a valid file is fine
- over the 1 MB limit ("config is over the 1 MB limit"; exactly 1 MB is accepted), checked by `stat` before reading and again on the bytes read
- not valid JSON, not an object, an unknown or non-string mode, or a wrong type for any key the loader reads
- a stray top-level `mode` (it is ignored; the mode lives at `defaults.mode`)
- a near-miss key that the loader would silently ignore, with a did-you-mean hint: at the top level `default`, `defualts`, `Default`, `harness`, `timeout` and the like; under `harnesses`, `claude`, `antigravty`, `opencod`, `Claude-Code` and the like
- a `mode` inside a `harnesses` block whose name matches no known harness (`antigravity`, `claude-code`, `opencode`, `cli`): a mode that applies to nothing
- `JEV_CONFIG_FILE` set to a path that does not exist (explicitly pointing at nothing is a mistake; with the variable unset a missing default file is just unset)

The near-miss rule: a key that is not exactly a known key is flagged when, compared case-insensitively, it is an explicit alias (`default`, `harness`, `claude`, `claudecode`, `gemini`, `agy`) or within edit distance 2 of a known key (1 when the known key has 5 characters or fewer, so a short unrelated name such as `api` is not taken for `cli`; a swap of two adjacent letters counts as 1). Any other extra key is never flagged, since other tools may add their own.

With no config file the mode resolves to `off` and the guard is silent, so a fresh install with the hook registered is unprotected until you run `/jev on` (or `/jev on-demand`); explicit `off` is silent by decision. A malformed config is neither on nor off: the subagent guard asks on every guarded call until the file is fixed or `off` is set explicitly, and `jev_get_mode` returns a `configWarning` with the same problems. The guard reads the file once, derives the mode from the object it validated (`resolveJevModeFromConfig`, the same function the loader uses), and cross-checks the loader; a mismatch, or a file that stops being readable between the two reads, asks instead of falling back to `off`.

Harness detection reads `JEV_HARNESS` first (`antigravity`, `claude-code`, `opencode`, `cli`), then falls back to each harness's own environment markers.

## Development

```sh
bun test       # unit and integration tests
bun run lint   # tsc --noEmit
bun run build  # bundle to dist/
```

Set `JEV_LIVE_TEST=1` to include tests that call OpenRouter.

## Lineage

Ported from `agentic-hub/plugins/jev` v2.3.0; version 2.4.0 adds the Claude prompt advisor and the case-study improvements (see [CHANGELOG.md](CHANGELOG.md)). Track 1 kept only the subagent-ceiling check (see below) from the Claude Code hooks; the session-start and model-switch recorders followed with the guard, and the `UserPromptSubmit` advisor is ported in [Prompt advisor hook](#prompt-advisor-hook-claude-code). Hooks are not prohibited: add one wherever a workflow or process needs it.

## Subagent guard hook

`bun src/hook.ts claude|antigravity` is a narrow `PreToolUse` guard (`src/guard.ts`). Claude Code asks before a subagent runs on a model above the session's (version-aware ranking, `src/models.ts`); Antigravity asks before any Pro-class subagent. It is the only enforcement point (`jev_dispatch` and its clamp are advice). For a call with no `model`, the model comes from the subagent's definition, looked up from the hook cwd up to the project root (`CLAUDE_PROJECT_DIR`, else the git root), then `~/.claude/agents`, then installed plugins (`plugin:agent` names look only in that plugin); a subagent type that is not a Claude Code built-in (`general-purpose`, `Explore`, `Plan`, `statusline-setup`, `claude-code-guide`) and has no definition found asks, as the Workflow guard does. It is deterministic, makes no network calls, and runs in Jev modes `on` and `on-demand` (not `off`); once active, it asks rather than passing silently when it cannot evaluate a call. A malformed Jev config fails loudly: see Configuration.

### Project config: tighten only

A repository can carry `.agents/jev.json` and `.jev.json`. The guard hook reads EVERY such file, not just the nearest, so a nested file can never cancel an ancestor's ceiling. **Where the search stops:** it does not stop at the project root or the git root. It walks from the hook cwd up to the filesystem root (so a ceiling in a parent workspace such as `~/projects/.jev.json` counts, and a planted `sub/.git` cannot cut it off); it also walks up from `CLAUDE_PROJECT_DIR` when that is set; and when the project root (`CLAUDE_PROJECT_DIR`, else the git root) is a linked git worktree (its `.git` is a file, as in `.claude/worktrees/X`), it also reads the main checkout the `gitdir:` pointer leads to (and walks up from there), so a ceiling kept in the main checkout applies in its worktrees. Both names in one directory count. Nothing is read from anywhere else. The lowest valid ceiling and the highest mode across all of them win: the lowest valid `guard.ceiling` and the highest mode across the files win. It reads them for one purpose: to tighten the user config. It can never loosen it (`src/project-config.ts`).

```json
{ "guard": { "ceiling": "sonnet" }, "defaults": { "mode": "on" } }
```

Precedence, in full:

- **User mode `off` wins.** With the user's mode `off` (explicit, or no config file, the fresh-install default) the guard is silent and the project file is not read at all, so a repository cannot switch the guard on for a user who turned it off, and a broken project file cannot make it noisy.
- **`guard.ceiling`** (a model Jev can rank) lowers the model the guard compares requests against: the LOWER of the session model and the ceiling. A session on opus with a `sonnet` ceiling asks before a subagent runs on opus. A ceiling above the session model changes nothing. An unknown session model still asks. It applies to Agent/Task calls and to Workflow literals, and also to subagents that INHERIT the session model: a call with no `model`, `inherit`, a built-in type, or a definition without a model (and a Workflow `agent()` with no model, or an `agentType` whose definition has none) is ranked as the real session model against the ceiling, so on opus with a `sonnet` ceiling it asks. With a ceiling set, a session model that is unknown or that Jev cannot rank also asks for an inheriting subagent, since it cannot be compared with the ceiling. The Antigravity Pro rule has no model ceiling and ignores it. The ask text names the ceiling and the file, and the audit record carries `projectCeiling` (its `sessionModel` is the effective, lowered one).
- **`defaults.mode` / `harnesses.<harness>.mode`** can keep or raise the user's mode (`off` < `on-demand` < `on`) and never lower it; a project `off` is ignored. For the guard `on` and `on-demand` act alike, so this only shows in the audit (`mode`). The MCP tools are unchanged: `jev_get_mode` and the loader still take no mode from a project file (`sanitizeProjectConfig`).
- **A project file that cannot be trusted asks on every guarded call**, naming the file and the problems, like a malformed user config: unreadable, not a regular file, a dangling symlink, over 1 MB, not valid JSON, not an object, `defaults`, `harnesses`, `harnesses.<h>` or `guard` that is not an object, a mode that is not `off`/`on-demand`/`on`, a `guard.ceiling` that is not a string or not a model Jev can rank. One malformed file anywhere the search reads (see above: up to the filesystem root, plus the main checkout of a linked worktree) is enough (the nearest malformed one is named). Every other key is ignored and never makes the file malformed (the loader's own project keys, such as `timeouts`, are validated by the loader, not here).

### Agent definition lookup

Both guards (the Agent/Task guard and the Workflow guard's `agentType`) read a subagent's `model:` from its definition file, nearest first: project `.claude/agents` from the hook cwd up to the project root, `~/.claude/agents`, then installed plugins. The FIRST definition file found decides, as in Claude Code (which uses only the highest-priority definition): a nearer file without a model means "inherit" and is never skipped for a farther file that names one. The one exception is within the project: Claude Code loads project agents from the project root, so a subdirectory's `.claude/agents/x.md` must not shadow the root's for the guard alone. Every project-level definition of the name is read, and when they disagree on the model (including none against one, or an unreadable one) the guards ask and name the files; when they agree, that model is used. The user-level and plugin definitions are shadowed by any project one, as before. Plugins are searched in this order: those enabled per `enabledPlugins` (`~/.claude/settings.json`, then the project's `.claude/settings.json` and `settings.local.json`, later layers winning, only a literal `true` enables), then every other installed plugin, each group in manifest order. This only orders the search: a disabled or unlisted plugin's definition is still found, so a lookup never finds fewer definitions than a plain search of all. Unreadable settings leave the manifest order.

**Agents are matched on their declared `name:`, not on the file name.** Claude Code registers each agent under the frontmatter `name:` and loads every `.md` file of the agents directories, subdirectories included. Jev reads the same way: in each directory it scans every `*.md` file (recursively, up to 2000 files and 8 levels) and matches on the parsed `name:` (read by the strict parser and cross-checked with Claude Code's pipeline, like the model). A file called `<name>.md` with no `name:` at all still counts under its file name. Directory order and the first-directory-wins rule are unchanged. The guards ask (a conflict, reported as an unrankable model that names the files) when:

- more than one file in one agents directory claims the name (an agreeing model does not help: which one runs is not known);
- a file called `<name>.md` declares a different `name:`;
- a file called `<name>.md` has a `name:` that cannot be read;
- another file's `name:` cannot be read and could be this agent (Claude Code's own reading gives the agent, no YAML parser is available to say, or the text mentions the agent);
- a tree has more files or levels than Jev reads.

#### Frontmatter is read strictly

No YAML library is a dependency, so `src/agent-frontmatter.ts` is a small strict parser for one question: what does the top-level `model` key hold? A UTF-8 BOM, CRLF and CR line endings, `"model":` / `'model':` quoted keys, `model :`, trailing comments and quoted values are all read. A frontmatter block that is present but whose model cannot be established makes the guard ASK, never reads as "no model": a `model` that is a sequence, mapping, flow value, block scalar, anchor, alias or tag; flow-style (`{model: opus}`) or non-mapping blocks; a duplicate `model` key; a key that only differs from `model` by case or Unicode form (`Model`, full-width letters); a merge key; an escape sequence in the key or value; an unterminated block or quoted value; a delimiter preceded by whitespace; a Unicode line separator. The ask names the problem (the model is reported as `<unreadable agent frontmatter: ...>`, which Jev cannot rank). A file with no frontmatter, or a fully understood block without a `model` (or an empty one), inherits the session model. `tests/agent-frontmatter.test.ts` is table-driven over every shape.

**Jev's reader is checked against Claude Code's own.** Claude Code finds the block with `/^---\s*\n([\s\S]*?)---\s*\n?/`, parses it with `Bun.YAML.parse`, and when that throws it double-quotes every top-level `key: value` line whose value holds one of `` {}[]*&#!|>%@` `` or `: ` and parses again (2.1.312). That fallback can turn a quoted or flow value that runs over several lines into several top-level keys (`description: "a#b` / `model: opus` / `z: c"` / `bad: x: y` gives Claude Code `model: opus` while a strict reader sees one description). So the model is read twice: by the strict parser, and by a copy of that pipeline (`parseFrontmatterModel`, run under `Bun.YAML`, which the hooks and the MCP server always run under). The result is `unreadable` (the guard asks) when the two disagree on the model, when Claude Code's parser would throw on both attempts, or when Claude Code would read `model` as something other than a string. Independently, a quoted or flow value of ANY key other than `model` that spans several lines is `unreadable`. If no YAML parser exists (not under bun), only the strict reader runs, and a block with a line the fallback could re-quote is `unreadable`. The copy follows the 2.1.312 binary and Bun's own YAML parser may differ from Claude Code's embedded one in corner cases; `tests/agent-frontmatter-differential.test.ts` holds the reviewer's inputs and variants.

When no definition is found both guards ask, in the same words: the agent is named, with the sentence "no definition for it could be found, so the model it will run on cannot be checked against the session ceiling" and every location searched. A Workflow `agentType` that names a Claude Code built-in (`Explore`, `Plan`, ...) still asks, since the Workflow guard has no built-in list; the Agent/Task guard passes built-ins.

### Workflow guard: what counts as provably harmless

The Workflow hook parses the script with acorn and asks for anything it cannot read as a plain `agent()` call. Two shapes that used to ask although they were harmless are now followed (`src/workflow-guard.ts`):

- **`const` bindings.** A name with exactly one binding in the whole script (no shadowing, parameter, assignment or redeclaration), declared `const`, in scope at the use, and initialised with a string literal or a flat object of string literals, is a known string. The object must only ever be read (`cfg.a`, `cfg[key]`): never assigned into, deleted from, destructured, called, aliased, spread or passed. `model: M`, `{model}`, `model: cfg[key]`, a computed key `{[k]: v}`, a computed member `o[k]` and `workflow(W)` then behave as if the literal were written there. The value is not trusted, only followed: the model is still ranked against the session, and a key that spells `constructor`, `__proto__`, `agent` and so on still asks.
- **`this.member` in a class body.** In a method, constructor, field initialiser, static block or function nested there, `this` is allowed only as the object of a non-computed member access whose name is neither watched nor an escape: `this.x`, `this.x = v`, `this.x()`. Strict mode only stops the implicit substitution of the global object, and a caller can still pass it (`C.run.call(globalThis)`, a host callback), so bare `this` flowing as a value (an argument, spread, return, assignment right-hand side, field value, computed member object, `typeof this`, `for ... in this`) asks. A computed key and an `extends` clause use the outer `this` and still ask.

Not followed, still ask: `let`/`var`, parameters, destructuring, call results, templates with `${}`, concatenation, spreads (arguments, options, objects), computed keys from data, options held in a variable, aliasing of `agent`/`workflow`, `eval`, `Function`, dynamic `import()`, `globalThis` and the rest of the escape set, and `this` outside a class body or as a bare value in one. The tests are table-driven (`tests/workflow-guard-literals.test.ts`, `tests/workflow-guard-hardening.test.ts`, `tests/workflow-guard-this.test.ts`); every bypass shape from the earlier reviews is in an ask table.

### Workflow guard: the script can change after it is analysed

The hook analyses the script text it reads at `PreToolUse`; Claude Code reads the file again to run it. What the hook can and cannot see (`src/workflow-pin.ts`):

- **Every call is re-analysed from the current content.** A `scriptPath` (or saved `name`, or a nested `workflow()` file) that was edited since an earlier call is judged on what it says now, never on an earlier verdict. A resume (`resumeFromRunId`) is analysed from the file like any other call. There is deliberately no extra "content differs from the last approved hash" prompt: it would ask after every edit of a script that is still clean.
- **Every decision is bound to the text it analysed.** The ask text lists each analysed file with `sha256:<first 16 hex>` and its size, the `ask` audit record carries the full hashes (never the text), and the hook writes a pin (`~/.config/jev/claude_sessions/workflow-pins/<tool_use_id>.json`, hashes only) for both allows and asks.
- **Drift is reported after the run.** `PostToolUse` (matcher `Workflow`, `record-hook.ts PostToolUse`) re-hashes the files named in the pin. A file that changed or disappeared writes a `workflow_changed` audit event (both hashes) and prints a `systemMessage` for the user; a clean run prints nothing and consumes the pin.
- **Silent when Jev is off.** With mode `off` (explicit, or no config file) the hook returns before it evaluates anything, so no pin exists and the recorder does nothing. A malformed config asks before any analysis and writes no pin. The recorder also reads the CURRENT user-config mode: a pin written while Jev was on is consumed with no stdout and no warning if Jev was switched off (`/jev off`) before the run ended (status `off`), but any drift is still written to the audit (`workflow_changed`), so a model that calls `jev_set_mode off` between the two hooks cannot erase the evidence of a decision made while Jev was on. A config that cannot be trusted is not off, so drift is still reported then. The `SessionStart` resume marker and pin pruning still run with Jev off; they only write files and print nothing, and the resume marker has to stay accurate if Jev is switched on later in the session.

Cannot see or do: hold the file still between the analysis and Claude Code's read (the window is not closed, only audited); a file swapped and restored inside the window; an inline `script` (hashed, but it has no file to re-check); what Claude Code actually runs on a `resumeFromRunId` (the file or a saved copy of the earlier run) is not observable from the hook. The drift report is detection, not prevention: by then the run has happened.

### Session model (Claude Code)

The ceiling is the session's model, which Claude Code does not put in any `PreToolUse` payload. The guard builds it from two pieces of evidence and orders them by time (`resolveSessionModel`, `src/session-model.ts`):

- the state file the `PostModelSwitch` recorder writes (`~/.config/jev/claude_sessions/<session_id>.json`, dated by `updatedAt`)
- the model of the latest assistant turn in the transcript (dated by the entry's `timestamp`, else the file's mtime)

The model can change with no `PostModelSwitch` event: a session resumed with another `--model`, or a switch the recorder missed. So:

- `SessionStart` with `source: resume` writes a resume marker (`<session_id>.resumed.json`). Evidence dated before the last resume is discarded, because it describes a model the session may no longer run on. `startup` and `clear` drop the state and the marker. A marker that cannot be read counts as "resumed just now" (all evidence discarded).
- When the state file and the transcript turn name different models, the later one wins. If they cannot be ordered (same instant) the model is unknown rather than guessed.
- With no usable evidence the model is unknown and the guard asks. After a resume this means the first subagent call asks until the first assistant turn of the resumed session lands in the transcript (or a `PostModelSwitch` fires), which is the point where the model is known again.

Limits: this depends on the `SessionStart` recorder being registered (it is, in `adapters/claude/hooks/hooks.json`); without it a resume cannot be seen. The transcript is only read from its last 256 KiB.

## Prompt advisor hook (Claude Code)

`bun src/advisor-hook.ts user-prompt-submit` is the `UserPromptSubmit` hook (`src/advisor-hook.ts`, logic in `src/advisor-logic.ts`), ported from `agentic-hub/plugins/jev/claude/`. In Jev mode `on` it asks Jev for a subagent, tier and effort for the prompt and injects one `[JEV]` line as `additionalContext`.

- **Advice only.** It never asks, denies or blocks; the `PreToolUse` guard above stays the only enforcement point. The agentic-hub `PreToolUse` check was not ported because the guard already does it (with a version-aware ranking and a project ceiling).
- **Fail-safe.** Mode other than `on`, a prompt under 8 characters, an egress denial, a Jev call that fails or exceeds its 1.5 s budget, malformed stdin, or any exception: the hook prints nothing and exits 0. It carries no `onFailure: block`.
- **Session model.** It reads the model from the same evidence as the guard (`resolveSessionModel`, see below); it writes no session state of its own.
- **Log.** Each advice is appended to `~/.config/jev/claude_decisions.jsonl` as an `advice` record (recommendation and model names only, never prompt text) and to the shared telemetry file.
- **Explicit instructions (case study 5.1).** `parseUserIntent` reads imperatives only ("use opus", "run an opus subagent", "choose minimal viable model", "cheapest"): a model that is merely mentioned, negated, or set against another ("use opus or sonnet") yields nothing, and only the first 2000 characters are scanned. An explicit model replaces Jev's tier; a minimal-cost request lowers it by one step, never below `flash_lite`. The model wins when both appear. The text says "You asked for ..." and the log records `by` and `routerTier`, never the matched words.
- **One ceiling sentence (5.2).** On the `pro` tier the line carries a single sentence: below opus, "Jev rates this opus-level; the session model is the hard ceiling, so ask the user before delegating on opus ... let the user choose"; on opus or above, "the session model allows it". The ceiling line and the ask line are never printed together.
- **Stage rubric (5.3).** `src/stage-rubric.ts` holds one table: mechanical edit or checklist update gives haiku; spec or plan edit with a settled decision gives sonnet; judgement whose error is expensive (an undecided reversal, adversarial verification, synthesis across disagreeing sources) gives opus. It is a short copy of the user's standing rule (Jev cannot read the user's `CLAUDE.md`), the criteria text the router sees for the three tiers, and a line on delegation turns. A final-review kind from the case study's 7.1 proposal is not added.
- **Silence (5.5).** No advice, and no Jev call, for host-injected turns (`<task-notification>`, `<bash-*>`, `<local-command-*>`, `<system-reminder>`) and for prompts over 20,000 characters (a paste, not a task). Only the first 2000 characters go to Jev. A status turn (Jev says `self` on a cheap tier and the prompt has no verb of work, such as "A3 completed the run") gets no line; an explicit instruction in the prompt always does. The verb list is deliberately generous: a missed hint costs less than a wrong silence.
- **Personas (5.6).** The router is offered kebab-case names only: a display-name alias such as `Code Engineer` is dropped when `code-engineer` was also discovered. A lower-case file-name alias is kept, so a custom agent with a display name only still works. Discovery itself (`core/discovery.ts`) is unchanged and shared with the other harnesses. Limit: the filter looks at spelling, not at what Claude Code actually registered, so `code_engineer` and `code-engineer` can both be offered when a file declares the first.
- **One key for recommended and used (5.7).** Each advice record carries `session_id` (the same field the guard's audit uses), `turn` (a 12-character hash of the prompt, never the text), `tier` and `recommended_model`. The telemetry field `modelUsed` on this hook was wrong (it held the recommended model) and is now `modelRecommended`, with `modelUsed` kept as a deprecated alias of the same value for one release (drop in 2.5). The last advice per session is kept in `~/.config/jev/claude_sessions/<session_id>.advice.json` so a later hook can join the two.
- **Precedence (5.9).** One order decides the model, and the line says which rule applied (`rule: per your instruction | per the Jev guard | per standing rule`; `resolveModel` in `src/advisor-logic.ts`):
  1. an explicit instruction in this turn ("use opus", "minimal viable model");
  2. the Jev guard: the opus tier is gated behind the user's approval when the session model is below opus, and an explicit request for opus satisfies it;
  3. the standing rule (default sonnet, escalate only where a wrong judgement is expensive): Jev cannot read the user's `CLAUDE.md`, so this layer is the router's tier as shaped by the stage rubric, which is a copy of that rule.

  The `PreToolUse` guard cannot read the prompt and was not changed, so for a model above the session model it still shows its one prompt even when the user asked for that model. That prompt is the user's approval; the advice tells the assistant not to ask separately before it. The agentic-hub variant that skipped the prompt on an explicit request is therefore not ported.
- **Pre-flight reminder (5.10).** A second `PreToolUse` entry (`advisor-hook.ts pre-tool-use`, matcher `Agent|Task|Workflow`, no `onFailure`) sits beside the guard and never asks, denies or blocks. For each call it writes an `agent_call` record: `turn`, `tier` and `recommended_model` from the session's last advice next to `requested_model`, so recommended and used join on one key. When the work looks staged (a `Workflow`, or the third agent call of the session within ten minutes) and the shared telemetry shows no `jev_dispatch` or `jev_batch_dispatch` call from Claude Code in the last 30 minutes, it adds one `additionalContext` reminder naming the tool and the rubric, at most once per ten minutes per session. Limits: the dispatch tools record no session id, so a dispatch in one Claude session silences the reminder in another; the dispatch tools label their telemetry `harness: "claude-code"` (from `JEV_HARNESS`, set by the bundle's `.mcp.json`), and other harnesses keep the `antigravity` label.

### Stage plans and the dispatch tools on Claude Code

- **Stage plans (5.4).** When the prompt names a workflow or workstreams, the advisor line ends with a reminder to call `jev_batch_dispatch` with one task per stream, and the pre-flight reminder above treats the session as staged. The tool takes two optional fields per task, honoured on Claude Code only: `kind` (`mechanical` gives haiku, `settled-edit` sonnet, `judgement` opus, from the stage rubric) and `needsEdit`. With a `kind` the tier is the caller's own classification (`tierSource: "kind"`). Without one, the router is asked for the tier of each stage in the same call as the persona, with the rubric as its criteria (`"router"`), and when it is unreachable a whole-word keyword rule applies (`"keyword"`: `deep reasoning` and `architecture overhaul` give pro, `scaffold`, `boilerplate` and `format` give the free tier, else flash). A `needsEdit` stage never gets `Plan` or `Explore` (neither can edit) and never the free tier (it returns text only). The router is offered Claude Code's real subagent types, built-ins plus custom agents by their kebab-case name.
- **Harness-aware tools (5.11).** `jev_dispatch` and `jev_batch_dispatch` read the harness from `detectActiveHarness()` (the bundle's `.mcp.json` sets `JEV_HARNESS=claude-code`). On Claude Code the guidance is `Agent(subagent_type, model)` or `agent({ agentType, model })`, the free tier is described without "Gemini", and `activeSessionModel` is the model of the Claude session the call belongs to (the guard's evidence, located through `CLAUDE_CODE_SESSION_ID`, `src/claude-active-session.ts`), not Antigravity's quota file. For every other harness, including OpenCode and the plain CLI, the output is byte-for-byte what it was (`src/tools/harness.ts` keeps the old wording; `tests/dispatch-harness.test.ts` runs both versions of the handlers and pins it).
- **Reported, not clamped.** The Antigravity clamp lowers a recommended tier to the session's. On Claude Code that would turn an expensive judgement into sonnet in a sonnet session and hide it, against the standing rule that lets it escalate. So the tool keeps the tier, reports `sessionCeilingTier` and `wasClampedByCeiling: false`, sets `requiresUserApproval` for opus, and says in the guidance that the Jev guard will ask once. Limit: the session id is fixed when the server starts, so a session cleared or resumed under a new id reads as `unknown`, and an unknown model is reported as such and treated as below opus.
- **Not changed.** The substring rule in the other harnesses' batch tier (`"information"` and `"transformation"` contain `"format"` and go to the free tier) is left alone because those harnesses must not change; the Claude Code path uses whole words. TODO for a change that is allowed to touch Antigravity: use `claudeKeywordTier`'s whole-word match there too.

### Workflow `agent()` calls (5.8, design check)

Finding: no hook event can intercept a `Workflow`'s inner `agent()` call to advise or gate it, so none was added.

- **What fires.** The guard's own note (verified live on Claude Code 2.1.312 in the work behind the Workflow guard) is that agents spawned inside a `Workflow` do not pass through the `Agent|Task` `PreToolUse` hook; only `SubagentStart` and `SubagentStop` fire for them. The 2.1.319 binary's hook schemas agree: `SubagentStart` input is `{ agent_id, agent_type }` (no model, no prompt) and its only output is `additionalContext` (no decision), so it can neither read the requested model nor block. Not re-run live for this change: a one-agent workflow with a decisions-log check is still the manual test.
- **What covers the gap instead.** The `PreToolUse` guard on the `Workflow` call itself analyses the script statically (literal `model`, `agentType` definitions, `effort`) and asks, and the `PostToolUse` recorder reports drift; the pre-flight reminder above fires on that same `Workflow` call and points at `jev_batch_dispatch`; the stage plan (5.4) puts the per-stream model into the script before it runs.
- **Not covered.** A model that only exists at run time (a variable, `args`) is asked about by the guard, not advised; and the model a Workflow agent actually used is not logged next to the recommendation, because no hook sees it. TODO if Claude Code later exposes the model on `SubagentStart` or fires `PreToolUse` for workflow agents: add a `SubagentStart` entry that writes an `agent_call` record, and move the reminder there.
