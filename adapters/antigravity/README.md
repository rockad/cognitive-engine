# Antigravity adapter

Sidecar integration for Google Antigravity: MCP registration and one narrow `PreToolUse` guard.

## MCP registration

`installMcpConfig(jevServerEntry(engineRoot))` merges a `jev` stdio entry into `~/.gemini/config/mcp_config.json`. It keeps a `.bak` of the previous file, is idempotent, refuses to overwrite a file that is not valid JSON, and writes no credentials (Jev reads its key from `~/.config/zsh/secrets.env` or `~/.config/jev/env`).

## Subagent guard

`jevHooksEntry(engineRoot)` returns a `jev-enforcement` group for a workspace `.agents/hooks.json`. It runs `packages/jev/src/hook.ts antigravity` before every `invoke_subagent` call and answers `force_ask` when any subagent requests `Model: "pro"`, so Pro always needs your approval.

- Active only when the Jev mode for Antigravity is `on`; fails open on any error.
- It replaces nothing in the engine itself: quota lockouts, the free-tier model guard and the multi-file write guard remain in the consumer workspace's own hook script. Merging into a workspace that already has a `jev-enforcement` group replaces that group, so keep the existing script's other checks if you rely on them.
