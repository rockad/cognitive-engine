/**
 * Where a project starts, for the lookups that walk up from the hook cwd (project Jev config,
 * project agent definitions): `CLAUDE_PROJECT_DIR` when set, else the git root, else nowhere (the
 * walk then runs to the filesystem root).
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
