/**
 * Antigravity adapter: registers the Jev stdio MCP server in the global Antigravity
 * config (`~/.gemini/config/mcp_config.json`) and points the harness at the shared rules.
 *
 * The entry carries no secrets. Jev resolves `JEV_OPENROUTER_API_KEY` itself from
 * `~/.config/zsh/secrets.env` or `~/.config/jev/env`.
 */
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';

export const SERVER_NAME = 'jev';

export const DEFAULT_MCP_CONFIG = path.join(os.homedir(), '.gemini', 'config', 'mcp_config.json');

export interface McpServerEntry {
  command: string;
  args: string[];
  env?: Record<string, string>;
}

export interface McpConfig {
  mcpServers?: Record<string, McpServerEntry | Record<string, unknown>>;
  [key: string]: unknown;
}

/** The server entry for a checkout of cognitive-engine at `engineRoot`. */
export function jevServerEntry(engineRoot: string, bunBin = 'bun'): McpServerEntry {
  return {
    command: bunBin,
    args: ['run', path.join(engineRoot, 'packages', 'jev', 'src', 'index.ts')]
  };
}

/** Returns a copy of `config` with the `jev` server set, leaving every other server untouched. */
export function mergeMcpConfig(config: McpConfig, entry: McpServerEntry): McpConfig {
  return { ...config, mcpServers: { ...config.mcpServers, [SERVER_NAME]: entry } };
}

export interface InstallResult {
  path: string;
  changed: boolean;
  backup: string | null;
}

/**
 * Writes the `jev` entry into the Antigravity MCP config. A missing file is created; an
 * unparseable one is left alone and reported, since overwriting it would drop the user's servers.
 * The previous file is kept as `<path>.bak` whenever the content changes.
 */
export function installMcpConfig(entry: McpServerEntry, configPath = DEFAULT_MCP_CONFIG): InstallResult {
  let current: McpConfig = {};
  let exists = false;
  try {
    const raw = fs.readFileSync(configPath, 'utf8');
    exists = true;
    current = JSON.parse(raw) as McpConfig;
  } catch (err) {
    if (exists) throw new Error(`${configPath} is not valid JSON; refusing to overwrite: ${(err as Error).message}`);
  }

  const next = mergeMcpConfig(current, entry);
  const before = JSON.stringify(current);
  const after = JSON.stringify(next);
  if (exists && before === after) return { path: configPath, changed: false, backup: null };

  fs.mkdirSync(path.dirname(configPath), { recursive: true });
  let backup: string | null = null;
  if (exists) {
    backup = `${configPath}.bak`;
    fs.copyFileSync(configPath, backup);
  }
  fs.writeFileSync(configPath, `${JSON.stringify(next, null, 2)}\n`);
  return { path: configPath, changed: true, backup };
}

export const HOOK_GROUP = 'jev-enforcement';

export interface HooksConfig {
  [group: string]: unknown;
}

/**
 * The narrow PreToolUse guard: Antigravity asks the user before any `invoke_subagent` call that
 * requests the Pro tier. Quota lockouts and write guards stay with the consumer workspace.
 */
export function jevHooksEntry(engineRoot: string, bunBin = 'bun'): Record<string, unknown> {
  return {
    enabled: true,
    PreToolUse: [
      {
        matcher: 'invoke_subagent',
        hooks: [
          {
            type: 'command',
            command: `${bunBin} ${path.join(engineRoot, 'packages', 'jev', 'src', 'hook.ts')} antigravity`,
            timeout: 5
          }
        ]
      }
    ]
  };
}

/** Returns a copy of a workspace `.agents/hooks.json` with the `jev-enforcement` group set. */
export function mergeHooksConfig(config: HooksConfig, entry: Record<string, unknown>): HooksConfig {
  return { ...config, [HOOK_GROUP]: entry };
}
