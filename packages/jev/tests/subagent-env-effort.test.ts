import { describe, it, expect, beforeEach, afterEach } from 'bun:test';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { decideClaudeAgentCall, evaluateClaude } from '../src/guard.ts';
import { checkScriptText, type WorkflowResolver } from '../src/workflow-guard.ts';
import { effortReason, readAgentDefinition } from '../src/agent-definitions.ts';
import { parseFrontmatterFields } from '../src/agent-frontmatter.ts';
import { callSetsModel, readSubagentModelEnv, subagentModelCandidates } from '../src/models.ts';
import { discoverWorkspaceAgents } from '../src/core/discovery.ts';
import { clearConfigCacheForTesting } from '../src/core/config.ts';

const SESSION = 'claude-sonnet-5-5';
const ENV_VARS = [
  'JEV_HARNESS', 'ANTIGRAVITY_CLI', 'GEMINI_CLI', 'CLAUDE_CODE', 'CLAUDECODE', 'CLAUDE_PROJECT_DIR', 'OPENCODE_SESSION', 'OPENCODE_HOME',
  'CORTEX_SESSION', 'CLAUDE_CONFIG_DIR', 'CLAUDE_CODE_SUBAGENT_MODEL', 'CLAUDE_CODE_SUBAGENT_MODEL_FORCE', 'CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL'
];

let tmp = '';
let project = '';
let savedEnv: NodeJS.ProcessEnv = {};

beforeEach(() => {
  savedEnv = { ...process.env };
  for (const v of ENV_VARS) delete process.env[v];
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-envfx-'));
  project = path.join(tmp, 'proj');
  fs.mkdirSync(path.join(project, '.claude', 'agents'), { recursive: true });
  process.env.HOME = path.join(tmp, 'home'); // a sibling of proj, so the project walk never reaches it
  process.env.JEV_CONFIG_FILE = path.join(tmp, 'config.json');
  clearConfigCacheForTesting();
});

afterEach(() => {
  process.env = savedEnv;
  clearConfigCacheForTesting();
  fs.rmSync(tmp, { recursive: true, force: true });
});

const agentFile = (name: string, frontmatter: string) => fs.writeFileSync(path.join(project, '.claude', 'agents', `${name}.md`), `---\n${frontmatter}\n---\nbody\n`);
const input = (tool_input: Record<string, unknown>) => ({ tool_name: 'Agent', tool_input, cwd: project, model: SESSION });
const ask = (tool_input: Record<string, unknown>) => evaluateClaude({ ...input(tool_input), transcript_path: transcript() });
function transcript(): string {
  const file = path.join(tmp, 't.jsonl');
  fs.writeFileSync(file, `${JSON.stringify({ type: 'assistant', message: { model: SESSION } })}\n`);
  return file;
}

describe('subagent model environment: reading', () => {
  it('reads unset, empty and inherit as no model, and only 1/true/yes/on as force', () => {
    expect(readSubagentModelEnv({})).toEqual({ model: null, force: false, workflowModel: null });
    expect(readSubagentModelEnv({ CLAUDE_CODE_SUBAGENT_MODEL: ' Inherit ', CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL: '' }).model).toBeNull();
    expect(readSubagentModelEnv({ CLAUDE_CODE_SUBAGENT_MODEL: ' opus ' }).model).toBe('opus');
    for (const v of ['1', 'true', 'YES', ' on ']) expect(readSubagentModelEnv({ CLAUDE_CODE_SUBAGENT_MODEL_FORCE: v }).force).toBe(true);
    for (const v of ['', '0', 'false', 'off', 'no', 'maybe']) expect(readSubagentModelEnv({ CLAUDE_CODE_SUBAGENT_MODEL_FORCE: v }).force).toBe(false);
  });

  it('an empty or null call model counts as unset, as in Claude Code', () => {
    expect(callSetsModel(undefined)).toBe(false);
    expect(callSetsModel(null)).toBe(false);
    expect(callSetsModel('  ')).toBe(false);
    expect(callSetsModel('inherit')).toBe(true);
    expect(callSetsModel(5)).toBe(true);
  });

  it('orders call, definition, environment, session; forced and workflow models replace the first two', () => {
    const env = { model: 'opus', force: false, workflowModel: null };
    expect(subagentModelCandidates({ call: 'haiku', definition: 'sonnet' }, env)).toEqual([{ model: 'haiku', source: 'call' }]);
    expect(subagentModelCandidates({ definition: 'sonnet' }, env)).toEqual([{ model: 'sonnet', source: 'definition' }]);
    expect(subagentModelCandidates({ definition: 'inherit' }, env)).toEqual([{ model: 'inherit', source: 'definition' }]);
    expect(subagentModelCandidates({}, env)).toEqual([{ model: 'opus', source: 'env' }]);
    expect(subagentModelCandidates({}, { ...env, model: null })).toEqual([{ model: null, source: 'session' }]);
    expect(subagentModelCandidates({ call: 'haiku', definition: 'sonnet' }, { ...env, force: true })).toEqual([{ model: 'opus', source: 'env-forced' }]);
    expect(subagentModelCandidates({ call: 'inherit' }, { ...env, force: true })).toEqual([{ model: null, source: 'call' }]);
    expect(subagentModelCandidates({ call: 'haiku' }, { ...env, workflowModel: 'opus' }, )).toEqual([{ model: 'haiku', source: 'call' }]);
    expect(subagentModelCandidates({ call: 'haiku', workflow: true }, { ...env, workflowModel: 'opus' })).toEqual([{ model: 'opus', source: 'workflow-env' }]);
    // a workflow model and a forced model that differ: both are ranked
    expect(subagentModelCandidates({ workflow: true }, { model: 'opus', force: true, workflowModel: 'haiku' }).map((c) => c.model)).toEqual(['haiku', 'opus']);
  });
});

describe('Agent guard: CLAUDE_CODE_SUBAGENT_MODEL and CLAUDE_CODE_SUBAGENT_MODEL_FORCE', () => {
  it('asks when the environment model is above the session and the call and its definition set none', () => {
    process.env.CLAUDE_CODE_SUBAGENT_MODEL = 'opus';
    const v = ask({ prompt: 'p' });
    expect(v?.decision).toBe('ask');
    expect(v?.reason).toContain('CLAUDE_CODE_SUBAGENT_MODEL');
    expect(v?.reason).toContain('stronger than the session model');
    expect(v?.audit).toMatchObject({ kind: 'escalation', requested: 'opus', modelSource: 'env' });
  });

  it('is silent when the environment model is at or below the session, unset or inherit', () => {
    for (const m of ['sonnet', 'haiku', 'inherit', '']) {
      process.env.CLAUDE_CODE_SUBAGENT_MODEL = m;
      expect(ask({ prompt: 'p' })).toBeNull();
    }
  });

  it('asks when the environment model cannot be ranked', () => {
    process.env.CLAUDE_CODE_SUBAGENT_MODEL = 'banana';
    const v = ask({ prompt: 'p' });
    expect(v?.audit).toMatchObject({ kind: 'unknown-model' });
  });

  it('does not apply when the call or its definition sets a model (unforced)', () => {
    process.env.CLAUDE_CODE_SUBAGENT_MODEL = 'opus';
    expect(ask({ prompt: 'p', model: 'haiku' })).toBeNull();
    agentFile('cheap', 'name: cheap\nmodel: haiku');
    agentFile('inheriting', 'name: inheriting\nmodel: inherit');
    expect(ask({ prompt: 'p', subagent_type: 'cheap' })).toBeNull();
    expect(ask({ prompt: 'p', subagent_type: 'inheriting' })).toBeNull();
    // a definition with no model falls through to the environment
    agentFile('plain', 'name: plain\ndescription: x');
    expect(ask({ prompt: 'p', subagent_type: 'plain' })?.audit).toMatchObject({ modelSource: 'env' });
    // an empty call model is unset, so the environment applies
    expect(ask({ prompt: 'p', model: '' })?.decision).toBe('ask');
  });

  it('a built-in type runs on the environment model too', () => {
    process.env.CLAUDE_CODE_SUBAGENT_MODEL = 'opus';
    expect(ask({ prompt: 'p', subagent_type: 'general-purpose' })?.decision).toBe('ask');
  });

  it('FORCE replaces the call model: an env-forced model is ranked, not bypassed', () => {
    process.env.CLAUDE_CODE_SUBAGENT_MODEL = 'opus';
    process.env.CLAUDE_CODE_SUBAGENT_MODEL_FORCE = '1';
    for (const model of [undefined, 'haiku', 'sonnet', 'opus']) {
      const v = ask(model === undefined ? { prompt: 'p' } : { prompt: 'p', model });
      expect(v?.decision).toBe('ask');
      expect(v?.audit).toMatchObject({ requested: 'opus', modelSource: 'env-forced' });
      expect(v?.reason).toContain('CLAUDE_CODE_SUBAGENT_MODEL_FORCE');
    }
    agentFile('cheap', 'name: cheap\nmodel: haiku');
    expect(ask({ prompt: 'p', subagent_type: 'cheap' })?.audit).toMatchObject({ modelSource: 'env-forced' });
  });

  it('FORCE with a model at or below the session passes whatever the call asks for, which Claude Code ignores', () => {
    process.env.CLAUDE_CODE_SUBAGENT_MODEL = 'haiku';
    process.env.CLAUDE_CODE_SUBAGENT_MODEL_FORCE = 'true';
    expect(ask({ prompt: 'p', model: 'opus' })).toBeNull();
    expect(ask({ prompt: 'p' })).toBeNull();
  });

  it('FORCE with no environment model leaves every call on the session model; an unknown agent type is not asked about', () => {
    process.env.CLAUDE_CODE_SUBAGENT_MODEL_FORCE = '1';
    expect(ask({ prompt: 'p', model: 'opus' })).toBeNull();
    expect(ask({ prompt: 'p', subagent_type: 'not-installed' })).toBeNull();
    delete process.env.CLAUDE_CODE_SUBAGENT_MODEL_FORCE;
    expect(ask({ prompt: 'p', subagent_type: 'not-installed' })?.audit).toMatchObject({ kind: 'unknown-agent' });
  });

  it('FORCE still lets an explicit model "inherit" run on the session model', () => {
    process.env.CLAUDE_CODE_SUBAGENT_MODEL = 'opus';
    process.env.CLAUDE_CODE_SUBAGENT_MODEL_FORCE = '1';
    expect(ask({ prompt: 'p', model: 'inherit' })).toBeNull();
  });

  it('FORCE off values leave the call model in charge', () => {
    process.env.CLAUDE_CODE_SUBAGENT_MODEL = 'opus';
    process.env.CLAUDE_CODE_SUBAGENT_MODEL_FORCE = '0';
    expect(ask({ prompt: 'p', model: 'haiku' })).toBeNull();
  });

  it('the environment model is ranked against a project ceiling too', () => {
    process.env.CLAUDE_CODE_SUBAGENT_MODEL = 'sonnet';
    const v = decideClaudeAgentCall({}, 'haiku', null, 'none', null);
    expect(v?.decision).toBe('ask');
    // session opus, ceiling sonnet: the effective session model is sonnet, so a sonnet env model passes and opus asks
    expect(decideClaudeAgentCall({}, 'sonnet', null, 'none', { sessionModel: 'opus', ceiling: 'sonnet' }, { env: readSubagentModelEnv({ CLAUDE_CODE_SUBAGENT_MODEL: 'sonnet' }) })).toBeNull();
    expect(decideClaudeAgentCall({}, 'sonnet', null, 'none', { sessionModel: 'opus', ceiling: 'sonnet' }, { env: readSubagentModelEnv({ CLAUDE_CODE_SUBAGENT_MODEL: 'opus' }) })?.decision).toBe('ask');
  });
});

describe('Agent guard: effort', () => {
  it('asks for xhigh and max, with or without a built-in subagent_type', () => {
    for (const effort of ['max', 'xhigh']) {
      for (const extra of [{}, { subagent_type: 'general-purpose' }, { model: 'sonnet' }]) {
        const v = ask({ prompt: 'p', effort, ...extra });
        expect(v?.decision).toBe('ask');
        expect(v?.reason).toContain(`Agent call: effort "${effort}"`);
        expect(v?.audit).toMatchObject({ kind: 'effort', effort: { call: effort } });
      }
    }
  });

  it('passes low, medium, high and lower', () => {
    for (const effort of ['low', 'medium', 'high', 'lower']) expect(ask({ prompt: 'p', effort })).toBeNull();
  });

  it('asks for higher, an unknown level and a non-string effort', () => {
    expect(ask({ prompt: 'p', effort: 'higher' })?.reason).toContain('session effort');
    expect(ask({ prompt: 'p', effort: 'ludicrous' })?.reason).toContain('not a level Jev recognises');
    expect(ask({ prompt: 'p', effort: 3 })?.reason).toContain('not a string');
    expect(ask({ prompt: 'p', effort: null })?.reason).toContain('null');
  });

  it('names both problems when the model and the effort are above the limits', () => {
    const v = ask({ prompt: 'p', model: 'opus', effort: 'max' });
    expect(v?.reason).toContain('stronger than the session model');
    expect(v?.reason).toContain('interim effort threshold');
    expect(v?.audit).toMatchObject({ kind: 'escalation' });
  });

  it('effortReason is shared: the workflow wording is unchanged and `lower` is accepted only when relative', () => {
    expect(effortReason('max', 'agent() call')).toContain('agent() call: effort "max" is above the interim effort threshold');
    expect(effortReason('lower', 'agent() call')).toContain('not a level Jev recognises');
    expect(effortReason('lower', 'Agent call', true)).toBeNull();
  });
});

describe('agent definitions: effort', () => {
  const cases: Array<[string, string, boolean]> = [
    ['max', 'effort: max', true],
    ['xhigh', 'effort: xhigh', true],
    ['quoted max', 'effort: "max"', true],
    ['single-quoted max with a comment', "effort: 'max' # deep", true],
    ['quoted key', '"effort": max', true],
    ['high', 'effort: high', false],
    ['low', 'effort: low', false],
    ['an integer', 'effort: 5', true],
    ['a list', 'effort: [max]', true],
    ['a nested value', 'effort:\n  level: max', true],
    ['an unknown level', 'effort: turbo', true],
    ['two effort keys', 'effort: low\neffort: max', true],
    ['a case-variant key', 'Effort: max', true]
  ];

  it('is read from the frontmatter', () => {
    for (const [label, line, asks] of cases) {
      agentFile('thinker', `name: thinker\n${line}`);
      const v = ask({ prompt: 'p', subagent_type: 'thinker' });
      expect(v === null ? 'pass' : 'ask').toBe(asks ? 'ask' : 'pass');
      if (asks) expect(v?.reason).toContain('the definition of "thinker"');
      void label;
    }
  });

  it('is checked whatever model the call names, and with no model in the definition', () => {
    agentFile('thinker', 'name: thinker\neffort: max');
    expect(ask({ prompt: 'p', subagent_type: 'thinker' })?.audit).toMatchObject({ kind: 'effort', effort: { definition: 'max' } });
    expect(ask({ prompt: 'p', subagent_type: 'thinker', model: 'haiku' })?.audit).toMatchObject({ kind: 'effort' });
  });

  it('a definition without an effort, and an unknown agent with a call model, add nothing', () => {
    agentFile('calm', 'name: calm\nmodel: haiku');
    expect(ask({ prompt: 'p', subagent_type: 'calm' })).toBeNull();
    expect(ask({ prompt: 'p', subagent_type: 'not-installed', model: 'haiku' })).toBeNull();
  });

  it('project definitions that disagree on effort ask', () => {
    fs.mkdirSync(path.join(project, 'sub', '.claude', 'agents'), { recursive: true });
    fs.writeFileSync(path.join(project, '.git'), '');
    agentFile('dup', 'name: dup\neffort: low');
    fs.writeFileSync(path.join(project, 'sub', '.claude', 'agents', 'dup.md'), '---\nname: dup\neffort: max\n---\n');
    process.env.CLAUDE_PROJECT_DIR = project;
    const def = readAgentDefinition('dup', path.join(project, 'sub'));
    expect(def.found).toBe(true);
    expect(def.effort).toContain('unreadable agent frontmatter');
    expect(evaluateClaude({ ...input({ prompt: 'p', subagent_type: 'dup' }), cwd: path.join(project, 'sub'), transcript_path: transcript() })?.decision).toBe('ask');
  });

  it('leaves `effort` off a definition that has none', () => {
    agentFile('calm', 'name: calm\nmodel: haiku');
    expect(readAgentDefinition('calm', project)).toEqual({ found: true, model: 'haiku' });
    agentFile('calm2', 'name: calm2\neffort: low');
    expect(readAgentDefinition('calm2', project)).toEqual({ found: true, model: null, effort: 'low' });
  });
});

describe('frontmatter reader: effort', () => {
  const fm = (body: string) => `---\n${body}\n---\nbody\n`;

  it('reads plain, quoted and absent values', () => {
    expect(parseFrontmatterFields(fm('effort: max')).effort).toEqual({ kind: 'effort', value: 'max' });
    expect(parseFrontmatterFields(fm('effort: "high" # c')).effort).toEqual({ kind: 'effort', value: 'high' });
    expect(parseFrontmatterFields(fm('effort:')).effort).toEqual({ kind: 'none' });
    expect(parseFrontmatterFields(fm('model: haiku')).effort).toEqual({ kind: 'none' });
    expect(parseFrontmatterFields('no frontmatter').effort).toEqual({ kind: 'none' });
  });

  it('reports what it cannot read as unreadable without disturbing model and name', () => {
    const r = parseFrontmatterFields(fm('name: n\nmodel: haiku\neffort: 5'));
    expect(r.effort.kind).toBe('unreadable'); // Claude Code's parser reads a number
    expect(r.model).toEqual({ kind: 'model', value: 'haiku' });
    expect(r.name).toEqual({ kind: 'name', value: 'n' });
    expect(parseFrontmatterFields(fm('name: n\nEFFORT: max')).effort.kind).toBe('unreadable');
    expect(parseFrontmatterFields(fm('name: n\neffort: max\neffort: low')).effort.kind).toBe('unreadable');
    expect(parseFrontmatterFields(fm('effort: [max]'), null).effort.kind).toBe('unreadable');
  });

  it('a quoted multi-line effort is unreadable, not read as a key of its own', () => {
    expect(parseFrontmatterFields(fm('effort: "max\n  more"'), null).effort.kind).toBe('unreadable');
  });
});

describe('Workflow guard: effort and the environment model', () => {
  const resolver = (agents: Record<string, { model: string | null; effort?: string }>): WorkflowResolver => ({
    readScriptFile: (f) => ({ error: `cannot read ${f}` }),
    findNamedWorkflow: (n) => ({ error: `workflow "${n}" not found` }),
    agentDefinition: (t) => (t in agents ? { found: true, ...agents[t] } : { found: false, model: null })
  });
  const check = (script: string, r: WorkflowResolver = resolver({})) => checkScriptText(script, SESSION, r);

  it('checks the effort of an agentType definition, with or without a model on the call', () => {
    const r = resolver({ thinker: { model: null, effort: 'max' }, calm: { model: null, effort: 'high' } });
    expect(check(`await agent('p', {agentType: 'thinker'})`, r)[0].reason).toContain('agentType "thinker" definition: effort "max"');
    expect(check(`await agent('p', {agentType: 'thinker', model: 'haiku'})`, r)).toHaveLength(1);
    expect(check(`await agent('p', {agentType: 'calm'})`, r)).toEqual([]);
    expect(check(`await agent('p', {agentType: 'general-purpose', effort: 'max'})`, r)).toHaveLength(1);
  });

  it('an unknown agentType with an explicit model still passes, and without one still asks', () => {
    expect(check(`await agent('p', {agentType: 'ghost', model: 'haiku'})`)).toEqual([]);
    expect(check(`await agent('p', {agentType: 'ghost'})`)[0].reason).toContain('not an agent Jev can read');
  });

  it('CLAUDE_CODE_SUBAGENT_MODEL applies to an agent() that names no model', () => {
    process.env.CLAUDE_CODE_SUBAGENT_MODEL = 'opus';
    const sites = check(`await agent('p')`);
    expect(sites).toHaveLength(1);
    expect(sites[0].reason).toContain('agent() call with no model');
    expect(sites[0].reason).toContain('CLAUDE_CODE_SUBAGENT_MODEL');
    expect(check(`await agent('p', {model: 'haiku'})`)).toEqual([]);
    expect(check(`await agent('p', {agentType: 'cheap'})`, resolver({ cheap: { model: 'haiku' } }))).toEqual([]);
    expect(check(`await agent('p', {agentType: 'plain'})`, resolver({ plain: { model: null } }))).toHaveLength(1);
    expect(check(`await agent('p', {agentType: 'general-purpose'})`)).toHaveLength(1);
  });

  it('FORCE ranks the forced model for every agent(), whatever the call names', () => {
    process.env.CLAUDE_CODE_SUBAGENT_MODEL = 'opus';
    process.env.CLAUDE_CODE_SUBAGENT_MODEL_FORCE = 'yes';
    expect(check(`await agent('p', {model: 'haiku'})`)[0].reason).toContain('CLAUDE_CODE_SUBAGENT_MODEL_FORCE is on');
    expect(check(`await agent('p', {model: 'inherit'})`)).toEqual([]);
    process.env.CLAUDE_CODE_SUBAGENT_MODEL = 'haiku';
    expect(check(`await agent('p', {model: 'opus'})`)).toEqual([]);
  });

  it('CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL replaces the model of every agent()', () => {
    process.env.CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL = 'opus';
    expect(check(`await agent('p', {model: 'haiku'})`)[0].reason).toContain('CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL');
    expect(check(`await agent('p')`)).toHaveLength(1);
    process.env.CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL = 'haiku';
    expect(check(`await agent('p', {model: 'opus'})`)).toEqual([]);
  });

  it('a meta.phases model stays a display hint, unaffected by the environment', () => {
    process.env.CLAUDE_CODE_SUBAGENT_MODEL = 'opus';
    process.env.CLAUDE_CODE_SUBAGENT_MODEL_FORCE = '1';
    const sites = check(`export const meta = { name: 'x', description: 'y', phases: [{ title: 'T', model: 'haiku' }] }`);
    expect(sites).toEqual([]);
  });
});

describe('subagent discovery honours CLAUDE_CONFIG_DIR', () => {
  it('reads the Claude user agents from the config dir, not ~/.claude, resolving a relative value against the cwd', async () => {
    const home = process.env.HOME as string;
    const cfg = path.join(tmp, 'cfg');
    fs.mkdirSync(path.join(cfg, 'agents'), { recursive: true });
    fs.mkdirSync(path.join(home, '.claude', 'agents'), { recursive: true });
    fs.writeFileSync(path.join(cfg, 'agents', 'cfg-agent.md'), '---\nname: cfg-agent\ndescription: "from the config dir"\n---\n');
    fs.writeFileSync(path.join(home, '.claude', 'agents', 'stale-agent.md'), '---\nname: stale-agent\ndescription: "from HOME"\n---\n');
    const ws = path.join(tmp, 'ws');
    fs.mkdirSync(path.join(ws, '.git'), { recursive: true });

    process.env.CLAUDE_CONFIG_DIR = cfg;
    let agents = await discoverWorkspaceAgents(ws, 'claude-code');
    expect(agents['cfg-agent']).toBe('from the config dir');
    expect(agents['stale-agent']).toBeUndefined();

    process.env.CLAUDE_CONFIG_DIR = '../cfg';
    agents = await discoverWorkspaceAgents(ws, 'claude-code');
    expect(agents['cfg-agent']).toBe('from the config dir');

    delete process.env.CLAUDE_CONFIG_DIR;
    agents = await discoverWorkspaceAgents(ws, 'claude-code');
    expect(agents['stale-agent']).toBe('from HOME');
    expect(agents['cfg-agent']).toBeUndefined();
  });
});
