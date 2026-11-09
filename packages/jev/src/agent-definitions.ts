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
 * Within each directory every `.md` file (subdirectories included) is read and matched on its
 * frontmatter `name:`, as Claude Code registers agents; see `scanAgentDir` for the conflict rules
 * (two claimants, a `<name>.md` that declares another name, an unreadable name).
 *
 * The name is restricted to file-name characters (one `:` allowed, as the plugin separator), so a
 * path-like name never escapes the directories. A name that resolves to no file is "not found",
 * which callers treat as unverifiable and ask about; this module never decides to allow.
 */
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { parseFrontmatterFields, readClaudeCodeName, unreadableModelValue } from './agent-frontmatter.js';
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

/** Claude Code's built-in subagent types: they have no definition file and run on the session model. */
export const BUILTIN_AGENT_TYPES: ReadonlySet<string> = new Set(['general-purpose', 'Explore', 'Plan', 'statusline-setup', 'claude-code-guide', 'claude']);

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

/**
 * Claude Code's user config dir: `CLAUDE_CONFIG_DIR` when set and non-empty (it replaces `~/.claude`
 * entirely; a relative value is resolved against the working directory, verified on 2.1.318), else
 * `~/.claude`. `cwd` is the hook's working directory (default `process.cwd()`).
 */
export function userClaudeDir(cwd?: string): string {
  const dir = process.env.CLAUDE_CONFIG_DIR;
  if (dir) return path.resolve(cwd || process.cwd(), dir);
  return path.join(homeDir(), '.claude');
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
  const files = [path.join(userClaudeDir(cwd), 'settings.json')];
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
    doc = JSON.parse(fs.readFileSync(path.join(userClaudeDir(cwd), 'plugins', 'installed_plugins.json'), 'utf8'));
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
    dirs.add(path.join(userClaudeDir(cwd), 'agents'));
  }
  for (const [plugin, dir] of pluginAgentDirs(cwd)) if (!parsed.plugin || plugin === parsed.plugin) dirs.add(dir);
  return [...dirs];
}

/** One-line account of where a definition is looked for, for ask reasons. */
export function describeAgentLookup(cwd: string | undefined): string {
  const project = projectAgentDirs(cwd);
  return (
    `${project.length > 0 ? project.join(', ') : '<cwd>/.claude/agents'} (hook cwd up to the project root), ` +
    `${path.join(userClaudeDir(cwd), 'agents')}, and the agents/ dir of each plugin in ` +
    `${path.join(userClaudeDir(cwd), 'plugins', 'installed_plugins.json')} (enabled plugins first; a "plugin:agent" name only looks in that plugin)`
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

/** Most `.md` files read from one agents directory tree; past it the directory is a conflict (ask). */
const MAX_AGENT_FILES = 2000;
const MAX_AGENT_DEPTH = 8;
/** An agent file larger than this is not parsed; it can only be a conflict. */
const MAX_AGENT_FILE_BYTES = 1024 * 1024;

/** Every `.md` file under `root`, subdirectories included, sorted; null when `root` is not a readable directory. */
function listMarkdown(root: string): { files: string[]; truncated: boolean } | null {
  const files: string[] = [];
  const visited = new Set<string>();
  let truncated = false;
  const walk = (dir: string, depth: number): boolean => {
    let real: string;
    let entries: fs.Dirent[];
    try {
      real = fs.realpathSync(dir);
      if (visited.has(real)) return true;
      visited.add(real);
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch {
      return false;
    }
    entries.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
    for (const entry of entries) {
      const full = path.join(dir, entry.name);
      let isDir = entry.isDirectory();
      let isFile = entry.isFile();
      if (entry.isSymbolicLink()) {
        try {
          const st = fs.statSync(full);
          isDir = st.isDirectory();
          isFile = st.isFile();
        } catch {
          continue; // dangling
        }
      }
      if (isDir) {
        if (depth >= MAX_AGENT_DEPTH) truncated = true;
        else walk(full, depth + 1);
      } else if (isFile && /\.md$/i.test(entry.name)) {
        if (files.length >= MAX_AGENT_FILES) {
          truncated = true;
          return true;
        }
        files.push(full);
      }
    }
    return true;
  };
  return walk(root, 0) ? { files, truncated } : null;
}

type DirScan =
  | { kind: 'none' }
  | { kind: 'conflict'; reason: string }
  | { kind: 'def'; file: string; model: string | null; unreadable: boolean };

/**
 * The definition of `agent` in ONE agents directory, as Claude Code would register it: every `.md`
 * file under it (subdirectories included) is read and registered under its frontmatter `name:`.
 * - a file whose `name:` is the agent claims it;
 * - a file called `<agent>.md` with no `name:` at all claims it by its file name (as always);
 * - a file called `<agent>.md` that declares ANOTHER name, or whose name cannot be read, is a
 *   conflict (which file Claude Code runs is not known);
 * - another file whose name cannot be read is a conflict when it could be this agent: Claude Code's
 *   own reading gives the agent, is unavailable, or the text mentions the agent;
 * - more than one claimant is a conflict, and so is a tree too large to read.
 * A conflict makes both guards ask.
 */
function scanAgentDir(dir: string, agent: string): DirScan {
  const listing = listMarkdown(dir);
  if (listing === null) return { kind: 'none' };
  const claims: Array<{ file: string; model: string | null; unreadable: boolean }> = [];
  const conflicts: string[] = [];
  if (listing.truncated) conflicts.push(`${dir} holds more agent files than Jev reads (${MAX_AGENT_FILES}), or nests deeper than ${MAX_AGENT_DEPTH} levels`);
  for (const file of listing.files) {
    const byFileName = path.basename(file).replace(/\.md$/i, '') === agent;
    let text: string;
    try {
      if (fs.statSync(file).size > MAX_AGENT_FILE_BYTES) {
        if (byFileName) conflicts.push(`${file} is over ${MAX_AGENT_FILE_BYTES} bytes and is not read`);
        continue;
      }
      text = fs.readFileSync(file, 'utf8');
    } catch {
      continue; // not readable: not a definition (as before)
    }
    const { model, name } = parseFrontmatterFields(text);
    const claim = () => ({
      file,
      unreadable: model.kind === 'unreadable',
      model: model.kind === 'unreadable' ? unreadableModelValue(model.reason) : model.kind === 'model' ? model.value : null
    });
    if (name.kind === 'name') {
      if (name.value === agent) claims.push(claim());
      else if (byFileName) conflicts.push(`${file} declares the name "${name.value}", so it is not clear which file defines "${agent}"`);
    } else if (name.kind === 'none') {
      if (byFileName) claims.push(claim());
    } else if (byFileName) {
      conflicts.push(`${file} has a name that cannot be read (${name.reason})`);
    } else {
      const cc = readClaudeCodeName(text);
      if (cc.kind === 'unreadable') conflicts.push(`${file} has a name that cannot be read (${name.reason})`);
      else if (cc.kind === 'name' && cc.value === agent) conflicts.push(`${file} may declare the name "${agent}" (${name.reason})`);
      else if (text.includes(agent)) conflicts.push(`${file} has a name that cannot be read (${name.reason}) and mentions "${agent}"`);
    }
  }
  if (conflicts.length > 0) return { kind: 'conflict', reason: `${conflicts.join('; ')}` };
  if (claims.length === 0) return { kind: 'none' };
  if (claims.length > 1) {
    return { kind: 'conflict', reason: `more than one file in ${dir} defines "${agent}", and which one Claude Code runs is not known: ${claims.map((c) => `${c.file} (${c.model ?? 'no model'})`).join(' vs ')}` };
  }
  return { kind: 'def', ...claims[0] };
}

export function readAgentDefinition(subagentType: string | undefined, cwd: string | undefined): AgentDefinition {
  const result: AgentDefinition = { found: false, model: null };
  const parsed = parseAgentName(subagentType);
  if (!parsed) return result;
  // (a walk without a project root can reach $HOME, whose .claude/agents is the USER level, not the project's)
  const userDir = path.join(userClaudeDir(cwd), 'agents');
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
    const scan = scanAgentDir(dir, parsed.agent);
    if (scan.kind === 'none') continue; // no definition here
    if (scan.kind === 'conflict') return { found: true, model: unreadableModelValue(scan.reason) };
    if (!inProject) return { found: true, model: scan.model };
    project.push(scan);
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
