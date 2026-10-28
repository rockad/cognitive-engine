import { describe, it, expect, beforeEach, afterEach } from 'bun:test';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import {
  checkScriptText,
  evaluateWorkflowInput,
  fsResolver,
  MAX_SCRIPT_BYTES,
  NO_FS_RESOLVER,
  type WorkflowResolver
} from '../src/workflow-guard.ts';
import { evaluateClaude } from '../src/guard.ts';
import { run } from '../src/hook.ts';
import { claudeDecisionsFile } from '../src/claude-audit.ts';
import { clearConfigCacheForTesting } from '../src/core/config.ts';

const FIX = path.join(import.meta.dir, 'fixtures', 'payloads');
const fixture = (name: string) => JSON.parse(fs.readFileSync(path.join(FIX, name), 'utf8'));
const SESSION = 'claude-sonnet-5-5';

let tmp = '';
let savedEnv: NodeJS.ProcessEnv = {};

beforeEach(() => {
  savedEnv = { ...process.env };
  for (const v of ['JEV_HARNESS', 'ANTIGRAVITY_CLI', 'GEMINI_CLI', 'CLAUDE_CODE', 'CLAUDECODE', 'CLAUDE_PROJECT_DIR', 'OPENCODE_SESSION', 'OPENCODE_HOME', 'CORTEX_SESSION']) delete process.env[v];
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-wf-'));
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

/** A resolver over in-memory tables. */
function fake(opts: { files?: Record<string, string>; names?: Record<string, string>; agents?: Record<string, string | null> } = {}): WorkflowResolver {
  return {
    readScriptFile: (f) => (f in (opts.files ?? {}) ? { text: opts.files![f], label: f } : { error: `cannot read ${f}` }),
    findNamedWorkflow: (n) => (n in (opts.names ?? {}) ? { text: opts.names![n], label: `${n}.js` } : { error: `workflow "${n}" not found` }),
    agentDefinition: (t) => (t in (opts.agents ?? {}) ? { found: true, model: opts.agents![t] } : { found: false, model: null })
  };
}

const check = (script: string, session: string | null = SESSION, resolver: WorkflowResolver = fake()) => checkScriptText(script, session, resolver);

describe('allowed shapes', () => {
  it('a script with no model anywhere is silent', () => {
    expect(check(`export const meta = { name: 'x', description: 'y' }
const rs = await parallel(['a','b'].map(p => () => agent('do ' + p, { label: p, schema: { type: 'object', properties: {} } })))
log('done'); return rs`)).toEqual([]);
  });

  it('literal models at or below the session model are silent, "inherit" too', () => {
    for (const m of ['sonnet', 'haiku', 'claude-sonnet-5-5', 'inherit']) expect(check(`await agent('p', {model: '${m}'})`)).toEqual([]);
    expect(check('await agent(`p`, {model: `haiku`})')).toEqual([]);
  });

  it('top-level await, top-level return, hashbang and `export const meta` parse', () => {
    expect(check(`#!/usr/bin/env node
export const meta = { name: 'a', description: 'b', phases: [{ title: 'T', detail: 'd' }] }
phase('T')
const r = await agent('x', {model: 'sonnet'})
return r`)).toEqual([]);
  });

  it('a phase model in meta is ranked too', () => {
    const s = check(`export const meta = { name: 'a', description: 'b', phases: [{ title: 'T', model: 'opus' }] }\nawait agent('x')`);
    expect(s).toHaveLength(1);
    expect(s[0].reason).toContain('meta.phases:');
    expect(check(`export const meta = { name: 'a', description: 'b', phases: [{ title: 'T', model: 'haiku' }] }`)).toEqual([]);
  });

  it('comments and strings mentioning model, agent or workflow never trigger', () => {
    expect(check(`// agent('p', {model: 'opus'})
/* workflow({scriptPath: x}); model: opus */
const note = "agent('x', {model: 'opus'}) and workflow(y)"
const t = \`model: \${1} opus agent\`
const data = { agent: 1, workflow: 2, model: 'opus' }
const v = args.agent + args.workflow + data['agent']
await agent('summarise the model section', {label: 'model'})`)).toEqual([]);
  });
});

describe('escalations and unknowns name the call site', () => {
  it('asks for a literal model above the session, with line and column', () => {
    const s = check(`await agent('a')\n  await agent('b', {label: 'x', model: 'opus'})`);
    expect(s).toHaveLength(1);
    expect(s[0]).toMatchObject({ file: '<inline>', line: 2 });
    expect(s[0].col).toBeGreaterThan(1);
    expect(s[0].reason).toContain('stronger than the session model');
  });

  it('lists every offending call site', () => {
    const s = check(`await agent('a', {model: 'opus'})\nawait agent('b', {model: 'fable'})\nawait agent('c', {model: 'haiku'})`);
    expect(s.map((x) => x.line)).toEqual([1, 2]);
  });

  it('asks for an unknown model and for any literal model when the session model is unknown', () => {
    expect(check(`await agent('a', {model: 'gpt-5'})`)).toHaveLength(1);
    expect(check(`await agent('a', {model: 'haiku'})`, null)).toHaveLength(1);
    expect(check(`await agent('a')`, null)).toEqual([]);
  });

  it('a quoted key and an expression-free template are read as literals', () => {
    expect(check(`await agent('a', {'model': 'opus'})`)).toHaveLength(1);
    expect(check(`await agent('a', {['model']: 'opus'})`)).toHaveLength(1);
    expect(check('await agent(`a`, {model: `opus`})')).toHaveLength(1);
  });

  it('checks every duplicate model key', () => {
    expect(check(`await agent('a', {model: 'haiku', model: 'opus'})`)).toHaveLength(1);
  });
});

describe('adversarial table: anything not understood asks', () => {
  const asks: Array<[string, string]> = [
    ['alias', `const a = agent\nawait a('x', {model: 'opus'})`],
    ['alias via destructuring default', `const { x = agent } = {}\nawait x('p')`],
    ['agent passed as a value', `await parallel([agent])`],
    ['agent stored in an object', `const o = { agent }\nawait o.agent('p')`],
    ['agent used with .call', `await agent.call(null, 'p')`],
    ['agent as a tagged template tag', 'agent`p`'],
    ['agent rebound as a function', `function agent() {}\nawait agent('p')`],
    ['agent rebound as a parameter', `const f = (agent) => agent('p')\nawait f(x)`],
    ['workflow aliased', `const w = workflow\nawait w('x')`],
    ['spread options', `await agent('p', {...opts})`],
    ['spread alongside a model', `await agent('p', {model: 'haiku', ...opts})`],
    ['spread arguments', `await agent(...a)`],
    ['computed key', `await agent('p', {['mo' + 'del']: 'opus'})`],
    ['computed key from variable', `await agent('p', {[k]: 'opus'})`],
    ['template literal with an expression', 'await agent(`p`, {model: `${tier}`})'],
    ['model from args', `await agent('p', {model: args.model})`],
    ['model from a variable', `const m = 'opus'\nawait agent('p', {model: m})`],
    ['shorthand model', `const model = 'haiku'\nawait agent('p', {model})`],
    ['conditional model', `await agent('p', {model: cond ? 'haiku' : 'opus'})`],
    ['concatenated model', `await agent('p', {model: 'op' + 'us'})`],
    ['non-string model', `await agent('p', {model: 5})`],
    ['options held in a variable', `const o = {model: 'haiku'}\nawait agent('p', o)`],
    ['optional call', `await agent?.('p')`],
    ['member call', `await tools.agent('p', {model: 'opus'})`],
    ['computed member call', `await tools['agent']('p')`],
    ['globalThis lookup', `await globalThis['ag' + 'ent']('p')`],
    ['eval', `eval("agent('p', {model: 'opus'})")`],
    ['Function constructor', `new Function("return agent")()('p')`],
    ['dynamic import', `await import('x')`],
    ['static import', `import x from 'y'\nawait agent('p')`],
    ['workflow() with a variable', `await workflow(name)`],
    ['workflow() with a dynamic scriptPath', `await workflow({scriptPath: dir + '/x.js'})`],
    ['workflow() with no argument', `await workflow()`],
    ['parse error', `await agent('p', {model: )`],
    ['TypeScript syntax', `const x: string = 'a'`],
    ['agentType from a variable', `await agent('p', {agentType: t})`]
  ];
  for (const [name, script] of asks) {
    it(`asks: ${name}`, () => {
      const s = check(script);
      expect(s.length).toBeGreaterThan(0);
      expect(s[0].line).toBeGreaterThan(0);
      expect(s[0].reason.length).toBeGreaterThan(5);
    });
  }

  it('reports the position of a parse error', () => {
    const s = check(`const a = 1\nawait agent('p', {model: )`);
    expect(s[0]).toMatchObject({ line: 2 });
    expect(s[0].reason).toContain('does not parse');
  });

  it('asks for a script over the size cap, inline and from a file', () => {
    const huge = `// ${'x'.repeat(MAX_SCRIPT_BYTES)}\nawait agent('p')`;
    expect(check(huge)[0].reason).toContain('byte limit');
    const file = path.join(tmp, 'huge.js');
    fs.writeFileSync(file, huge);
    const d = evaluateWorkflowInput({ scriptPath: file }, tmp, SESSION);
    expect(d.decision).toBe('ask');
    expect(d.message).toContain('byte limit');
  });

  it('asks (rather than crashing) on pathologically deep nesting', () => {
    const deep = `${'['.repeat(20000)}${']'.repeat(20000)}`;
    expect(check(deep).length).toBeGreaterThan(0);
  });
});

describe('agentType', () => {
  it('uses the definition model', () => {
    const r = fake({ agents: { deep: 'opus', cheap: 'haiku', plain: null, same: 'sonnet', inh: 'inherit' } });
    expect(check(`await agent('p', {agentType: 'deep'})`, SESSION, r)).toHaveLength(1);
    expect(check(`await agent('p', {agentType: 'cheap'})`, SESSION, r)).toEqual([]);
    expect(check(`await agent('p', {agentType: 'same'})`, SESSION, r)).toEqual([]);
    expect(check(`await agent('p', {agentType: 'plain'})`, SESSION, r)).toEqual([]); // no model: inherits
    expect(check(`await agent('p', {agentType: 'inh'})`, SESSION, r)).toEqual([]);
  });

  it('asks when the definition is not found', () => {
    const s = check(`await agent('p', {agentType: 'nope'})`);
    expect(s).toHaveLength(1);
    expect(s[0].reason).toContain('no definition');
  });

  it('an explicit literal model wins over the definition', () => {
    const r = fake({ agents: { deep: 'opus' } });
    expect(check(`await agent('p', {agentType: 'deep', model: 'haiku'})`, SESSION, r)).toEqual([]);
    expect(check(`await agent('p', {agentType: 'cheap', model: 'opus'})`, SESSION, fake({ agents: { cheap: 'haiku' } }))).toHaveLength(1);
  });

  it('reads the real definition files, project before user', () => {
    const project = path.join(tmp, 'proj');
    fs.mkdirSync(path.join(project, '.claude', 'agents'), { recursive: true });
    fs.mkdirSync(path.join(tmp, '.claude', 'agents'), { recursive: true });
    fs.writeFileSync(path.join(project, '.claude', 'agents', 'deep.md'), '---\nname: deep\nmodel: opus\n---\n');
    fs.writeFileSync(path.join(tmp, '.claude', 'agents', 'user-cheap.md'), '---\nmodel: haiku\n---\n');
    const r = fsResolver(project);
    expect(checkScriptText(`await agent('p', {agentType: 'deep'})`, SESSION, r)).toHaveLength(1);
    expect(checkScriptText(`await agent('p', {agentType: 'user-cheap'})`, SESSION, r)).toEqual([]);
    expect(checkScriptText(`await agent('p', {agentType: '../deep'})`, SESSION, r)).toHaveLength(1); // not followed: not found
  });
});

describe('nested workflow()', () => {
  it('follows a name and a scriptPath one level and names the nested file', () => {
    const r = fake({ names: { child: `await agent('c', {model: 'opus'})`, ok: `await agent('c')` }, files: { '/abs/c.js': `await agent('c', {model: 'fable'})` } });
    const byName = check(`await workflow('child')`, SESSION, r);
    expect(byName).toHaveLength(1);
    expect(byName[0].file).toBe('child.js');
    expect(check(`await workflow({scriptPath: '/abs/c.js'}, {a: 1})`, SESSION, r)[0].file).toBe('/abs/c.js');
    expect(check(`await workflow({name: 'child'})`, SESSION, r)).toHaveLength(1);
    expect(check(`await workflow('ok')`, SESSION, r)).toEqual([]);
  });

  it('asks when the target cannot be found or read', () => {
    expect(check(`await workflow('ghost')`)[0].reason).toContain('not found');
    expect(check(`await workflow({scriptPath: '/nope.js'})`)[0].reason).toContain('cannot read');
  });

  it('asks when the nested script nests again', () => {
    const r = fake({ names: { a: `await workflow('b')`, b: `await agent('x')` } });
    const s = check(`await workflow('a')`, SESSION, r);
    expect(s).toHaveLength(1);
    expect(s[0].reason).toContain('one level');
    expect(s[0].file).toBe('a.js');
  });

  it('asks when the nested script is itself dynamic', () => {
    const r = fake({ names: { a: `const x = agent\nawait x('p')` } });
    expect(check(`await workflow('a')`, SESSION, r)[0].file).toBe('a.js');
  });
});

describe('Workflow tool_input and the real captured fixtures', () => {
  const scriptDir = () => {
    const d = path.join(tmp, 'scratch');
    fs.mkdirSync(d, { recursive: true });
    return d;
  };

  it('inline `script` fixture (agent on sonnet) is silent from a Sonnet session, asks from Haiku', () => {
    const p = fixture('PreToolUse.workflow-script.json');
    expect(evaluateWorkflowInput(p.tool_input, tmp, 'claude-sonnet-5-5', NO_FS_RESOLVER).decision).toBe('allow');
    expect(evaluateWorkflowInput(p.tool_input, tmp, 'claude-haiku-5-5', NO_FS_RESOLVER).decision).toBe('ask');
  });

  it('`scriptPath` and resume fixtures: read the file at the absolute path', () => {
    const dir = scriptDir();
    const file = path.join(dir, 'wf-path.js');
    fs.writeFileSync(file, `export const meta = { name: 'p', description: 'd' }\nreturn await agent('ok', {model: 'sonnet'})`);
    for (const name of ['PreToolUse.workflow-scriptpath.json', 'PreToolUse.workflow-resume.json']) {
      const p = fixture(name);
      expect(Object.keys(p.tool_input)).toContain('scriptPath');
      const input = { ...p.tool_input, scriptPath: file }; // fixture path is redacted to ~; real ones are absolute
      expect(evaluateWorkflowInput(input, dir, SESSION).decision).toBe('allow');
      fs.writeFileSync(file, `return await agent('ok', {model: 'opus'})`);
      expect(evaluateWorkflowInput(input, dir, SESSION).decision).toBe('ask');
      fs.writeFileSync(file, `return await agent('ok', {model: 'sonnet'})`);
    }
  });

  it('the redacted fixture scriptPath is unreadable, so it asks', () => {
    const p = fixture('PreToolUse.workflow-scriptpath.json');
    const d = evaluateWorkflowInput(p.tool_input, tmp, SESSION);
    expect(d.decision).toBe('ask');
    expect(d.message).toContain('cannot read');
  });

  it('SYNTHESIZED `name` fixture resolves .claude/workflows in the project, then in ~', () => {
    const p = fixture('PreToolUse.workflow-name.synthesized.json');
    expect(p._synthesized).toContain('NOT a capture');
    const project = path.join(tmp, 'proj');
    expect(evaluateWorkflowInput(p.tool_input, project, SESSION).decision).toBe('ask'); // not found anywhere
    fs.mkdirSync(path.join(tmp, '.claude', 'workflows'), { recursive: true });
    fs.writeFileSync(path.join(tmp, '.claude', 'workflows', 'ok-test.mjs'), `await agent('x')`);
    expect(evaluateWorkflowInput(p.tool_input, project, SESSION).decision).toBe('allow');
    fs.mkdirSync(path.join(project, '.claude', 'workflows'), { recursive: true });
    fs.writeFileSync(path.join(project, '.claude', 'workflows', 'ok-test.js'), `await agent('x', {model: 'opus'})`); // project wins
    expect(evaluateWorkflowInput(p.tool_input, project, SESSION).decision).toBe('ask');
  });

  it('refuses a path-like workflow name', () => {
    for (const name of ['../x', '.hidden', 'a/b']) expect(evaluateWorkflowInput({ name }, tmp, SESSION).decision).toBe('ask');
  });

  it('shape mismatch asks: no source, wrong types, not an object, resume id alone', () => {
    for (const bad of [{}, { args: 1 }, { resumeFromRunId: 'wf_1' }, { script: 5 }, { script: '' }, { scriptPath: ['x'] }, null, 'str', [1]]) {
      const d = evaluateWorkflowInput(bad, tmp, SESSION);
      expect(d.decision).toBe('ask');
    }
  });

  it('analyses every source present', () => {
    const dir = scriptDir();
    const file = path.join(dir, 'a.js');
    fs.writeFileSync(file, `await agent('x', {model: 'opus'})`);
    const d = evaluateWorkflowInput({ script: `await agent('ok')`, scriptPath: file }, dir, SESSION);
    expect(d.decision).toBe('ask');
    expect(d.sites[0].file).toBe(file);
  });

  it('the ask message names each call site as file:line:col', () => {
    const d = evaluateWorkflowInput({ script: `await agent('a')\nawait agent('b', {model: 'opus'})` }, tmp, SESSION, NO_FS_RESOLVER);
    expect(d.message).toMatch(/- <inline>:2:\d+ /);
    expect(d.message).toContain('"claude-sonnet-5-5"');
  });
});

describe('hook integration', () => {
  const payload = (toolInput: unknown, extra: object = {}) => ({
    ...fixture('PreToolUse.workflow-script.json'),
    session_id: 'sess-wf',
    cwd: tmp,
    tool_input: toolInput,
    ...extra
  });
  const withTranscript = (model: string) => {
    const t = path.join(tmp, 't.jsonl');
    fs.writeFileSync(t, `${JSON.stringify({ type: 'assistant', message: { model } })}\n`);
    return t;
  };

  it('evaluateClaude routes tool_name Workflow, and uses the session model', () => {
    const t = withTranscript('claude-sonnet-5-5');
    expect(evaluateClaude(payload({ script: `await agent('a', {model: 'opus'})` }, { transcript_path: t }))?.decision).toBe('ask');
    expect(evaluateClaude(payload({ script: `await agent('a', {model: 'haiku'})` }, { transcript_path: t }))).toBeNull();
    // an Opus session may run an opus workflow
    const opus = withTranscript('claude-opus-5-5');
    expect(evaluateClaude(payload({ script: `await agent('a', {model: 'opus'})` }, { transcript_path: opus }))).toBeNull();
  });

  it('the real script fixture passes through the hook in on-demand mode from a Sonnet session', async () => {
    const t = withTranscript('claude-sonnet-5-5');
    const p = payload(fixture('PreToolUse.workflow-script.json').tool_input, { transcript_path: t });
    expect(await run('claude', JSON.stringify(p))).toBeNull();
  });

  it('shape mismatch asks through the hook and is audited with its sites', async () => {
    const out = JSON.parse((await run('claude', JSON.stringify(payload({ args: { a: 1 } }, { transcript_path: withTranscript(SESSION) }))))!);
    expect(out.hookSpecificOutput.permissionDecision).toBe('ask');
    expect(out.hookSpecificOutput.permissionDecisionReason).toContain('none of script, scriptPath or name');
    const line = JSON.parse(fs.readFileSync(claudeDecisionsFile(), 'utf8').trim());
    expect(line).toMatchObject({ event: 'ask', tool_name: 'Workflow', kind: 'workflow', session_id: 'sess-wf' });
    expect(line.sites[0].reason).toContain('none of script');
  });

  it('never writes script text into the audit', async () => {
    await run('claude', JSON.stringify(payload({ script: `// SECRET-SCRIPT-TEXT\nawait agent('SECRET-PROMPT', {model: 'opus'})` }, { transcript_path: withTranscript(SESSION) })));
    const audit = fs.readFileSync(claudeDecisionsFile(), 'utf8');
    expect(audit).toContain('"kind":"workflow"');
    expect(audit).not.toContain('SECRET');
  });

  it('is silent in mode off', async () => {
    fs.writeFileSync(process.env.JEV_CONFIG_FILE!, JSON.stringify({ defaults: { mode: 'off' } }));
    clearConfigCacheForTesting();
    expect(await run('claude', JSON.stringify(payload({ args: 1 })))).toBeNull();
  });
});
