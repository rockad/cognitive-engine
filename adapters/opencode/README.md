# OpenCode adapter

> **Deferred.** This adapter is committed but unverified against a live OpenCode, the V2 plugin API is beta, and it is expected to need an overhaul. It is not part of the Track 1 review (see RFC 002).

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

## Model tiers bill per token by default

Jev's OpenCode tier map defaults to `~anthropic/claude-{haiku,sonnet,opus}-latest` on OpenRouter, which is pay-per-token. If OpenCode runs on a subscription, override the tiers in `~/.config/jev/config.json` under `harnesses.opencode.models` with model ids your provider lists. The package keeps the default on purpose (decided 2026-11-10); see Jev's README, "Model tiers per harness". Never use `claude-agent/*`.
