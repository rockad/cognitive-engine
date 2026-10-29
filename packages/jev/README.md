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

A malformed config is neither on nor off: the subagent guard asks on every guarded call until the file is fixed or `off` is set explicitly, and `jev_get_mode` returns a `configWarning` with the same problems. The guard reads the file once, derives the mode from the object it validated (`resolveJevModeFromConfig`, the same function the loader uses), and cross-checks the loader; a mismatch, or a file that stops being readable between the two reads, asks instead of falling back to `off`.

Harness detection reads `JEV_HARNESS` first (`antigravity`, `claude-code`, `opencode`, `cli`), then falls back to each harness's own environment markers.

## Development

```sh
bun test       # unit and integration tests
bun run lint   # tsc --noEmit
bun run build  # bundle to dist/
```

Set `JEV_LIVE_TEST=1` to include tests that call OpenRouter.

## Lineage

Ported from `agentic-hub/plugins/jev` v2.3.0. Of the Claude Code hooks, only the subagent-ceiling check was kept (see below); the session-start, model-switch and prompt-advice hooks were not ported in Track 1 because nothing in it needed them. Hooks are not prohibited: add one wherever a workflow or process needs it.

## Subagent guard hook

`bun src/hook.ts claude|antigravity` is a narrow `PreToolUse` guard (`src/guard.ts`). Claude Code asks before a subagent runs on a model above the session's (version-aware ranking, `src/models.ts`); Antigravity asks before any Pro-class subagent. It is the only enforcement point (`jev_dispatch` and its clamp are advice). It is deterministic, makes no network calls, and runs in Jev modes `on` and `on-demand` (not `off`); once active, it asks rather than passing silently when it cannot evaluate a call. A malformed Jev config fails loudly: see Configuration.
