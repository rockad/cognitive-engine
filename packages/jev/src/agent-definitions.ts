/**
 * Subagent definition lookup, nearest first:
 *   1. `.claude/agents/<name>.md` in the hook cwd, then in each parent up to the project root
 *      (`CLAUDE_PROJECT_DIR` if set, else the git root, else the filesystem root), and
 *      `$CLAUDE_PROJECT_DIR/.claude/agents` when the cwd is outside it. Claude Code loads project
 *      agents from the project root, so EVERY project-level definition of the name is read: when they
 *      disagree on the model (a subdirectory's file against the root's), the answer is a conflict
 *      value that makes the guards ask; when they agree, that model is used;
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
import { parseFrontmatterModel, unreadableModelValue } from './agent-frontmatter.js';
import { projectDirs } from './project-root.js';

export interface AgentDefinition {
  /** A definition file exists in one of the lookup locations. */
  found: boolean;
  /**
   * The `model:` from the first definition found, or null (it sets none, so it inherits). A
   * frontmatter whose model cannot be established is reported as a value Jev cannot rank.
   */
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

/** Project-level `.claude/agents` dirs: the cwd up to the project root, nearest first (project-root.ts). */
function projectAgentDirs(cwd: string | undefined): string[] {
  return projectDirs(cwd).map((dir) => path.join(dir, '.claude', 'agents'));
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

/** A definition's model as a comparable string: none and `inherit` are the same, case does not matter. */
function modelKey(model: string | null): string {
  const m = model === null ? '' : model.trim().toLowerCase();
  return m === '' ? 'inherit' : m;
}

export function readAgentDefinition(subagentType: string | undefined, cwd: string | undefined): AgentDefinition {
  const result: AgentDefinition = { found: false, model: null };
  const parsed = parseAgentName(subagentType);
  if (!parsed) return result;
  // (a walk without a project root can reach $HOME, whose .claude/agents is the USER level, not the project's)
  const userDir = path.join(homeDir(), '.claude', 'agents');
  const projectDirSet = new Set(parsed.plugin ? [] : projectAgentDirs(cwd).filter((d) => d !== userDir));
  // The FIRST definition file found decides, as in Claude Code (it uses only the highest-priority
  // definition). A nearer file with no model means "inherit" and is never skipped for a farther file
  // that names one; a nearer file whose model cannot be established is never replaced by a farther one.
  // One exception: Claude Code loads project agents from the project root, so a definition in a
  // subdirectory of the project must not shadow the root's for the guard alone. Every project-level
  // definition of the name is therefore read, and when they disagree on the model the answer is the
  // unrankable "conflict" value, which makes both guards ask.
  const project: Array<{ file: string; model: string | null; unreadable: boolean }> = [];
  for (const dir of agentLookupDirs(subagentType, cwd)) {
    const inProject = projectDirSet.has(dir);
    if (project.length > 0 && !inProject) break; // past the project: the project definition decided
    const file = path.join(dir, `${parsed.agent}.md`);
    let text: string;
    try {
      text = fs.readFileSync(file, 'utf8');
    } catch {
      continue; // no definition here
    }
    const front = parseFrontmatterModel(text);
    const found = {
      file,
      unreadable: front.kind === 'unreadable',
      model: front.kind === 'unreadable' ? unreadableModelValue(front.reason) : front.kind === 'model' ? front.value : null
    };
    if (!inProject) return { found: true, model: found.model };
    project.push(found);
  }
  if (project.length === 0) return result;
  const [first, ...others] = project;
  const differing = others.filter((o) => o.unreadable || first.unreadable || modelKey(o.model) !== modelKey(first.model));
  if (differing.length === 0) return { found: true, model: first.model };
  const describeOne = (d: { file: string; model: string | null }) => `${d.file} (${d.model ?? 'no model'})`;
  return {
    found: true,
    model: unreadableModelValue(
      `the project defines "${parsed.agent}" in more than one place with different models, and which one Claude Code runs is not known: ${[first, ...differing].map(describeOne).join(' vs ')}`
    )
  };
}
