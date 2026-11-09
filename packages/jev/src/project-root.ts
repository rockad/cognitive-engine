/**
 * Where a project starts, for the lookups that walk up from the hook cwd. Project agent definitions
 * stop at the project root (`projectDirs`): `CLAUDE_PROJECT_DIR` when set, else the git root, else
 * nowhere (the walk then runs to the filesystem root). The Jev config search does NOT stop there
 * (`configDirs`): it runs to the filesystem root and adds the main checkout of a linked worktree.
 */
import * as fs from 'node:fs';
import * as path from 'node:path';

export function isWithin(root: string, dir: string): boolean {
  const rel = path.relative(root, dir);
  return rel === '' || (!rel.startsWith('..') && !path.isAbsolute(rel));
}

export function gitRoot(start: string): string | null {
  let dir = start;
  for (;;) {
    if (fs.existsSync(path.join(dir, '.git'))) return dir;
    const parent = path.dirname(dir);
    if (parent === dir) return null;
    dir = parent;
  }
}

/** `CLAUDE_PROJECT_DIR`, resolved, or null when unset. */
export function envProjectRoot(): string | null {
  return process.env.CLAUDE_PROJECT_DIR ? path.resolve(process.env.CLAUDE_PROJECT_DIR) : null;
}

/**
 * The directories from `cwd` up to the project root, nearest first, plus `CLAUDE_PROJECT_DIR` when
 * the cwd is outside it. The walk stops at the project root: `CLAUDE_PROJECT_DIR` when the cwd is
 * within it, else (cwd outside it) the cwd itself, else the git root, else the filesystem root.
 */
export function projectDirs(cwd: string | undefined): string[] {
  const envRoot = envProjectRoot();
  const dirs: string[] = [];
  if (cwd && path.isAbsolute(cwd)) {
    const start = path.resolve(cwd);
    const stop = envRoot ? (isWithin(envRoot, start) ? envRoot : start) : gitRoot(start);
    let dir = start;
    for (;;) {
      dirs.push(dir);
      const parent = path.dirname(dir);
      if (dir === stop || parent === dir) break;
      dir = parent;
    }
  }
  if (envRoot && !dirs.includes(envRoot)) dirs.push(envRoot);
  return dirs;
}

/** `dir` and every parent of it up to the filesystem root, nearest first. */
function ancestorsOf(dir: string): string[] {
  const out: string[] = [];
  let d = path.resolve(dir);
  for (;;) {
    out.push(d);
    const parent = path.dirname(d);
    if (parent === d) return out;
    d = parent;
  }
}

/**
 * The main checkout a linked git worktree belongs to, or null. A linked worktree's `.git` is a FILE
 * (`gitdir: <main>/.git/worktrees/<name>`); that directory's `commondir` names the main `.git`
 * (default `../..`), whose parent is the main checkout. Anything unreadable or odd yields null.
 */
export function mainCheckoutOf(root: string): string | null {
  try {
    const dotGit = path.join(root, '.git');
    if (!fs.lstatSync(dotGit).isFile()) return null;
    const pointer = /^gitdir:[ \t]*(.+?)[ \t]*$/m.exec(fs.readFileSync(dotGit, 'utf8').slice(0, 4096));
    if (!pointer) return null;
    const gitdir = path.resolve(root, pointer[1]);
    let common = path.resolve(gitdir, '..', '..');
    try {
      const named = fs.readFileSync(path.join(gitdir, 'commondir'), 'utf8').trim();
      if (named !== '') common = path.resolve(gitdir, named);
    } catch {
      // no commondir file: the default stands
    }
    if (!fs.statSync(common).isDirectory()) return null;
    return path.basename(common) === '.git' ? path.dirname(common) : null;
  } catch {
    return null;
  }
}

/**
 * Every directory whose Jev config file applies to a session at `cwd`, nearest first. Unlike
 * `projectDirs` this does NOT stop at the project root: the walk runs from the cwd to the filesystem
 * root, so a ceiling in a parent workspace still counts and a planted `sub/.git` cannot cut it off.
 * It also covers, each walked to the filesystem root:
 * - `CLAUDE_PROJECT_DIR` when it is outside the cwd's own chain;
 * - when the cwd's project root (`CLAUDE_PROJECT_DIR` or the git root) is a linked git worktree (its
 *   `.git` is a file), the main checkout it was made from, so a ceiling kept in the main checkout
 *   still applies in `.claude/worktrees/<name>` and in sibling worktrees.
 */
export function configDirs(cwd: string | undefined): string[] {
  const envRoot = envProjectRoot();
  const dirs: string[] = [];
  const add = (from: string) => {
    for (const d of ancestorsOf(from)) if (!dirs.includes(d)) dirs.push(d);
  };
  const roots: string[] = [];
  if (cwd && path.isAbsolute(cwd)) {
    const start = path.resolve(cwd);
    add(start);
    const git = gitRoot(start);
    if (git) roots.push(git);
  }
  if (envRoot) {
    add(envRoot);
    roots.push(envRoot);
  }
  for (const root of roots) {
    const main = mainCheckoutOf(root);
    if (main) add(main);
  }
  return dirs;
}
