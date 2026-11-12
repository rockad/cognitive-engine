import { describe, it, expect } from 'bun:test';
import { buildDirective, parseUserIntent, resolveModel, type Dispatch } from '../src/advisor-logic.ts';
import { compareModels, parseModel } from '../src/models.ts';

// Case study 5.2: the advice must not hold a "never delegate to a stronger model" ceiling line and,
// next to it, an action that delegates to a stronger model. Above the session model there is ONE
// sentence, whatever the model is (not only opus).

const NEVER = 'never delegate to a stronger model';

const rec = (tier: Dispatch['tier'], model: Dispatch['model'], subagent = 'Explore'): Dispatch => ({
  subagent,
  tier,
  model,
  effort: 'medium',
  confidence: 0.9
});

function advise(session: string | null, router: Dispatch, prompt = 'Review the storage layer'): string {
  const intent = parseUserIntent(prompt);
  const { advised } = resolveModel({ userIntent: intent, rec: router, sessionModel: session });
  return buildDirective(advised, session, 'unknown', intent);
}

describe('one ceiling sentence for any model above the session model', () => {
  it('session haiku and "use sonnet": the guard-prompt sentence, not the hard-ceiling line', () => {
    const text = advise('claude-haiku-5-5', rec('flash_lite', 'haiku'), 'Review the storage layer and use sonnet');

    expect(text).toContain('one approval prompt');
    expect(text).not.toContain(NEVER);
  });

  it('session haiku and a flash (sonnet) recommendation: ask first, no "delegate to sonnet" action', () => {
    const text = advise('claude-haiku-5-5', rec('flash', 'sonnet'));

    expect(text).toContain('hard ceiling, so ask the user before delegating on sonnet');
    expect(text).not.toContain(NEVER);
    expect(text).not.toContain('Delegate to the Explore subagent');
  });

  it('a custom persona above the session model gets the same single sentence', () => {
    const text = advise('claude-haiku-5-5', rec('flash', 'sonnet', 'code-engineer'));

    expect(text).toContain('ask the user before delegating on sonnet');
    expect(text).not.toContain('Agent(subagent_type');
  });

  it('a delegation within the ceiling keeps the ceiling line and the action', () => {
    const text = advise('claude-sonnet-5-5', rec('flash', 'sonnet'));

    expect(text).toContain(NEVER);
    expect(text).toContain('Delegate to the Explore subagent with model "sonnet"');
  });

  it('a direct answer or a free offload is not an escalation', () => {
    expect(advise('claude-haiku-5-5', rec('flash', 'sonnet', 'self'))).toContain('Handle it directly.');
    expect(advise('claude-haiku-5-5', rec('openrouter_free', 'haiku'))).toContain('jev_execute_free');
  });

  const sessions = ['claude-haiku-5-5', 'claude-sonnet-5-5', 'claude-opus-5-5'];
  const recs: Dispatch[] = [rec('flash_lite', 'haiku'), rec('flash', 'sonnet'), rec('pro', 'opus', 'general-purpose')];
  const prompts = ['Review the storage layer', 'Review the storage layer and use sonnet', 'Review the storage layer and use opus'];
  const cases = sessions.flatMap((s) => recs.flatMap((r) => prompts.map((p) => [s, r.tier, p, r] as const)));

  it.each(cases)('session %s, router %s, "%s": never the ceiling line next to a delegation above it', (session, _tier, prompt, router) => {
    const text = advise(session, router, prompt);

    if (!text.includes(NEVER)) return;
    const delegated = [...text.matchAll(/with model "(haiku|sonnet|opus)"/g)].map((m) => parseModel(m[1]));
    const sessionParsed = parseModel(session);
    for (const d of delegated) {
      if (d === 'unknown' || sessionParsed === 'unknown') continue;
      expect(compareModels(d, sessionParsed)).toBeLessThanOrEqual(0);
    }
  });
});
