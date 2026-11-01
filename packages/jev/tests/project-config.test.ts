import { describe, it, expect, beforeEach, afterEach } from 'bun:test';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { run } from '../src/hook.ts';
import { evaluateClaude } from '../src/guard.ts';
import { inspectProjectConfig, lowerModel, tightenMode } from '../src/project-config.ts';
import { claudeDecisionsFile } from '../src/claude-audit.ts';
import { clearConfigCacheForTesting, readJevModeSync } from '../src/core/config.ts';

/**
 * Project-level Jev config (`.agents/jev.json`, else `.jev.json`, nearest directory first, found
 * from the hook cwd). It may TIGHTEN the user config for the guard and never loosen it:
 * - `guard.ceiling` (a model) lowers the session model the guard compares against;
 * - `defaults.mode` / `harnesses.<h>.mode` can only keep or raise the user's mode, never lower it;
 * - with the user's mode `off` (explicit or no config file) the guard stays silent and the project
 *   file is not even read: a repository cannot switch the guard on for a user who turned it off;
 * - a project file that cannot be read as such asks on every guarded call, like a malformed user
 *   config.
 */
let tmp = '';
let proj = '';
let savedEnv: NodeJS.ProcessEnv = {};

beforeEach(() => {
  savedEnv = { ...process.env };
  for (const v of ['JEV_HARNESS', 'ANTIGRAVITY_CLI', 'GEMINI_CLI', 'CLAUDE_CODE', 'CLAUDECODE', 'CLAUDE_PROJECT_DIR', 'OPENCODE_SESSION', 'OPENCODE_HOME', 'CORTEX_SESSION']) delete process.env[v];
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-proj-'));
  proj = path.join(tmp, 'proj');
  fs.mkdirSync(path.join(proj, 'sub', 'deep'), { recursive: true });
  process.env.HOME = tmp;
  process.env.JEV_CONFIG_FILE = path.join(tmp, 'config.json');
  process.env.JEV_CLAUDE_STATE_DIR = path.join(tmp, 'state');
  process.env.JEV_CLAUDE_DECISIONS_FILE = path.join(tmp, 'decisions.jsonl');
  setUser({ defaults: { mode: 'on-demand' } });
});

afterEach(() => {
  process.env = savedEnv;
  clearConfigCacheForTesting();
  fs.rmSync(tmp, { recursive: true, force: true });
});

function setUser(cfg: unknown) {
  fs.writeFileSync(process.env.JEV_CONFIG_FILE!, JSON.stringify(cfg));
  clearConfigCacheForTesting();
}
function setProject(body: unknown, rel = '.agents/jev.json', dir = proj) {
  const file = path.join(dir, rel);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, typeof body === 'string' ? body : JSON.stringify(body));
  return file;
}
function transcript(model: string): string {
  const file = path.join(tmp, 't.jsonl');
  fs.writeFileSync(file, `${JSON.stringify({ type: 'assistant', message: { model } })}\n`);
  return file;
}
const agent = (model: string, session: string, cwd: string | undefined = proj) =>
  JSON.stringify({ tool_name: 'Agent', session_id: 's1', transcript_path: transcript(session), cwd, tool_input: { model, prompt: 'SECRET-PROMPT' } });
const workflow = (script: string, session: string) =>
  JSON.stringify({ tool_name: 'Workflow', session_id: 's1', transcript_path: transcript(session), cwd: proj, tool_input: { script } });
const decide = async (stdin: string) => {
  const out = await run('claude', stdin);
  return out ? (JSON.parse(out).hookSpecificOutput.permissionDecision as string) : null;
};
const reason = async (stdin: string) => JSON.parse((await run('claude', stdin))!).hookSpecificOutput.permissionDecisionReason as string;
const auditLines = () => (fs.existsSync(claudeDecisionsFile()) ? fs.readFileSync(claudeDecisionsFile(), 'utf8').trim().split('\n').map((l) => JSON.parse(l)) : []);

describe('tightenMode', () => {
  it('keeps or raises the user mode, never lowers it, and never turns an off user mode on', () => {
    expect(tightenMode('on-demand', 'on')).toBe('on');
    expect(tightenMode('on', 'on-demand')).toBe('on');
    expect(tightenMode('on', 'off')).toBe('on');
    expect(tightenMode('on-demand', 'off')).toBe('on-demand');
    expect(tightenMode('on-demand', null)).toBe('on-demand');
    expect(tightenMode('off', 'on')).toBe('off');
    expect(tightenMode('off', null)).toBe('off');
  });
});

describe('lowerModel', () => {
  it('returns the weaker of two rankable models, and the session model when it cannot rank', () => {
    expect(lowerModel('claude-opus-5-5', 'sonnet')).toBe('sonnet');
    expect(lowerModel('claude-haiku-5-5', 'opus')).toBe('claude-haiku-5-5');
    expect(lowerModel('claude-sonnet-5-5', 'sonnet')).toBe('claude-sonnet-5-5');
    expect(lowerModel(null, 'sonnet')).toBeNull();
    expect(lowerModel('mystery-model', 'sonnet')).toBe('mystery-model');
  });
});

describe('a project ceiling tightens the Agent guard', () => {
  it('asks for the session model itself when the project ceiling is lower, and names the file', async () => {
    const file = setProject({ guard: { ceiling: 'sonnet' } });
    expect(await decide(agent('opus', 'claude-opus-5-5'))).toBe('ask');
    const r = await reason(agent('opus', 'claude-opus-5-5'));
    expect(r).toContain('project ceiling');
    expect(r).toContain(file);
    expect(r).toContain('sonnet');
    expect(await decide(agent('sonnet', 'claude-opus-5-5'))).toBeNull();
    expect(await decide(agent('haiku', 'claude-opus-5-5'))).toBeNull();
  });

  it('cannot raise the ceiling: a ceiling above the session model changes nothing', async () => {
    setProject({ guard: { ceiling: 'fable' } });
    expect(await decide(agent('opus', 'claude-sonnet-5-5'))).toBe('ask');
    expect(await decide(agent('sonnet', 'claude-sonnet-5-5'))).toBeNull();
  });

  it('an unknown session model still asks, ceiling or not', async () => {
    setProject({ guard: { ceiling: 'opus' } });
    const input = JSON.stringify({ tool_name: 'Agent', session_id: 'nope', cwd: proj, tool_input: { model: 'haiku' } });
    expect(await decide(input)).toBe('ask');
  });

  it('is found from a subdirectory; with both file names in one directory the lower ceiling wins', async () => {
    setProject({ guard: { ceiling: 'haiku' } }, '.jev.json');
    expect(inspectProjectConfig(path.join(proj, 'sub', 'deep'))).toMatchObject({ state: 'valid', ceiling: 'haiku', file: path.join(proj, '.jev.json') });
    setProject({ guard: { ceiling: 'sonnet' } }, '.agents/jev.json');
    expect(inspectProjectConfig(proj)).toMatchObject({ ceiling: 'haiku', file: path.join(proj, '.jev.json') });
    setProject({ guard: { ceiling: 'haiku' } }, '.agents/jev.json');
    setProject({ guard: { ceiling: 'sonnet' } }, '.jev.json');
    expect(inspectProjectConfig(proj)).toMatchObject({ ceiling: 'haiku', file: path.join(proj, '.agents', 'jev.json') });
    setProject({ guard: { ceiling: 'haiku' } }, '.jev.json', path.join(proj, 'sub'));
    expect(inspectProjectConfig(path.join(proj, 'sub', 'deep'))).toMatchObject({ ceiling: 'haiku', file: path.join(proj, 'sub', '.jev.json') });
    expect(await decide(agent('sonnet', 'claude-opus-5-5', path.join(proj, 'sub', 'deep')))).toBe('ask');
  });

  it('no project file: nothing changes', async () => {
    expect(inspectProjectConfig(proj)).toEqual({ state: 'none' });
    expect(await decide(agent('opus', 'claude-opus-5-5'))).toBeNull();
  });

  it('records the project ceiling in the audit, never prompt text', async () => {
    setProject({ guard: { ceiling: 'sonnet' } });
    await run('claude', agent('opus', 'claude-opus-5-5'));
    const rec = auditLines().find((l) => l.event === 'ask');
    expect(rec.projectCeiling).toBe('sonnet');
    expect(JSON.stringify(auditLines())).not.toContain('SECRET-PROMPT');
  });
});

describe('a project ceiling tightens the Workflow guard', () => {
  it('asks for a literal model above the ceiling even when it equals the session model', async () => {
    setProject({ guard: { ceiling: 'sonnet' } });
    expect(await decide(workflow(`await agent('p', { model: 'opus' })`, 'claude-opus-5-5'))).toBe('ask');
    expect(await decide(workflow(`await agent('p', { model: 'sonnet' })`, 'claude-opus-5-5'))).toBeNull();
    // an agent() with no model runs on the session model (opus), which is above the ceiling
    expect(await decide(workflow(`await agent('p')`, 'claude-opus-5-5'))).toBe('ask');
    expect(await decide(workflow(`await agent('p')`, 'claude-sonnet-5-5'))).toBeNull();
  });
});

describe('a project file can never loosen', () => {
  it('cannot turn the guard off', async () => {
    setUser({ defaults: { mode: 'on' } });
    setProject({ defaults: { mode: 'off' }, harnesses: { 'claude-code': { mode: 'off' } } });
    expect(await decide(agent('opus', 'claude-sonnet-5-5'))).toBe('ask');
  });

  it('cannot turn the guard on for a user whose mode is off (explicit off stays silent)', async () => {
    setUser({ defaults: { mode: 'off' } });
    setProject({ defaults: { mode: 'on' }, guard: { ceiling: 'haiku' } });
    expect(await decide(agent('opus', 'claude-sonnet-5-5'))).toBeNull();
    expect(auditLines()).toEqual([]);
  });

  it('an off user mode does not read the project file, so a malformed one is silent too', async () => {
    setUser({ defaults: { mode: 'off' } });
    setProject('{ not json');
    expect(await decide(agent('opus', 'claude-sonnet-5-5'))).toBeNull();
    expect(auditLines()).toEqual([]);
  });

  it('a per-harness user off beats a project default of on', async () => {
    setUser({ defaults: { mode: 'on' }, harnesses: { 'claude-code': { mode: 'off' } } });
    setProject({ defaults: { mode: 'on' } });
    expect(await decide(agent('opus', 'claude-sonnet-5-5'))).toBeNull();
  });

  it('a project mode of on over a user on-demand keeps the guard active', async () => {
    setProject({ defaults: { mode: 'on' } });
    expect(await decide(agent('opus', 'claude-sonnet-5-5'))).toBe('ask');
    expect(await decide(agent('haiku', 'claude-sonnet-5-5'))).toBeNull();
  });
});

describe('a malformed project file asks on every guarded call', () => {
  const bad: Array<[string, unknown]> = [
    ['invalid JSON', '{ not json'],
    ['empty file', ''],
    ['a top-level array', '[]'],
    ['a top-level string', '"x"'],
    ['null', 'null'],
    ['an unknown default mode', { defaults: { mode: 'banana' } }],
    ['a non-string default mode', { defaults: { mode: true } }],
    ['defaults that is not an object', { defaults: 'on' }],
    ['an unknown harness mode', { harnesses: { 'claude-code': { mode: 'maybe' } } }],
    ['harnesses that is not an object', { harnesses: [] }],
    ['a harness entry that is not an object', { harnesses: { 'claude-code': 'on' } }],
    ['guard that is not an object', { guard: 'strict' }],
    ['a ceiling that is not a string', { guard: { ceiling: 5 } }],
    ['a ceiling that is null', { guard: { ceiling: null } }],
    ['a ceiling Jev cannot rank', { guard: { ceiling: 'gpt-9' } }],
    ['a ceiling that is empty', { guard: { ceiling: '' } }],
    ['a ceiling that is "inherit"', { guard: { ceiling: 'inherit' } }]
  ];
  for (const [name, body] of bad) {
    it(name, async () => {
      const file = setProject(body);
      expect(await decide(agent('haiku', 'claude-sonnet-5-5'))).toBe('ask'); // even a call that is otherwise fine
      const r = await reason(agent('haiku', 'claude-sonnet-5-5'));
      expect(r).toContain(file);
      expect(r).toContain('project');
      expect(auditLines().some((l) => l.event === 'config_malformed' && l.scope === 'project' && l.file === file)).toBe(true);
    });
  }

  it('a project file over the size limit', async () => {
    setProject(JSON.stringify({ guard: { ceiling: 'sonnet' }, pad: 'x'.repeat(1024 * 1024 + 10) }));
    expect(await decide(agent('haiku', 'claude-sonnet-5-5'))).toBe('ask');
  });

  it('a project path that is a directory, not a file', async () => {
    fs.mkdirSync(path.join(proj, '.jev.json'));
    expect(await decide(agent('haiku', 'claude-sonnet-5-5'))).toBe('ask');
  });

  it('a dangling symlink', async () => {
    fs.symlinkSync(path.join(tmp, 'nowhere.json'), path.join(proj, '.jev.json'));
    expect(await decide(agent('haiku', 'claude-sonnet-5-5'))).toBe('ask');
  });

  it('applies to a Workflow call and to the Antigravity guard too, and never to unguarded tools', async () => {
    setProject('{ not json');
    expect(await decide(workflow(`await agent('p')`, 'claude-sonnet-5-5'))).toBe('ask');
    expect(await run('claude', JSON.stringify({ tool_name: 'Bash', cwd: proj, tool_input: {} }))).toBeNull();
    const out = await run('antigravity', JSON.stringify({ cwd: proj, toolCall: { name: 'invoke_subagent', args: { Subagents: [{ Model: 'flash' }] } } }));
    expect(JSON.parse(out!).decision).toBe('force_ask');
  });

  it('an unrelated bad key does not make it malformed', async () => {
    setProject({ guard: { ceiling: 'sonnet', note: 5 }, timeouts: { decisionMs: 'soon' }, anything: [1, 2], mode: 'off' });
    expect(await decide(agent('haiku', 'claude-sonnet-5-5'))).toBeNull();
    expect(inspectProjectConfig(proj)).toMatchObject({ state: 'valid', ceiling: 'sonnet' });
  });

  it('a valid project file that only sets a mode is fine', async () => {
    setProject({ defaults: { mode: 'on-demand' } });
    expect(inspectProjectConfig(proj)).toMatchObject({ state: 'valid', ceiling: null });
    expect(await decide(agent('haiku', 'claude-sonnet-5-5'))).toBeNull();
  });
});

describe('the loader still ignores a project mode (MCP tools are unchanged)', () => {
  it('readJevModeSync takes the mode from the user config only', () => {
    setUser({ defaults: { mode: 'on-demand' } });
    setProject({ defaults: { mode: 'on' }, guard: { ceiling: 'haiku' } });
    expect(readJevModeSync('claude-code', proj)).toBe('on-demand');
  });
});

describe('evaluateClaude without a project context is unchanged', () => {
  it('a bare call has no project ceiling', () => {
    const input = { tool_name: 'Agent', session_id: 's1', transcript_path: transcript('claude-opus-5-5'), tool_input: { model: 'opus' } };
    expect(evaluateClaude(input)).toBeNull();
  });
});

describe('the audit records the effective mode', () => {
  it('raised by the project, never lowered', async () => {
    setProject({ defaults: { mode: 'on' } });
    await run('claude', agent('opus', 'claude-sonnet-5-5'));
    expect(auditLines().find((l) => l.event === 'ask').mode).toBe('on');
    setProject({ defaults: { mode: 'off' } });
    await run('claude', agent('opus', 'claude-sonnet-5-5'));
    expect(auditLines().filter((l) => l.event === 'ask').pop().mode).toBe('on-demand');
  });
});
