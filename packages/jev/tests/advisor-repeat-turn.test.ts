import { describe, it, expect, beforeEach, afterEach } from 'bun:test';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { turnId, type Dispatch } from '../src/advisor-logic.ts';
import { onPreToolUse, onUserPromptSubmit, type HookDeps, type PreToolDeps } from '../src/advisor-hook.ts';
import { clearTurnState, currentTurn, readLastAdvice, readTurnState, writeLastAdvice, writeTurnState, type LastAdvice, type TurnState } from '../src/advisor-state.ts';

// The turn id is a hash of the prompt only, so "continue" in turn 5 and "continue" in turn 9 share
// an id. The marker's timestamp tells them apart: advice older than the marker is an earlier turn's.

const advice: LastAdvice = { turn: 'turnaaaaaaaa', tier: 'pro', model: 'opus', subagent: 'general-purpose', staged: true, at: '2026-11-10T10:00:00.000Z' };
const marker = (at: string): TurnState => ({ turn: advice.turn, mode: 'on', intent: {}, at });

describe('currentTurn and a repeated prompt', () => {
  it.each([
    ['advice written after the marker (the same turn)', '2026-11-10T09:59:59.000Z', true],
    ['advice written in the same millisecond as the marker', '2026-11-10T10:00:00.000Z', true],
    ['advice older than the marker (an earlier turn with the same words)', '2026-11-10T10:05:00.000Z', false]
  ])('%s', (_name, markerAt, keeps) => {
    expect(currentTurn(marker(markerAt), advice, 'on').advice).toEqual(keeps ? advice : null);
  });

  it('advice or marker with an unreadable timestamp is no match', () => {
    expect(currentTurn(marker('not a date'), advice, 'on').advice).toBeNull();
    expect(currentTurn(marker('2026-11-10T09:00:00Z'), { ...advice, at: 'x' }, 'on').advice).toBeNull();
  });
});

describe('a repeated prompt without advice does not inherit the previous advice (end to end)', () => {
  let tmp = '';
  let saved: string | undefined;
  beforeEach(() => {
    tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-repeat-'));
    saved = process.env.JEV_CLAUDE_STATE_DIR;
    process.env.JEV_CLAUDE_STATE_DIR = tmp;
  });
  afterEach(() => {
    if (saved === undefined) delete process.env.JEV_CLAUDE_STATE_DIR;
    else process.env.JEV_CLAUDE_STATE_DIR = saved;
    fs.rmSync(tmp, { recursive: true, force: true });
  });

  const flashRec: Dispatch = { subagent: 'general-purpose', tier: 'flash', model: 'sonnet', effort: 'medium', confidence: 0.9 };
  const deps = (over: Partial<HookDeps> = {}): HookDeps => ({
    readMode: async () => 'on',
    readSessionModel: () => 'claude-sonnet-5-5',
    readQuotaStrategy: async () => 'unknown',
    dispatch: async () => flashRec,
    log: () => {},
    markTurn: writeTurnState,
    forgetTurn: clearTurnState,
    remember: writeLastAdvice,
    ...over
  });
  const preDeps = (log: Record<string, unknown>[]): PreToolDeps => ({
    readMode: async () => 'on',
    lastAdvice: readLastAdvice,
    turnState: readTurnState,
    recentDispatch: () => false,
    recentAgentCalls: () => ({ count: 0, reminded: false }),
    log: (e) => log.push(e)
  });

  it('turn 1 gets staged advice; the same words in turn 2 get none, so PreToolUse sees neither advice nor staged flag', async () => {
    const prompt = 'continue with the workflow and the workstreams';
    await onUserPromptSubmit({ session_id: 's1', prompt }, deps());
    expect(readLastAdvice('s1')?.staged).toBe(true);
    await Bun.sleep(10);
    // Turn 2: the same prompt, but Jev is unreachable, so no advice is written for it.
    await onUserPromptSubmit({ session_id: 's1', prompt }, deps({ dispatch: async () => null }));
    const log: Record<string, unknown>[] = [];

    const out = await onPreToolUse({ session_id: 's1', tool_name: 'Agent', tool_input: { model: 'haiku' } }, preDeps(log));

    expect(turnId(prompt)).toBe(readLastAdvice('s1')!.turn);
    expect(log[0]).toMatchObject({ tier: null, recommended_model: null, preflight_reminder: false });
    expect(out).toBeNull();
  });

  it('the same turn still sees its own advice', async () => {
    const prompt = 'continue with the workflow and the workstreams';
    await onUserPromptSubmit({ session_id: 's1', prompt }, deps());
    const log: Record<string, unknown>[] = [];

    await onPreToolUse({ session_id: 's1', tool_name: 'Agent', tool_input: { model: 'haiku' } }, preDeps(log));

    expect(log[0]).toMatchObject({ tier: 'flash', recommended_model: 'sonnet', preflight_reminder: true });
  });
});
