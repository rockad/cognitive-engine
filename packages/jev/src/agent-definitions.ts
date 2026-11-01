/**
 * Subagent definition lookup, nearest first:
 *   1. `.claude/agents/<name>.md` in the hook cwd, then in each parent up to the project root
 *      (`CLAUDE_PROJECT_DIR` if set, else the git root, else the filesystem root), and
 *      `$CLAUDE_PROJECT_DIR/.claude/agents` when the cwd is outside it;
 *   2. `~/.claude/agents/<name>.md`;
 *   3. installed plugins' `agents/<name>.md`, found through `~/.claude/plugins/installed_plugins.json`
 *      (read-only), enabled plugins first per `enabledPlugins` in the settings files, then the rest
 *      (never fewer definitions than a plain search of all). A namespaced name `plugin:agent` only
 *      looks in the plugin called `plugin`.
 *
 * The name is restricted to file-name characters (one `:` allowed, as the plugin separator), so a
 * path-like name never escapes the directories. A name that resolves to no file is "not found",
 * which callers treat as unverifiable and ask about; this module never decides to allow.
 */
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';

export interface AgentDefinition {
  /** A definition file exists in one of the lookup locations. */
  found: boolean;
  /** The `model:` from its frontmatter (first definition that sets one), or null. */
  model: string | null;
}

const SEGMENT = /^[A-Za-z0-9_-][A-Za-z0-9_.-]*$/;

/** Parse `name` or `plugin:name` into its parts, or null for anything else. */
function parseAgentName(subagentType: unknown): { plugin: string | null; agent: string } | null {
  if (typeof subagentType !== 'string' || subagentType.length === 0) return null;
  const parts = subagentType.split(':');
  if (parts.length > 2 || !parts.every((p) => SEGMENT.test(p))) return null;
  return parts.length === 2 ? { plugin: parts[0], agent: parts[1] } : { plugin: null, agent: parts[0] };
}

function homeDir(): string {
  return process.env.HOME || os.homedir();
}

function isWithin(root: string, dir: string): boolean {
  const rel = path.relative(root, dir);
  return rel === '' || (!rel.startsWith('..') && !path.isAbsolute(rel));
}

function gitRoot(start: string): string | null {
  let dir = start;
  for (;;) {
    if (fs.existsSync(path.join(dir, '.git'))) return dir;
    const parent = path.dirname(dir);
    if (parent === dir) return null;
    dir = parent;
  }
}

/** Project-level `.claude/agents` dirs: the cwd up to the project root, nearest first. */
function projectAgentDirs(cwd: string | undefined): string[] {
  const envRoot = process.env.CLAUDE_PROJECT_DIR ? path.resolve(process.env.CLAUDE_PROJECT_DIR) : null;
  const dirs: string[] = [];
  if (cwd && path.isAbsolute(cwd)) {
    const start = path.resolve(cwd);
    const stop = envRoot ? (isWithin(envRoot, start) ? envRoot : start) : gitRoot(start);
    let dir = start;
    for (;;) {
      dirs.push(path.join(dir, '.claude', 'agents'));
      const parent = path.dirname(dir);
      if (dir === stop || parent === dir) break;
      dir = parent;
    }
  }
  if (envRoot) dirs.push(path.join(envRoot, '.claude', 'agents'));
  return dirs;
}

/**
 * `enabledPlugins` as Claude Code layers it: `~/.claude/settings.json`, then each project's
 * `.claude/settings.json` and `.claude/settings.local.json` (later layers win). Only a literal
 * `true` enables a plugin key (`name@marketplace`). Unreadable or odd files contribute nothing.
 */
function enabledPluginKeys(cwd: string | undefined): Set<string> {
  const files = [path.join(homeDir(), '.claude', 'settings.json')];
  // Project roots: the parents of the project `.claude/agents` dirs, farthest (project root) first.
  for (const agents of [...projectAgentDirs(cwd)].reverse()) {
    const claudeDir = path.dirname(agents);
    files.push(path.join(claudeDir, 'settings.json'), path.join(claudeDir, 'settings.local.json'));
  }
  const state = new Map<string, boolean>();
  for (const file of files) {
    let enabled: unknown;
    try {
      enabled = JSON.parse(fs.readFileSync(file, 'utf8'))?.enabledPlugins;
    } catch {
      continue;
    }
    if (!enabled || typeof enabled !== 'object' || Array.isArray(enabled)) continue;
    for (const [key, value] of Object.entries(enabled)) state.set(key, value === true);
  }
  return new Set([...state].filter(([, on]) => on).map(([key]) => key));
}

/**
 * `agents/` dirs of installed plugins as `[pluginName, dir]`: enabled plugins (per
 * `enabledPlugins` in the settings files, when readable) first, then every other installed plugin,
 * each group in manifest order. Ordering only: no plugin is ever left out. A missing or odd
 * manifest yields none.
 */
function pluginAgentDirs(cwd: string | undefined): Array<[string, string]> {
  const out: Array<{ name: string; dir: string; key: string }> = [];
  let doc: any;
  try {
    doc = JSON.parse(fs.readFileSync(path.join(homeDir(), '.claude', 'plugins', 'installed_plugins.json'), 'utf8'));
  } catch {
    return [];
  }
  const plugins = doc?.plugins;
  if (!plugins || typeof plugins !== 'object' || Array.isArray(plugins)) return [];
  for (const [key, installs] of Object.entries(plugins)) {
    const name = key.split('@')[0];
    if (!name || !Array.isArray(installs)) continue;
    for (const inst of installs) {
      const p = (inst as any)?.installPath;
      if (typeof p === 'string' && path.isAbsolute(p)) out.push({ name, dir: path.join(p, 'agents'), key });
    }
  }
  const enabled = enabledPluginKeys(cwd);
  const ordered = [...out.filter((e) => enabled.has(e.key)), ...out.filter((e) => !enabled.has(e.key))];
  return ordered.map((e) => [e.name, e.dir]);
}

/** The directories searched for `subagentType`, nearest first (empty for an invalid name). */
export function agentLookupDirs(subagentType: unknown, cwd: string | undefined): string[] {
  const parsed = parseAgentName(subagentType);
  if (!parsed) return [];
  const dirs = new Set<string>();
  if (!parsed.plugin) {
    for (const d of projectAgentDirs(cwd)) dirs.add(d);
    dirs.add(path.join(homeDir(), '.claude', 'agents'));
  }
  for (const [plugin, dir] of pluginAgentDirs(cwd)) if (!parsed.plugin || plugin === parsed.plugin) dirs.add(dir);
  return [...dirs];
}

/** One-line account of where a definition is looked for, for ask reasons. */
export function describeAgentLookup(cwd: string | undefined): string {
  const project = projectAgentDirs(cwd);
  return (
    `${project.length > 0 ? project.join(', ') : '<cwd>/.claude/agents'} (hook cwd up to the project root), ` +
    `${path.join(homeDir(), '.claude', 'agents')}, and the agents/ dir of each plugin in ` +
    `${path.join(homeDir(), '.claude', 'plugins', 'installed_plugins.json')} (enabled plugins first; a "plugin:agent" name only looks in that plugin)`
  );
}

/**
 * The sentence both guards use when a subagent's definition cannot be found. The caller prefixes
 * what was looked up (the agent name or type) and passes the lookup description.
 */
export function missingAgentDetail(lookup: string): string {
  return `no definition for it could be found, so the model it will run on cannot be checked against the session ceiling. Looked in ${lookup}.`;
}

export function readAgentDefinition(subagentType: string | undefined, cwd: string | undefined): AgentDefinition {
  const result: AgentDefinition = { found: false, model: null };
  const parsed = parseAgentName(subagentType);
  if (!parsed) return result;
  for (const dir of agentLookupDirs(subagentType, cwd)) {
    try {
      const text = fs.readFileSync(path.join(dir, `${parsed.agent}.md`), 'utf8');
      result.found = true;
      const front = /^---\r?\n([\s\S]*?)\r?\n---/.exec(text)?.[1];
      const model = front && /^model:\s*(.+?)\s*$/m.exec(front)?.[1]?.replace(/^["']|["']$/g, '');
      if (model) return { found: true, model };
    } catch {
      // no definition here
    }
  }
  return result;
}
