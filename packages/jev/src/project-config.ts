/**
 * Project-level Jev config for the guard: it may TIGHTEN the user config, never loosen it.
 *
 * Files: `.agents/jev.json` and `.jev.json` in the hook cwd and in every parent up to the filesystem
 * root, in `CLAUDE_PROJECT_DIR` and its parents, and (in a linked git worktree) in the main checkout
 * and its parents; see `configDirs` in project-root.ts. The search does NOT stop at the project root
 * or the git root. ALL of them count: the lowest valid `guard.ceiling` and the highest mode win, and
 * one malformed file makes the result malformed, so a nested file can never cancel an ancestor's
 * ceiling. Discovery uses lstat, so a dangling symlink is a file that cannot be read, not "no file".
 *
 * Read by the guard hook only. Keys it looks at (everything else is ignored, and never makes the
 * file malformed):
 * - `guard.ceiling`: a model Jev can rank. The guard compares requests against the LOWER of the
 *   session model and this ceiling, so a repository can cap what its sessions may delegate to.
 *   A ceiling above the session model changes nothing. Subagents that inherit the session model
 *   (no model, `inherit`, a built-in type, a definition without a model) are ranked too.
 * - `defaults.mode`, `harnesses.<harness>.mode`: resolved like the user config. They can keep or
 *   raise the user's mode and never lower it (`tightenMode`); for the guard `on` and `on-demand`
 *   behave the same, so this is recorded in the audit and nothing more.
 *
 * Precedence, in full: with the user's mode `off` (explicit, or no user config file) the guard is
 * silent and this file is not read at all, so a repository cannot switch the guard on for a user who
 * turned it off. Otherwise a valid project file tightens as above. A project file that cannot be
 * trusted (unreadable, not a regular file, over 1 MB, not JSON, not an object, a key it reads with
 * the wrong type, an unknown mode, a ceiling Jev cannot rank) asks on every guarded call.
 *
 * The MCP-side loader is unchanged: it still takes no mode from a project file
 * (`sanitizeProjectConfig`).
 */
import * as fsSync from 'node:fs';
import * as path from 'node:path';
import { DEFAULT_CONFIG_IO, VALID_JEV_MODES, readConfigFile, type JevHarness, type JevMode } from './core/config.js';
import { compareModels, parseModel } from './models.js';
import { configDirs } from './project-root.js';

export type ProjectConfig =
  | { state: 'none' }
  | { state: 'malformed'; file: string; problems: string[] }
  | { state: 'valid'; file: string; mode: JevMode | null; ceiling: string | null };

/**
 * Every project config file that applies to `startDir`, nearest first: `.agents/jev.json` and
 * `.jev.json` in each directory from the cwd up to the FILESYSTEM ROOT (not just the project root),
 * then the same for `CLAUDE_PROJECT_DIR` and, in a linked git worktree, the main checkout
 * (`configDirs` in project-root.ts). Both names in one directory count, `.agents/jev.json` first.
 */
export function findProjectConfigs(startDir: string): string[] {
  const files: string[] = [];
  for (const dir of configDirs(path.resolve(startDir))) {
    for (const rel of [path.join('.agents', 'jev.json'), '.jev.json']) {
      const file = path.join(dir, rel);
      try {
        fsSync.lstatSync(file);
        files.push(file);
      } catch {
        // not here
      }
    }
  }
  return files;
}

/** The nearest project config file from `startDir` up, or null. */
export function findProjectConfig(startDir: string): string | null {
  return findProjectConfigs(startDir)[0] ?? null;
}

const isObj = (v: unknown): v is Record<string, unknown> => v !== null && typeof v === 'object' && !Array.isArray(v);
const describe = (v: unknown) => (v === null ? 'null' : Array.isArray(v) ? 'an array' : `a ${typeof v}`);

function modeProblem(at: string, v: unknown, out: string[]): void {
  if (v === undefined) return;
  if (typeof v !== 'string' || !(VALID_JEV_MODES as readonly string[]).includes(v)) {
    out.push(`${at} must be one of ${VALID_JEV_MODES.join(', ')}, found ${typeof v === 'string' ? JSON.stringify(v) : describe(v)}`);
  }
}

/** The problems with the keys the guard reads. Unknown and unrelated keys are not problems. */
function guardProblems(cfg: unknown): string[] {
  if (!isObj(cfg)) return [`the top level must be a JSON object, found ${describe(cfg)}`];
  const out: string[] = [];
  if (cfg.defaults !== undefined) {
    if (!isObj(cfg.defaults)) out.push(`defaults must be an object, found ${describe(cfg.defaults)}`);
    else modeProblem('defaults.mode', cfg.defaults.mode, out);
  }
  if (cfg.harnesses !== undefined) {
    if (!isObj(cfg.harnesses)) out.push(`harnesses must be an object, found ${describe(cfg.harnesses)}`);
    else {
      for (const [name, entry] of Object.entries(cfg.harnesses)) {
        if (!isObj(entry)) out.push(`harnesses.${name} must be an object, found ${describe(entry)}`);
        else modeProblem(`harnesses.${name}.mode`, entry.mode, out);
      }
    }
  }
  if (cfg.guard !== undefined) {
    if (!isObj(cfg.guard)) out.push(`guard must be an object, found ${describe(cfg.guard)}`);
    else if (cfg.guard.ceiling !== undefined) {
      const c = cfg.guard.ceiling;
      if (typeof c !== 'string') out.push(`guard.ceiling must be a model name, found ${describe(c)}`);
      else if (parseModel(c) === 'unknown') out.push(`guard.ceiling ${JSON.stringify(c)} is not a model Jev can rank`);
    }
  }
  return out;
}

/** Read and validate ONE project config file. A file that is gone by now counts as none. */
function inspectOne(file: string, harness: JevHarness): ProjectConfig {
  const read = readConfigFile(file, DEFAULT_CONFIG_IO);
  if (read.kind === 'missing') return { state: 'none' };
  if (read.kind === 'problem') return { state: 'malformed', file, problems: [read.problem] };
  let parsed: unknown;
  try {
    parsed = JSON.parse(read.text);
  } catch {
    return { state: 'malformed', file, problems: ['the file is not valid JSON'] };
  }
  const problems = guardProblems(parsed);
  if (problems.length > 0) return { state: 'malformed', file, problems };
  const cfg = parsed as { defaults?: { mode?: JevMode }; harnesses?: Record<string, { mode?: JevMode }>; guard?: { ceiling?: string } };
  return {
    state: 'valid',
    file,
    mode: cfg.harnesses?.[harness]?.mode ?? cfg.defaults?.mode ?? null,
    ceiling: cfg.guard?.ceiling ?? null
  };
}

const MODE_RANK: Record<JevMode, number> = { off: 0, 'on-demand': 1, on: 2 };

/**
 * Read and validate the project config that applies to `startDir`, for `harness`. Never throws.
 *
 * EVERY file from the cwd up to the FILESYSTEM root counts (not just the project or git root), plus the
 * ones above `CLAUDE_PROJECT_DIR` and, in a linked git worktree, above the main checkout (see
 * `findProjectConfigs`), so a nested file can never cancel an ancestor's ceiling: the lowest valid
 * ceiling and the highest mode across them win, and one malformed file anywhere on the way makes the whole result malformed (the nearest malformed file
 * is the one reported). `file` is the file the effective ceiling came from, else the nearest file.
 */
export function inspectProjectConfig(startDir: string | undefined, harness: JevHarness = 'claude-code'): ProjectConfig {
  let files: string[];
  try {
    files = findProjectConfigs(startDir && path.isAbsolute(startDir) ? startDir : process.cwd());
  } catch {
    return { state: 'none' };
  }
  let mode: JevMode | null = null;
  let ceiling: string | null = null;
  let ceilingFile: string | null = null;
  let nearest: string | null = null;
  for (const file of files) {
    const one = inspectOne(file, harness);
    if (one.state === 'malformed') return one;
    if (one.state === 'none') continue;
    nearest ??= one.file;
    if (one.mode !== null && (mode === null || MODE_RANK[one.mode] > MODE_RANK[mode])) mode = one.mode;
    if (one.ceiling !== null) {
      const next = parseModel(one.ceiling);
      const have = ceiling === null ? 'unknown' : parseModel(ceiling);
      if (ceiling === null || (next !== 'unknown' && have !== 'unknown' && compareModels(next, have) < 0)) {
        ceiling = one.ceiling;
        ceilingFile = one.file;
      }
    }
  }
  if (nearest === null) return { state: 'none' };
  return { state: 'valid', file: ceilingFile ?? nearest, mode, ceiling };
}

/**
 * The effective mode: the stricter of the user's and the project's, where the project can only keep
 * or raise it. A user mode of `off` stays `off` (the project cannot switch the guard on), and a
 * project `off` or unset changes nothing.
 */
export function tightenMode(user: JevMode, project: JevMode | null): JevMode {
  if (user === 'off' || project === null) return user;
  return MODE_RANK[project] > MODE_RANK[user] ? project : user;
}

/**
 * The weaker of the session model and the project ceiling. An unknown session model stays as it is
 * (the guard asks about it regardless), and so does one Jev cannot rank.
 */
export function lowerModel(session: string | null, ceiling: string): string | null {
  if (!session) return session;
  const s = parseModel(session);
  const c = parseModel(ceiling);
  if (s === 'unknown' || c === 'unknown') return session;
  return compareModels(c, s) < 0 ? ceiling : session;
}

