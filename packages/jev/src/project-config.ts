/**
 * Project-level Jev config for the guard: it may TIGHTEN the user config, never loosen it.
 *
 * File: `.agents/jev.json`, else `.jev.json`, in the nearest directory (from the hook cwd upward)
 * that has one, the same names and order the loader uses (`findProjectConfigFile`). Discovery here
 * uses lstat, so a dangling symlink is a file that cannot be read, not "no file".
 *
 * Read by the guard hook only. Keys it looks at (everything else is ignored, and never makes the
 * file malformed):
 * - `guard.ceiling`: a model Jev can rank. The guard compares requests against the LOWER of the
 *   session model and this ceiling, so a repository can cap what its sessions may delegate to.
 *   A ceiling above the session model changes nothing.
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

export type ProjectConfig =
  | { state: 'none' }
  | { state: 'malformed'; file: string; problems: string[] }
  | { state: 'valid'; file: string; mode: JevMode | null; ceiling: string | null };

/** Nearest project config file from `startDir` up, `.agents/jev.json` before `.jev.json` in each directory. */
export function findProjectConfig(startDir: string): string | null {
  let dir = path.resolve(startDir);
  for (;;) {
    for (const rel of [path.join('.agents', 'jev.json'), '.jev.json']) {
      const file = path.join(dir, rel);
      try {
        fsSync.lstatSync(file);
        return file;
      } catch {
        // not here
      }
    }
    const parent = path.dirname(dir);
    if (parent === dir) return null;
    dir = parent;
  }
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

/** Read and validate the project config that applies to `startDir`, for `harness`. Never throws. */
export function inspectProjectConfig(startDir: string | undefined, harness: JevHarness = 'claude-code'): ProjectConfig {
  let file: string | null;
  try {
    file = findProjectConfig(startDir && path.isAbsolute(startDir) ? startDir : process.cwd());
  } catch {
    return { state: 'none' };
  }
  if (!file) return { state: 'none' };
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

