/**
 * Session-model tracking for the Claude Code escalation guard.
 *
 * Evidence, ordered by time (see `resolveSessionModel`): the per-session state file written by the
 * PostModelSwitch recorder, and the model of the most recent assistant turn in the transcript.
 * `SessionStart` carries no model, PostModelSwitch never fired in headless runs, and a resume can
 * change the model silently, so the recorder also marks resumes and evidence older than the last
 * resume is discarded. Reading never throws; no usable evidence is just "unknown", which the guard
 * turns into an ask.
 */
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';

export type SessionModelSource = 'state' | 'transcript' | 'none';

export interface SessionModel {
  model: string | null;
  source: SessionModelSource;
}

export interface SessionState {
  model: string;
  /** What triggered the change: command, picker, sdk, auto or resume (as reported by Claude Code). */
  source?: string;
  updatedAt: string;
}

const STATE_MAX_AGE_MS = 14 * 24 * 60 * 60 * 1000;

function home(): string {
  return process.env.HOME || os.homedir();
}

/** Directory holding one `<session_id>.json` per session. Overridable for tests. */
export function claudeStateDir(): string {
  return process.env.JEV_CLAUDE_STATE_DIR || path.join(home(), '.config', 'jev', 'claude_sessions');
}

/** A session id reduced to file-name characters, or null when nothing usable is left. */
export function safeSessionId(sessionId: unknown): string | null {
  if (typeof sessionId !== 'string') return null;
  const id = sessionId.replace(/[^A-Za-z0-9_-]/g, '_').slice(0, 128);
  return id && /[A-Za-z0-9]/.test(id) ? id : null;
}

function stateFile(sessionId: unknown): string | null {
  const id = safeSessionId(sessionId);
  return id ? path.join(claudeStateDir(), `${id}.json`) : null;
}

/** `<session_id>.resumed.json`: when the session was last resumed. Ids carry no dots, so it cannot collide with a state file. */
function resumeFile(sessionId: unknown): string | null {
  const id = safeSessionId(sessionId);
  return id ? path.join(claudeStateDir(), `${id}.resumed.json`) : null;
}

/**
 * Record that the session was resumed. A resume can change the model with no PostModelSwitch event
 * and `SessionStart` carries no model, so everything dated before this instant describes a model
 * the session may no longer run on. Best effort: false when it could not be written.
 */
export function markSessionResumed(sessionId: unknown, now: Date = new Date()): boolean {
  const file = resumeFile(sessionId);
  if (!file) return false;
  try {
    fs.mkdirSync(path.dirname(file), { recursive: true });
    const tmp = `${file}.${process.pid}.tmp`;
    fs.writeFileSync(tmp, JSON.stringify({ resumedAt: now.toISOString() }));
    fs.renameSync(tmp, file);
    return true;
  } catch {
    return false;
  }
}

/**
 * Epoch ms of the last resume of this session, or null when it was never resumed. A marker that
 * exists but cannot be read fails closed as +Infinity: all evidence then counts as pre-resume.
 */
export function readResumedAt(sessionId: unknown): number | null {
  const file = resumeFile(sessionId);
  if (!file) return null;
  let text: string;
  try {
    text = fs.readFileSync(file, 'utf8');
  } catch (err) {
    return (err as NodeJS.ErrnoException)?.code === 'ENOENT' ? null : Number.POSITIVE_INFINITY;
  }
  try {
    const at = Date.parse(JSON.parse(text)?.resumedAt);
    return Number.isFinite(at) ? at : Number.POSITIVE_INFINITY;
  } catch {
    return Number.POSITIVE_INFINITY;
  }
}

export function writeSessionState(sessionId: unknown, model: string, source?: string, now: Date = new Date()): boolean {
  const file = stateFile(sessionId);
  if (!file || !model) return false;
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const state: SessionState = { model, ...(source ? { source } : {}), updatedAt: now.toISOString() };
  const tmp = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(state));
  fs.renameSync(tmp, file);
  return true;
}

export function readSessionState(sessionId: unknown): SessionState | null {
  const file = stateFile(sessionId);
  if (!file) return null;
  try {
    const parsed = JSON.parse(fs.readFileSync(file, 'utf8'));
    return typeof parsed?.model === 'string' && parsed.model ? (parsed as SessionState) : null;
  } catch {
    return null;
  }
}

/** Drop the state file and the resume marker of a session (a fresh start has neither). */
export function clearSessionState(sessionId: unknown): void {
  for (const file of [stateFile(sessionId), resumeFile(sessionId)]) {
    if (!file) continue;
    try {
      fs.rmSync(file, { force: true });
    } catch {
      // nothing to clear
    }
  }
}

/** Delete state files not touched for two weeks. Best effort. */
export function pruneSessionState(now = Date.now()): void {
  const dir = claudeStateDir();
  try {
    for (const name of fs.readdirSync(dir)) {
      if (!name.endsWith('.json')) continue;
      const file = path.join(dir, name);
      try {
        if (now - fs.statSync(file).mtimeMs > STATE_MAX_AGE_MS) fs.rmSync(file, { force: true });
      } catch {
        // skip
      }
    }
  } catch {
    // no state dir yet
  }
}

/** The most recent assistant turn of a transcript: its model and when it happened (epoch ms). */
export interface TranscriptEvidence {
  model: string;
  /** The entry's `timestamp`, else the file's mtime (an upper bound), else null. */
  at: number | null;
  /** `at` is the file's mtime: the turn happened at or before it, not exactly then. */
  bound: boolean;
}

/** The latest assistant turn in the tail of a Claude Code transcript, or null. */
export function transcriptEvidence(transcriptPath: string | undefined): TranscriptEvidence | null {
  if (!transcriptPath) return null;
  try {
    const TAIL_BYTES = 256 * 1024;
    const fd = fs.openSync(transcriptPath, 'r');
    try {
      const st = fs.fstatSync(fd);
      const start = Math.max(0, st.size - TAIL_BYTES);
      const buf = Buffer.alloc(st.size - start);
      fs.readSync(fd, buf, 0, buf.length, start);
      const lines = buf.toString('utf8').split('\n');
      if (start > 0) lines.shift(); // first line is likely cut mid-record
      for (let i = lines.length - 1; i >= 0; i--) {
        if (!lines[i]) continue;
        try {
          const entry = JSON.parse(lines[i]);
          const model = entry?.type === 'assistant' ? entry?.message?.model : undefined;
          if (typeof model === 'string' && model && !model.startsWith('<')) {
            const stamped = typeof entry.timestamp === 'string' ? Date.parse(entry.timestamp) : NaN;
            if (Number.isFinite(stamped)) return { model, at: stamped, bound: false };
            return { model, at: Number.isFinite(st.mtimeMs) ? Math.floor(st.mtimeMs) : null, bound: true };
          }
        } catch {
          // skip partial or non-JSON line
        }
      }
    } finally {
      fs.closeSync(fd);
    }
  } catch {
    // unreadable transcript: unknown model
  }
  return null;
}

/** The model of the most recent assistant turn in a Claude Code transcript, or null. */
export function sessionModelFromTranscript(transcriptPath: string | undefined): string | null {
  return transcriptEvidence(transcriptPath)?.model ?? null;
}

const sameModel = (a: string, b: string) => a.trim().toLowerCase() === b.trim().toLowerCase();

/**
 * Session model for a hook payload, from two pieces of evidence ordered by time:
 * - the PostModelSwitch state file (dated by `updatedAt`), and
 * - the latest assistant turn in the transcript (dated by its timestamp).
 *
 * The model can change with no PostModelSwitch event (a session resumed on another model, a missed
 * switch), and `SessionStart` carries no model, so neither source is trusted blindly:
 * - Evidence dated before the last resume of the session (the marker `SessionStart` writes) is
 *   discarded: it describes a model the session may no longer run on.
 * - When both remain and name different models, the later one wins. If they cannot be ordered
 *   (equal dates) the model is unknown rather than guessed.
 * - With nothing left the model is unknown, and the guard asks.
 */
export function resolveSessionModel(input: { session_id?: unknown; transcript_path?: string } | null | undefined): SessionModel {
  const resumedAt = readResumedAt(input?.session_id);
  const fresh = (at: number | null): boolean => resumedAt === null || (at !== null && at >= resumedAt);

  const stateRead = readSessionState(input?.session_id);
  const stateAt = stateRead ? Date.parse(stateRead.updatedAt) : NaN;
  // An unreadable `updatedAt` cannot be ordered; with a resume on record it counts as pre-resume.
  const state = stateRead && fresh(Number.isFinite(stateAt) ? stateAt : null) ? { model: stateRead.model, at: Number.isFinite(stateAt) ? stateAt : null } : null;
  const ev = transcriptEvidence(input?.transcript_path);
  const transcript = ev && fresh(ev.at) ? ev : null;

  if (state && transcript) {
    if (sameModel(state.model, transcript.model)) return { model: state.model, source: 'state' };
    if (state.at !== null && transcript.at !== null) {
      // A turn dated only by the file's mtime happened at or before it, so a state file written at or
      // after that instant is the later evidence; two exact timestamps that are equal cannot be ordered.
      if (state.at > transcript.at || (transcript.bound && state.at === transcript.at)) return { model: state.model, source: 'state' };
      if (state.at < transcript.at) return { model: transcript.model, source: 'transcript' };
    }
    return { model: null, source: 'none' };
  }
  if (state) return { model: state.model, source: 'state' };
  if (transcript) return { model: transcript.model, source: 'transcript' };
  return { model: null, source: 'none' };
}
