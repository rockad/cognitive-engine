import { describe, it, expect, beforeEach, afterEach } from 'bun:test';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { onPreToolUse, onUserPromptSubmit, preflightReminder, type HookDeps, type PreToolDeps } from '../src/advisor-hook.ts';
import { turnId } from '../src/advisor-logic.ts';
import { hasRecentDispatch, recentAgentCalls, type LastAdvice } from '../src/advisor-state.ts';
import { STAGE_RUBRIC_TEXT } from '../src/stage-rubric.ts';

const advice: LastAdvice = { turn: 'abc123abc123', tier: 'flash', model: 'sonnet', subagent: 'Explore', at: '2026-11-09T10:00:00Z' };

function pre(overrides: Partial<PreToolDeps> = {}, log: Record<string, unknown>[] = []): PreToolDeps {
  return {
    readMode: async () => 'on',
    lastAdvice: () => advice,
    recentDispatch: () => false,
    recentAgentCalls: () => ({ count: 0, reminded: false }),
    log: (e) => log.push(e),
    ...overrides
  };
}

describe('pre-flight reminder (5.10)', () => {
  it('reminds on a Workflow when no dispatch call is recent, and names the tool and the rubric', async () => {
    const out = await onPreToolUse({ session_id: 's1', tool_name: 'Workflow', tool_input: {} }, pre());

    const text = String(out?.hookSpecificOutput.additionalContext);
    expect(out?.hookSpecificOutput.hookEventName).toBe('PreToolUse');
    expect(text).toContain('jev_batch_dispatch');
    expect(text).toContain(STAGE_RUBRIC_TEXT);
  });

  it('gives no reminder when a recent jev_batch_dispatch exists', async () => {
    const out = await onPreToolUse({ session_id: 's1', tool_name: 'Workflow', tool_input: {} }, pre({ recentDispatch: () => true }));

    expect(out).toBeNull();
  });

  it('does not remind on a lone Agent call', async () => {
    const out = await onPreToolUse({ session_id: 's1', tool_name: 'Agent', tool_input: { model: 'haiku' } }, pre());

    expect(out).toBeNull();
  });

  it('reminds on the third agent call of a session, once', async () => {
    const third = await onPreToolUse({ session_id: 's1', tool_name: 'Agent', tool_input: {} }, pre({ recentAgentCalls: () => ({ count: 2, reminded: false }) }));
    const fourth = await onPreToolUse({ session_id: 's1', tool_name: 'Agent', tool_input: {} }, pre({ recentAgentCalls: () => ({ count: 3, reminded: true }) }));

    expect([third !== null, fourth]).toEqual([true, null]);
  });

  it('never asks, denies or blocks', async () => {
    const out = await onPreToolUse({ session_id: 's1', tool_name: 'Workflow', tool_input: {} }, pre());

    expect(Object.keys(out!.hookSpecificOutput).sort()).toEqual(['additionalContext', 'hookEventName']);
    expect(preflightReminder('Workflow')).toContain('not a block');
  });

  it('is silent outside mode on, and for other tools', async () => {
    const off = await onPreToolUse({ session_id: 's1', tool_name: 'Workflow' }, pre({ readMode: async () => 'on-demand' }));
    const bash = await onPreToolUse({ session_id: 's1', tool_name: 'Bash' }, pre());

    expect([off, bash]).toEqual([null, null]);
  });

  it('logs the requested model next to the last recommendation, with the tier and the turn', async () => {
    const log: Record<string, unknown>[] = [];

    await onPreToolUse({ session_id: 's1', tool_name: 'Agent', tool_input: { model: 'opus', subagent_type: 'Explore' } }, pre({}, log));

    expect(log[0]).toMatchObject({
      hook: 'PreToolUse',
      event: 'agent_call',
      session_id: 's1',
      turn: 'abc123abc123',
      tier: 'flash',
      recommended_model: 'sonnet',
      requested_model: 'opus',
      subagent_type: 'Explore',
      reminded: false
    });
  });

  it('logs a null recommendation when no advice was given in the session', async () => {
    const log: Record<string, unknown>[] = [];

    await onPreToolUse({ session_id: 's1', tool_name: 'Agent', tool_input: {} }, pre({ lastAdvice: () => null }, log));

    expect(log[0]).toMatchObject({ turn: null, tier: null, recommended_model: null, requested_model: null });
  });

  it('writes the same turn id from both hooks for the same prompt', async () => {
    const prompt = 'Find where the quota file is written';
    const logged: Record<string, unknown>[] = [];
    let remembered: LastAdvice | null = null;
    const deps: HookDeps = {
      readMode: async () => 'on',
      readSessionModel: () => 'claude-sonnet-5',
      readQuotaStrategy: async () => 'unknown',
      dispatch: async () => ({ subagent: 'Explore', tier: 'flash', model: 'sonnet', effort: 'medium', confidence: 0.9 }),
      log: (e) => logged.push(e),
      remember: (_s, a) => (remembered = a)
    };

    await onUserPromptSubmit({ session_id: 's1', prompt }, deps);
    await onPreToolUse({ session_id: 's1', tool_name: 'Agent', tool_input: {} }, pre({ lastAdvice: () => remembered }, logged));

    expect(logged.map((e) => e.turn)).toEqual([turnId(prompt), turnId(prompt)]);
    expect(logged[1].tier).toBe('flash');
  });
});

describe('reading the logs', () => {
  let tmp = '';
  beforeEach(() => (tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-pre-'))));
  afterEach(() => fs.rmSync(tmp, { recursive: true, force: true }));

  const write = (name: string, lines: object[]) => {
    const file = path.join(tmp, name);
    fs.writeFileSync(file, lines.map((l) => JSON.stringify(l)).join('\n') + '\n');
    return file;
  };
  const NOW = Date.parse('2026-11-09T12:00:00Z');
  const ago = (min: number) => new Date(NOW - min * 60_000).toISOString();

  it('finds a recent dispatch from Claude Code in the telemetry file', () => {
    const file = write('t.jsonl', [{ timestamp: ago(5), tool: 'jev_batch_dispatch', harness: 'claude-code' }]);

    expect(hasRecentDispatch({ file, now: NOW })).toBe(true);
  });

  it('ignores an old dispatch, another harness and another tool', () => {
    const file = write('t.jsonl', [
      { timestamp: ago(45), tool: 'jev_dispatch', harness: 'claude-code' },
      { timestamp: ago(5), tool: 'jev_dispatch', harness: 'antigravity' },
      { timestamp: ago(5), tool: 'jev_judge', harness: 'claude-code' }
    ]);

    expect(hasRecentDispatch({ file, now: NOW })).toBe(false);
  });

  it('answers no for a missing file', () => {
    expect(hasRecentDispatch({ file: path.join(tmp, 'none.jsonl'), now: NOW })).toBe(false);
  });

  it('counts the session agent calls in the window and sees a reminder already sent', () => {
    const file = write('d.jsonl', [
      { ts: ago(30), event: 'agent_call', session_id: 's1' },
      { ts: ago(8), event: 'agent_call', session_id: 's1' },
      { ts: ago(3), event: 'agent_call', session_id: 's1', reminded: true },
      { ts: ago(2), event: 'agent_call', session_id: 's2' },
      { ts: ago(1), event: 'ask', session_id: 's1' }
    ]);

    expect(recentAgentCalls('s1', { file, now: NOW })).toEqual({ count: 2, reminded: true });
    expect(recentAgentCalls('s2', { file, now: NOW })).toEqual({ count: 1, reminded: false });
    expect(recentAgentCalls(null, { file, now: NOW })).toEqual({ count: 0, reminded: false });
  });
});

describe('pre-tool-use process', () => {
  it('exits 0 and prints nothing on malformed stdin', () => {
    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-pre-proc-'));
    try {
      const proc = Bun.spawnSync(['bun', path.join(import.meta.dir, '..', 'src', 'advisor-hook.ts'), 'pre-tool-use'], {
        stdin: new TextEncoder().encode('not json'),
        env: { ...process.env, HOME: tmp, JEV_CONFIG_FILE: path.join(tmp, 'config.json') }
      });

      expect([proc.exitCode, proc.stdout.toString()]).toEqual([0, '']);
    } finally {
      fs.rmSync(tmp, { recursive: true, force: true });
    }
  });
});

describe('stage plan reminder (5.4)', () => {
  const baseDeps = (remembered: { advice?: LastAdvice }): HookDeps => ({
    readMode: async () => 'on',
    readSessionModel: () => 'claude-sonnet-5',
    readQuotaStrategy: async () => 'unknown',
    dispatch: async () => ({ subagent: 'Plan', tier: 'flash', model: 'sonnet', effort: 'medium', confidence: 0.9 }),
    log: () => {},
    remember: (_s, a) => (remembered.advice = a)
  });

  it('adds the batch-dispatch reminder when the prompt names a workflow or workstreams', async () => {
    const remembered: { advice?: LastAdvice } = {};

    const out = await onUserPromptSubmit({ session_id: 's1', prompt: 'go ahead with the workstreams in a workflow' }, baseDeps(remembered));

    const text = String(out?.hookSpecificOutput.additionalContext);
    expect(text).toContain('call jev_batch_dispatch with one task per stream');
    expect(text).toContain('kind: mechanical | settled-edit | judgement');
    expect(remembered.advice?.staged).toBe(true);
  });

  it('does not add it to an ordinary prompt', async () => {
    const remembered: { advice?: LastAdvice } = {};

    const out = await onUserPromptSubmit({ session_id: 's1', prompt: 'Find where the quota file is written' }, baseDeps(remembered));

    expect(String(out?.hookSpecificOutput.additionalContext)).not.toContain('jev_batch_dispatch');
    expect(remembered.advice?.staged).toBeUndefined();
  });

  it('treats a lone Agent call as staged when the last prompt named a workflow', async () => {
    const out = await onPreToolUse(
      { session_id: 's1', tool_name: 'Agent', tool_input: {} },
      pre({ lastAdvice: () => ({ ...advice, staged: true }) })
    );

    expect(String(out?.hookSpecificOutput.additionalContext)).toContain('one task per stream');
  });
});
