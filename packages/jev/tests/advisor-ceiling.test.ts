import { describe, it, expect, afterEach } from 'bun:test';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { buildDirective, effectiveSessionModel, resolveModel, type Dispatch } from '../src/advisor-logic.ts';
import { onUserPromptSubmit, projectCeiling, type HookDeps } from '../src/advisor-hook.ts';

// The guard compares a delegation with the LOWER of the session model and the project ceiling
// (guard.ts); the advice must say the same, or it tells the assistant that opus is fine while the
// guard asks.

const flashRec: Dispatch = { subagent: 'Explore', tier: 'flash', model: 'sonnet', effort: 'medium', confidence: 0.9 };
const proRec: Dispatch = { subagent: 'general-purpose', tier: 'pro', model: 'opus', effort: 'high', confidence: 0.8 };

function deps(overrides: Partial<HookDeps> = {}): HookDeps {
  return {
    readMode: async () => 'on',
    readSessionModel: () => 'claude-opus-5-5',
    readQuotaStrategy: async () => 'unknown',
    dispatch: async () => flashRec,
    log: () => {},
    ...overrides
  };
}

const context = (out: Awaited<ReturnType<typeof onUserPromptSubmit>>) => String(out?.hookSpecificOutput.additionalContext);

describe('effectiveSessionModel', () => {
  it.each([
    ['opus session, sonnet ceiling', 'claude-opus-5-5', 'sonnet', 'sonnet'],
    ['sonnet session, opus ceiling (a ceiling above changes nothing)', 'claude-sonnet-5-5', 'opus', 'claude-sonnet-5-5'],
    ['no ceiling', 'claude-opus-5-5', null, 'claude-opus-5-5'],
    ['unknown session model stays unknown', null, 'sonnet', null],
    ['an unrankable session model stays as it is', 'mystery-model', 'sonnet', 'mystery-model']
  ] as const)('%s', (_name, session, ceiling, expected) => {
    expect(effectiveSessionModel(session, ceiling)).toBe(expected);
  });
});

describe('the advisor honours the project ceiling', () => {
  it('session opus, ceiling sonnet, "use opus": the guard-prompt wording, not "the session model allows it"', async () => {
    const out = await onUserPromptSubmit(
      { session_id: 's1', prompt: 'Review the storage layer and use opus for it' },
      deps({ readCeiling: () => 'sonnet' })
    );

    const text = context(out);
    expect(text).toContain('one approval prompt');
    expect(text).not.toContain('allows it');
  });

  it('session opus, ceiling sonnet, a pro recommendation: asks before opus and names the ceiling', async () => {
    const out = await onUserPromptSubmit(
      { session_id: 's1', prompt: 'Design the storage layer for the new service' },
      deps({ readCeiling: () => 'sonnet', dispatch: async () => proRec })
    );

    const text = context(out);
    expect(text).toContain('ask the user before delegating on opus');
    expect(text).toContain('project ceiling "sonnet" (below the session model "claude-opus-5-5")');
    expect(text).not.toContain('allows it');
    expect(text).toContain('rule: per the Jev guard');
  });

  it('without a ceiling the same session still allows opus', async () => {
    const out = await onUserPromptSubmit(
      { session_id: 's1', prompt: 'Design the storage layer for the new service' },
      deps({ dispatch: async () => proRec })
    );

    expect(context(out)).toContain('allows it');
  });

  it('a ceiling reader that throws falls back to the session model', async () => {
    const out = await onUserPromptSubmit(
      { session_id: 's1', prompt: 'Design the storage layer for the new service' },
      deps({
        dispatch: async () => proRec,
        readCeiling: () => {
          throw new Error('boom');
        }
      })
    );

    expect(context(out)).toContain('allows it');
  });

  it('resolveModel applies the ceiling to the rule', () => {
    const open = resolveModel({ userIntent: {}, rec: proRec, sessionModel: 'claude-opus-5-5' });
    const capped = resolveModel({ userIntent: {}, rec: proRec, sessionModel: 'claude-opus-5-5', ceiling: 'sonnet' });

    expect([open.rule, capped.rule]).toEqual(['standing', 'guard']);
  });

  it('buildDirective takes the ceiling as the limit', () => {
    const text = buildDirective({ ...flashRec, tier: 'flash_lite', model: 'haiku' }, 'claude-opus-5-5', 'unknown', {}, 'haiku');

    expect(text).toContain('The project ceiling "haiku" (below the session model "claude-opus-5-5") is the hard ceiling');
  });
});

describe('projectCeiling', () => {
  let tmp = '';
  afterEach(() => fs.rmSync(tmp, { recursive: true, force: true }));
  const project = (file: string | null) => {
    tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-ceiling-'));
    if (file !== null) fs.writeFileSync(path.join(tmp, '.jev.json'), file);
    return tmp;
  };

  it('reads a valid guard.ceiling', () => {
    expect(projectCeiling(project(JSON.stringify({ guard: { ceiling: 'sonnet' } })))).toBe('sonnet');
  });

  it.each([
    ['no config file', null],
    ['a malformed file (the guard asks about it)', 'not json'],
    ['a ceiling Jev cannot rank', JSON.stringify({ guard: { ceiling: 'mystery' } })]
  ] as const)('%s: no ceiling, no throw', (_name, file) => {
    expect(projectCeiling(project(file))).toBeNull();
  });

  it('no cwd: no throw', () => {
    expect(() => projectCeiling(undefined)).not.toThrow();
  });
});
