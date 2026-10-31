/**
 * Subagent definition lookup, nearest first:
 *   1. `.claude/agents/<name>.md` in the hook cwd, then in each parent up to the project root
 *      (`CLAUDE_PROJECT_DIR` if set, else the git root, else the filesystem root), and
 *      `$CLAUDE_PROJECT_DIR/.claude/agents` when the cwd is outside it;
 *   2. `~/.claude/agents/<name>.md`;
 *   3. installed plugins' `agents/<name>.md`, found through `~/.claude/plugins/installed_plugins.json`
 *      (read-only). A namespaced name `plugin:agent` only looks in the plugin called `plugin`.
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

/** `agents/` dirs of installed plugins as `[pluginName, dir]`. A missing or odd manifest yields none. */
function pluginAgentDirs(): Array<[string, string]> {
  const out: Array<[string, string]> = [];
  let doc: any;
  try {
    doc = JSON.parse(fs.readFileSync(path.join(homeDir(), '.claude', 'plugins', 'installed_plugins.json'), 'utf8'));
  } catch {
    return out;
  }
  const plugins = doc?.plugins;
  if (!plugins || typeof plugins !== 'object' || Array.isArray(plugins)) return out;
  for (const [key, installs] of Object.entries(plugins)) {
    const name = key.split('@')[0];
    if (!name || !Array.isArray(installs)) continue;
    for (const inst of installs) {
      const p = (inst as any)?.installPath;
      if (typeof p === 'string' && path.isAbsolute(p)) out.push([name, path.join(p, 'agents')]);
    }
  }
  return out;
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
  for (const [plugin, dir] of pluginAgentDirs()) if (!parsed.plugin || plugin === parsed.plugin) dirs.add(dir);
  return [...dirs];
}

/** One-line account of where a definition is looked for, for ask reasons. */
export function describeAgentLookup(cwd: string | undefined): string {
  const project = projectAgentDirs(cwd);
  return (
    `${project.length > 0 ? project.join(', ') : '<cwd>/.claude/agents'} (hook cwd up to the project root), ` +
    `${path.join(homeDir(), '.claude', 'agents')}, and the agents/ dir of each plugin in ` +
    `${path.join(homeDir(), '.claude', 'plugins', 'installed_plugins.json')} (a "plugin:agent" name only looks in that plugin)`
  );
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
