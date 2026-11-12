import { describe, it, expect, beforeEach, afterEach } from 'bun:test';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { writeLastAdvice, writeTurnState } from '../src/advisor-state.ts';
import { appendClaudeDecision } from '../src/claude-audit.ts';

// What the advisor creates holds hashes, model names and timestamps, never prompt text, but it is
// still per-user state: private when this code creates it, untouched when it already exists.

const mode = (p: string) => fs.statSync(p).mode & 0o777;

describe('files and directories the advisor creates are owner-only', () => {
  let tmp = '';
  let saved: Record<string, string | undefined> = {};
  const KEYS = ['JEV_CLAUDE_STATE_DIR', 'JEV_CLAUDE_DECISIONS_FILE'];
  beforeEach(() => {
    tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-perm-'));
    saved = Object.fromEntries(KEYS.map((k) => [k, process.env[k]]));
  });
  afterEach(() => {
    for (const k of KEYS) {
      if (saved[k] === undefined) delete process.env[k];
      else process.env[k] = saved[k];
    }
    fs.rmSync(tmp, { recursive: true, force: true });
  });

  const advice = { turn: 'abc', tier: 'flash', model: 'sonnet', subagent: 'Explore', at: new Date().toISOString() };
  const marker = { turn: 'abc', mode: 'on', intent: {}, at: new Date().toISOString() };

  it('creates the state directory 0700 and the advice and turn files 0600', () => {
    const dir = path.join(tmp, 'config', 'jev', 'claude_sessions');
    process.env.JEV_CLAUDE_STATE_DIR = dir;

    expect(writeLastAdvice('s1', advice)).toBe(true);
    expect(writeTurnState('s1', marker)).toBe(true);

    expect([mode(dir), mode(path.join(dir, 's1.advice.json')), mode(path.join(dir, 's1.turn.json'))]).toEqual([0o700, 0o600, 0o600]);
  });

  it('creates the decisions log 0600 in a 0700 directory', () => {
    const file = path.join(tmp, 'jevdir', 'claude_decisions.jsonl');
    process.env.JEV_CLAUDE_DECISIONS_FILE = file;

    expect(appendClaudeDecision({ event: 'advice' })).toBe(true);

    expect([mode(path.dirname(file)), mode(file)]).toEqual([0o700, 0o600]);
  });

  it('does not chmod a directory or a file that already exists', () => {
    const dir = path.join(tmp, 'existing');
    fs.mkdirSync(dir, { mode: 0o755 });
    fs.chmodSync(dir, 0o755);
    const file = path.join(dir, 'claude_decisions.jsonl');
    fs.writeFileSync(file, '', { mode: 0o644 });
    fs.chmodSync(file, 0o644);
    process.env.JEV_CLAUDE_DECISIONS_FILE = file;
    process.env.JEV_CLAUDE_STATE_DIR = dir;

    appendClaudeDecision({ event: 'advice' });
    writeLastAdvice('s1', advice);

    expect([mode(dir), mode(file)]).toEqual([0o755, 0o644]);
  });

  it('never throws when the directory cannot be created', () => {
    const blocker = path.join(tmp, 'file');
    fs.writeFileSync(blocker, 'x');
    process.env.JEV_CLAUDE_STATE_DIR = path.join(blocker, 'sub');
    process.env.JEV_CLAUDE_DECISIONS_FILE = path.join(blocker, 'sub', 'd.jsonl');

    expect(writeLastAdvice('s1', advice)).toBe(false);
    expect(writeTurnState('s1', marker)).toBe(false);
    expect(appendClaudeDecision({ event: 'advice' })).toBe(false);
  });
});
