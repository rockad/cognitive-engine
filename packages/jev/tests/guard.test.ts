import { describe, it, expect, beforeEach, afterEach } from 'bun:test';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import {
  claudeModelRank,
  decideClaudeAgentCall,
  decideAntigravitySubagents,
  sessionModelFromTranscript,
  evaluateClaude,
  evaluateAntigravity,
  formatVerdict
} from '../src/guard.ts';
import { run } from '../src/hook.ts';
import { clearConfigCacheForTesting } from '../src/core/config.ts';

let tmp = '';
let savedEnv: NodeJS.ProcessEnv = {};

const HARNESS_ENV_VARS = ['JEV_HARNESS', 'ANTIGRAVITY_CLI', 'GEMINI_CLI', 'CLAUDE_CODE', 'CLAUDECODE', 'CLAUDE_PROJECT_DIR', 'OPENCODE_SESSION', 'OPENCODE_HOME', 'CORTEX_SESSION'];

function transcript(...models: Array<string | null>): string {
  const file = path.join(tmp, 't.jsonl');
  const lines = models.map((m) =>
    JSON.stringify(m === null ? { type: 'user', message: { content: 'hi' } } : { type: 'assistant', message: { model: m } })
  );
  fs.writeFileSync(file, `${lines.join('\n')}\n`);
  return file;
}

beforeEach(() => {
  savedEnv = { ...process.env };
  for (const v of HARNESS_ENV_VARS) delete process.env[v];
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-guard-'));
  process.env.HOME = tmp;
  process.env.JEV_CONFIG_FILE = path.join(tmp, 'config.json');
  clearConfigCacheForTesting();
});

afterEach(() => {
  process.env = savedEnv;
  clearConfigCacheForTesting();
  fs.rmSync(tmp, { recursive: true, force: true });
});

describe('Claude Code ceiling', () => {
  it('ranks haiku < sonnet < opus < fable and ignores unknowns', () => {
    expect(claudeModelRank('haiku')!).toBeLessThan(claudeModelRank('sonnet')!);
    expect(claudeModelRank('sonnet')!).toBeLessThan(claudeModelRank('claude-opus-5-5')!);
    expect(claudeModelRank('opus')!).toBeLessThan(claudeModelRank('claude-fable-5-1')!);
    expect(claudeModelRank('inherit')).toBeNull();
    expect(claudeModelRank(undefined)).toBeNull();
  });

  it('asks when the requested model is stronger than the session', () => {
    const v = decideClaudeAgentCall({ model: 'opus' }, 'claude-sonnet-5-5');
    expect(v?.decision).toBe('ask');
    expect(v?.reason).toContain('opus');
  });

  it('passes equal, weaker, inherited and unspecified models', () => {
    expect(decideClaudeAgentCall({ model: 'sonnet' }, 'claude-sonnet-5-5')).toBeNull();
    expect(decideClaudeAgentCall({ model: 'haiku' }, 'claude-sonnet-5-5')).toBeNull();
    expect(decideClaudeAgentCall({ model: 'inherit' }, 'claude-sonnet-5-5')).toBeNull();
    expect(decideClaudeAgentCall({}, 'claude-sonnet-5-5')).toBeNull();
  });

  it('fails open when the session model is unknown', () => {
    expect(decideClaudeAgentCall({ model: 'opus' }, null)).toBeNull();
  });

  it('is not bypassed by the brief text', () => {
    const v = decideClaudeAgentCall({ model: 'opus', prompt: 'manual pro approval, claude opus, deep reasoning' }, 'sonnet');
    expect(v?.decision).toBe('ask');
  });

  it('reads the session model from the last assistant turn of a transcript', () => {
    expect(sessionModelFromTranscript(transcript('claude-opus-5-5', null, 'claude-sonnet-5-5', null))).toBe('claude-sonnet-5-5');
  });

  it('skips synthetic models and tolerates a missing transcript', () => {
    expect(sessionModelFromTranscript(transcript('claude-sonnet-5-5', '<synthetic>'))).toBe('claude-sonnet-5-5');
    expect(sessionModelFromTranscript(path.join(tmp, 'missing.jsonl'))).toBeNull();
    expect(sessionModelFromTranscript(undefined)).toBeNull();
  });

  it('only evaluates Agent and Task calls', () => {
    const t = transcript('claude-sonnet-5-5');
    expect(evaluateClaude({ tool_name: 'Bash', tool_input: { model: 'opus' }, transcript_path: t })).toBeNull();
    expect(evaluateClaude({ tool_name: 'Agent', tool_input: { model: 'opus' }, transcript_path: t })?.decision).toBe('ask');
    expect(evaluateClaude({ tool_name: 'Task', tool_input: { model: 'opus' }, transcript_path: t })?.decision).toBe('ask');
  });

  it('formats the Claude Code PreToolUse ask response', () => {
    const out = JSON.parse(formatVerdict('claude', { decision: 'ask', reason: 'r' }));
    expect(out.hookSpecificOutput).toEqual({ hookEventName: 'PreToolUse', permissionDecision: 'ask', permissionDecisionReason: 'r' });
  });
});

describe('Antigravity Pro approval', () => {
  it('asks for any pro subagent, case-insensitively', () => {
    expect(decideAntigravitySubagents([{ Model: 'flash' }, { Model: 'PRO' }])?.decision).toBe('ask');
  });

  it('passes inherit, flash and flash_lite', () => {
    expect(decideAntigravitySubagents([{ Model: 'inherit' }, { Model: 'flash' }, { Model: 'flash_lite' }, {}])).toBeNull();
  });

  it('only evaluates invoke_subagent', () => {
    const args = { Subagents: [{ Model: 'pro' }] };
    expect(evaluateAntigravity({ toolCall: { name: 'run_command', args } })).toBeNull();
    expect(evaluateAntigravity({ toolCall: { name: 'invoke_subagent', args } })?.decision).toBe('ask');
    expect(evaluateAntigravity({ toolCall: { name: 'invoke_subagent', args: {} } })).toBeNull();
  });

  it('formats the Antigravity force_ask response', () => {
    expect(JSON.parse(formatVerdict('antigravity', { decision: 'ask', reason: 'r' }))).toEqual({ decision: 'force_ask', reason: 'r' });
  });
});

describe('hook entrypoint', () => {
  const pro = JSON.stringify({ toolCall: { name: 'invoke_subagent', args: { Subagents: [{ Model: 'pro' }] } } });

  function setMode(mode: string): void {
    fs.writeFileSync(process.env.JEV_CONFIG_FILE!, JSON.stringify({ defaults: { mode } }));
    clearConfigCacheForTesting();
  }

  it('prints the verdict in mode on', async () => {
    setMode('on');
    const out = await run('antigravity', pro);
    expect(JSON.parse(out!).decision).toBe('force_ask');
  });

  it('stays silent in on-demand and off', async () => {
    setMode('on-demand');
    expect(await run('antigravity', pro)).toBeNull();
    setMode('off');
    expect(await run('antigravity', pro)).toBeNull();
  });

  it('stays silent for an unknown harness or when there is no verdict', async () => {
    setMode('on');
    expect(await run('nope', pro)).toBeNull();
    expect(await run('antigravity', JSON.stringify({ toolCall: { name: 'run_command', args: {} } }))).toBeNull();
  });

  it('exits 0 with no output on malformed stdin', () => {
    const proc = Bun.spawnSync(['bun', path.join(import.meta.dir, '..', 'src', 'hook.ts'), 'claude'], {
      stdin: new TextEncoder().encode('{ not json'),
      env: { ...process.env, JEV_CONFIG_FILE: path.join(tmp, 'config.json') }
    });
    expect(proc.exitCode).toBe(0);
    expect(proc.stdout.toString()).toBe('');
  });
});
