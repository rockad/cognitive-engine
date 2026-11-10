
# Adversarial check of the local claims in todo-tools-evaluation-2026-11-07.md

Scope: sections 2 and 6 of `agentic-hub/plugins/coordination/docs/research/todo-tools-evaluation-2026-11-07.md`, plus every other statement it makes about ax, cognitive-engine or coordination. I cloned no external repos, ran nothing and wrote nothing.

The repos as read: cognitive-engine is at `a3af522`; its only branches are `main` and `claude/gracious-rubin-cbvavl`, and no `feat/track*` branch exists locally. ax is at `ef5ccf2`. agentic-hub is at `3253985`.

Labels: **V** means verified (read in source, file:line given). **R** means reported (README or docs only). **U** means unverified.

## A. Section 2 baseline

| Claim | Verdict | Evidence |
|---|---|---|
| Coordination has no server code. SP1, SP2 Claude half and SP5 pass. The agy half of SP2 (A8, A2, A3) and SP3 are open. | confirmed (V) | `plans/master-tasklist.md:144-163` |
| Gap: there is no "ready work" or dependency query. | confirmed (V) | `research/a2a-deep-dive-verification-by-claude-2026-11-03.md:29` |
| Binding rules: DDR-003, symmetric agency, never write AGENTS.md, keys, parked tasks, sbx exclusion. | confirmed (V) | `plugins/coordination/AGENTS.md:29-32, 39, 43, 72, 80, 91-92` |
| ax is v0.8.4. | confirmed (V) | `ax/Cargo.toml:3` |
| ax keeps a registry in `~/.config/ax/ax.db` and snapshots in `~/.config/ax/sessions/<project>.json`. | confirmed (V) | `core/tab_registry.rs:22`, `core/persistence.rs:37-44` |
| ax writes one hook, `ax-tab-sync` `Stop`, into `~/.gemini/config/hooks.json`. | confirmed (V) | `src/lib.rs:2393-2432` |
| ax writes `custom-title.json` and renames inside the agy and OpenCode databases, "around lib.rs:2393". | partly | The writes are real, but they are at `src/core/harness.rs:474-530`: agy `conversation_summaries.db` UPDATE at :480/:485, `~/.claude/projects/.../custom-title.json` at :505-508, OpenCode `opencode.db` `session_v2` at :524. `lib.rs:2393` is the hooks.json writer. |
| ax has no send-keys and no MCP. | confirmed (V) | `rg 'send-keys\|send_keys\|paste-buffer\|mcp'` over `src docs README.md config scripts bin lib` finds 0 hits. |
| ax registers no tasks or messaging. | confirmed (V) | Same grep; no network client at all. |
| ax has "Stop hooks for both agy and Claude-shaped stdin" (section 6). | partly | `run_hook` accepts `session_id`/`sessionId` (`lib.rs:1671-1676`). ax installs only the agy hook: it has no writer for `~/.claude/settings.json` (grep finds no `settings.json` in `src`). The id is bound to a window only if it is found in **agy's** `conversation_summaries.db` (`lib.rs:1677-1690`, `db_path` `lib.rs:1008-1015`, bind at `lib.rs:1893-1900`). A Claude session id therefore never binds `@conversation_id` through the hook. |
| cognitive-engine Track 1 is merged at `a3af522`. | confirmed (V) | `git log` |
| Tracks 2 to 4 have no code on `main`. | confirmed (V) | `packages/harness2/pyproject.toml` and `packages/memory/pyproject.toml` both have `dependencies = []`. `packages/lookup`, `cli/` and `tools/` do not exist. |
| Plan §6 Dual-Harness Cross-Delegation, the bridge doc with Turn 4 pending, and the key `session-handoff:cognitive-engine`. | confirmed (V) | `plans/2026-10-26-multi-track-implementation-plan.md:299-345`, `docs/handoffs/antigravity-claude-bridge.md:9,139`, `HANDOFF.md` |
| `BatchDispatchPlan` DAG is advisory. | confirmed (V) | `packages/jev/src/core/graph.ts:21`, `tools/dispatch.ts:415` |
| cognitive-engine has no UI. | confirmed (V) | No react, html or web files in `packages/` or `adapters/`. |

## B. Section 6

### B1. Port clash on 8120: partly

- Coordination decision 5 puts the server on 8120: confirmed (V), `plans/consolidated-bridge-v2-plan.md:131`.
- "cognitive-engine **RFC 001** and plan WS3 put Harness² on 8120" is **wrong for RFC 001**. `rg -c 8120` over cognitive-engine `docs` finds only the plan, at `:209` (Track 3 Slice 1 "Completed … FastMCP server on 127.0.0.1:8120") and `:258` (WS3). RFC 001 and RFC 004 never name a Harness² port.
- "Track 3 Slice 1 is reported done there" is accurate as reported (R). On `main` it is contradicted: harness2 is an empty stub and the plan's `tools/mcp_conformance.py` does not exist. The 129 tests and 12/12 conformance cannot be checked locally (U).
- The clash is still real at the plan level: both projects target `127.0.0.1:8120` on the same host.

Ports each project plans or uses:

| Port | Project | Use | Evidence |
|---|---|---|---|
| 8100 | hub `plugins/memory` | Lookup | `docker-compose.yml:46,50` |
| 8100 | cognitive-engine | native lookup | RFC 003:33,45; RFC 005:104 |
| 8101 | (coordination docs only) | "native Lookup staging" | Only in `a2a-audit-2026-11-04/t2-cognitive-engine-integration.md:23`. Not anywhere in the cognitive-engine tree. Stale (R). |
| 8105 | cognitive-engine | memory staging | RFC 001:22,104,183; RFC 003:28,44; bridge:108 |
| 8110 | hub memory | Recall (container port 8000) | `docker-compose.yml:18` |
| 8110 | cognitive-engine | Recall, **hard-coded in Jev code** | `packages/jev/src/core/telemetry.ts:65`, test `egress.test.ts:237` |
| 8115 | cognitive-engine | Jev streamable HTTP | RFC 002:34; `tests/server.test.ts:134-136` |
| 8120 | coordination | A2A server, S1 and S2 | decision 5 (`consolidated-bridge-v2-plan.md:131`), `server-api.md:190` |
| 8120 | cognitive-engine | Harness² | plan :209, :258 |
| 8121 | coordination | **S3 public listener (proposal)** | `server-api.md:1136-1138`. The note's port list omits it. |
| 18120-18127 | coordination | spikes only | `spikes.md:30`, `spike-results-2026-11-05-sp5.md:73` |
| 11434 | hub `plugins/local-transcribe` | Ollama | `scripts/lt/ollama.py:13`. Not used by cognitive-engine or ax. |
| 9222 | hub `chrome-devtools` | CDP | grep count only |
| none | ax | no listener | grep finds 0 |

Candidates 8125 and 8130 have no hits outside the note. One caveat: whichever project moves to 8125 must keep 8121 free if coordination keeps its "port + 1" public listener.

Latent break: cognitive-engine Jev hard-codes Recall `:8110` (`telemetry.ts:65`). RFC 003 moves memory to `:8105`, and RFC 005 retires the `:8110` container. Jev's recall read will fail silently after retirement.

### B2. cognitive-engine has no reference to coordination: confirmed (V)

`rg -i 'a2a|coordination|status.client|tmux'` over cognitive-engine finds 0 hits for A2A, coordination or the status client. The only "agentic-hub" mentions are about the retirement (RFC 001:93,229,277-301; RFC 005).

Related inconsistency: `coordination/docs/specs/status-client.md:109` cites `packages/harness2/README.md`, `session_runner.py` and `sbx_session.py`. None of them exist on cognitive-engine `main`; they come from a T2 reply that read other refs (R).

### B3. Bridge doc duplicates the frozen file channel: confirmed (V)

- It is a 140-line hand-relayed transcript. Quick-Start says "Paste the prompt below" (`antigravity-claude-bridge.md:22`). Turns 1-3 are filled and Turn 4 is pending (`:31-139`). `HANDOFF.md` repeats the active turn.
- Shared state goes through Recall `:8110` and Lookup `:8100` (`:9`).
- It also assigns **asymmetric roles**: "Harness A (Orchestrator & Verifier)" and "Harness B (Deep Builder)" (`:5-6`), echoed in plan §6.1 and §6.3 Mode B. This conflicts with coordination's symmetric-agency rule. The note does not mention it.

### B4. ax as a read-only surface: claim confirmed, mechanism partly

- `@harness` is set per window and per pane (`lib.rs:1373-1389`). `@conversation_id` is **window-level only** (`lib.rs:1416`, `persistence.rs:317`), so split panes holding different sessions cannot be told apart.
- Is the Claude session_id available per tab? Mostly **no**:
  - `conv_id` is filled only from `--conversation`, `-c` or `--continue` arguments (`lib.rs:1287-1306`), or from a snapshot restore (`persistence.rs:295-298`).
  - A fresh `ax run -H claude` tab has no id.
  - The picker resumes Claude with `-- --resume '<id>'` (`ui/picker.rs:1328`). The parser does not recognise `--resume`, and the bare id then lands in `initial_prompt` (`lib.rs:1308-1309`) (V). The downstream effect on naming is U.
  - The hook does not bind Claude ids (see A).
- Implication: a coordination segment cannot rebuild the handle locally. It needs either:
  - (a) a pane-level option written by the harness's own `SessionStart` hook, for example `tmux set-option -p -t "$TMUX_PANE" @a2a_session_id <id>` plus `@a2a_handle`. This does not type into the pane. Whether `TMUX_PANE` reaches Claude hooks is U.
  - (b) a lookup of `GET /sessions` by `harness_session_id`, which the server keeps unique on (`card_id`, `harness_session_id`) (`server-api.md:111,444-449`).
- Pane-scoped options to add: `@a2a_session_id`, `@a2a_handle`, `@a2a_pending`. `@harness` already exists.
- Handle format conflict: the shape the task names, `claude-code@<host>/<name>#<sid8>`, comes from the SP2 spike helper (`spike-results-2026-11-06-sp2-claude.md:24,34,346`). The spec still says `<card>/<session-name>` with name regex `^[a-z0-9][a-z0-9._-]{0,47}$` (`server-api.md:733`), and on a collision it appends `-` plus 4 hex (`:471-472`). `#` is **invalid** under that regex. The "spec fold-in 2026-11-07" (`master-tasklist.md:150-151`) did not reconcile this.

### B5. ax hooks.json versus decision 26 ("each uses its own key, should coexist"): partly, and riskier than the note says

`ensure_hook_installed` (`lib.rs:2394-2432`):

- It **merges at the top level**. It reads the JSON, removes `agymux-tab-sync`, inserts `ax-tab-sync`, and keeps every other top-level key (`:2422-2425`). The test checks that `other-hook` survives (`:2599-2635`), but the test re-implements the logic instead of calling the function.
- It **resets to `{}` and overwrites** when the file is unreadable or not valid JSON (`:2400-2410`: `unwrap_or_default`, then `unwrap_or_else(|_| json!({}))`). Any coordination or jev group is lost, with no backup.
- It runs on **every `ax` attach**, with errors ignored (`lib.rs:328`), and writes non-atomically (`fs::write`, `:2429`).
- It **re-sorts keys and reformats the file**: `serde_json = "1.0"` has no `preserve_order` (`Cargo.toml:26`). A byte-compare restore check ("verify the file matches the backup", `spikes.md:328`, decision 25) fails if ax attaches during a spike. Compare with `jq -S` instead.

Shape compatibility:

- The agy format, as verified by the runbook, is a map of named groups. `SessionStart`, `PreInvocation` and `Stop` use the **flat form**; only `PreToolUse` and `PostToolUse` use the matcher/`hooks` wrapper (`spike-runbooks-2026-11-05.md:62-68`). The runbooks themselves use group `"a2a-spike"` in flat form (`:871-873`).
- ax's entry (`{"enabled":true,"Stop":[{type,command,timeout}]}`) matches that format.
- cognitive-engine's `jev-enforcement` group (`adapters/antigravity/src/index.ts:84-108`) matches too, and it targets workspace `.agents/hooks.json`, not the user-level file.
- **`server-api.md` 11.2 is still wrong** (`:1439-1450`): top-level key `"hooks"` and Claude-style nested `[{"hooks":[...]}]`. Under ax's merge it would survive as a group literally named `hooks` whose arrays are in the wrong form, so agy probably would not run it (U). Fix: a named group such as `a2a-coord`, in flat form.
- `implementation-plan.md:602` says "a scan of the shipped `hooks.json` finds no `Stop` entry". Run on the user-level file, that scan fails because of ax's `Stop`. The scan must be limited to coordination's own group.

### B6. cognitive-engine PR #3 remediation not in code: confirmed, with two of five items apparently done

The list is at `plan:119-132`; WS7 is at `:263` and §3.1.3.

| Item | Status | Evidence |
|---|---|---|
| `Workflow` matcher | missing | `adapters/claude/hooks/hooks.json` has only `"matcher": "Agent\|Task"` |
| `workflow-guard.ts` | missing | absent from `packages/jev/src/` (`rg workflow` finds only prose) |
| hook.ts mode check | **wrong** | `hook.ts:31` returns early unless the mode is `'on'`. Plan `:107` and item 5, and RFC 002 `:62` (active in `on` **and** `on-demand`), say otherwise. |
| Item 1, fail-closed wrapper | not done | the command is a plain `bun "$COGNITIVE_ENGINE_HOME/..."` |
| Item 2, namespaced plugin agents | not done | `guard.ts:92` regex `^[A-Za-z0-9_.-]+$` rejects `:` |
| Item 3, quoting | appears done | `adapters/antigravity/src/index.ts:96` quotes the path. It uses the resolved `engineRoot`, not the env var. |
| Item 4, `fable` | appears done | `guard.ts:31` (`fable` rank 4) and `:120` (`>= STRONG_RANK`). Rank use is V; `modelTierRank` internals are U. |

## C. Jev and memory duplication versus RFC 005

- **Duplication (V).** `diff -rq` shows `agentic-hub/plugins/jev/src` and `cognitive-engine/packages/jev/src` diverged: config, governance, index and every tool differ, and `guard.ts`, `hook.ts`, `server.ts` and `tools/define.ts` exist only in cognitive-engine. Both expose the same 12 `jev_*` tool names. Both use plugin name `jev` and MCP server name `jev` (hub `.mcp.json`; cognitive-engine `adapters/claude/.claude-plugin/plugin.json`, `.mcp.json`). Both read and write the same `~/.config/jev/` files (`config.json`, `cache.sqlite`, `egress.json`; `cache.ts:9` and `egress.ts:38` in both trees). Installing both would give two `PreToolUse Agent|Task` guards and a name collision.
- **Retirement not started (V).**
  - RFC 005 Phase A (deprecation notices in the hub READMEs, RFC 001 `:293-294`, RFC 005 `:110`) is not done: the only "deprecated" text in the hub READMEs concerns `mode.json`.
  - The RFC 005 checkboxes are unchecked (`:103-104, :134-135`).
  - `packages/memory` is a stub and `packages/lookup` does not exist, so the hub memory plugin is still the only live memory stack. cognitive-engine itself still depends on it (`telemetry.ts:65`, bridge `:9`).
- **Hub AGENTS.md still describes jev and memory as live: confirmed (V)**, in the jev and memory sections of `agentic-hub/AGENTS.md`. It does not mention the migration to cognitive-engine.
- The RFC's target marketplace (RFC 001 `:299`, RFC 005 `:121`) lists only 4 peripheral plugins. It would drop `plugins/coordination` and `plugins/subagents`.

## D. Other cross-project inconsistencies

1. **Stale hub AGENTS.md and README** (V):
   - The tree omits `plugins/coordination` and `plugins/subagents`.
   - Both point to an OpenCode plugin at `plugins/jev/opencode/` (`AGENTS.md:61`, `README.md:40`) that does not exist. cognitive-engine says the old one was decommissioned (plan `:112`).
2. **Hub jev README hook path is wrong** (V): it says `hooks/hooks.json` (`plugins/jev/README.md:230,391`), but the real path is `./claude/hooks.json` (`plugin.json` `hooks`). It registers 4 events: SessionStart, PostModelSwitch, UserPromptSubmit, PreToolUse.
3. **Hub jev version mismatch** (V): `plugin.json` and `marketplace.json` say 2.3.0; `package.json` says 2.0.0.
4. **Writers of `~/.gemini/config/mcp_config.json`** (V):
   - cognitive-engine `installMcpConfig` merges a `jev` entry, keeps a `.bak`, and refuses to touch invalid JSON (`adapters/antigravity/src/index.ts:51-75`).
   - Coordination spikes used `agy mcp add`, which writes the same file (`spike-results-2026-11-04-sp2-a6.md:8`).
   - Coordination's planned key file is the separate plugin-scoped `.agents/plugins/coordination/mcp_config.json` (D24), so there is no direct clash.
   - ax does not write it (0 grep hits).
5. **Writers of `~/.gemini/config/hooks.json`** (V/R): ax on every attach (V), the coordination installer (planned, D26), and potentially the cognitive-engine `jev-enforcement` group (designed for workspace `.agents/hooks.json`). Only cognitive-engine keeps a backup or refuses on bad JSON; ax does neither.
6. **ax writes inside other tools' own state** (V): agy's `conversation_summaries.db`, Claude's `~/.claude/projects/<proj>/<sid>/custom-title.json`, and OpenCode's `opencode.db` (`harness.rs:474-530`). This does not breach DDR-003 (it never types into agy or drives it), but it is a schema dependency on internals no tool documents.
7. **cognitive-engine plan contradicts itself** (V): there are two "6.3" headings. §6.2 says isolated worktrees and "never … branch switches in the same worktree". The second §6.3 says "Both harnesses operate directly on `<checkout>/cognitive-engine`". The bridge Quick-Start runs `git checkout` in that shared checkout (`bridge:16-20`).
8. **Path base differs** (R): the note and coordination docs link absolute home-directory paths, while the local checkouts are at a different base path. The links are only valid on the workstation.
9. **Note's port list includes 8101 from t2** (R): it is not present in the cognitive-engine tree, so it is probably stale.
