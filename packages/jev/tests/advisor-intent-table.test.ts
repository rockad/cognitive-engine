import { describe, it, expect, beforeEach, afterEach } from 'bun:test';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { applyUserIntent, buildDirective, INTENT_SCAN_CHARS, parseUserIntent, type Dispatch } from '../src/advisor-logic.ts';
import { onPreToolUse, onUserPromptSubmit, type HookDeps, type PreToolDeps } from '../src/advisor-hook.ts';
import { clearTurnState, currentTurn, readTurnState, writeLastAdvice, writeTurnState, type LastAdvice, type TurnState } from '../src/advisor-state.ts';
import { turnId } from '../src/advisor-logic.ts';

const rec = (tier: Dispatch['tier'], model: Dispatch['model']): Dispatch => ({ subagent: 'general-purpose', tier, model, effort: 'medium', confidence: 0.9 });

describe('the explicit-instruction parser reads imperatives only (table)', () => {
  it.each([
    ['use opus', 'opus'],
    ['Use sonnet for this', 'sonnet'],
    ['run an opus subagent', 'opus'],
    ['launch a haiku agent', 'haiku'],
    ['with sonnet', 'sonnet'],
    ['With sonnet, please fix the tests', 'sonnet'],
    ['Fix the parser. With haiku.', 'haiku'],
    ['run it with sonnet', 'sonnet'],
    ['go with haiku', 'haiku'],
    ['delegate this to opus', 'opus']
  ])('%j names %s', (prompt, model) => {
    expect(parseUserIntent(prompt).model).toBe(model as 'opus');
  });

  it.each([
    ['minimal viable'],
    ['choose minimal viable model for each'],
    ['cheapest'],
    ['use the cheapest option'],
    ['as cheap as possible']
  ])('%j is a minimal-cost instruction', (prompt) => {
    expect(parseUserIntent(prompt).cost).toBe('minimal');
  });

  it.each([
    ['do not use opus'],
    ["don't use opus here"],
    ['never use opus'],
    ['not opus'],
    ['instead of opus'],
    ['without opus'],
    ['rather than use opus'],
    ['I have no idea about cost, so do not use opus'],
    ['compare with opus'],
    ['we compared results with sonnet yesterday'],
    ['the opus subagent finished'],
    ['opus is the strongest model'],
    ['sonnet or opus, whichever is available'],
    ['use opus or sonnet'],
    ['not the cheapest, the best one'],
    ['do not choose the cheapest']
  ])('%j gives no model instruction', (prompt) => {
    expect(parseUserIntent(prompt).model).toBeUndefined();
  });

  it('reads the model that is asked for, not the one it replaces', () => {
    expect(parseUserIntent('use sonnet instead of opus').model).toBe('sonnet');
  });

  it.each([['not the cheapest, the best one'], ['do not choose the cheapest'], ['without the cheapest option']])('%j gives no cost instruction', (prompt) => {
    expect(parseUserIntent(prompt).cost).toBeUndefined();
  });

  it('keeps a negator in an earlier clause from cancelling a later instruction', () => {
    expect(parseUserIntent('There is no time left, so use opus').model).toBe('opus');
  });

  it('scans only the first 1500 characters', () => {
    expect(INTENT_SCAN_CHARS).toBe(1500);
    const filler = 'x '.repeat(740);
    const inside = parseUserIntent(`use opus ${filler}`);
    const outside = parseUserIntent(`${filler.repeat(1)}${'y '.repeat(10)} use opus`);

    expect([inside.model, outside.model]).toEqual(['opus', undefined]);
  });
});

describe('the effect is capped at one tier step from the recommendation (table)', () => {
  it.each([
    ['flash', 'opus', 'pro', 'opus', false],
    ['flash', 'haiku', 'flash_lite', 'haiku', false],
    ['flash', 'sonnet', 'flash', 'sonnet', false],
    ['flash_lite', 'opus', 'flash', 'sonnet', true],
    ['openrouter_free', 'sonnet', 'flash_lite', 'haiku', true],
    ['openrouter_free', 'opus', 'flash_lite', 'haiku', true],
    ['pro', 'haiku', 'flash', 'sonnet', true],
    ['pro', 'opus', 'pro', 'opus', false]
  ] as const)('router %s, user asks for %s: tier %s, model %s, capped %s', (routerTier, asked, tier, model, capped) => {
    const modelOf = { openrouter_free: 'haiku', flash_lite: 'haiku', flash: 'sonnet', pro: 'opus' } as const;

    const out = applyUserIntent(rec(routerTier, modelOf[routerTier]), { model: asked });

    expect([out.tier, out.model, out.asked !== undefined]).toEqual([tier, model, capped]);
    expect(out.routerTier).toBe(routerTier);
  });

  it('measures the step from the router tier, not from a tier an earlier instruction set', () => {
    const once = applyUserIntent(rec('flash', 'sonnet'), { model: 'opus' });

    const twice = applyUserIntent(once, { model: 'haiku' });

    expect([once.tier, twice.tier]).toEqual(['pro', 'flash_lite']);
  });

  it('a minimal-cost request lowers one step and never below flash_lite', () => {
    const out = [applyUserIntent(rec('pro', 'opus'), { cost: 'minimal' }).tier, applyUserIntent(rec('flash', 'sonnet'), { cost: 'minimal' }).tier, applyUserIntent(rec('flash_lite', 'haiku'), { cost: 'minimal' }).tier];

    expect(out).toEqual(['flash', 'flash_lite', 'flash_lite']);
  });

  it('says in the directive that the cap held the tier short of the named model', () => {
    const capped = applyUserIntent(rec('flash_lite', 'haiku'), { model: 'opus' });

    const text = buildDirective(capped, 'claude-sonnet-5', 'unknown', { model: 'opus', phrase: 'use opus' });

    expect(text).toContain('You asked for opus');
    expect(text).toContain('at most one tier step');
    expect(text).toContain('model "sonnet"');
    expect(text).not.toContain('do not ask separately');
  });
});

// ── Staleness: the marker stored at UserPromptSubmit ────────────────────────────────────────────

const flashRec = rec('flash', 'sonnet');
const advice: LastAdvice = { turn: 'turnaaaaaaaa', tier: 'pro', model: 'opus', subagent: 'general-purpose', by: 'instruction-model', staged: true, at: '2026-11-10T10:00:00Z' };
const marker = (over: Partial<TurnState> = {}): TurnState => ({ turn: advice.turn, mode: 'on', intent: { model: 'opus' }, at: advice.at, ...over });

describe('currentTurn drops advice and intent that are not this turn\'s', () => {
  it('keeps both for the same turn and the same mode', () => {
    expect(currentTurn(marker(), advice, 'on')).toEqual({ advice, intent: { model: 'opus' }, turn: advice.turn });
  });

  it('discards both when the Jev mode differs from the mode the turn started in', () => {
    expect(currentTurn(marker({ mode: 'on-demand' }), advice, 'on')).toEqual({ advice: null, intent: null, turn: null });
  });

  it('discards the advice, and keeps the new turn\'s intent, when the advice is from another turn', () => {
    const out = currentTurn(marker({ turn: 'turnbbbbbbbb', intent: {} }), advice, 'on');

    expect(out).toEqual({ advice: null, intent: {}, turn: 'turnbbbbbbbb' });
  });

  it('discards everything without a marker', () => {
    expect(currentTurn(null, advice, 'on')).toEqual({ advice: null, intent: null, turn: null });
  });
});

function pre(over: Partial<PreToolDeps> = {}, log: Record<string, unknown>[] = []): PreToolDeps {
  return {
    readMode: async () => 'on',
    lastAdvice: () => advice,
    turnState: () => marker(),
    recentDispatch: () => false,
    recentAgentCalls: () => ({ count: 0, reminded: false }),
    log: (e) => log.push(e),
    ...over
  };
}

describe('PreToolUse ignores a stale turn', () => {
  it('logs the recommendation and the intent of the current turn', async () => {
    const log: Record<string, unknown>[] = [];

    await onPreToolUse({ session_id: 's1', tool_name: 'Agent', tool_input: { model: 'opus' } }, pre({}, log));

    expect(log[0]).toMatchObject({ turn: advice.turn, tier: 'pro', recommended_model: 'opus', user_intent: { model: 'opus' } });
  });

  it('discards the stored intent and advice when the mode at PreToolUse differs from the stored mode', async () => {
    const log: Record<string, unknown>[] = [];

    await onPreToolUse({ session_id: 's1', tool_name: 'Agent', tool_input: {} }, pre({ turnState: () => marker({ mode: 'on-demand' }) }, log));

    expect(log[0]).toMatchObject({ turn: null, tier: null, recommended_model: null, user_intent: null });
  });

  it('discards the stored advice and its staged flag when the turn id differs', async () => {
    const log: Record<string, unknown>[] = [];

    const out = await onPreToolUse({ session_id: 's1', tool_name: 'Agent', tool_input: {} }, pre({ turnState: () => marker({ turn: 'turnbbbbbbbb', intent: {} }) }, log));

    // The advice said staged: with it discarded, a lone Agent call is not staged and gets no reminder.
    expect(out).toBeNull();
    expect(log[0]).toMatchObject({ turn: 'turnbbbbbbbb', tier: null, recommended_model: null, user_intent: {}, preflight_reminder: false });
  });
});

describe('UserPromptSubmit stores the turn marker before anything can return early', () => {
  let tmp = '';
  let saved: string | undefined;
  beforeEach(() => {
    tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-turn-'));
    saved = process.env.JEV_CLAUDE_STATE_DIR;
    process.env.JEV_CLAUDE_STATE_DIR = tmp;
  });
  afterEach(() => {
    if (saved === undefined) delete process.env.JEV_CLAUDE_STATE_DIR;
    else process.env.JEV_CLAUDE_STATE_DIR = saved;
    fs.rmSync(tmp, { recursive: true, force: true });
  });

  const deps = (over: Partial<HookDeps> = {}): HookDeps => ({
    readMode: async () => 'on',
    readSessionModel: () => 'claude-sonnet-5',
    readQuotaStrategy: async () => 'unknown',
    dispatch: async () => flashRec,
    log: () => {},
    markTurn: writeTurnState,
    forgetTurn: clearTurnState,
    ...over
  });

  it('stores the turn id, the mode and the intent (no prompt text) when Jev is unreachable', async () => {
    const prompt = 'use opus to write the changelog entry';

    await onUserPromptSubmit({ session_id: 's1', prompt }, deps({ dispatch: async () => null }));

    const stored = readTurnState('s1');
    expect(stored).toMatchObject({ turn: turnId(prompt), mode: 'on', intent: { model: 'opus' } });
    expect(JSON.stringify(stored)).not.toContain('changelog');
  });

  it('end to end: a turn that got no advice does not inherit the previous turn\'s advice or intent', async () => {
    const first = 'use opus for the workflow with three workstreams';
    await onUserPromptSubmit({ session_id: 's1', prompt: first }, deps({ remember: writeLastAdvice }));
    // The next real user turn: Jev is unreachable, so no advice is written for it.
    await onUserPromptSubmit({ session_id: 's1', prompt: 'carry on with the next one' }, deps({ dispatch: async () => null }));
    const log: Record<string, unknown>[] = [];

    await onPreToolUse(
      { session_id: 's1', tool_name: 'Agent', tool_input: { model: 'haiku' } },
      pre({ lastAdvice: (id) => (id === 's1' ? JSON.parse(fs.readFileSync(path.join(tmp, 's1.advice.json'), 'utf8')) : null), turnState: readTurnState }, log)
    );

    expect(log[0]).toMatchObject({ turn: turnId('carry on with the next one'), tier: null, recommended_model: null, user_intent: {} });
  });

  it('a host-injected task notification keeps the marker of the prompt it belongs to', async () => {
    const prompt = 'use sonnet to run the workflow';
    await onUserPromptSubmit({ session_id: 's1', prompt }, deps());

    await onUserPromptSubmit({ session_id: 's1', prompt: '<task-notification><id>a3</id></task-notification>' }, deps());

    expect(readTurnState('s1')?.turn).toBe(turnId(prompt));
  });

  it('a turn with Jev off drops the marker, so a later flip back to on finds no stale intent', async () => {
    await onUserPromptSubmit({ session_id: 's1', prompt: 'use opus to write the changelog' }, deps());
    expect(readTurnState('s1')).not.toBeNull();

    await onUserPromptSubmit({ session_id: 's1', prompt: 'something else entirely' }, deps({ readMode: async () => 'off' }));

    expect(readTurnState('s1')).toBeNull();
  });
});
