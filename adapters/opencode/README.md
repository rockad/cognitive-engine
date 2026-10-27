# OpenCode adapter

OpenCode V2 plugin for Jev, plus the MCP registration.

## What the plugin does

- **Context advisory** (`session.hook('context')`): appends a one-line routing directive to the system prompt, once per unique turn.
- **Delegation guard** (`tool.hook('execute.before')`, `enforce` mode only): blocks a `task` delegation that Jev routes to the Pro tier until you approve it.

Options: `mode` (`off`, `advisory` default, `enforce`), `delegationTool` (default `task`), `allowPro`, `timeoutMs` (default 1500).

The V2 plugin API is beta, so every hook is defensive. A Jev failure, a timeout or an unexpected event shape no-ops instead of failing the turn; only the deliberate `[jev]` approval error propagates. Jev mode `off` silences both hooks.

`opencode serve` caches plugin modules in memory. Restart it with `pkill -f "opencode serve"` after changing this source.

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
