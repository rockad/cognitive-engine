import { describe, it, expect } from 'bun:test';
import { appliedRule, buildDirective, parseUserIntent, resolveModel, type Dispatch } from '../src/advisor-logic.ts';
import { onUserPromptSubmit, type HookDeps } from '../src/advisor-hook.ts';

const flashRec: Dispatch = { subagent: 'Explore', tier: 'flash', model: 'sonnet', effort: 'medium', confidence: 0.9 };
const proRec: Dispatch = { subagent: 'general-purpose', tier: 'pro', model: 'opus', effort: 'high', confidence: 0.8 };

function deps(rec: Dispatch, session = 'claude-sonnet-5'): HookDeps {
  return {
    readMode: async () => 'on',
    readSessionModel: () => session,
    readQuotaStrategy: async () => 'unknown',
    dispatch: async () => rec,
    log: () => {},
    remember: () => {}
  };
}

describe('precedence: instruction, then guard, then standing rule (5.9)', () => {
  // [prompt, router recommendation, session model, rule, model advised, needs approval]
  const table: Array<[string, Dispatch, string, string, string, boolean]> = [
    ['run an opus subagent to wrap findings', flashRec, 'claude-sonnet-5', 'instruction', 'opus', false],
    ['choose minimal viable model for each stream', proRec, 'claude-sonnet-5', 'instruction', 'sonnet', false],
    ['write the storage layer design', proRec, 'claude-sonnet-5', 'guard', 'opus', true],
    ['write the storage layer design', proRec, 'claude-opus-5-5', 'standing', 'opus', false],
    ['find where the quota file is written', flashRec, 'claude-sonnet-5', 'standing', 'sonnet', false]
  ];

  it.each(table)('%j with rec %#: rule %s', (prompt, rec, session, rule, model, needsApproval) => {
    const res = resolveModel({ userIntent: parseUserIntent(prompt), rec, sessionModel: session });

    expect([res.rule, res.advised.model, res.needsApproval]).toEqual([rule, model, needsApproval]);
  });

  it('keeps the guard rule for an unknown session model', () => {
    expect(appliedRule(proRec, null)).toBe('guard');
  });

  it('says in the line which rule applied', () => {
    expect(buildDirective(flashRec, 'claude-sonnet-5', 'normal')).toContain('rule: per standing rule');
    expect(buildDirective(proRec, 'claude-sonnet-5', 'normal')).toContain('rule: per the Jev guard');
    const asked = resolveModel({ userIntent: { model: 'opus', phrase: 'use opus' }, rec: flashRec, sessionModel: 'claude-sonnet-5' });
    expect(buildDirective(asked.advised, 'claude-sonnet-5', 'normal', { model: 'opus', phrase: 'use opus' })).toContain('rule: per your instruction');
  });

  it('an explicit opus request gets no ask text from Jev, only the guard prompt that Claude Code shows', async () => {
    const out = await onUserPromptSubmit({ session_id: 's1', prompt: 'use opus for the review of this plan' }, deps(flashRec));

    const text = String(out?.hookSpecificOutput.additionalContext);
    expect(text).not.toContain('ask the user before delegating');
    expect(text).not.toContain('let the user choose');
    expect(text).toContain('do not ask separately');
  });

  it('no instruction and a pro recommendation gives the ask text', async () => {
    const out = await onUserPromptSubmit({ session_id: 's1', prompt: 'design the storage layer for the new service' }, deps(proRec));

    expect(String(out?.hookSpecificOutput.additionalContext)).toContain('ask the user before delegating on opus');
  });

  it('no instruction and a flash recommendation gives sonnet', async () => {
    const out = await onUserPromptSubmit({ session_id: 's1', prompt: 'find where the quota file is written' }, deps(flashRec));

    expect(String(out?.hookSpecificOutput.additionalContext)).toContain('model="sonnet"');
  });
});
