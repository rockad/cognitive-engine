import { describe, it, expect, beforeEach, afterEach } from 'bun:test';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { evaluateClaude } from '../src/guard.ts';
import { readResumedAt, readSessionState, resolveSessionModel, writeSessionState } from '../src/session-model.ts';
import { record } from '../src/record-hook.ts';
import { clearConfigCacheForTesting } from '../src/core/config.ts';

/**
 * The "resume gap": the session model can change with no PostModelSwitch event (a resumed session
 * started with another --model, a switch the recorder missed). The state file and the transcript
 * tail then both describe the OLD model, so the ceiling is wrong, and in the dangerous direction
 * when the old model was stronger. The guard must order its evidence by time and fail closed.
 */
let tmp = '';
let savedEnv: NodeJS.ProcessEnv = {};

beforeEach(() => {
  savedEnv = { ...process.env };
  for (const v of ['JEV_HARNESS', 'ANTIGRAVITY_CLI', 'GEMINI_CLI', 'CLAUDE_CODE', 'CLAUDECODE', 'CLAUDE_PROJECT_DIR', 'OPENCODE_SESSION', 'OPENCODE_HOME', 'CORTEX_SESSION']) delete process.env[v];
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-resume-'));
  process.env.HOME = tmp;
  process.env.JEV_CONFIG_FILE = path.join(tmp, 'config.json');
  process.env.JEV_CLAUDE_STATE_DIR = path.join(tmp, 'state');
  process.env.JEV_CLAUDE_DECISIONS_FILE = path.join(tmp, 'decisions.jsonl');
  clearConfigCacheForTesting();
});

afterEach(() => {
  process.env = savedEnv;
  clearConfigCacheForTesting();
  fs.rmSync(tmp, { recursive: true, force: true });
});

const T0 = Date.parse('2026-11-01T10:00:00.000Z');
const at = (min: number) => new Date(T0 + min * 60_000);

/** A transcript of `[model, minute]` assistant turns; a null minute omits the timestamp. */
function transcript(...turns: Array<[string, number | null]>): string {
  const file = path.join(tmp, 't.jsonl');
  const lines = turns.map(([model, min]) => JSON.stringify({ type: 'assistant', ...(min === null ? {} : { timestamp: at(min).toISOString() }), message: { model } }));
  fs.writeFileSync(file, `${lines.join('\n')}\n`);
  // The file's mtime is "now", far later than the turns: only a missing timestamp may fall back to it.
  return file;
}

const SID = 'sess-1';
const agent = (model: string, t: string) => evaluateClaude({ tool_name: 'Agent', session_id: SID, transcript_path: t, tool_input: { model } });
const resolve = (t: string) => resolveSessionModel({ session_id: SID, transcript_path: t });

describe('resume: pre-resume evidence is not trusted', () => {
  it('REPRODUCTION: a resumed session no longer takes its stale state file as the session model', () => {
    // session ran on opus (state + transcript), is resumed on another model, no PostModelSwitch fires
    writeSessionState(SID, 'claude-opus-5-5', undefined, at(0));
    const t = transcript(['claude-opus-5-5', 1]);
    record('SessionStart', JSON.stringify({ session_id: SID, source: 'resume' }), at(5));
    expect(readResumedAt(SID)).toBe(at(5).getTime());
    // before the fix: { opus, state } and a sonnet request passed silently although the session may be haiku
    expect(resolve(t)).toEqual({ model: null, source: 'none' });
    expect(agent('sonnet', t)?.decision).toBe('ask');
    expect(agent('haiku', t)?.decision).toBe('ask'); // nothing can be compared until the model is known
  });

  it('the first assistant turn after the resume re-establishes the model', () => {
    writeSessionState(SID, 'claude-opus-5-5', undefined, at(0));
    record('SessionStart', JSON.stringify({ session_id: SID, source: 'resume' }), at(5));
    const t = transcript(['claude-opus-5-5', 1], ['claude-haiku-5-5', 6]);
    expect(resolve(t)).toEqual({ model: 'claude-haiku-5-5', source: 'transcript' });
    expect(agent('sonnet', t)?.decision).toBe('ask'); // now above the session
    expect(agent('haiku', t)).toBeNull();
  });

  it('a PostModelSwitch after the resume beats older transcript turns', () => {
    record('SessionStart', JSON.stringify({ session_id: SID, source: 'resume' }), at(5));
    writeSessionState(SID, 'claude-sonnet-5-5', 'resume', at(5.5));
    const t = transcript(['claude-opus-5-5', 1]);
    expect(resolve(t)).toEqual({ model: 'claude-sonnet-5-5', source: 'state' });
  });

  it('startup and clear drop the resume marker with the state; a second resume moves it', () => {
    record('SessionStart', JSON.stringify({ session_id: SID, source: 'resume' }), at(5));
    record('SessionStart', JSON.stringify({ session_id: SID, source: 'resume' }), at(9));
    expect(readResumedAt(SID)).toBe(at(9).getTime());
    record('SessionStart', JSON.stringify({ session_id: SID, source: 'startup' }));
    expect(readResumedAt(SID)).toBeNull();
    expect(readSessionState(SID)).toBeNull();
  });

  it('a transcript turn without a timestamp is dated by the file mtime, so a stale transcript stays stale', () => {
    const t = transcript(['claude-opus-5-5', null]);
    fs.utimesSync(t, at(1), at(1));
    record('SessionStart', JSON.stringify({ session_id: SID, source: 'resume' }), at(5));
    expect(resolve(t)).toEqual({ model: null, source: 'none' });
  });
});

describe('no resume event: the newer of state and transcript wins', () => {
  it('a transcript turn newer than the state file, on a different model, wins (a missed switch)', () => {
    writeSessionState(SID, 'claude-opus-5-5', 'picker', at(0));
    const t = transcript(['claude-haiku-5-5', 3]);
    expect(resolve(t)).toEqual({ model: 'claude-haiku-5-5', source: 'transcript' });
    expect(agent('sonnet', t)?.decision).toBe('ask'); // was silently allowed against the stale opus state
  });

  it('a state file newer than the transcript turn wins (a switch after the last turn)', () => {
    writeSessionState(SID, 'claude-sonnet-5-5', 'picker', at(4));
    const t = transcript(['claude-opus-5-5', 3]);
    expect(resolve(t)).toEqual({ model: 'claude-sonnet-5-5', source: 'state' });
    expect(agent('opus', t)?.decision).toBe('ask');
  });

  it('agreeing evidence needs no ordering, with or without timestamps', () => {
    writeSessionState(SID, 'claude-sonnet-5-5', 'picker', at(0));
    expect(resolve(transcript(['claude-sonnet-5-5', 3]))).toEqual({ model: 'claude-sonnet-5-5', source: 'state' });
    expect(resolve(transcript(['claude-sonnet-5-5', null]))).toEqual({ model: 'claude-sonnet-5-5', source: 'state' });
  });

  it('disagreeing evidence that cannot be ordered is not guessed: the model is unknown and the guard asks', () => {
    writeSessionState(SID, 'claude-opus-5-5', 'picker', at(3));
    const t = transcript(['claude-haiku-5-5', 3]);
    expect(resolve(t)).toEqual({ model: null, source: 'none' });
    expect(agent('haiku', t)?.decision).toBe('ask');
  });

  it('a turn dated only by the file mtime is at or before it: a state file written in the same instant is the later evidence', () => {
    const t = transcript(['claude-opus-5-5', null]);
    fs.utimesSync(t, at(2), at(2));
    writeSessionState(SID, 'claude-sonnet-5-5', 'picker', at(2));
    expect(resolve(t)).toEqual({ model: 'claude-sonnet-5-5', source: 'state' });
  });

  it('an unreadable timestamp counts as no timestamp, never as "newer"', () => {
    writeSessionState(SID, 'claude-sonnet-5-5', 'picker', at(2));
    const file = path.join(tmp, 't.jsonl');
    fs.writeFileSync(file, `${JSON.stringify({ type: 'assistant', timestamp: 'not a date', message: { model: 'claude-opus-5-5' } })}\n`);
    fs.utimesSync(file, at(1), at(1));
    expect(resolve(file)).toEqual({ model: 'claude-sonnet-5-5', source: 'state' });
  });

  it('only a transcript, or only a state file, still resolves as before', () => {
    expect(resolve(transcript(['claude-sonnet-5-5', 1]))).toEqual({ model: 'claude-sonnet-5-5', source: 'transcript' });
    writeSessionState(SID, 'claude-haiku-5-5', undefined, at(0));
    expect(resolve(path.join(tmp, 'missing.jsonl'))).toEqual({ model: 'claude-haiku-5-5', source: 'state' });
  });
});
