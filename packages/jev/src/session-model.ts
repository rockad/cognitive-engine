/**
 * Session-model tracking for the Claude Code escalation guard.
 *
 * Source order: the per-session state file written by the PostModelSwitch recorder, then the model
 * of the most recent assistant turn in the transcript. `SessionStart` carries no model, and
 * PostModelSwitch never fired in headless runs, so the transcript tail stays as the fallback.
 * Reading never throws; a missing source is just "unknown", which the guard turns into an ask.
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

export function writeSessionState(sessionId: unknown, model: string, source?: string): boolean {
  const file = stateFile(sessionId);
  if (!file || !model) return false;
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const state: SessionState = { model, ...(source ? { source } : {}), updatedAt: new Date().toISOString() };
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

export function clearSessionState(sessionId: unknown): void {
  const file = stateFile(sessionId);
  if (!file) return;
  try {
    fs.rmSync(file, { force: true });
  } catch {
    // nothing to clear
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

/** The model of the most recent assistant turn in a Claude Code transcript, or null. */
export function sessionModelFromTranscript(transcriptPath: string | undefined): string | null {
  if (!transcriptPath) return null;
  try {
    const TAIL_BYTES = 256 * 1024;
    const fd = fs.openSync(transcriptPath, 'r');
    try {
      const { size } = fs.fstatSync(fd);
      const start = Math.max(0, size - TAIL_BYTES);
      const buf = Buffer.alloc(size - start);
      fs.readSync(fd, buf, 0, buf.length, start);
      const lines = buf.toString('utf8').split('\n');
      if (start > 0) lines.shift(); // first line is likely cut mid-record
      for (let i = lines.length - 1; i >= 0; i--) {
        if (!lines[i]) continue;
        try {
          const entry = JSON.parse(lines[i]);
          const model = entry?.type === 'assistant' ? entry?.message?.model : undefined;
          if (typeof model === 'string' && model && !model.startsWith('<')) return model;
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

/** Session model for a hook payload: state file first, transcript tail second. */
export function resolveSessionModel(input: { session_id?: unknown; transcript_path?: string } | null | undefined): SessionModel {
  const state = readSessionState(input?.session_id);
  if (state) return { model: state.model, source: 'state' };
  const fromTranscript = sessionModelFromTranscript(input?.transcript_path);
  return fromTranscript ? { model: fromTranscript, source: 'transcript' } : { model: null, source: 'none' };
}
