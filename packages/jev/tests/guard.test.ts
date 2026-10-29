import { describe, it, expect, beforeEach, afterEach } from 'bun:test';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import {
  decideClaudeAgentCall,
  decideAntigravitySubagents,
  agentDefinitionModel,
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

  it('asks for any model when the session model is unknown, but not for inherit', () => {
    expect(decideClaudeAgentCall({ model: 'opus' }, null)?.decision).toBe('ask');
    expect(decideClaudeAgentCall({ model: 'sonnet' }, null)?.decision).toBe('ask');
    expect(decideClaudeAgentCall({ model: 'haiku' }, null)?.decision).toBe('ask');
    expect(decideClaudeAgentCall({ model: 'inherit' }, null)).toBeNull();
    expect(decideClaudeAgentCall({}, null)).toBeNull();
  });

  it('words every ask as a fluent sentence (no "uses model ... cannot be compared")', () => {
    const reasons = [
      decideClaudeAgentCall({ model: 'opus' }, null)?.reason, // session model unknown
      decideClaudeAgentCall({ model: 'haiku' }, 'some-future-model')?.reason, // session model not rankable
      decideClaudeAgentCall({ model: 'mystery' }, 'claude-sonnet-5-5')?.reason, // requested model unknown
      decideClaudeAgentCall({ model: 'opus' }, 'claude-sonnet-5-5')?.reason // escalation
    ];
    for (const r of reasons) {
      expect(r).toBeDefined();
      expect(r).not.toMatch(/uses model/);
      expect(r).toContain('needs approval: model "');
    }
    expect(reasons[0]).toContain('cannot be compared with the session model, which could not be determined.');
    expect(reasons[1]).toContain('cannot be compared with the session model "some-future-model", which Jev cannot rank.');
    expect(reasons[2]).toContain('is not a model Jev can rank.');
    expect(reasons[3]).toContain('is stronger than the session model');
  });

  it('uses the subagent definition model when the call sets none', () => {
    expect(decideClaudeAgentCall({ subagent_type: 'x' }, 'claude-sonnet-5-5', 'opus')?.decision).toBe('ask');
    expect(decideClaudeAgentCall({ subagent_type: 'x' }, 'claude-sonnet-5-5', 'haiku')).toBeNull();
    // an explicit call model wins over the definition, and inherit means the session model
    expect(decideClaudeAgentCall({ model: 'haiku' }, 'claude-sonnet-5-5', 'opus')).toBeNull();
    expect(decideClaudeAgentCall({ model: 'inherit' }, 'claude-sonnet-5-5', 'opus')).toBeNull();
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

  it('reads the model from a subagent definition, project before user', () => {
    const project = path.join(tmp, 'proj');
    fs.mkdirSync(path.join(project, '.claude', 'agents'), { recursive: true });
    fs.mkdirSync(path.join(tmp, '.claude', 'agents'), { recursive: true });
    fs.writeFileSync(path.join(project, '.claude', 'agents', 'deep.md'), '---\nname: deep\nmodel: opus\n---\nbody model: haiku\n');
    fs.writeFileSync(path.join(tmp, '.claude', 'agents', 'deep.md'), '---\nmodel: haiku\n---\n');
    fs.writeFileSync(path.join(tmp, '.claude', 'agents', 'quoted.md'), '---\nmodel: "sonnet"\n---\n');
    fs.writeFileSync(path.join(tmp, '.claude', 'agents', 'plain.md'), '---\nname: plain\n---\n');

    expect(agentDefinitionModel('deep', project)).toBe('opus');
    expect(agentDefinitionModel('deep', undefined)).toBe('haiku');
    expect(agentDefinitionModel('quoted', project)).toBe('sonnet');
    expect(agentDefinitionModel('plain', project)).toBeNull();
    expect(agentDefinitionModel('missing', project)).toBeNull();
  });

  it('does not follow path-like subagent names', () => {
    fs.writeFileSync(path.join(tmp, 'escape.md'), '---\nmodel: opus\n---\n');
    expect(agentDefinitionModel('../escape', path.join(tmp, 'proj'))).toBeNull();
    expect(agentDefinitionModel(undefined, tmp)).toBeNull();
  });

  it('catches a stronger model set only in the subagent definition', () => {
    const project = path.join(tmp, 'proj');
    fs.mkdirSync(path.join(project, '.claude', 'agents'), { recursive: true });
    fs.writeFileSync(path.join(project, '.claude', 'agents', 'deep.md'), '---\nmodel: opus\n---\n');
    const t = transcript('claude-sonnet-5-5');
    const base = { tool_name: 'Agent', transcript_path: t, cwd: project };

    expect(evaluateClaude({ ...base, tool_input: { subagent_type: 'deep' } })?.decision).toBe('ask');
    expect(evaluateClaude({ ...base, tool_input: { subagent_type: 'deep', model: 'haiku' } })).toBeNull();
    expect(evaluateClaude({ ...base, tool_input: { subagent_type: 'Explore' } })).toBeNull();
  });

  it('asks for an opus call when the transcript is unreadable', () => {
    const input = { tool_name: 'Agent', tool_input: { model: 'opus' }, transcript_path: path.join(tmp, 'missing.jsonl') };
    expect(evaluateClaude(input)?.decision).toBe('ask');
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

  it('asks for Pro-class model names, not just the literal pro', () => {
    expect(decideAntigravitySubagents([{ Model: 'gemini-3.8-pro' }])?.decision).toBe('ask');
    expect(decideAntigravitySubagents([{ Model: ' Opus ' }])?.decision).toBe('ask');
  });

  it('passes inherit, flash and flash_lite', () => {
    expect(decideAntigravitySubagents([{ Model: 'inherit' }, { Model: 'flash' }, { Model: 'flash_lite' }, {}])).toBeNull();
  });

  it('only evaluates invoke_subagent', () => {
    const args = { Subagents: [{ Model: 'pro' }] };
    expect(evaluateAntigravity({ toolCall: { name: 'run_command', args } })).toBeNull();
    expect(evaluateAntigravity({ toolCall: { name: 'invoke_subagent', args } })?.decision).toBe('ask');
    // a subagent list that cannot be read fails closed
    expect(evaluateAntigravity({ toolCall: { name: 'invoke_subagent', args: {} } })?.decision).toBe('ask');
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

  it('is active in on-demand and silent only in off', async () => {
    setMode('on-demand');
    expect(JSON.parse((await run('antigravity', pro))!).decision).toBe('force_ask');
    setMode('off');
    expect(await run('antigravity', pro)).toBeNull();
  });

  it('stays silent for an unknown harness or when there is no verdict', async () => {
    setMode('on');
    expect(await run('nope', pro)).toBeNull();
    expect(await run('antigravity', JSON.stringify({ toolCall: { name: 'run_command', args: {} } }))).toBeNull();
  });

  it('asks instead of passing silently when it cannot evaluate a call in mode on', async () => {
    setMode('on');
    const claude = JSON.parse((await run('claude', '{ not json'))!);
    expect(claude.hookSpecificOutput.permissionDecision).toBe('ask');
    expect(claude.hookSpecificOutput.permissionDecisionReason).toContain('could not evaluate');
    expect(JSON.parse((await run('antigravity', '{ not json'))!).decision).toBe('force_ask');
  });

  it('stays silent on malformed stdin when the mode is not on', async () => {
    setMode('off');
    expect(await run('claude', '{ not json')).toBeNull();
  });

  it('exits 0 and prints the ask on malformed stdin through the real command', () => {
    setMode('on');
    const run1 = (mode: string) => {
      fs.writeFileSync(path.join(tmp, 'config.json'), JSON.stringify({ defaults: { mode } }));
      return Bun.spawnSync(['bun', path.join(import.meta.dir, '..', 'src', 'hook.ts'), 'claude'], {
        stdin: new TextEncoder().encode('{ not json'),
        env: { ...process.env, JEV_CONFIG_FILE: path.join(tmp, 'config.json') }
      });
    };
    const on = run1('on');
    expect(on.exitCode).toBe(0);
    expect(JSON.parse(on.stdout.toString()).hookSpecificOutput.permissionDecision).toBe('ask');
    const off = run1('off');
    expect(off.exitCode).toBe(0);
    expect(off.stdout.toString()).toBe('');
  });
});
