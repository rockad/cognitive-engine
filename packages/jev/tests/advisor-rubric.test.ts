import { describe, it, expect, beforeEach, afterEach } from 'bun:test';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import {
  buildDirective,
  dispatchQuestions,
  isNonTaskPrompt,
  isStatusTurn,
  parseDispatch,
  resolveClaudeSubagentCriteria,
  trimTask,
  turnId,
  withoutDisplayNames,
  TIER_CRITERIA,
  PASTE_SKIP_CHARS,
  TASK_TRIM_CHARS,
  type Dispatch
} from '../src/advisor-logic.ts';
import { STAGE_RUBRIC, STAGE_RUBRIC_TEXT, STAGE_KINDS, isStageKind } from '../src/stage-rubric.ts';
import { adviceTelemetry, onUserPromptSubmit, type HookDeps } from '../src/advisor-hook.ts';
import { readLastAdvice } from '../src/advisor-state.ts';

const flashRec: Dispatch = { subagent: 'Explore', tier: 'flash', model: 'sonnet', effort: 'medium', confidence: 0.9 };
const selfLite: Dispatch = { subagent: 'self', tier: 'flash_lite', model: 'haiku', effort: 'low', confidence: 0.9 };

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

describe('stage rubric (5.3)', () => {
  it('maps the three kinds to haiku, sonnet and opus', () => {
    expect(STAGE_KINDS.map((k) => STAGE_RUBRIC[k].model)).toEqual(['haiku', 'sonnet', 'opus']);
    expect(STAGE_KINDS.map((k) => STAGE_RUBRIC[k].tier)).toEqual(['flash_lite', 'flash', 'pro']);
  });

  it('puts every rubric line into the tier criteria the router sees', () => {
    expect(TIER_CRITERIA.flash_lite).toContain(STAGE_RUBRIC.mechanical.rule);
    expect(TIER_CRITERIA.flash).toContain(STAGE_RUBRIC['settled-edit'].rule);
    expect(TIER_CRITERIA.pro).toContain(STAGE_RUBRIC.judgement.rule);
    expect(JSON.stringify(dispatchQuestions())).toContain('checklist update');
  });

  it('leaves parseDispatch unchanged', () => {
    const data = { answers: { recommendedSubagent: { choice: 'Plan' }, recommendedTier: { choice: 'flash_lite' } } };

    const rec = parseDispatch(data, 'Tick the boxes in the tasklist', 'normal');

    expect([rec?.subagent, rec?.tier, rec?.model]).toEqual(['Plan', 'flash_lite', 'haiku']);
  });

  it('adds the rubric to the line on a delegation turn only', () => {
    expect(buildDirective(flashRec, 'claude-sonnet-5', 'normal')).toContain(STAGE_RUBRIC_TEXT);
    expect(buildDirective(selfLite, 'claude-sonnet-5', 'normal')).not.toContain('Stage rubric');
  });

  it('recognises a stage kind', () => {
    expect([isStageKind('judgement'), isStageKind('review'), isStageKind(undefined)]).toEqual([true, false, false]);
  });
});

describe('silence on turns without delegation intent (5.5)', () => {
  it('returns null for a task notification without calling Jev', async () => {
    let called = false;
    const input = { session_id: 's1', prompt: '<task-notification>\n<task-id>a1</task-id> completed the run</task-notification>' };

    const out = await onUserPromptSubmit(input, deps({ dispatch: async () => ((called = true), flashRec) }));

    expect([out, called]).toEqual([null, false]);
  });

  it('returns null for a giant paste', async () => {
    const input = { session_id: 's1', prompt: `Find the cause ${'x'.repeat(PASTE_SKIP_CHARS)}` };

    const out = await onUserPromptSubmit(input, deps());

    expect(out).toBeNull();
  });

  it('trims a 6000 character paste to its head in the dispatch call', async () => {
    let sent = '';
    const input = { session_id: 's1', prompt: `Find where the table is built\n${'| a | b |\n'.repeat(600)}` };

    await onUserPromptSubmit(input, deps({ dispatch: async (task) => ((sent = task), flashRec) }));

    expect(sent.length).toBe(TASK_TRIM_CHARS);
    expect(sent.startsWith('Find where the table is built')).toBe(true);
  });

  it('stays silent on a status turn: self, a cheap tier and no verb of work', async () => {
    const input = { session_id: 's1', prompt: 'A3 completed the run, all green' };

    const out = await onUserPromptSubmit(input, deps({ dispatch: async () => selfLite }));

    expect(out).toBeNull();
  });

  it('still advises a cheap self turn that holds a verb of work', async () => {
    const input = { session_id: 's1', prompt: 'please fix the typo in the readme' };

    const out = await onUserPromptSubmit(input, deps({ dispatch: async () => selfLite }));

    expect(String(out?.hookSpecificOutput.additionalContext)).toContain('[JEV]');
  });

  it('never goes silent when the user gave an instruction', () => {
    expect(isStatusTurn(selfLite, 'thanks', { cost: 'minimal' })).toBe(false);
    expect(isStatusTurn(selfLite, 'thanks', {})).toBe(true);
  });

  it('recognises host-injected turns', () => {
    expect(isNonTaskPrompt('<bash-input>ls</bash-input>')).toBe(true);
    expect(isNonTaskPrompt('Find where the quota file is written')).toBe(false);
    expect(trimTask('short')).toBe('short');
  });
});

describe('personas match real subagent types (5.6)', () => {
  it('drops the display-name alias and keeps the kebab-case name', () => {
    const discovered = {
      'Code Engineer': 'Writes code',
      'code-engineer': 'Writes code',
      'Knowledge Researcher': 'Researches',
      'knowledge-researcher': 'Researches',
      self: 'x'
    };

    expect(Object.keys(withoutDisplayNames(discovered))).toEqual(['code-engineer', 'knowledge-researcher', 'self']);
  });

  it('keeps a display name that has no alias and a lower-case file-name alias', () => {
    const discovered = { Reviewer: 'Reviews', code_engineer: 'Writes code' };

    expect(Object.keys(withoutDisplayNames(discovered))).toEqual(['Reviewer', 'code_engineer']);
  });

  describe('against a discovered directory', () => {
    let dir = '';
    beforeEach(() => {
      dir = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-persona-'));
      fs.mkdirSync(path.join(dir, '.claude', 'agents'), { recursive: true });
      fs.writeFileSync(path.join(dir, '.claude', 'agents', 'code-engineer.md'), '---\nname: Code Engineer\ndescription: "Scripts and tests"\n---\n');
    });
    afterEach(() => fs.rmSync(dir, { recursive: true, force: true }));

    it('offers only code-engineer for a discovered "Code Engineer"', async () => {
      const criteria = await resolveClaudeSubagentCriteria(dir);

      expect(Object.keys(criteria).filter((k) => /code/i.test(k))).toEqual(['code-engineer']);
      expect(criteria.Explore).toBeDefined();
      expect(criteria.Plan).toBeDefined();
    });
  });
});

describe('recommended and used share one key (5.7)', () => {
  it('gives the same turn id for the same prompt and a different one for another', () => {
    expect(turnId(' Find the quota file ')).toBe(turnId('Find the quota file'));
    expect(turnId('Find the quota file')).not.toBe(turnId('Find the config file'));
    expect(turnId('Find the quota file')).toMatch(/^[0-9a-f]{12}$/);
  });

  it('logs the turn, the tier and the recommended model, never the prompt', async () => {
    const logged: Record<string, unknown>[] = [];
    const prompt = 'Find where the quota file is written';

    await onUserPromptSubmit({ session_id: 's1', prompt }, deps({ log: (e) => logged.push(e) }));

    expect(logged[0]).toMatchObject({ hook: 'UserPromptSubmit', session_id: 's1', turn: turnId(prompt), tier: 'flash', recommended_model: 'sonnet' });
    expect(JSON.stringify(logged)).not.toContain('quota file');
  });

  it('renames the telemetry field and keeps the old one as a deprecated alias', () => {
    const event = adviceTelemetry({ hook: 'UserPromptSubmit', session_id: 's1', turn: 'abc', subagent: 'Explore', tier: 'flash', recommended_model: 'sonnet', quota: 'unknown' });

    expect(event).toMatchObject({ harness: 'claude', turn: 'abc', modelRecommended: 'sonnet', modelUsed: 'sonnet', tierRecommended: 'flash', sessionId: 's1' });
  });

  describe('the remembered advice', () => {
    let dir = '';
    let saved: string | undefined;
    beforeEach(() => {
      dir = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-advice-'));
      saved = process.env.JEV_CLAUDE_STATE_DIR;
      process.env.JEV_CLAUDE_STATE_DIR = dir;
    });
    afterEach(() => {
      if (saved === undefined) delete process.env.JEV_CLAUDE_STATE_DIR;
      else process.env.JEV_CLAUDE_STATE_DIR = saved;
      fs.rmSync(dir, { recursive: true, force: true });
    });

    it('round-trips through the session state directory', async () => {
      const { writeLastAdvice } = await import('../src/advisor-state.ts');
      const prompt = 'Find where the quota file is written';

      await onUserPromptSubmit({ session_id: 's1', prompt }, deps({ remember: writeLastAdvice }));

      expect(readLastAdvice('s1')).toMatchObject({ turn: turnId(prompt), tier: 'flash', model: 'sonnet', subagent: 'Explore' });
      expect(readLastAdvice('other')).toBeNull();
    });
  });
});
