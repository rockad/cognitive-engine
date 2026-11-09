# Real Claude Code hook payloads

Captured with the log-only canary in `tools/probes/hook-canary/`, then redacted with its `redact.sh`
(home dir to `~`, session, prompt and tool-use ids and UUIDs to placeholders, job id to `JOB`).
Field names and structure are as received. Each file is the raw hook stdin JSON, one representative
payload per case.

- Captured: 2026-10-28
- Claude Code: `2.1.312`
- Model: `--model sonnet` (`claude-sonnet-5-5`)
- Caveat: redaction rewrites the home prefix, so `scriptPath` reads `~/.claude/jobs/JOB/...` here.
  The raw value was an absolute path (`/home/<user>/...`).

## Files

| File | Case |
| --- | --- |
| `PreToolUse.workflow-script.json` | Workflow, inline form: `tool_input = {script}` |
| `PreToolUse.workflow-scriptpath.json` | Workflow, file form: `tool_input = {scriptPath, args}` |
| `PreToolUse.workflow-resume.json` | Workflow, resume form: `tool_input = {scriptPath, resumeFromRunId, args}` |
| `PreToolUse.workflow-name.synthesized.json` | SYNTHESIZED, not captured: Workflow saved-workflow form `tool_input = {name, args}`, written by hand from the tool schema; carries a `_synthesized` marker field |
| `PreToolUse.agent.json` | Agent tool (`tool_name` is `Agent`, not `Task`, on 2.1.312) |
| `SessionStart.json` | `source: "startup"`, no model field |
| `SessionStart.resume.json` | `source: "resume"`, adds cache and context fields, still no model |
| `SubagentStart.json` / `SubagentStop.json` | Agent-tool subagent (`agent_type: "general-purpose"`) |
| `SubagentStart.workflow.json` / `SubagentStop.workflow.json` | agent() spawned inside a Workflow (`agent_type: "workflow-subagent"`) |

## Command

Run from a scratch directory (not the repo). `CANARY_HOME` is `tools/probes/hook-canary`, and
`CANARY_DIR` is where the JSONL lands.

```sh
CANARY_HOME=<repo>/tools/probes/hook-canary CANARY_DIR=<out> \
claude -p --model sonnet --setting-sources project \
  --settings "$CANARY_HOME/settings.json" --strict-mcp-config --disable-slash-commands \
  --permission-mode bypassPermissions --output-format stream-json --verbose \
  --include-hook-events --no-session-persistence < prompt.txt
```

- `--setting-sources project` with a scratch cwd keeps the user's settings, hooks and plugins out
  (only the three built-in plugins loaded).
- The Workflow tool is gated by the user-level `enableWorkflows` setting, so `settings.json` sets
  `"enableWorkflows": true`. Without it the tool is absent in `-p`.
- The scriptPath and resume cases came from a second and third run: a `--session-id` run, then
  `--resume <id> --model haiku`.

## Findings

- Workflow `tool_input` carries only the keys the model passed, so check `script`, `scriptPath` and
  `resumeFromRunId` by presence. `scriptPath` was absolute. `args` appeared as an object.
- `PreToolUse` with matcher `Agent|Task` did NOT fire for the agent() call inside a Workflow
  (re-verified: two PreToolUse lines in the run, Workflow and the direct Agent only).
  `SubagentStart` and `SubagentStop` did fire for it, with `agent_type: "workflow-subagent"`.
- `SubagentStart` carries no model and no prompt. `SubagentStop` carries `last_assistant_message`
  and `background_tasks`.
- `onFailure: "block"` blocked the tool call when the hook exited 1. The model saw
  `PreToolUse:Bash hook error: [...]: failed; blocking because onFailure is "block"`. The identical
  crashing hook without `onFailure` was fail-open: the command ran and printed its output.

## Not obtainable

- `PreModelSwitch` and `PostModelSwitch` never fired in `-p`, including `--resume --model haiku`.
  No fixture. The 2.1.312 binary's schema (not a capture) names the fields `from_model`, `to_model`,
  `requested_model` and `source` (`command|picker|sdk|auto|resume`, Post only adds `auto|resume`),
  plus `context_tokens`, `prompt_cache_warm`, `cache_ttl`, `estimated_cache_write_usd` and
  `pricing` on the Pre event. Treat these as unverified until captured.
- Workflow by `name` (saved workflow). Not run.
- `SessionStart` has no model. The `system/init` stream message does (`model`), the hook does not.
