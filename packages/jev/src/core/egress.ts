/**
 * Outbound data control (egress policy).
 *
 * Policy lives in its own file, ~/.config/jev/egress.json, which jev never writes:
 *   { "allowPaths": ["~/Work/public-repo"], "denyPaths": ["~/Work/private"] }
 *
 * - No file and no JEV_EGRESS_DENY: no policy, every request goes out as before.
 * - File present: the session directories (hook input cwd, CLAUDE_PROJECT_DIR, process.cwd())
 *   must all lie under an `allowPaths` entry (when the key is present) and under no `denyPaths` entry.
 * - File unreadable, not JSON, or the wrong shape: every outbound request is blocked.
 * - JEV_EGRESS_DENY (paths separated by the platform delimiter) only adds deny entries.
 *
 * Loopback hosts are always allowed. When a policy is active, every sent and blocked request is
 * logged to ~/.config/jev/egress-log.jsonl without its body.
 */
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';

export type ZdrPolicy = 'off' | 'on' | 'strict';

export type EgressPolicy =
  | { state: 'absent' }
  | { state: 'broken'; problem: string }
  | { state: 'active'; allowPaths: string[] | null; denyPaths: string[] };

export type EgressDecision = { allowed: true } | { allowed: false; reason: string };

export const EGRESS_DENIED_CODE = 'JEV_EGRESS_DENIED';

const LOOPBACK_HOSTS = new Set(['127.0.0.1', 'localhost', '::1', '[::1]']);

function homeDir(): string {
  return process.env.HOME || os.homedir();
}

export function egressPolicyFile(): string {
  return path.join(homeDir(), '.config', 'jev', 'egress.json');
}

export function egressLogFile(): string {
  return path.join(homeDir(), '.config', 'jev', 'egress-log.jsonl');
}

export function parseZdrValue(v: unknown): ZdrPolicy | null {
  if (v === undefined || v === null) return null;
  if (typeof v === 'boolean') return v ? 'on' : 'off';
  const s = String(v).trim().toLowerCase();
  if (!s) return null;
  if (s === 'on' || s === 'true' || s === 'yes' || s === '1') return 'on';
  if (s === 'off' || s === 'false' || s === 'no' || s === '0') return 'off';
  if (s === 'strict') return 'strict';
  // For any other unrecognized non-empty string (e.g. typos like 'strcit', 'onn', 'enabled'): fail closed and return 'strict'.
  return 'strict';
}

export function readZdrPolicy(): ZdrPolicy {
  const RANK: Record<ZdrPolicy, number> = { off: 0, on: 1, strict: 2 };
  let highest: ZdrPolicy = 'off';
  const consider = (val: ZdrPolicy | null) => {
    if (val && RANK[val] > RANK[highest]) highest = val;
  };

  // 1. ~/.config/jev/egress.json ("zdr": "on" | "strict" | "off" | true | false, or privacy?.zdr)
  const egressFile = egressPolicyFile();
  if (fs.existsSync(egressFile)) {
    try {
      const parsed = JSON.parse(fs.readFileSync(egressFile, 'utf8'));
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        if (parsed.zdr !== undefined) consider(parseZdrValue(parsed.zdr));
        if (parsed.privacy?.zdr !== undefined) consider(parseZdrValue(parsed.privacy.zdr));
      }
    } catch {}
  }

  // 2. ~/.config/jev/config.json (privacy?.zdr or root zdr)
  const configFile = process.env.JEV_CONFIG_FILE || path.join(homeDir(), '.config', 'jev', 'config.json');
  if (fs.existsSync(configFile)) {
    try {
      const parsed = JSON.parse(fs.readFileSync(configFile, 'utf8'));
      if (parsed && typeof parsed === 'object') {
        if (parsed.privacy?.zdr !== undefined) consider(parseZdrValue(parsed.privacy.zdr));
        if (parsed.zdr !== undefined) consider(parseZdrValue(parsed.zdr));
      }
    } catch {}
  }

  // 3. process.env.JEV_ZDR ("on" | "strict" | "off" | "1" | "0" | "true" | "false")
  if (process.env.JEV_ZDR !== undefined) {
    consider(parseZdrValue(process.env.JEV_ZDR));
  }

  return highest;
}

function expandHome(p: string): string {
  if (p === '~') return homeDir();
  if (p.startsWith('~/')) return path.join(homeDir(), p.slice(2));
  return p;
}

/** Resolves symlinks (and letter case on case-insensitive volumes); a missing path stays lexical. */
export function canonicalPath(p: string): string {
  const resolved = path.resolve(expandHome(p));
  try {
    return fs.realpathSync.native(resolved);
  } catch {
    return resolved;
  }
}

export function isInside(dir: string, parent: string): boolean {
  const rel = path.relative(parent, dir);
  return rel === '' || (rel !== '..' && !rel.startsWith(`..${path.sep}`) && !path.isAbsolute(rel));
}

function isStringList(v: unknown): v is string[] {
  return Array.isArray(v) && v.every((s) => typeof s === 'string' && s.trim() !== '');
}

function envDenyPaths(): string[] {
  return (process.env.JEV_EGRESS_DENY || '')
    .split(path.delimiter)
    .map((s) => s.trim())
    .filter(Boolean);
}

export function readEgressPolicy(): EgressPolicy {
  const envDeny = envDenyPaths();
  const file = egressPolicyFile();
  if (!fs.existsSync(file)) {
    return envDeny.length > 0 ? { state: 'active', allowPaths: null, denyPaths: envDeny } : { state: 'absent' };
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch {
    return { state: 'broken', problem: 'egress.json is not readable JSON' };
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return { state: 'broken', problem: 'egress.json must be a JSON object' };
  }
  const { allowPaths, denyPaths } = parsed as Record<string, unknown>;
  if (allowPaths !== undefined && !isStringList(allowPaths)) {
    return { state: 'broken', problem: 'allowPaths must be a list of paths' };
  }
  if (denyPaths !== undefined && !isStringList(denyPaths)) {
    return { state: 'broken', problem: 'denyPaths must be a list of paths' };
  }
  return {
    state: 'active',
    allowPaths: allowPaths === undefined ? null : allowPaths,
    denyPaths: [...(denyPaths ?? []), ...envDeny]
  };
}

/** Directories the current request is made from: hook input cwd plus the process context. */
export function egressContextDirs(extraDirs: Array<string | undefined> = []): string[] {
  const dirs = [...extraDirs, process.env.CLAUDE_PROJECT_DIR, process.cwd()];
  return Array.from(new Set(dirs.filter((d): d is string => typeof d === 'string' && d.trim() !== '')));
}

export function checkEgress(dirs: string[], policy: EgressPolicy = readEgressPolicy()): EgressDecision {
  if (policy.state === 'absent') return { allowed: true };
  if (policy.state === 'broken') return { allowed: false, reason: policy.problem };
  if (dirs.length === 0) return { allowed: false, reason: 'no session directory to check' };

  const allow = policy.allowPaths?.map(canonicalPath) ?? null;
  const deny = policy.denyPaths.map(canonicalPath);
  for (const dir of dirs.map(canonicalPath)) {
    if (allow && !allow.some((a) => isInside(dir, a))) {
      return { allowed: false, reason: 'session directory is outside allowPaths' };
    }
    if (deny.some((d) => isInside(dir, d))) {
      return { allowed: false, reason: 'session directory is inside denyPaths' };
    }
  }
  return { allowed: true };
}

function isLoopback(url: string): boolean {
  try {
    return LOOPBACK_HOSTS.has(new URL(url).hostname);
  } catch {
    return false;
  }
}

function modelOf(body: unknown): string | null {
  if (typeof body !== 'string') return null;
  try {
    const m = (JSON.parse(body) as { model?: unknown }).model;
    return typeof m === 'string' ? m : null;
  } catch {
    return null;
  }
}

function isZdrApplied(body: unknown): boolean {
  if (typeof body !== 'string') return false;
  try {
    const b = JSON.parse(body) as { provider?: { zdr?: unknown } };
    return Boolean(b?.provider?.zdr);
  } catch {
    return false;
  }
}

function logEgress(entry: Record<string, unknown>): void {
  try {
    const file = egressLogFile();
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.appendFileSync(file, JSON.stringify({ ts: new Date().toISOString(), ...entry }) + '\n');
  } catch {
    // the log is best-effort; the decision itself never depends on it
  }
}

/**
 * Throws before any byte leaves the machine when the policy blocks this request.
 * The error message names no directory: error text can reach tracing spans.
 */
export function assertEgressAllowed(url: string, body: unknown, extraDirs: Array<string | undefined> = []): void {
  if (isLoopback(url)) return;
  const policy = readEgressPolicy();
  const zdr_policy = readZdrPolicy();
  if (policy.state === 'absent' && zdr_policy === 'off') return;

  const dirs = egressContextDirs(extraDirs);
  const decision = checkEgress(dirs, policy);
  let host = '';
  try {
    host = new URL(url).host;
  } catch {
    host = 'invalid-url';
  }
  const zdr_applied = isZdrApplied(body);
  const meta = {
    host,
    model: modelOf(body),
    bytes: typeof body === 'string' ? Buffer.byteLength(body) : 0,
    dirs,
    zdr_policy,
    zdr_applied
  };
  if (decision.allowed) {
    logEgress({ decision: 'sent', ...meta });
    return;
  }
  logEgress({ decision: 'blocked', reason: decision.reason, ...meta });
  const err = new Error(`jev: outbound request blocked by egress policy (${decision.reason})`);
  (err as Error & { code?: string }).code = EGRESS_DENIED_CODE;
  throw err;
}
