import { describe, it, expect } from 'bun:test';
import { applyUserIntent, buildDirective, parseUserIntent, type Dispatch } from '../src/advisor-logic.ts';
import { onUserPromptSubmit, type HookDeps } from '../src/advisor-hook.ts';

const flashRec: Dispatch = { subagent: 'Explore', tier: 'flash', model: 'sonnet', effort: 'medium', confidence: 0.9 };
const proRec: Dispatch = { subagent: 'general-purpose', tier: 'pro', model: 'opus', effort: 'high', confidence: 0.8 };
const liteRec: Dispatch = { subagent: 'Explore', tier: 'flash_lite', model: 'haiku', effort: 'low', confidence: 0.9 };

function deps(overrides: Partial<HookDeps> = {}): HookDeps {
  return {
    readMode: async () => 'on',
    readSessionModel: () => 'claude-sonnet-5',
    readQuotaStrategy: async () => 'unknown',
    dispatch: async () => flashRec,
    log: () => {},
    ...overrides
  };
}

describe('parseUserIntent (5.1)', () => {
  it.each([
    ['run an opus subagent to wrap findings', 'opus'],
    ['use opus for the review', 'opus'],
    ['please use the sonnet model here', 'sonnet'],
    ['spawn a haiku agent for the checklist', 'haiku'],
    ['go with haiku', 'haiku'],
    ['delegate this to opus', 'opus']
  ])('reads an explicit model from %j', (prompt, model) => {
    expect(parseUserIntent(prompt).model).toBe(model as 'opus');
  });

  it.each([
    ['go ahead with the workstreams, choose minimal viable model for each'],
    ['choos minimal viable model for each'],
    ['use the cheapest option'],
    ['as cheap as possible please']
  ])('reads a minimal-cost instruction from %j', (prompt) => {
    expect(parseUserIntent(prompt).cost).toBe('minimal');
  });

  it.each([
    ['sonnet is cheaper than opus for this kind of work'],
    ['compare the numbers with opus and sonnet'],
    ['the opus subagent finished its run'],
    ['do not use opus here'],
    ['never use opus for this'],
    ['use opus or sonnet, whichever is faster'],
    ['not the cheapest, the best one']
  ])('reads nothing from prose that only mentions a model: %j', (prompt) => {
    expect(parseUserIntent(prompt)).toEqual({});
  });

  it('records the matched words', () => {
    expect(parseUserIntent('run an opus subagent').phrase).toBe('run an opus');
  });

  it('only scans the start of a long prompt', () => {
    const prompt = `${'x '.repeat(1100)} use opus`;

    expect(parseUserIntent(prompt)).toEqual({});
  });
});

describe('applyUserIntent (5.1)', () => {
  it('lets an explicit model override the tier', () => {
    const out = applyUserIntent(flashRec, { model: 'opus' });

    expect([out.tier, out.model, out.routerTier, out.by]).toEqual(['pro', 'opus', 'flash', 'instruction-model']);
  });

  it('lowers the tier by one step for a minimal cost request', () => {
    const out = applyUserIntent(proRec, { cost: 'minimal' });

    expect([out.tier, out.model]).toEqual(['flash', 'sonnet']);
  });

  it('never lowers below flash_lite', () => {
    const out = applyUserIntent(liteRec, { cost: 'minimal' });

    expect([out.tier, out.model]).toEqual(['flash_lite', 'haiku']);
  });

  it('lets the model win over a minimal cost request in the same prompt', () => {
    const out = applyUserIntent(proRec, { model: 'sonnet', cost: 'minimal' });

    expect([out.tier, out.model, out.by]).toEqual(['flash', 'sonnet', 'instruction-model']);
  });

  it('returns the recommendation untouched without an instruction', () => {
    expect(applyUserIntent(proRec, {})).toBe(proRec);
  });
});

describe('buildDirective ceiling sentence (5.2)', () => {
  it('prints the ceiling once and no second ask on the pro tier', () => {
    const directive = buildDirective(proRec, 'claude-sonnet-5', 'normal');

    expect(directive.split('hard ceiling')).toHaveLength(2);
    expect(directive).not.toContain('present the cheaper');
    expect(directive).toContain('ask the user before delegating on opus');
    expect(directive).toContain('let the user choose');
  });

  it('prints no ask when the session is already on opus', () => {
    const directive = buildDirective(proRec, 'claude-opus-5-5', 'normal');

    expect(directive).not.toContain('ask the user');
    expect(directive).toContain('allows it');
  });

  it('keeps the plain ceiling sentence below the pro tier', () => {
    const directive = buildDirective(flashRec, 'claude-sonnet-5', 'normal');

    expect(directive).toContain('"claude-sonnet-5" is the hard ceiling');
  });
});

describe('onUserPromptSubmit with an explicit instruction (5.1)', () => {
  it('names opus and the explicit request when the user asked for it, with no ask text', async () => {
    const input = { session_id: 's1', prompt: 'run an opus subagent to wrap findings' };

    const out = await onUserPromptSubmit(input, deps({ dispatch: async () => flashRec }));

    const text = String(out?.hookSpecificOutput.additionalContext);
    expect(text).toContain('model="opus"');
    expect(text).toContain('explicit user request');
    expect(text).not.toContain('let the user choose');
    expect(text).toContain('do not ask separately');
  });

  it('gives sonnet for "minimal viable" against a pro recommendation', async () => {
    const input = { session_id: 's1', prompt: 'go ahead with the workstreams, choose minimal viable model for each' };

    const out = await onUserPromptSubmit(input, deps({ dispatch: async () => proRec }));

    const text = String(out?.hookSpecificOutput.additionalContext);
    expect(text).toContain('model="sonnet"');
    expect(text).toContain('lowered the tier from pro to flash');
  });

  it('leaves the router recommendation alone when the prompt has no instruction', async () => {
    const input = { session_id: 's1', prompt: 'Find where the quota file is written' };

    const out = await onUserPromptSubmit(input, deps());

    expect(String(out?.hookSpecificOutput.additionalContext)).not.toContain('explicit user request');
  });

  it('logs the instruction without the prompt text', async () => {
    const logged: Record<string, unknown>[] = [];
    const input = { session_id: 's1', prompt: 'run an opus subagent to wrap findings' };

    await onUserPromptSubmit(input, deps({ log: (e) => logged.push(e) }));

    expect(logged[0]).toMatchObject({ by: 'instruction-model', routerTier: 'flash', tier: 'pro' });
    expect(JSON.stringify(logged)).not.toContain('wrap findings');
  });
});
