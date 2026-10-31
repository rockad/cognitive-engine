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

`bun src/hook.ts claude|antigravity` is a narrow `PreToolUse` guard (`src/guard.ts`). Claude Code asks before a subagent runs on a model stronger than the session's; Antigravity asks before any Pro-class subagent. It is the only enforcement point (`jev_dispatch` and its clamp are advice). It is deterministic, makes no network calls, and runs only in Jev mode `on`; once on, it asks rather than passing silently when it cannot evaluate a call.
