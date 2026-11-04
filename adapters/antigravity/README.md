# Antigravity adapter

Sidecar integration for Google Antigravity: MCP registration and one narrow `PreToolUse` guard.

## MCP registration

`installMcpConfig(jevServerEntry(engineRoot))` merges a `jev` stdio entry into `~/.gemini/config/mcp_config.json`. It keeps a `.bak` of the previous file, is idempotent, refuses to overwrite a file that is not valid JSON, and writes no credentials (Jev reads its key from `~/.config/zsh/secrets.env` or `~/.config/jev/env`).

## Subagent guard

`jevHooksEntry(engineRoot)` returns a `jev-enforcement` group for a workspace `.agents/hooks.json`. It runs `packages/jev/src/hook.ts antigravity` before every `invoke_subagent` call and answers `force_ask` when any subagent requests a Pro-class model (`Model: "pro"`), so Pro always needs your approval.

- Active only when the Jev mode for Antigravity is `on`; once on, it asks rather than passing silently when it cannot evaluate a call.
- It replaces nothing in the engine itself: quota lockouts, the free-tier model guard and the multi-file write guard remain in the consumer workspace's own hook script. Merging into a workspace that already has a `jev-enforcement` group replaces that group, so keep the existing script's other checks if you rely on them.

## Manual install

There is no installer yet; `engine install-adapters` is deferred to the `engine` CLI work. From the repository root:

```sh
# 1. Register the MCP server in ~/.gemini/config/mcp_config.json
bun -e "import { installMcpConfig, jevServerEntry } from './adapters/antigravity/src/index.ts'; console.log(installMcpConfig(jevServerEntry(process.cwd())))"

# 2. Print the guard entry, then merge it into the workspace .agents/hooks.json by hand
bun -e "import { jevHooksEntry } from './adapters/antigravity/src/index.ts'; console.log(JSON.stringify({ 'jev-enforcement': jevHooksEntry(process.cwd()) }, null, 2))"

# 3. Turn Jev on so the guard is active
#    /jev on
```

The guard does nothing unless the Jev mode for Antigravity is `on`.
