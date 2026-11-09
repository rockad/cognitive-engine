import { describe, it, expect, beforeEach, afterEach } from 'bun:test';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { run } from '../src/hook.ts';
import { recordPostToolUse } from '../src/workflow-pin.ts';
import { record } from '../src/record-hook.ts';
import { claudeDecisionsFile } from '../src/claude-audit.ts';
import { clearConfigCacheForTesting } from '../src/core/config.ts';

/**
 * The PostToolUse drift recorder checks the CURRENT Jev mode: a pin written while Jev was on is
 * dropped without any stdout or warning when Jev was switched off before the run ended, but its drift
 * is still written to the audit (the decision was made while Jev was on). A missing config means
 * off. A config that cannot be trusted is not off, so drift is still reported.
 */
let tmp = '';
let savedEnv: NodeJS.ProcessEnv = {};

beforeEach(() => {
  savedEnv = { ...process.env };
  for (const v of ['JEV_HARNESS', 'ANTIGRAVITY_CLI', 'GEMINI_CLI', 'CLAUDE_CODE', 'CLAUDECODE', 'CLAUDE_PROJECT_DIR', 'OPENCODE_SESSION', 'OPENCODE_HOME', 'CORTEX_SESSION', 'CLAUDE_CODE_SUBAGENT_MODEL', 'CLAUDE_CODE_SUBAGENT_MODEL_FORCE', 'CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL']) delete process.env[v];
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-pinmode-'));
  process.env.HOME = tmp;
  process.env.JEV_CONFIG_FILE = path.join(tmp, 'config.json');
  process.env.JEV_CLAUDE_STATE_DIR = path.join(tmp, 'state');
  process.env.JEV_CLAUDE_DECISIONS_FILE = path.join(tmp, 'decisions.jsonl');
  setConfig({ defaults: { mode: 'on-demand' } });
});

afterEach(() => {
  process.env = savedEnv;
  clearConfigCacheForTesting();
  fs.rmSync(tmp, { recursive: true, force: true });
});

function setConfig(body: unknown) {
  fs.writeFileSync(process.env.JEV_CONFIG_FILE!, typeof body === 'string' ? body : JSON.stringify(body));
  clearConfigCacheForTesting();
}
const pinDir = () => path.join(tmp, 'state', 'workflow-pins');
const audit = () => (fs.existsSync(claudeDecisionsFile()) ? fs.readFileSync(claudeDecisionsFile(), 'utf8').trim().split('\n').map((l) => JSON.parse(l)) : []);

function transcript(): string {
  const file = path.join(tmp, 't.jsonl');
  fs.writeFileSync(file, `${JSON.stringify({ type: 'assistant', message: { model: 'claude-sonnet-5-5' } })}\n`);
  return file;
}
const pre = (file: string, id = 'toolu_01ABC') =>
  JSON.stringify({ tool_name: 'Workflow', session_id: 's1', transcript_path: transcript(), cwd: tmp, tool_use_id: id, tool_input: { scriptPath: file } });
const post = (file: string, id = 'toolu_01ABC') =>
  JSON.stringify({ hook_event_name: 'PostToolUse', tool_name: 'Workflow', session_id: 's1', cwd: tmp, tool_use_id: id, tool_input: { scriptPath: file }, tool_response: {} });

/** A workflow file analysed while Jev is on, then edited: the state every case below starts from. */
async function pinnedAndEdited(): Promise<string> {
  const file = path.join(tmp, 'w.js');
  fs.writeFileSync(file, `await agent('p')`);
  await run('claude', pre(file));
  expect(fs.readdirSync(pinDir())).toHaveLength(1);
  fs.writeFileSync(file, `await agent('p', { model: 'opus' })`);
  return file;
}

describe('drift is reported only while Jev is on', () => {
  it('control: Jev still on reports the drift', async () => {
    const file = await pinnedAndEdited();
    const r = recordPostToolUse(post(file));
    expect(r.status).toBe('changed');
    expect(JSON.parse(r.output!).systemMessage).toContain('changed');
  });

  const off: Array<[string, unknown]> = [
    ['defaults.mode off', { defaults: { mode: 'off' } }],
    ['claude-code harness mode off', { defaults: { mode: 'on' }, harnesses: { 'claude-code': { mode: 'off' } } }]
  ];
  for (const [name, config] of off) {
    it(`no stdout or warning, pin consumed, drift still audited when Jev was switched off after the pin: ${name}`, async () => {
      const file = await pinnedAndEdited();
      setConfig(config);
      const r = recordPostToolUse(post(file));
      expect(r.status).toBe('off');
      expect(r.output).toBeUndefined();
      expect(fs.readdirSync(pinDir())).toHaveLength(0); // the pin is dropped, not kept for a later run
      // the decision was made while Jev was on: switching it off must not erase the evidence
      const records = audit().filter((l) => l.event === 'workflow_changed');
      expect(records).toHaveLength(1);
      expect(records[0]).toMatchObject({ tool_name: 'Workflow', tool_use_id: 'toolu_01ABC', session_id: 's1', file, decision: 'allow' });
      expect(records[0].analysed_sha256).not.toBe(records[0].current_sha256);
    });
  }

  it('Jev switched off and the file unchanged: nothing is audited', async () => {
    const file = path.join(tmp, 'w.js');
    fs.writeFileSync(file, `await agent('p')`);
    await run('claude', pre(file));
    setConfig({ defaults: { mode: 'off' } });
    const r = recordPostToolUse(post(file));
    expect(r.status).toBe('off');
    expect(audit().some((l) => l.event === 'workflow_changed')).toBe(false);
  });

  it('Jev switched off and the file deleted: the drift is audited with a null current hash', async () => {
    const file = await pinnedAndEdited();
    fs.rmSync(file);
    setConfig({ defaults: { mode: 'off' } });
    expect(recordPostToolUse(post(file)).output).toBeUndefined();
    const records = audit().filter((l) => l.event === 'workflow_changed');
    expect(records).toHaveLength(1);
    expect(records[0].current_sha256).toBeNull();
  });

  it('silent when the config file was removed (no config means off)', async () => {
    const file = await pinnedAndEdited();
    // (the default config location is not HOME-relative, so the missing file is injected)
    const r = recordPostToolUse(post(file), () => ({ state: 'missing', file: path.join(tmp, 'none.json'), problems: [] }));
    expect(r.status).toBe('off');
    expect(r.output).toBeUndefined();
  });

  it('an explicit JEV_CONFIG_FILE that points at a missing file is untrusted, not off: drift is reported', async () => {
    const file = await pinnedAndEdited();
    fs.rmSync(process.env.JEV_CONFIG_FILE!);
    clearConfigCacheForTesting();
    expect(recordPostToolUse(post(file)).status).toBe('changed');
  });

  it('still reports when the config became malformed (not off, so not silent)', async () => {
    const file = await pinnedAndEdited();
    setConfig('{ not json');
    expect(recordPostToolUse(post(file)).status).toBe('changed');
  });

  it('through record() the status is also off', async () => {
    const file = await pinnedAndEdited();
    setConfig({ defaults: { mode: 'off' } });
    expect(record('PostToolUse', post(file))).toBe('off');
    expect(audit().filter((l) => l.event === 'workflow_changed')).toHaveLength(1);
  });

  it('a switch back on affects only later pins: an off-time call leaves nothing to report', async () => {
    const file = path.join(tmp, 'w.js');
    fs.writeFileSync(file, `await agent('p')`);
    setConfig({ defaults: { mode: 'off' } });
    await run('claude', pre(file));
    setConfig({ defaults: { mode: 'on' } });
    fs.writeFileSync(file, `await agent('p', { model: 'opus' })`);
    expect(recordPostToolUse(post(file)).status).toBe('no-pin');
  });
});
