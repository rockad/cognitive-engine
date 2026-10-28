import { describe, it, expect, beforeEach, afterEach } from 'bun:test';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';

/**
 * The guard entrypoint run as a real process, the way Claude Code runs it. With
 * "onFailure": "block" in hooks.json, a non-zero exit blocks the call; these tests pin the other
 * half: whatever goes wrong, a clean exit carries an ask, never an allow.
 */
const HOOK = path.join(import.meta.dir, '..', 'src', 'hook.ts');
let tmp = '';

beforeEach(() => {
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-entry-'));
  fs.writeFileSync(path.join(tmp, 'config.json'), JSON.stringify({ defaults: { mode: 'on-demand' } }));
});
afterEach(() => fs.rmSync(tmp, { recursive: true, force: true }));

function runHook(stdin: string | null, arg: string | undefined = 'claude', hook = HOOK) {
  const r = Bun.spawnSync(['bun', hook, ...(arg ? [arg] : [])], {
    stdin: stdin === null ? 'ignore' : new TextEncoder().encode(stdin),
    env: {
      ...process.env,
      HOME: tmp,
      JEV_CONFIG_FILE: path.join(tmp, 'config.json'),
      JEV_CLAUDE_STATE_DIR: path.join(tmp, 'state'),
      JEV_CLAUDE_DECISIONS_FILE: path.join(tmp, 'decisions.jsonl')
    }
  });
  return { code: r.exitCode, out: r.stdout.toString(), err: r.stderr.toString() };
}

const decision = (out: string) => JSON.parse(out).hookSpecificOutput.permissionDecision;

describe('guard entrypoint never allows what it cannot evaluate', () => {
  it('asks on malformed stdin', () => {
    const r = runHook('{ not json');
    expect(r.code).toBe(0);
    expect(decision(r.out)).toBe('ask');
  });

  it('asks on empty stdin', () => {
    const r = runHook('');
    expect(r.code).toBe(0);
    expect(decision(r.out)).toBe('ask');
  });

  it('asks on a closed stdin', () => {
    const r = runHook(null);
    expect(r.code).toBe(0);
    expect(decision(r.out)).toBe('ask');
  });

  it('asks on JSON that is not an object', () => {
    for (const stdin of ['null', '42', '"x"', '[]']) {
      const r = runHook(stdin);
      expect(r.code).toBe(0);
      expect(decision(r.out)).toBe('ask');
    }
  });

  it('asks on an internal exception during evaluation', () => {
    // a numeric cwd makes the agent-definition lookup throw inside the guard
    const r = runHook(JSON.stringify({ tool_name: 'Agent', cwd: 5, session_id: 's', tool_input: { subagent_type: 'x', prompt: 'p' } }));
    expect(r.code).toBe(0);
    expect(decision(r.out)).toBe('ask');
    expect(JSON.parse(r.out).hookSpecificOutput.permissionDecisionReason).toContain('could not evaluate');
  });

  it('asks on a Workflow call with a shape it does not know', () => {
    const r = runHook(JSON.stringify({ tool_name: 'Workflow', cwd: tmp, tool_input: { something: 'else' } }));
    expect(r.code).toBe(0);
    expect(decision(r.out)).toBe('ask');
  });

  it('stays active, rather than falling back to off, when the Jev config is not valid JSON', () => {
    fs.writeFileSync(path.join(tmp, 'config.json'), '{ broken');
    const r = runHook(JSON.stringify({ tool_name: 'Agent', tool_input: { model: 'opus' } }));
    expect(r.code).toBe(0);
    expect(decision(r.out)).toBe('ask'); // no session model is known, so any model request asks
    expect(runHook('{ not json').out).not.toBe('');
  });

  it('asks on a payload with no tool name', () => {
    expect(decision(runHook(JSON.stringify({ session_id: 's' })).out)).toBe('ask');
  });

  it('is silent only in mode off', () => {
    fs.writeFileSync(path.join(tmp, 'config.json'), JSON.stringify({ defaults: { mode: 'off' } }));
    const r = runHook('{ not json');
    expect(r.code).toBe(0);
    expect(r.out).toBe('');
  });

  it('a crashed hook process exits non-zero with no allow on stdout, which onFailure "block" turns into a block', () => {
    const r = runHook('{}', 'claude', path.join(tmp, 'does-not-exist.ts'));
    expect(r.code).not.toBe(0);
    expect(r.out).toBe('');
    expect(r.out).not.toContain('allow');
  });
});
