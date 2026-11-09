/**
 * The last advice given in a session, so a later hook can join "recommended" with "used"
 * (case study 5.7 and 5.10). The UserPromptSubmit advisor writes it; the PreToolUse reminder reads it.
 *
 * It lives next to the session-model state (`<session_id>.advice.json` in `claudeStateDir()`), is
 * pruned with it, and holds model names, a tier and a prompt hash: never prompt text. Reading and
 * writing are best effort and can never fail a turn.
 */
import * as fs from 'node:fs';
import * as path from 'node:path';
import { TELEMETRY_FILE } from './core/index.js';
import { claudeDecisionsFile } from './claude-audit.js';
import { claudeStateDir, safeSessionId } from './session-model.js';

export interface LastAdvice {
  /** `turnId` of the prompt the advice was given for. */
  turn: string;
  tier: string;
  /** The model recommended, after the user's own instruction was applied. */
  model: string;
  subagent: string;
  /** What set the tier: absent for the router, else the user's instruction. */
  by?: string;
  /** ISO time the advice was given. */
  at: string;
}

function adviceFile(sessionId: unknown): string | null {
  const id = safeSessionId(sessionId);
  return id ? path.join(claudeStateDir(), `${id}.advice.json`) : null;
}

export function writeLastAdvice(sessionId: unknown, advice: LastAdvice): boolean {
  const file = adviceFile(sessionId);
  if (!file) return false;
  try {
    fs.mkdirSync(path.dirname(file), { recursive: true });
    const tmp = `${file}.${process.pid}.tmp`;
    fs.writeFileSync(tmp, JSON.stringify(advice));
    fs.renameSync(tmp, file);
    return true;
  } catch {
    return false;
  }
}

export function readLastAdvice(sessionId: unknown): LastAdvice | null {
  const file = adviceFile(sessionId);
  if (!file) return null;
  try {
    const raw = JSON.parse(fs.readFileSync(file, 'utf8'));
    const ok = ['turn', 'tier', 'model', 'subagent', 'at'].every((k) => typeof raw?.[k] === 'string');
    return ok ? (raw as LastAdvice) : null;
  } catch {
    return null;
  }
}

// ── Reading the logs for the pre-flight reminder (5.10) ─────────────────────────────────────────

/** A dispatch call counts for this long when deciding whether to remind. */
export const DISPATCH_WINDOW_MS = 30 * 60 * 1000;
/** Agent calls and reminders are counted over this long. */
export const AGENT_WINDOW_MS = 10 * 60 * 1000;
const TAIL_BYTES = 128 * 1024;

/** The parsed JSON lines in the last 128 KiB of a file. Unreadable or missing: none. Never throws. */
export function tailJsonl(file: string, maxBytes = TAIL_BYTES): Record<string, unknown>[] {
  try {
    const fd = fs.openSync(file, 'r');
    try {
      const size = fs.fstatSync(fd).size;
      const start = Math.max(0, size - maxBytes);
      const buf = Buffer.alloc(size - start);
      fs.readSync(fd, buf, 0, buf.length, start);
      const lines = buf.toString('utf8').split('\n');
      if (start > 0) lines.shift(); // cut mid-record
      const out: Record<string, unknown>[] = [];
      for (const line of lines) {
        if (!line) continue;
        try {
          const parsed = JSON.parse(line);
          if (parsed && typeof parsed === 'object') out.push(parsed as Record<string, unknown>);
        } catch {
          // skip a partial line
        }
      }
      return out;
    } finally {
      fs.closeSync(fd);
    }
  } catch {
    return [];
  }
}

const stamp = (entry: Record<string, unknown>): number => {
  const raw = entry.timestamp ?? entry.ts;
  return typeof raw === 'string' ? Date.parse(raw) : NaN;
};

/**
 * Whether `jev_dispatch` or `jev_batch_dispatch` ran from Claude Code within the window, from the
 * shared telemetry file both tools append to. The tools record no session id, so this is by time
 * and harness: with two Claude sessions open, a dispatch in one silences the reminder in the other.
 * A false "yes" only costs a missed reminder, never a blocked call.
 */
export function hasRecentDispatch(opts: { file?: string; now?: number; windowMs?: number } = {}): boolean {
  const file = opts.file ?? TELEMETRY_FILE;
  const now = opts.now ?? Date.now();
  const windowMs = opts.windowMs ?? DISPATCH_WINDOW_MS;
  return tailJsonl(file).some((e) => {
    if (e.tool !== 'jev_dispatch' && e.tool !== 'jev_batch_dispatch') return false;
    if (e.harness !== 'claude-code') return false;
    const at = stamp(e);
    return Number.isFinite(at) && now - at <= windowMs && at <= now + 60_000;
  });
}

/** The session's recent `agent_call` records in the decisions log, and whether one already carried a reminder. */
export function recentAgentCalls(
  sessionId: string | null,
  opts: { file?: string; now?: number; windowMs?: number } = {}
): { count: number; reminded: boolean } {
  if (!sessionId) return { count: 0, reminded: false };
  const file = opts.file ?? claudeDecisionsFile();
  const now = opts.now ?? Date.now();
  const windowMs = opts.windowMs ?? AGENT_WINDOW_MS;
  const mine = tailJsonl(file).filter((e) => {
    const at = stamp(e);
    return e.event === 'agent_call' && e.session_id === sessionId && Number.isFinite(at) && now - at <= windowMs;
  });
  return { count: mine.length, reminded: mine.some((e) => e.reminded === true) };
}
