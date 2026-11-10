import { describe, it, expect } from 'bun:test';
import { adviceTelemetry, onPreToolUse, onUserPromptSubmit, type HookDeps, type PreToolDeps } from '../src/advisor-hook.ts';
import { turnId, type Dispatch } from '../src/advisor-logic.ts';
import type { LastAdvice, TurnState } from '../src/advisor-state.ts';

/**
 * Field names are a contract with readers of the decisions log and the shared telemetry file, and
 * they match agentic-hub Jev 2.3.1 (case study 5.7). A rename must fail here, not in a dashboard.
 */
const flashRec: Dispatch = { subagent: 'Explore', tier: 'flash', model: 'sonnet', effort: 'medium', confidence: 0.9 };
const keys = (o: object) => Object.keys(JSON.parse(JSON.stringify(o))).sort();

async function runPrompt(prompt: string): Promise<{ log: Record<string, unknown>; advice: LastAdvice; marker: TurnState }> {
  const logged: Record<string, unknown>[] = [];
  let advice: LastAdvice | null = null;
  let marker: TurnState | null = null;
  const deps: HookDeps = {
    readMode: async () => 'on',
    readSessionModel: () => 'claude-sonnet-5',
    readQuotaStrategy: async () => 'unknown',
    dispatch: async () => flashRec,
    log: (e) => logged.push(e),
    remember: (_s, a) => (advice = a),
    markTurn: (_s, m) => (marker = m)
  };
  await onUserPromptSubmit({ session_id: 's1', prompt }, deps);
  return { log: logged[0], advice: advice!, marker: marker! };
}

describe('UserPromptSubmit record', () => {
  it('the decisions-log entry carries turn, tier and recommended_model', async () => {
    const prompt = 'Find where the quota file is written';

    const { log } = await runPrompt(prompt);

    expect(log).toMatchObject({ hook: 'UserPromptSubmit', session_id: 's1', turn: turnId(prompt), tier: 'flash', recommended_model: 'sonnet' });
  });

  it('the telemetry event has modelRecommended, and modelUsed as a deprecated alias with the same value', async () => {
    const { log } = await runPrompt('Find where the quota file is written');

    const event = adviceTelemetry(log);

    expect(keys(event)).toEqual(['harness', 'modelRecommended', 'modelUsed', 'quotaStrategy', 'sessionId', 'taskSummary', 'tierRecommended', 'toolOrHook', 'turn']);
    expect(event.modelRecommended).toBe('sonnet');
    expect(event.modelUsed).toBe(event.modelRecommended);
  });

  it('the remembered advice and the turn marker use the names PreToolUse reads', async () => {
    const { advice, marker } = await runPrompt('use opus to write the changelog');

    expect(keys(advice)).toEqual(['at', 'by', 'model', 'subagent', 'tier', 'turn']);
    expect(keys(marker)).toEqual(['at', 'intent', 'mode', 'turn']);
  });
});

describe('PreToolUse record', () => {
  const advice: LastAdvice = { turn: 'abc123abc123', tier: 'flash', model: 'sonnet', subagent: 'Explore', at: '2026-11-10T10:00:00Z' };
  const deps = (log: Record<string, unknown>[]): PreToolDeps => ({
    readMode: async () => 'on',
    lastAdvice: () => advice,
    turnState: () => ({ turn: advice.turn, mode: 'on', intent: {}, at: advice.at }),
    recentDispatch: () => false,
    recentAgentCalls: () => ({ count: 0, reminded: false }),
    log: (e) => log.push(e)
  });

  it('the decisions-log entry has turn, tier, recommended_model, requested_model and preflight_reminder', async () => {
    const log: Record<string, unknown>[] = [];

    await onPreToolUse({ session_id: 's1', tool_name: 'Workflow', tool_input: { model: 'opus' } }, deps(log));

    expect(log[0]).toMatchObject({ hook: 'PreToolUse', turn: advice.turn, tier: 'flash', recommended_model: 'sonnet', requested_model: 'opus', preflight_reminder: true });
    expect('reminded' in log[0]).toBe(false);
    expect('modelUsed' in log[0]).toBe(false);
  });

  it('the telemetry event keeps modelUsed for the requested model and adds modelRecommended and turn', async () => {
    const log: Record<string, unknown>[] = [];
    await onPreToolUse({ session_id: 's1', tool_name: 'Agent', tool_input: { model: 'opus', subagent_type: 'Explore' } }, deps(log));

    const event = adviceTelemetry(log[0]);

    expect(keys(event)).toEqual(['harness', 'modelRecommended', 'modelUsed', 'sessionId', 'taskSummary', 'tierRecommended', 'toolOrHook', 'turn']);
    expect([event.modelUsed, event.modelRecommended, event.turn, event.tierRecommended]).toEqual(['opus', 'sonnet', advice.turn, 'flash']);
  });

  it('the two records of one turn join on the same turn id', async () => {
    const prompt = 'Find where the quota file is written';
    const { log: promptLog, advice: remembered, marker } = await runPrompt(prompt);
    const preLog: Record<string, unknown>[] = [];

    await onPreToolUse({ session_id: 's1', tool_name: 'Agent', tool_input: { model: 'haiku' } }, { ...deps(preLog), lastAdvice: () => remembered, turnState: () => marker });

    expect([adviceTelemetry(promptLog).turn, adviceTelemetry(preLog[0]).turn]).toEqual([turnId(prompt), turnId(prompt)]);
  });
});
