import { describe, it, expect, beforeEach, afterEach } from 'bun:test';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { assessModelRequest, compareModels, parseModel } from '../src/models.ts';
import { evaluateClaude, decideClaudeAgentCall } from '../src/guard.ts';
import { resolveSessionModel, readSessionState, writeSessionState } from '../src/session-model.ts';
import { record } from '../src/record-hook.ts';
import { claudeDecisionsFile } from '../src/claude-audit.ts';
import { run } from '../src/hook.ts';
import { clearConfigCacheForTesting } from '../src/core/config.ts';

let tmp = '';
let savedEnv: NodeJS.ProcessEnv = {};

beforeEach(() => {
  savedEnv = { ...process.env };
  for (const v of ['JEV_HARNESS', 'ANTIGRAVITY_CLI', 'GEMINI_CLI', 'CLAUDE_CODE', 'CLAUDECODE', 'CLAUDE_PROJECT_DIR', 'OPENCODE_SESSION', 'OPENCODE_HOME', 'CORTEX_SESSION']) delete process.env[v];
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-esc-'));
  process.env.HOME = tmp;
  process.env.JEV_CONFIG_FILE = path.join(tmp, 'config.json');
  process.env.JEV_CLAUDE_STATE_DIR = path.join(tmp, 'state');
  process.env.JEV_CLAUDE_DECISIONS_FILE = path.join(tmp, 'decisions.jsonl');
  clearConfigCacheForTesting();
});

afterEach(() => {
  process.env = savedEnv;
  clearConfigCacheForTesting();
  fs.rmSync(tmp, { recursive: true, force: true });
});

function transcript(...models: string[]): string {
  const file = path.join(tmp, 't.jsonl');
  fs.writeFileSync(file, `${models.map((m) => JSON.stringify({ type: 'assistant', message: { model: m } })).join('\n')}\n`);
  return file;
}

const agentCall = (model: string | undefined, session: string, extra: object = {}) =>
  evaluateClaude({
    tool_name: 'Agent',
    session_id: 'sess-1',
    transcript_path: transcript(session),
    tool_input: model === undefined ? {} : { model },
    ...extra
  });

describe('model ranking', () => {
  const rank = (a: string, b: string) => compareModels(parseModel(a) as any, parseModel(b) as any);

  it('orders family-major, then by numeric version', () => {
    expect(rank('fable', 'opus')).toBeGreaterThan(0);
    expect(rank('opus', 'sonnet')).toBeGreaterThan(0);
    expect(rank('sonnet', 'haiku')).toBeGreaterThan(0);
    expect(rank('claude-opus-5-5', 'claude-opus-5')).toBeGreaterThan(0);
    expect(rank('claude-opus-5', 'claude-opus-4-8')).toBeGreaterThan(0);
    expect(rank('claude-opus-4-8', 'claude-sonnet-5-5')).toBeGreaterThan(0); // family beats version
    expect(rank('claude-fable-5-1', 'claude-opus-5-5')).toBeGreaterThan(0);
    expect(rank('opus', 'claude-opus-5-5')).toBe(0);
  });

  it('resolves aliases through the table', () => {
    expect(parseModel('opus')).toEqual({ family: 'opus', version: [5, 5] });
    expect(parseModel('sonnet')).toEqual({ family: 'sonnet', version: [5, 5] });
    expect(parseModel('haiku')).toEqual({ family: 'haiku', version: [5, 5] });
    expect(parseModel('fable')).toEqual({ family: 'fable', version: [5, 1] });
  });

  it('tolerates date and context-window suffixes and case', () => {
    expect(parseModel('claude-opus-5-5-20261028')).toEqual({ family: 'opus', version: [5, 5] });
    expect(parseModel('claude-fable-5-1[1m]')).toEqual({ family: 'fable', version: [5, 1] });
    expect(parseModel('Opus[1m]')).toEqual({ family: 'opus', version: [5, 5] });
    expect(parseModel(' CLAUDE-SONNET-5-5 ')).toEqual({ family: 'sonnet', version: [5, 5] });
  });

  it('returns unknown for anything it cannot place', () => {
    for (const m of ['', 'gpt-5', 'claude-opus-9-9', 'opusplan', 'claude-opus', '__proto__', 'constructor', undefined, null]) {
      expect(parseModel(m as any)).toBe('unknown');
    }
  });

  it('treats absent, empty and inherit as the session model', () => {
    for (const m of [undefined, null, '', 'inherit', ' Inherit ']) expect(assessModelRequest(m, 'claude-sonnet-5-5').kind).toBe('allowed');
    expect(assessModelRequest('inherit', null).kind).toBe('allowed');
  });
});

describe('escalation policy', () => {
  it('asks for the opus alias from an Opus 4.8 session', () => {
    const v = agentCall('opus', 'claude-opus-4-8');
    expect(v?.decision).toBe('ask');
    expect(v?.reason).toContain('stronger than the session model');
  });

  it('allows equal and lower silently', () => {
    expect(agentCall('haiku', 'claude-sonnet-5-5')).toBeNull();
    expect(agentCall('sonnet', 'claude-sonnet-5-5')).toBeNull();
    expect(agentCall('opus', 'claude-fable-5-1')).toBeNull();
    expect(agentCall('claude-opus-4-8', 'claude-opus-5-5')).toBeNull();
    expect(agentCall(undefined, 'claude-sonnet-5-5')).toBeNull();
  });

  it('asks for a newer version of the same family and for a higher family', () => {
    expect(agentCall('claude-sonnet-5-5', 'claude-sonnet-5')?.decision).toBe('ask');
    expect(agentCall('fable', 'claude-opus-5-5')?.decision).toBe('ask');
    expect(agentCall('opus', 'claude-sonnet-5-5')?.decision).toBe('ask');
  });

  it('asks for an unknown requested model and for an unknown session model', () => {
    expect(agentCall('mystery-model', 'claude-opus-5-5')?.decision).toBe('ask');
    expect(agentCall('haiku', 'some-future-model')?.decision).toBe('ask');
    expect(evaluateClaude({ tool_name: 'Agent', tool_input: { model: 'haiku' } })?.decision).toBe('ask'); // no state, no transcript
  });

  it('asks on a malformed tool_input instead of passing', () => {
    expect(evaluateClaude({ tool_name: 'Agent', tool_input: 'oops' })?.decision).toBe('ask');
    expect(evaluateClaude({ tool_name: 'Agent', tool_input: [1] })?.decision).toBe('ask');
  });

  it('carries audit facts without prompt text', () => {
    const v = decideClaudeAgentCall({ model: 'opus', prompt: 'SECRET-PROMPT' }, 'claude-sonnet-5-5', null, 'state');
    expect(JSON.stringify(v?.audit)).not.toContain('SECRET-PROMPT');
    expect(v?.audit).toMatchObject({ kind: 'escalation', requested: 'opus', sessionModelSource: 'state' });
  });
});

describe('session model: state file vs transcript', () => {
  const payload = { session_id: 'sess-1', transcript_path: '' };

  it('prefers the PostModelSwitch state file over the transcript', () => {
    payload.transcript_path = transcript('claude-opus-5-5');
    expect(resolveSessionModel(payload)).toEqual({ model: 'claude-opus-5-5', source: 'transcript' });
    expect(record('PostModelSwitch', JSON.stringify({ session_id: 'sess-1', from_model: 'claude-opus-5-5', to_model: 'claude-sonnet-5-5', source: 'picker' }))).toBe('recorded');
    expect(resolveSessionModel(payload)).toEqual({ model: 'claude-sonnet-5-5', source: 'state' });
    // the guard follows it: opus is now an escalation although the transcript tail says opus
    expect(evaluateClaude({ ...payload, tool_name: 'Agent', tool_input: { model: 'opus' } })?.decision).toBe('ask');
  });

  it('falls back to `model` when to_model is absent, and ignores a payload with neither', () => {
    expect(record('PostModelSwitch', JSON.stringify({ session_id: 's2', model: 'claude-haiku-5-5' }))).toBe('recorded');
    expect(readSessionState('s2')?.model).toBe('claude-haiku-5-5');
    expect(record('PostModelSwitch', JSON.stringify({ session_id: 's3' }))).toBe('no-model');
    expect(readSessionState('s3')).toBeNull();
  });

  it('falls back to the transcript when there is no state, and to none without either', () => {
    expect(resolveSessionModel({ session_id: 'nope', transcript_path: transcript('claude-sonnet-5-5') })).toEqual({ model: 'claude-sonnet-5-5', source: 'transcript' });
    expect(resolveSessionModel({ session_id: 'nope', transcript_path: path.join(tmp, 'missing') })).toEqual({ model: null, source: 'none' });
  });

  it('does not let a hostile session id escape the state directory', () => {
    writeSessionState('../../evil', 'claude-opus-5-5');
    expect(fs.existsSync(path.join(tmp, 'evil.json'))).toBe(false);
    expect(fs.readdirSync(process.env.JEV_CLAUDE_STATE_DIR!).length).toBe(1);
  });

  it('SessionStart drops stale state on startup but keeps it on resume; recorder never throws', () => {
    writeSessionState('sess-1', 'claude-opus-5-5');
    record('SessionStart', JSON.stringify({ session_id: 'sess-1', source: 'resume' }));
    expect(readSessionState('sess-1')).not.toBeNull();
    record('SessionStart', JSON.stringify({ session_id: 'sess-1', source: 'startup' }));
    expect(readSessionState('sess-1')).toBeNull();
    expect(record('PostModelSwitch', '{ not json')).toBe('error');
    expect(record('Other', '{}')).toBe('ignored');
  });

  it('runs the recorder as a real log-only command: exit 0, no output, even on garbage', () => {
    for (const stdin of ['{ not json', '', JSON.stringify({ session_id: 'sess-9', to_model: 'claude-opus-5-5', source: 'auto' })]) {
      const r = Bun.spawnSync(['bun', path.join(import.meta.dir, '..', 'src', 'record-hook.ts'), 'PostModelSwitch'], {
        stdin: new TextEncoder().encode(stdin),
        env: process.env
      });
      expect(r.exitCode).toBe(0);
      expect(r.stdout.toString()).toBe('');
    }
    expect(readSessionState('sess-9')?.model).toBe('claude-opus-5-5');
  });
});

describe('mode gating and audit', () => {
  const setMode = (mode: string) => {
    fs.writeFileSync(process.env.JEV_CONFIG_FILE!, JSON.stringify({ defaults: { mode } }));
    clearConfigCacheForTesting();
  };
  const input = () =>
    JSON.stringify({ tool_name: 'Agent', session_id: 'sess-1', transcript_path: transcript('claude-sonnet-5-5'), tool_input: { model: 'opus', prompt: 'SECRET-PROMPT' } });
  const auditLines = () =>
    fs.existsSync(claudeDecisionsFile()) ? fs.readFileSync(claudeDecisionsFile(), 'utf8').trim().split('\n').map((l) => JSON.parse(l)) : [];

  it('is active in on and on-demand, silent only in off (claude)', async () => {
    setMode('on');
    expect(JSON.parse((await run('claude', input()))!).hookSpecificOutput.permissionDecision).toBe('ask');
    setMode('on-demand');
    expect(JSON.parse((await run('claude', input()))!).hookSpecificOutput.permissionDecision).toBe('ask');
    setMode('off');
    expect(await run('claude', input())).toBeNull();
  });

  it('applies the same gating to the Antigravity Pro rule', async () => {
    const pro = JSON.stringify({ toolCall: { name: 'invoke_subagent', args: { Subagents: [{ Model: 'pro' }] } } });
    setMode('on-demand');
    expect(JSON.parse((await run('antigravity', pro))!).decision).toBe('force_ask');
    setMode('off');
    expect(await run('antigravity', pro)).toBeNull();
  });

  it('asks on empty and malformed stdin when active, and writes an audit line', async () => {
    setMode('on-demand');
    expect(JSON.parse((await run('claude', ''))!).hookSpecificOutput.permissionDecision).toBe('ask');
    expect(JSON.parse((await run('claude', '{ nope'))!).hookSpecificOutput.permissionDecision).toBe('ask');
    expect(auditLines().map((l) => l.kind)).toEqual(['guard-error', 'guard-error']);
  });

  it('writes one audit line per ask with models and session, and no prompt text', async () => {
    setMode('on');
    await run('claude', input());
    const lines = auditLines();
    expect(lines).toHaveLength(1);
    expect(lines[0]).toMatchObject({
      event: 'ask',
      tool_name: 'Agent',
      session_id: 'sess-1',
      kind: 'escalation',
      requested: 'opus',
      sessionModel: 'claude-sonnet-5-5',
      sessionModelSource: 'transcript'
    });
    expect(typeof lines[0].ts).toBe('string');
    expect(fs.readFileSync(claudeDecisionsFile(), 'utf8')).not.toContain('SECRET-PROMPT');
  });

  it('writes no audit line for an allowed call', async () => {
    setMode('on');
    await run('claude', JSON.stringify({ tool_name: 'Agent', transcript_path: transcript('claude-opus-5-5'), tool_input: { model: 'haiku' } }));
    expect(auditLines()).toHaveLength(0);
  });

  it('audits an observed model change with its source, including an automatic safety fallback', () => {
    record('PostModelSwitch', JSON.stringify({ session_id: 'sess-1', from_model: 'claude-opus-5-5', to_model: 'claude-sonnet-5-5', requested_model: 'opus', source: 'auto' }));
    expect(auditLines()).toEqual([
      expect.objectContaining({ event: 'model_change', from_model: 'claude-opus-5-5', to_model: 'claude-sonnet-5-5', requested_model: 'opus', source: 'auto', session_id: 'sess-1' })
    ]);
  });

  it('a failing audit write never changes the decision', async () => {
    setMode('on');
    process.env.JEV_CLAUDE_DECISIONS_FILE = path.join(tmp, 'state-is-a-file', 'x', 'd.jsonl');
    fs.writeFileSync(path.join(tmp, 'state-is-a-file'), 'x');
    expect(JSON.parse((await run('claude', input()))!).hookSpecificOutput.permissionDecision).toBe('ask');
  });
});
