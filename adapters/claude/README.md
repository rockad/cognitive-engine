# Claude Code adapter

Native Claude Code plugin bundle for Jev: `.claude-plugin/plugin.json` plus `.mcp.json`. It registers the `jev_*` MCP tools and one narrow `PreToolUse` hook. The session-start, model-switch and prompt-advice hooks from `agentic-hub` were not ported because Track 1 had no need for them; hooks are not prohibited and can be added where a workflow requires one.

## The subagent guard

The hook runs `packages/jev/src/hook.ts claude` before every `Agent` or `Task` call. If the call requests a model stronger than the session's (for example `opus` from a Sonnet session), it returns `permissionDecision: "ask"` and Claude Code prompts you.

- Ranking is version-aware: family first (fable > opus > sonnet > haiku), then version (5.5 > 5 > 4.8). Aliases resolve by the table in `packages/jev/src/models.ts` (`opus`, `sonnet`, `haiku` to 5.5, `fable` to 5.1). Any request strictly above the session model asks; equal or lower passes silently; a model Jev cannot rank asks.
- The session model comes from a per-session state file written by the `PostModelSwitch` recorder (`record-hook.ts`, log-only and fail-open), falling back to the last assistant turn of the transcript. State lives in `~/.config/jev/claude_sessions/`.
- Every ask and every observed model change (with its source) is appended to `~/.config/jev/claude_decisions.jsonl`: model names and reasons only, never prompt text. The guard never blocks or alters Claude Code's own model-switch fallbacks.
- It is deterministic and makes no network call.
- It is active in Jev modes `on` and `on-demand` and silent only in `off`.
- It still fires under `--permission-mode bypassPermissions`: a hook `ask` is not auto-approved by bypass mode (checked with `claude -p`, where the call was blocked rather than run).
- It is the only enforcement point; `jev_dispatch` and its tier clamp are advice. Nothing the agent passes to a tool counts as approval, only your answer to the prompt.
- It also checks the `model:` in the subagent's definition (`.claude/agents/<name>.md`) when the call sets none.
- Once active, it fails closed: if it cannot read the call, hits an error, or cannot determine the session model, it asks instead of letting the call through.

## The workflow guard

Agents spawned inside a `Workflow` do not pass through the `Agent|Task` hook (verified on Claude Code 2.1.312: only `SubagentStart`/`SubagentStop` fire for them, and those cannot block), so a second `PreToolUse` entry with matcher `Workflow` runs the same `hook.ts claude`. `packages/jev/src/workflow-guard.ts` reads the script (inline `script`, a `scriptPath` file, or a saved workflow `name` from `.claude/workflows`, plus `workflow()` references one level deep), parses it with acorn and checks every `agent` and `workflow` reference.

- A literal `model` (or an `agentType` whose definition sets one) is ranked against the session model like a direct call. No `model` inherits and is allowed.
- Anything it cannot read statically asks and names the call site (`file:line:col`): an alias such as `const a = agent`, spread options, computed keys, a model from a variable or `args`, a template with `${}`, member access, `eval`, a parse error, an unreadable or oversized file, a missing agent definition, an unknown `tool_input` shape.

## Requirements

- Claude Code **>= 2.1.295**: the guard entries use `"onFailure": "block"`, so a crashing, timing-out or missing hook blocks the tool call instead of failing open. Older versions ignore the key and fail open.
- The two recorder hooks (`SessionStart`, `PostModelSwitch`) are log-only and deliberately fail open.

## Install

Point `COGNITIVE_ENGINE_HOME` at your checkout, then add the plugin from this directory:

```sh
export COGNITIVE_ENGINE_HOME=~/projects/cognitive-engine
claude --plugin-dir "$COGNITIVE_ENGINE_HOME/adapters/claude"
```

`COGNITIVE_ENGINE_HOME` has no default on purpose. If it is unset, Claude Code reports the missing variable instead of starting a server from a guessed path.

Jev reads its OpenRouter key from `~/.config/zsh/secrets.env` or `~/.config/jev/env`; the bundle holds no credentials.

## Enable the guard

The hook is active in `on` and `on-demand`. Run `/jev on-demand` (or `/jev on`), or set `harnesses.claude-code.mode` in `~/.config/jev/config.json`. Only an explicit `off` silences it (`{"defaults":{"mode":"off"}}`, or `/jev off`). A missing config file keeps the loader default, which is `off`.

A config that exists but cannot be trusted is not treated as `off`. Unreadable, invalid JSON, not an object, an unknown mode, a wrong type for a key Jev reads, or a stray top-level `"mode"` (the mistake `{"mode":"on"}`; the mode belongs at `defaults.mode`) makes the guard ask on **every** `Agent`, `Task` and `Workflow` call. The prompt names the file and the problems, a one-line message goes to stderr, and a `config_malformed` event (path and problem list only) is appended to the decisions log. Fix the file to `{"defaults":{"mode":"on-demand"}}`, run `/jev status` to see what Jev reads, or set `/jev off` explicitly.

## Validate

```sh
claude plugin validate adapters/claude
```
