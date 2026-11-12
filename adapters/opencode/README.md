# OpenCode adapter

> **Deferred.** This adapter is committed but unverified against a live OpenCode, the V2 plugin API is beta, and it is expected to need an overhaul. It is not part of the Track 1 review (see RFC 002).

OpenCode V2 plugin for Jev, plus the MCP registration.

## What the plugin does

- **Context advisory** (`session.hook('context')`): appends a one-line routing directive to the system prompt, once per unique turn.
- **Delegation guard** (`tool.hook('execute.before')`, `enforce` mode only): blocks a `subagent` delegation (OpenCode V2's delegation tool) that Jev routes to the Pro tier until you approve it.

Options: `mode` (`off`, `advisory` default, `enforce`), `delegationTool` (default `subagent`), `allowPro`, `timeoutMs` (default 1500).

The V2 plugin API is beta, so every hook is defensive. A Jev failure, a timeout, an unexpected event shape or an unexpected error in the delegation guard (for example while reading the Jev mode) no-ops instead of failing the turn; only the deliberate `[jev]` approval error propagates. Jev mode `off` silences both hooks.

`opencode reload` reloads configuration; whether it re-imports plugin modules is unverified.

The hook shapes were checked against the opencode v2.0.25 binary, because the public docs at opencode.ai describe the V1 plugin API (`tool.execute.before(input, output)` with `output.args`). In V2, `tool.hook("execute.before", handler)` calls `handler(event)` with one argument, `{ tool, sessionID, agent, messageID, id, input }`; the delegation tool is `subagent` and its input is `{ agent, description, prompt, model?, sessionID?, background? }` (`background` is an optional boolean), so the guard reads the task from `event.input.prompt`, falling back to `description` when the prompt is empty. There is still no published V2 reference, so this stays unverified against the plugin loader itself.

## MCP registration

Add the stdio server to `~/.config/opencode/opencode.json`:

```json
{
  "mcp": {
    "servers": {
      "jev": {
        "type": "local",
        "command": ["bun", "run", "<engine>/packages/jev/src/index.ts"]
      }
    }
  }
}
```

No credentials go in the config; Jev reads its key from `~/.config/zsh/secrets.env` or `~/.config/jev/env`.
