# Claude Code adapter

Native Claude Code plugin bundle for Jev: `.claude-plugin/plugin.json` plus `.mcp.json`. It registers the `jev_*` MCP tools and one narrow `PreToolUse` hook. There are no session, prompt or model-switch hooks, per the 2026-10-27 settings consolidation.

## The subagent guard

The hook runs `packages/jev/src/hook.ts claude` before every `Agent` or `Task` call. If the call requests a model stronger than the session's (for example `opus` from a Sonnet session), it returns `permissionDecision: "ask"` and Claude Code prompts you.

- The session model is read from the last assistant turn of the session transcript, so the hook keeps no state.
- It is deterministic and makes no network call.
- It is active only when the Jev mode for Claude Code is `on`. In `on-demand` and `off` it stays silent.
- It fails open: an unreadable transcript, an unknown model or any error produces no output, and the call proceeds.

## Install

Point `COGNITIVE_ENGINE_HOME` at your checkout, then add the plugin from this directory:

```sh
export COGNITIVE_ENGINE_HOME=~/projects/cognitive-engine
claude --plugin-dir "$COGNITIVE_ENGINE_HOME/adapters/claude"
```

`COGNITIVE_ENGINE_HOME` has no default on purpose. If it is unset, Claude Code reports the missing variable instead of starting a server from a guessed path.

Jev reads its OpenRouter key from `~/.config/zsh/secrets.env` or `~/.config/jev/env`; the bundle holds no credentials.

## Enable the guard

The hook is silent until the Jev mode for Claude Code is `on`. Run `/jev on`, or set `harnesses.claude-code.mode` to `"on"` in `~/.config/jev/config.json`.

## Validate

```sh
claude plugin validate adapters/claude
```
