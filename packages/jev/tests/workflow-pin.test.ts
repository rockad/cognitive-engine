import { describe, it, expect, beforeEach, afterEach } from 'bun:test';
import * as crypto from 'node:crypto';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { checkScriptText, evaluateWorkflowInput, type ScriptFact, type WorkflowResolver } from '../src/workflow-guard.ts';
import { run } from '../src/hook.ts';
import { recordPostToolUse } from '../src/workflow-pin.ts';
import { record } from '../src/record-hook.ts';
import { claudeDecisionsFile } from '../src/claude-audit.ts';
import { clearConfigCacheForTesting } from '../src/core/config.ts';

/**
 * Script TOCTOU. The hook analyses the workflow script it reads at PreToolUse; Claude Code reads
 * the file again to run it. The hook cannot hold the file still, so it (1) re-analyses the CURRENT
 * content on every call, which covers every re-invocation of a scriptPath, (2) binds each decision
 * to the sha256 of the text it analysed (audit, ask text, a pin keyed by tool_use_id) and (3) a
 * PostToolUse recorder re-hashes the files after the run and reports any drift.
 */
let tmp = '';
let savedEnv: NodeJS.ProcessEnv = {};

beforeEach(() => {
  savedEnv = { ...process.env };
  for (const v of ['JEV_HARNESS', 'ANTIGRAVITY_CLI', 'GEMINI_CLI', 'CLAUDE_CODE', 'CLAUDECODE', 'CLAUDE_PROJECT_DIR', 'OPENCODE_SESSION', 'OPENCODE_HOME', 'CORTEX_SESSION', 'CLAUDE_CODE_SUBAGENT_MODEL', 'CLAUDE_CODE_SUBAGENT_MODEL_FORCE', 'CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL']) delete process.env[v];
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-pin-'));
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

const sha = (s: string) => crypto.createHash('sha256').update(s).digest('hex');
const SESSION = 'claude-sonnet-5-5';
const CLEAN = `await agent('p')`;
const BAD = `await agent('p', { model: 'opus' })`;

function transcript(): string {
  const file = path.join(tmp, 't.jsonl');
  fs.writeFileSync(file, `${JSON.stringify({ type: 'assistant', message: { model: SESSION } })}\n`);
  return file;
}
function script(name: string, text: string): string {
  const file = path.join(tmp, name);
  fs.writeFileSync(file, text);
  return file;
}
const pre = (toolInput: object, toolUseId: string | null = 'toolu_01ABC') =>
  JSON.stringify({ tool_name: 'Workflow', session_id: 's1', transcript_path: transcript(), cwd: tmp, tool_input: toolInput, ...(toolUseId !== null ? { tool_use_id: toolUseId } : {}) });
const post = (toolInput: object, toolUseId: string | null = 'toolu_01ABC') =>
  JSON.stringify({ hook_event_name: 'PostToolUse', tool_name: 'Workflow', session_id: 's1', cwd: tmp, tool_input: toolInput, tool_response: {}, ...(toolUseId !== null ? { tool_use_id: toolUseId } : {}) });
const pinDir = () => path.join(tmp, 'state', 'workflow-pins');
const audit = () => (fs.existsSync(claudeDecisionsFile()) ? fs.readFileSync(claudeDecisionsFile(), 'utf8').trim().split('\n').map((l) => JSON.parse(l)) : []);

describe('the analysis is bound to the text it read', () => {
  const resolver: WorkflowResolver = {
    readScriptFile: (f) => (f === 'child.js' ? { text: `await agent('c')`, label: '/abs/child.js' } : { error: 'no' }),
    findNamedWorkflow: () => ({ error: 'no' }),
    agentDefinition: () => ({ found: false, model: null })
  };

  it('checkScriptText reports the sha256 and size of every text it analysed, nested ones included', () => {
    const facts: ScriptFact[] = [];
    const parent = `await workflow({ scriptPath: 'child.js' })`;
    checkScriptText(parent, SESSION, resolver, '<inline>', 0, facts);
    expect(facts).toEqual([
      { label: '<inline>', sha256: sha(parent), bytes: Buffer.byteLength(parent) },
      { label: '/abs/child.js', sha256: sha(`await agent('c')`), bytes: Buffer.byteLength(`await agent('c')`) }
    ]);
  });

  it('evaluateWorkflowInput returns the facts for an allow and for an ask', () => {
    const f = script('a.js', CLEAN);
    const allow = evaluateWorkflowInput({ scriptPath: f }, tmp, SESSION);
    expect(allow.decision).toBe('allow');
    expect(allow.scripts).toEqual([{ label: f, sha256: sha(CLEAN), bytes: CLEAN.length }]);
    const g = script('b.js', BAD);
    const ask = evaluateWorkflowInput({ scriptPath: g }, tmp, SESSION);
    expect(ask.decision).toBe('ask');
    expect(ask.scripts[0].sha256).toBe(sha(BAD));
    expect(ask.message).toContain(`sha256:${sha(BAD).slice(0, 16)}`);
  });

  it('an unreadable script yields no fact', () => {
    expect(evaluateWorkflowInput({ scriptPath: path.join(tmp, 'missing.js') }, tmp, SESSION).scripts).toEqual([]);
  });

  it('the audit of an ask carries the hashes and never the script text', async () => {
    const g = script('b.js', `// SECRET-SCRIPT-TEXT\n${BAD}`);
    await run('claude', pre({ scriptPath: g }));
    const rec = audit().find((r) => r.event === 'ask');
    expect(rec.scripts).toEqual([{ label: g, sha256: sha(`// SECRET-SCRIPT-TEXT\n${BAD}`), bytes: Buffer.byteLength(`// SECRET-SCRIPT-TEXT\n${BAD}`) }]);
    expect(JSON.stringify(audit())).not.toContain('SECRET-SCRIPT-TEXT');
  });
});

describe('every call re-analyses the current content', () => {
  it('a scriptPath edited to something that escalates asks on the next call, though the old content was clean', async () => {
    const f = script('wf.js', CLEAN);
    expect(await run('claude', pre({ scriptPath: f }, 'toolu_1'))).toBeNull();
    fs.writeFileSync(f, BAD);
    const out = JSON.parse((await run('claude', pre({ scriptPath: f }, 'toolu_2')))!);
    expect(out.hookSpecificOutput.permissionDecision).toBe('ask');
  });

  it('a resume (resumeFromRunId) is analysed from the file like any other call', async () => {
    const f = script('wf.js', BAD);
    const out = JSON.parse((await run('claude', pre({ scriptPath: f, resumeFromRunId: 'wf_x' })))!);
    expect(out.hookSpecificOutput.permissionDecision).toBe('ask');
  });
});

describe('pins and the PostToolUse drift check', () => {
  it('a decision writes a pin with the analysed hashes, keyed by tool_use_id', async () => {
    const f = script('wf.js', CLEAN);
    expect(await run('claude', pre({ scriptPath: f }))).toBeNull();
    const pins = fs.readdirSync(pinDir());
    expect(pins).toHaveLength(1);
    const pin = JSON.parse(fs.readFileSync(path.join(pinDir(), pins[0]), 'utf8'));
    expect(pin).toMatchObject({ tool_use_id: 'toolu_01ABC', session_id: 's1', decision: 'allow', scripts: [{ label: f, sha256: sha(CLEAN) }] });
    expect(JSON.stringify(pin)).not.toContain('agent(');
  });

  it('an ask is pinned too', async () => {
    const f = script('wf.js', BAD);
    await run('claude', pre({ scriptPath: f }));
    const pin = JSON.parse(fs.readFileSync(path.join(pinDir(), fs.readdirSync(pinDir())[0]), 'utf8'));
    expect(pin.decision).toBe('ask');
  });

  it('unchanged files: the post check is silent and consumes the pin', async () => {
    const f = script('wf.js', CLEAN);
    await run('claude', pre({ scriptPath: f }));
    const r = recordPostToolUse(post({ scriptPath: f }));
    expect(r).toEqual({ status: 'unchanged' });
    expect(fs.readdirSync(pinDir())).toHaveLength(0);
    expect(audit().filter((a) => a.event === 'workflow_changed')).toHaveLength(0);
  });

  it('a file edited between the analysis and the end of the run is reported, with both hashes', async () => {
    const f = script('wf.js', CLEAN);
    await run('claude', pre({ scriptPath: f }));
    fs.writeFileSync(f, BAD);
    const r = recordPostToolUse(post({ scriptPath: f }));
    expect(r.status).toBe('changed');
    const msg = JSON.parse(r.output!).systemMessage as string;
    expect(msg).toContain(f);
    expect(msg).toContain(sha(CLEAN).slice(0, 16));
    expect(msg).toContain(sha(BAD).slice(0, 16));
    const rec = audit().find((a) => a.event === 'workflow_changed');
    expect(rec).toMatchObject({ tool_use_id: 'toolu_01ABC', file: f, analysed_sha256: sha(CLEAN), current_sha256: sha(BAD), decision: 'allow' });
  });

  it('a file deleted after the analysis is reported as changed', async () => {
    const f = script('wf.js', CLEAN);
    await run('claude', pre({ scriptPath: f }));
    fs.rmSync(f);
    const r = recordPostToolUse(post({ scriptPath: f }));
    expect(r.status).toBe('changed');
    expect(audit().find((a) => a.event === 'workflow_changed')).toMatchObject({ file: f, current_sha256: null });
  });

  it('a nested workflow file that changed is reported too', async () => {
    const child = script('child.js', CLEAN);
    const parent = script('parent.js', `await workflow({ scriptPath: ${JSON.stringify(child)} })`);
    await run('claude', pre({ scriptPath: parent }));
    fs.writeFileSync(child, BAD);
    const r = recordPostToolUse(post({ scriptPath: parent }));
    expect(r.status).toBe('changed');
    expect(audit().find((a) => a.event === 'workflow_changed')?.file).toBe(child);
  });

  it('an inline script has no file to re-check: the pin holds, the post check is unchanged', async () => {
    await run('claude', pre({ script: CLEAN }));
    expect(recordPostToolUse(post({ script: CLEAN })).status).toBe('unchanged');
  });

  it('with no pin the post check is silent (Jev was off, or the call never reached the guard)', () => {
    expect(recordPostToolUse(post({ scriptPath: path.join(tmp, 'x.js') }))).toEqual({ status: 'no-pin' });
    expect(audit()).toEqual([]);
  });

  it('Jev off: no pin is written and the post check stays silent', async () => {
    fs.writeFileSync(process.env.JEV_CONFIG_FILE!, JSON.stringify({ defaults: { mode: 'off' } }));
    clearConfigCacheForTesting();
    const f = script('wf.js', BAD);
    expect(await run('claude', pre({ scriptPath: f }))).toBeNull();
    expect(fs.existsSync(pinDir())).toBe(false);
    fs.writeFileSync(f, CLEAN);
    expect(recordPostToolUse(post({ scriptPath: f }))).toEqual({ status: 'no-pin' });
    expect(audit()).toEqual([]);
  });

  it('a missing tool_use_id writes no pin and breaks nothing', async () => {
    const f = script('wf.js', CLEAN);
    expect(await run('claude', pre({ scriptPath: f }, null))).toBeNull();
    expect(fs.existsSync(pinDir())).toBe(false);
    expect(recordPostToolUse(post({ scriptPath: f }, null))).toEqual({ status: 'no-pin' });
  });

  it('a hostile tool_use_id cannot escape the pin directory', async () => {
    const f = script('wf.js', CLEAN);
    await run('claude', pre({ scriptPath: f }, '../../evil'));
    expect(fs.existsSync(path.join(tmp, 'evil.json'))).toBe(false);
    for (const name of fs.readdirSync(pinDir())) expect(name).toMatch(/^[A-Za-z0-9_-]+\.json$/);
  });

  it('other tools and garbage are ignored; the recorder never throws and prints only on drift', async () => {
    expect(recordPostToolUse(JSON.stringify({ tool_name: 'Bash', tool_use_id: 't' })).status).toBe('ignored');
    expect(recordPostToolUse('{ not json').status).toBe('error');
    expect(recordPostToolUse('').status).toBe('error');
    expect(record('PostToolUse', JSON.stringify({ tool_name: 'Bash' }))).toBe('ignored');
    const f = script('wf.js', CLEAN);
    await run('claude', pre({ scriptPath: f }));
    expect(record('PostToolUse', post({ scriptPath: f }))).toBe('unchanged');
  });

  it('runs as a real process: exit 0 always, JSON on stdout only on drift', async () => {
    const f = script('wf.js', CLEAN);
    await run('claude', pre({ scriptPath: f }));
    const hook = path.join(import.meta.dir, '..', 'src', 'record-hook.ts');
    const spawn = (stdin: string) => Bun.spawnSync(['bun', hook, 'PostToolUse'], { stdin: new TextEncoder().encode(stdin), env: process.env });
    const quiet = spawn(post({ scriptPath: f }));
    expect(quiet.exitCode).toBe(0);
    expect(quiet.stdout.toString()).toBe('');
    await run('claude', pre({ scriptPath: f }, 'toolu_2'));
    fs.writeFileSync(f, BAD);
    const loud = spawn(post({ scriptPath: f }, 'toolu_2'));
    expect(loud.exitCode).toBe(0);
    expect(JSON.parse(loud.stdout.toString()).systemMessage).toContain('changed');
    expect(spawn('{ garbage').exitCode).toBe(0);
  });
});
