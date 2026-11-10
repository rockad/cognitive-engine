import { describe, it, expect, beforeEach, afterEach } from 'bun:test';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import {
  parseDispatch,
  buildDirective,
  resolveClaudeSubagentCriteria,
  CLAUDE_SUBAGENT_CRITERIA,
  type Dispatch
} from '../src/advisor-logic.ts';
import { onUserPromptSubmit, withTimeout, type HookDeps } from '../src/advisor-hook.ts';

const HOOK = path.join(import.meta.dir, '..', 'src', 'advisor-hook.ts');

const flashRec: Dispatch = { subagent: 'Explore', tier: 'flash', model: 'sonnet', effort: 'medium', confidence: 0.9 };
const proRec: Dispatch = { subagent: 'general-purpose', tier: 'pro', model: 'opus', effort: 'high', confidence: 0.8 };

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

describe('parseDispatch', () => {
  it('maps the pro tier to opus', () => {
    const data = { answers: { recommendedSubagent: { choice: 'Plan' }, recommendedTier: { choice: 'pro' } } };

    const rec = parseDispatch(data, 'Design the storage layer', 'normal');

    expect(rec?.model).toBe('opus');
  });

  it('falls back to self for a subagent Claude Code does not have', () => {
    const data = { answers: { recommendedSubagent: { choice: 'vault_librarian' }, recommendedTier: { choice: 'flash' } } };

    const rec = parseDispatch(data, 'Tidy the notes folder', 'normal');

    expect(rec?.subagent).toBe('self');
  });

  it('returns null when Jev sent no answers', () => {
    const data = { error: 'upstream' };

    const rec = parseDispatch(data, 'Anything at all', 'normal');

    expect(rec).toBeNull();
  });
});

describe('buildDirective', () => {
  it('names the session model as the ceiling', () => {
    const directive = buildDirective(flashRec, 'claude-sonnet-5', 'unknown');

    expect(directive).toContain('"claude-sonnet-5" is the hard ceiling');
  });

  it('asks for a user choice on the pro tier', () => {
    const directive = buildDirective(proRec, null, 'normal');

    expect(directive).toContain('let the user choose');
  });
});

describe('onUserPromptSubmit', () => {
  it('injects the directive as additionalContext in mode on', async () => {
    const input = { session_id: 's1', prompt: 'Find where the quota file is written' };

    const out = await onUserPromptSubmit(input, deps());

    expect(String(out?.hookSpecificOutput.additionalContext)).toContain('[JEV]');
  });

  it('stays silent in mode on-demand', async () => {
    const input = { session_id: 's1', prompt: 'Find where the quota file is written' };

    const out = await onUserPromptSubmit(input, deps({ readMode: async () => 'on-demand' }));

    expect(out).toBeNull();
  });

  it('stays silent when Jev is unreachable', async () => {
    const input = { session_id: 's1', prompt: 'Find where the quota file is written' };

    const out = await onUserPromptSubmit(input, deps({ dispatch: async () => null }));

    expect(out).toBeNull();
  });

  it('stays silent when the egress policy denies the working directory', async () => {
    const input = { session_id: 's1', cwd: '/somewhere', prompt: 'Find where the quota file is written' };

    const out = await onUserPromptSubmit(input, deps({ egressAllowed: () => false }));

    expect(out).toBeNull();
  });

  it('never prints a PreToolUse decision: the guard owns enforcement', async () => {
    const input = { session_id: 's1', prompt: 'Find where the quota file is written' };

    const out = await onUserPromptSubmit(input, deps({ dispatch: async () => proRec }));

    expect(Object.keys(out!.hookSpecificOutput).sort()).toEqual(['additionalContext', 'hookEventName']);
  });
});

describe('hook process', () => {
  let tmp = '';
  beforeEach(() => {
    tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-advisor-'));
    fs.writeFileSync(path.join(tmp, 'config.json'), JSON.stringify({ defaults: { mode: 'on' } }));
  });
  afterEach(() => fs.rmSync(tmp, { recursive: true, force: true }));

  const run = (stdin: string) =>
    Bun.spawnSync(['bun', HOOK, 'user-prompt-submit'], {
      stdin: new TextEncoder().encode(stdin),
      env: {
        ...process.env,
        HOME: tmp,
        JEV_CONFIG_FILE: path.join(tmp, 'config.json'),
        JEV_CLAUDE_DECISIONS_FILE: path.join(tmp, 'decisions.jsonl')
      }
    });

  it('exits 0 and prints nothing on malformed stdin', () => {
    const proc = run('not json');

    expect([proc.exitCode, proc.stdout.toString()]).toEqual([0, '']);
  });

  it('keeps one telemetry line per PreToolUse run: the process waits for the async append before it exits', () => {
    const telemetry = path.join(tmp, '.gemini', 'antigravity-cli', 'jev_telemetry.jsonl');
    const decisions = path.join(tmp, 'decisions.jsonl');
    const input = JSON.stringify({ session_id: 'sess-flush', tool_name: 'Agent', tool_input: { model: 'sonnet' } });

    for (let i = 0; i < 5; i++) {
      const proc = Bun.spawnSync(['bun', HOOK, 'pre-tool-use'], {
        stdin: new TextEncoder().encode(input),
        env: { ...process.env, HOME: tmp, JEV_CONFIG_FILE: path.join(tmp, 'config.json'), JEV_CLAUDE_DECISIONS_FILE: decisions }
      });
      expect(proc.exitCode).toBe(0);
    }

    const lines = (file: string) => fs.readFileSync(file, 'utf8').split('\n').filter(Boolean);
    expect(lines(decisions).length).toBe(5);
    const records = lines(telemetry).map((l) => JSON.parse(l));
    expect(records.length).toBe(5);
    expect(records.every((r) => r.toolOrHook === 'PreToolUse' && r.sessionId === 'sess-flush')).toBe(true);
  });

  it('exits 0 and prints nothing for an unknown event', () => {
    const proc = Bun.spawnSync(['bun', HOOK, 'nonsense'], {
      stdin: new TextEncoder().encode('{"prompt":"Find where the quota file is written"}'),
      env: { ...process.env, HOME: tmp, JEV_CONFIG_FILE: path.join(tmp, 'config.json') }
    });

    expect([proc.exitCode, proc.stdout.toString()]).toEqual([0, '']);
  });
});

describe('custom agent discovery and dispatch in Claude Code', () => {
  it('discovers custom workspace agents and merges with Claude built-ins', async () => {
    const fixtureDir = await fs.promises.mkdtemp(path.join(os.tmpdir(), 'jev-claude-agents-'));
    try {
      const agentsDir = path.join(fixtureDir, '.claude', 'agents');
      await fs.promises.mkdir(agentsDir, { recursive: true });
      await fs.promises.writeFile(
        path.join(agentsDir, 'code_engineer.md'),
        '---\nname: code_engineer\ndescription: "Software engineering and automation specialist"\n---\n# Code Engineer'
      );
      await fs.promises.writeFile(
        path.join(agentsDir, 'career_advisor.md'),
        '---\nname: career_advisor\ndescription: "Executive career partner"\n---\n# Career Advisor'
      );

      const criteria = await resolveClaudeSubagentCriteria(fixtureDir);
      expect(criteria.Explore).toBeDefined();
      expect(criteria.Plan).toBeDefined();
      expect(criteria['general-purpose']).toBeDefined();
      expect(criteria.self).toBeDefined();
      expect(criteria.code_engineer).toBe('Software engineering and automation specialist');
      expect(criteria.career_advisor).toBe('Executive career partner');
    } finally {
      await fs.promises.rm(fixtureDir, { recursive: true, force: true });
    }
  });

  it('parses custom personas in dispatch when criteria includes them', () => {
    const data = {
      answers: {
        recommendedSubagent: { choice: 'code_engineer' },
        recommendedTier: { choice: 'flash' }
      }
    };
    const criteria = { ...CLAUDE_SUBAGENT_CRITERIA, code_engineer: 'Software engineering specialist' };

    const rec = parseDispatch(data, 'Refactor the data pipeline', 'normal', criteria);

    expect(rec?.subagent).toBe('code_engineer');
    expect(rec?.model).toBe('sonnet');
    expect(rec?.tier).toBe('flash');
  });

  it('formats custom agent action directive with Agent(subagent_type, model)', () => {
    const customRec: Dispatch = { subagent: 'code_engineer', tier: 'flash', model: 'sonnet', effort: 'medium', confidence: 0.95 };

    const directive = buildDirective(customRec, 'claude-sonnet-5', 'normal');

    expect(directive).toContain('Action: Delegate to custom persona via Agent(subagent_type: "code_engineer", model: "sonnet").');
  });
});

describe('withTimeout', () => {
  it('aborts the AbortSignal when timeout expires', async () => {
    let aborted = false;
    let signalReceived: AbortSignal | undefined;

    const result = await withTimeout((signal) => {
      signalReceived = signal;
      signal.addEventListener('abort', () => {
        aborted = true;
      });
      return new Promise((resolve) => setTimeout(() => resolve('done'), 100));
    }, 20);

    expect(result).toBeNull();
    expect(aborted).toBe(true);
    expect(signalReceived?.aborted).toBe(true);
  });

  it('resolves value before timeout without aborting', async () => {
    let signalReceived: AbortSignal | undefined;

    const result = await withTimeout((signal) => {
      signalReceived = signal;
      return Promise.resolve('quick');
    }, 50);

    expect(result).toBe('quick');
    expect(signalReceived?.aborted).toBe(false);
  });
});
