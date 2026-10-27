# Claude Code adapter

Native Claude Code plugin bundle for Jev: `.claude-plugin/plugin.json` plus `.mcp.json`. It registers the `jev_*` MCP tools and one narrow `PreToolUse` hook. There are no session, prompt or model-switch hooks, per the 2026-10-27 settings consolidation.

## The subagent guard

The hook runs `packages/jev/src/hook.ts claude` before every `Agent` or `Task` call. If the call requests a model stronger than the session's (for example `opus` from a Sonnet session), it returns `permissionDecision: "ask"` and Claude Code prompts you.

- The session model is read from the last assistant turn of the session transcript, so the hook keeps no state.
- It is deterministic and makes no network call.
- It is active only when the Jev mode for Claude Code is `on`. In `on-demand` and `off` it stays silent.
- It still fires under `--permission-mode bypassPermissions`: a hook `ask` is not auto-approved by bypass mode (checked with `claude -p`, where the call was blocked rather than run).
- It is the only enforcement point; `jev_dispatch` and its tier clamp are advice. Nothing the agent passes to a tool counts as approval, only your answer to the prompt.
- It also checks the `model:` in the subagent's definition (`.claude/agents/<name>.md`) when the call sets none.
- Once on, it fails closed: if it cannot read the call, hits an error, or cannot determine the session model for an `opus`-class request, it asks instead of letting the call through. Weaker requests with an unknown session model proceed.

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
