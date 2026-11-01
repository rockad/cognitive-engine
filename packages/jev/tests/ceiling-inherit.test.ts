import { describe, it, expect, beforeEach, afterEach } from 'bun:test';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { run } from '../src/hook.ts';
import { clearConfigCacheForTesting } from '../src/core/config.ts';

/**
 * A project `guard.ceiling` below the session model also covers subagents that INHERIT: no model,
 * `inherit`, a built-in type, or a definition with no model all run on the session model, so they
 * are ranked against the lowered ceiling (and ask). A ceiling at or above the session model changes
 * nothing. Same for `agent()` inside a Workflow script.
 */
let tmp = '';
let proj = '';
let savedEnv: NodeJS.ProcessEnv = {};

beforeEach(() => {
  savedEnv = { ...process.env };
  for (const v of ['JEV_HARNESS', 'ANTIGRAVITY_CLI', 'GEMINI_CLI', 'CLAUDE_CODE', 'CLAUDECODE', 'CLAUDE_PROJECT_DIR', 'OPENCODE_SESSION', 'OPENCODE_HOME', 'CORTEX_SESSION']) delete process.env[v];
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-ceil-'));
  proj = path.join(tmp, 'proj');
  fs.mkdirSync(path.join(proj, '.git'), { recursive: true });
  process.env.HOME = tmp;
  process.env.JEV_CONFIG_FILE = path.join(tmp, 'config.json');
  process.env.JEV_CLAUDE_STATE_DIR = path.join(tmp, 'state');
  process.env.JEV_CLAUDE_DECISIONS_FILE = path.join(tmp, 'decisions.jsonl');
  fs.writeFileSync(process.env.JEV_CONFIG_FILE, JSON.stringify({ defaults: { mode: 'on-demand' } }));
  clearConfigCacheForTesting();
});

afterEach(() => {
  process.env = savedEnv;
  clearConfigCacheForTesting();
  fs.rmSync(tmp, { recursive: true, force: true });
});

const setCeiling = (ceiling: string | null) => {
  fs.writeFileSync(path.join(proj, '.jev.json'), JSON.stringify(ceiling ? { guard: { ceiling } } : {}));
};
const writeAgent = (name: string, text: string) => {
  const dir = path.join(proj, '.claude', 'agents');
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, `${name}.md`), text);
};
function transcript(model: string): string {
  const file = path.join(tmp, 't.jsonl');
  fs.writeFileSync(file, `${JSON.stringify({ type: 'assistant', message: { model } })}\n`);
  return file;
}
const agentCall = (toolInput: Record<string, unknown>, session = 'claude-opus-5-5') =>
  JSON.stringify({ tool_name: 'Agent', session_id: 's1', transcript_path: transcript(session), cwd: proj, tool_input: { prompt: 'p', ...toolInput } });
const workflowCall = (script: string, session = 'claude-opus-5-5') =>
  JSON.stringify({ tool_name: 'Workflow', session_id: 's1', transcript_path: transcript(session), cwd: proj, tool_input: { script } });
const decide = async (stdin: string) => {
  const out = await run('claude', stdin);
  return out ? (JSON.parse(out).hookSpecificOutput.permissionDecision as string) : null;
};
const reasonOf = async (stdin: string) => JSON.parse((await run('claude', stdin))!).hookSpecificOutput.permissionDecisionReason as string;

describe('Agent calls that inherit are checked against a project ceiling below the session model', () => {
  const inheriting: Array<[string, Record<string, unknown>, (() => void) | null]> = [
    ['no model and no subagent_type', {}, null],
    ['model "inherit"', { model: 'inherit' }, null],
    ['an empty model', { model: '' }, null],
    ['a built-in type: general-purpose', { subagent_type: 'general-purpose' }, null],
    ['a built-in type: Explore', { subagent_type: 'Explore' }, null],
    ['a built-in type: Plan', { subagent_type: 'Plan' }, null],
    ['a definition with no model', { subagent_type: 'plain' }, () => writeAgent('plain', '---\nname: plain\n---\nbody\n')],
    ['a definition with model: inherit', { subagent_type: 'plain' }, () => writeAgent('plain', '---\nmodel: inherit\n---\nbody\n')],
    ['a definition with no frontmatter', { subagent_type: 'plain' }, () => writeAgent('plain', 'body only\n')]
  ];
  for (const [name, input, setup] of inheriting) {
    it(`asks: ${name}`, async () => {
      setup?.();
      setCeiling('sonnet');
      expect(await decide(agentCall(input))).toBe('ask');
      const r = await reasonOf(agentCall(input));
      expect(r).toContain('sonnet');
      expect(r).toContain('project ceiling');
    });
    it(`passes with no ceiling: ${name}`, async () => {
      setup?.();
      setCeiling(null);
      expect(await decide(agentCall(input))).toBeNull();
    });
    it(`passes when the ceiling equals the session model: ${name}`, async () => {
      setup?.();
      setCeiling('opus');
      expect(await decide(agentCall(input))).toBeNull();
    });
    it(`passes when the ceiling is above the session model: ${name}`, async () => {
      setup?.();
      setCeiling('fable');
      expect(await decide(agentCall(input))).toBeNull();
    });
  }

  it('a model at or below the ceiling still passes', async () => {
    setCeiling('sonnet');
    expect(await decide(agentCall({ model: 'sonnet' }))).toBeNull();
    expect(await decide(agentCall({ model: 'haiku' }))).toBeNull();
    writeAgent('cheap', '---\nmodel: haiku\n---\n');
    expect(await decide(agentCall({ subagent_type: 'cheap' }))).toBeNull();
  });

  it('a model above the ceiling still asks', async () => {
    setCeiling('sonnet');
    expect(await decide(agentCall({ model: 'opus' }))).toBe('ask');
  });

  it('a ceiling below a haiku session does not matter: nothing is lowered', async () => {
    setCeiling('sonnet');
    expect(await decide(agentCall({}, 'claude-haiku-5-5'))).toBeNull();
  });

  it('an unknown session model asks whatever the ceiling', async () => {
    setCeiling('sonnet');
    expect(await decide(JSON.stringify({ tool_name: 'Agent', session_id: 'nope', cwd: proj, tool_input: {} }))).toBe('ask');
  });
});

describe('Workflow agent() calls that inherit are checked against a project ceiling below the session model', () => {
  const inheriting: Array<[string, string, (() => void) | null]> = [
    ['no options', "await agent('do it')", null],
    ['an options object without a model', "await agent('do it', { label: 'x' })", null],
    ['model "inherit"', "await agent('do it', { model: 'inherit' })", null],
    ['an agentType whose definition has no model', "await agent('do it', { agentType: 'plain' })", () => writeAgent('plain', '---\nname: plain\n---\n')],
    ['an agentType whose model is inherit', "await agent('do it', { agentType: 'plain' })", () => writeAgent('plain', '---\nmodel: inherit\n---\n')],
    ['an inherit call after a fine one', "await agent('a', { model: 'haiku' }); await agent('b')", null],
    ['an agent() inside a function', "async function f() { return agent('x') } await f()", null]
  ];
  for (const [name, script, setup] of inheriting) {
    it(`asks: ${name}`, async () => {
      setup?.();
      setCeiling('sonnet');
      expect(await decide(workflowCall(script))).toBe('ask');
      expect(await reasonOf(workflowCall(script))).toContain('project ceiling');
    });
    it(`passes with no ceiling: ${name}`, async () => {
      setup?.();
      setCeiling(null);
      expect(await decide(workflowCall(script))).toBeNull();
    });
    it(`passes when the ceiling is not below the session model: ${name}`, async () => {
      setup?.();
      setCeiling('opus');
      expect(await decide(workflowCall(script))).toBeNull();
    });
  }

  it('explicit models at or below the ceiling pass', async () => {
    setCeiling('sonnet');
    expect(await decide(workflowCall("await agent('a', { model: 'sonnet' }); await agent('b', { model: 'haiku' })"))).toBeNull();
  });

  it('a script with no agent() call passes', async () => {
    setCeiling('sonnet');
    expect(await decide(workflowCall('const x = 1 + 1'))).toBeNull();
  });

  it('an agent() call in a nested workflow file asks as well', async () => {
    setCeiling('sonnet');
    const dir = path.join(proj, '.claude', 'workflows');
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, 'inner.js'), "await agent('inner')");
    expect(await decide(workflowCall("await workflow({ scriptPath: '.claude/workflows/inner.js' })"))).toBe('ask');
  });
});
