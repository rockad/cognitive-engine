import { describe, it, expect, beforeEach, afterEach } from 'bun:test';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { validateJevConfig, inspectJevConfigFile, clearConfigCacheForTesting, DEFAULT_JEV_CONFIG } from '../src/core/config.js';
import { handleGetModeTool } from '../src/tools/mode.js';

/**
 * A Jev config that cannot be trusted must fail loudly on every guarded call, while an explicit
 * `off` stays silent. The hook is run as a real process, like Claude Code runs it.
 */
const HOOK = path.join(import.meta.dir, '..', 'src', 'hook.ts');
let tmp = '';
let cfgFile = '';

beforeEach(() => {
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-malformed-'));
  cfgFile = path.join(tmp, 'config.json');
});
afterEach(() => fs.rmSync(tmp, { recursive: true, force: true }));

function runHook(stdin: unknown, arg = 'claude', opts: { unsetConfigEnv?: boolean } = {}) {
  const env: Record<string, string | undefined> = {
    ...process.env,
    HOME: tmp,
    JEV_CONFIG_FILE: cfgFile,
    JEV_CLAUDE_STATE_DIR: path.join(tmp, 'state'),
    JEV_CLAUDE_DECISIONS_FILE: path.join(tmp, 'decisions.jsonl')
  };
  if (opts.unsetConfigEnv) delete env.JEV_CONFIG_FILE;
  const r = Bun.spawnSync(['bun', HOOK, arg], {
    stdin: new TextEncoder().encode(typeof stdin === 'string' ? stdin : JSON.stringify(stdin)),
    env: env as Record<string, string>
  });
  return { code: r.exitCode, out: r.stdout.toString(), err: r.stderr.toString() };
}

const plainAgent = { tool_name: 'Agent', session_id: 's1', tool_input: { subagent_type: 'x', prompt: 'p' } };
const opusAgent = { tool_name: 'Agent', session_id: 's1', tool_input: { model: 'opus', prompt: 'p' } };
const workflow = { tool_name: 'Workflow', session_id: 's1', cwd: os.tmpdir(), tool_input: { script: 'agent({ prompt: "x" })' } };
const reasonOf = (out: string) => JSON.parse(out).hookSpecificOutput.permissionDecisionReason as string;
const decisionOf = (out: string) => JSON.parse(out).hookSpecificOutput.permissionDecision as string;
const audit = () => {
  const f = path.join(tmp, 'decisions.jsonl');
  return fs.existsSync(f) ? fs.readFileSync(f, 'utf8').trim().split('\n').filter(Boolean).map((l) => JSON.parse(l)) : [];
};

describe('validateJevConfig: classification', () => {
  const valid: Array<[string, unknown]> = [
    ['empty object', {}],
    ['no mode anywhere', { version: 2, timeouts: { decisionMs: 100 } }],
    ['defaults.mode on', { defaults: { mode: 'on' } }],
    ['defaults.mode on-demand', { defaults: { mode: 'on-demand' } }],
    ['defaults.mode off', { defaults: { mode: 'off' } }],
    ['per-harness mode', { harnesses: { 'claude-code': { mode: 'on' }, cli: { mode: 'off' } } }],
    ['unrelated extra keys', { defaults: { mode: 'on', futureKey: 1 }, somethingElse: { a: [1] }, harnesses: { other: { mode: 'banana' } } }],
    ['the shipped default config', DEFAULT_JEV_CONFIG]
  ];
  for (const [name, cfg] of valid) {
    it(`accepts ${name}`, () => expect(validateJevConfig(cfg)).toEqual({ ok: true, problems: [] }));
  }

  const bad: Array<[string, unknown, string]> = [
    ['an array', [], 'must be a JSON object, found an array'],
    ['null', null, 'found null'],
    ['a string', 'on', 'found a string'],
    ['stray top-level mode', { mode: 'on' }, "top-level 'mode' is ignored; use defaults.mode"],
    ['unknown defaults.mode', { defaults: { mode: 'banana' } }, 'defaults.mode must be one of off, on-demand, on, found "banana"'],
    ['numeric defaults.mode', { defaults: { mode: 1 } }, 'defaults.mode must be one of off, on-demand, on, found a number'],
    ['null defaults.mode', { defaults: { mode: null } }, 'defaults.mode must be one of off, on-demand, on, found null'],
    ['empty-string mode', { defaults: { mode: '' } }, 'defaults.mode must be one of'],
    ['wrong-case mode', { defaults: { mode: 'ON' } }, 'found "ON"'],
    ['defaults not an object', { defaults: 'on' }, 'defaults must be an object'],
    ['harnesses not an object', { harnesses: [] }, 'harnesses must be an object'],
    ['harness entry not an object', { harnesses: { 'claude-code': 'on' } }, 'harnesses.claude-code must be an object'],
    ['harness mode unknown', { harnesses: { opencode: { mode: 'sometimes' } } }, 'harnesses.opencode.mode must be one of'],
    ['harness models wrong type', { harnesses: { cli: { models: { free: 3 } } } }, 'harnesses.cli.models.free must be a string'],
    ['harness freePool wrong type', { harnesses: { cli: { freePool: 'x' } } }, 'harnesses.cli.freePool must be an array of strings'],
    ['harness freePool item wrong type', { harnesses: { cli: { freePool: ['x', 2] } } }, 'array with non-string items'],
    ['requireUpgradeApproval wrong type', { harnesses: { cli: { requireUpgradeApproval: 'yes' } } }, 'requireUpgradeApproval must be a boolean'],
    ['defaults.availableSubagents wrong type', { defaults: { availableSubagents: 'self' } }, 'defaults.availableSubagents must be an array of strings'],
    ['version wrong type', { version: '2' }, 'version must be a number'],
    ['timeouts wrong type', { timeouts: { decisionMs: '3500' } }, 'timeouts.decisionMs must be a number'],
    ['cache wrong type', { cache: { ttlSec: 'x' } }, 'cache.ttlSec must be a number'],
    ['routing.strategy unknown', { routing: { strategy: 'dice' } }, 'routing.strategy must be'],
    ['models wrong type', { models: { router: 1 } }, 'models.router must be a string'],
    ['tiers entry wrong type', { tiers: { free: 'cheap' } }, 'tiers.free must be an object']
  ];
  for (const [name, cfg, fragment] of bad) {
    it(`rejects ${name}`, () => {
      const v = validateJevConfig(cfg);
      expect(v.ok).toBe(false);
      expect(v.problems.join('\n')).toContain(fragment);
    });
  }

  it('reports several problems at once', () => {
    const v = validateJevConfig({ mode: 'on', defaults: { mode: 'banana' } });
    expect(v.problems).toHaveLength(2);
  });
});

describe('inspectJevConfigFile', () => {
  it('missing', () => expect(inspectJevConfigFile(cfgFile)).toEqual({ state: 'missing', file: cfgFile, problems: [] }));
  it('valid', () => {
    fs.writeFileSync(cfgFile, '{"defaults":{"mode":"off"}}');
    expect(inspectJevConfigFile(cfgFile).state).toBe('valid');
  });
  it('invalid JSON', () => {
    fs.writeFileSync(cfgFile, '{ nope');
    expect(inspectJevConfigFile(cfgFile)).toEqual({ state: 'malformed', file: cfgFile, problems: ['the file is not valid JSON'] });
  });
  it('unreadable (a directory in place of the file)', () => {
    fs.mkdirSync(cfgFile);
    const r = inspectJevConfigFile(cfgFile);
    expect(r.state).toBe('malformed');
    expect(r.problems[0]).toContain('could not be read');
  });
});

describe('guard hook: off is silent, healthy configs unchanged', () => {
  it('explicit off: silent even for an obvious Opus escalation, on both tools and both harnesses', () => {
    fs.writeFileSync(cfgFile, JSON.stringify({ defaults: { mode: 'off' } }));
    for (const input of [opusAgent, plainAgent, workflow]) {
      const r = runHook(input);
      expect([r.code, r.out, r.err]).toEqual([0, '', '']);
    }
    expect(runHook({ toolCall: { name: 'invoke_subagent', args: { Subagents: [{ Model: 'pro' }] } } }, 'antigravity').out).toBe('');
    expect(audit()).toEqual([]);
  });

  for (const mode of ['on', 'on-demand']) {
    it(`valid ${mode}: guard active (Opus asks, a plain call passes), not the malformed reason`, () => {
      fs.writeFileSync(cfgFile, JSON.stringify({ defaults: { mode } }));
      const ask = runHook(opusAgent);
      expect(ask.code).toBe(0);
      expect(decisionOf(ask.out)).toBe('ask');
      expect(reasonOf(ask.out)).not.toContain('malformed');
      expect(ask.err).toBe('');
      expect(runHook(plainAgent).out).toBe('');
    });
  }

  it('per-harness off wins over defaults on for that harness', () => {
    fs.writeFileSync(cfgFile, JSON.stringify({ defaults: { mode: 'on' }, harnesses: { 'claude-code': { mode: 'off' } } }));
    expect(runHook(opusAgent).out).toBe('');
  });

  it('no JEV_CONFIG_FILE and no file at the default path keeps the loader default (off): silent', () => {
    const r = runHook(opusAgent, 'claude', { unsetConfigEnv: true });
    expect([r.code, r.out, r.err]).toEqual([0, '', '']);
  });

  it('{} is unset, takes the documented default (off): silent', () => {
    fs.writeFileSync(cfgFile, '{}');
    expect(runHook(opusAgent).out).toBe('');
  });

  it('a config with no mode anywhere is unset, not malformed', () => {
    fs.writeFileSync(cfgFile, JSON.stringify({ version: 2, timeouts: { decisionMs: 100 } }));
    expect(runHook(opusAgent).out).toBe('');
  });
});

describe('guard hook: a malformed config fails loudly on every guarded call', () => {
  const variants: Array<[string, () => void, string]> = [
    ['invalid JSON', () => fs.writeFileSync(cfgFile, '{ broken'), 'not valid JSON'],
    ['an array', () => fs.writeFileSync(cfgFile, '[]'), 'must be a JSON object'],
    ['a stray top-level mode', () => fs.writeFileSync(cfgFile, '{"mode":"on"}'), "top-level 'mode' is ignored; use defaults.mode"],
    ['an unknown mode', () => fs.writeFileSync(cfgFile, '{"defaults":{"mode":"banana"}}'), 'defaults.mode must be one of off, on-demand, on'],
    ['a numeric mode', () => fs.writeFileSync(cfgFile, '{"defaults":{"mode":1}}'), 'defaults.mode must be one of'],
    ['a wrong-typed per-harness key', () => fs.writeFileSync(cfgFile, '{"harnesses":{"claude-code":{"models":"sonnet"}}}'), 'harnesses.claude-code.models must be an object'],
    ['a wrong-typed per-harness mode', () => fs.writeFileSync(cfgFile, '{"harnesses":{"claude-code":{"mode":true}}}'), 'harnesses.claude-code.mode must be one of'],
    ['an unreadable file', () => fs.mkdirSync(cfgFile), 'could not be read']
  ];

  for (const [name, setup, problem] of variants) {
    it(`${name}: asks on a plain Agent call and a Workflow call, with path, problem, fix, stderr and audit`, () => {
      setup();
      for (const input of [plainAgent, { ...plainAgent, tool_name: 'Task' }, workflow]) {
        const r = runHook(input);
        expect(r.code).toBe(0);
        expect(decisionOf(r.out)).toBe('ask');
        const reason = reasonOf(r.out);
        expect(reason).toContain(cfgFile);
        expect(reason).toContain(problem);
        expect(reason).toContain('/jev status');
        expect(reason).toContain('{"defaults":{"mode":"on-demand"}}');
        expect(reason).toContain('/jev off');
        expect(r.err).toContain(cfgFile);
        expect(r.err.trim().split('\n')).toHaveLength(1);
      }
      const events = audit().filter((e) => e.event === 'config_malformed');
      expect(events).toHaveLength(3);
      expect(events[0]).toMatchObject({ event: 'config_malformed', file: cfgFile, tool_name: 'Agent', session_id: 's1' });
      expect(events[0].problems.join('\n')).toContain(problem);
      expect(audit().some((e) => e.event === 'ask')).toBe(false);
    });
  }

  it('the stray top-level mode gets its own hint in the reason', () => {
    fs.writeFileSync(cfgFile, '{"mode":"on"}');
    expect(reasonOf(runHook(plainAgent).out)).toContain('move the top-level "mode" under "defaults"');
  });

  it('does not fall back to off: an obvious Opus request asks with the malformed reason too', () => {
    fs.writeFileSync(cfgFile, '{"mode":"on"}');
    const r = runHook(opusAgent);
    expect(decisionOf(r.out)).toBe('ask');
    expect(reasonOf(r.out)).toContain('malformed');
  });

  it('never records file contents in the audit', () => {
    fs.writeFileSync(cfgFile, JSON.stringify({ defaults: { mode: 'on' }, secretish: 'SUPERSECRET', harnesses: { cli: { models: 'SUPERSECRET' } } }));
    runHook(plainAgent);
    expect(fs.readFileSync(path.join(tmp, 'decisions.jsonl'), 'utf8')).not.toContain('SUPERSECRET');
  });

  it('antigravity: asks with force_ask on a subagent call, exit 0', () => {
    fs.writeFileSync(cfgFile, '{"mode":"on"}');
    const r = runHook({ toolCall: { name: 'invoke_subagent', args: { Subagents: [{ Model: 'flash' }] } } }, 'antigravity');
    expect(r.code).toBe(0);
    expect(JSON.parse(r.out).decision).toBe('force_ask');
    expect(JSON.parse(r.out).reason).toContain(cfgFile);
    expect(r.err).toContain(cfgFile);
  });

  it('asks even when the hook input itself is unreadable, and stays valid JSON', () => {
    fs.writeFileSync(cfgFile, '{"mode":"on"}');
    const r = runHook('{ not json');
    expect(r.code).toBe(0);
    expect(reasonOf(r.out)).toContain('malformed');
  });

  it('leaves tools the hook is not registered for alone', () => {
    fs.writeFileSync(cfgFile, '{"mode":"on"}');
    expect(runHook({ tool_name: 'Bash', tool_input: { command: 'ls' } }).out).toBe('');
  });

  it('is fixed by setting off explicitly', () => {
    fs.writeFileSync(cfgFile, '{"mode":"on"}');
    expect(runHook(plainAgent).out).not.toBe('');
    fs.writeFileSync(cfgFile, '{"defaults":{"mode":"off"}}');
    expect(runHook(plainAgent).out).toBe('');
  });
});

describe('jev_get_mode surfaces the same validator', () => {
  let saved: string | undefined;
  beforeEach(() => {
    saved = process.env.JEV_CONFIG_FILE;
    process.env.JEV_CONFIG_FILE = cfgFile;
    clearConfigCacheForTesting();
  });
  afterEach(() => {
    if (saved === undefined) delete process.env.JEV_CONFIG_FILE;
    else process.env.JEV_CONFIG_FILE = saved;
    clearConfigCacheForTesting();
  });
  const result = async () => JSON.parse((await handleGetModeTool()).content[0].text);

  it('adds a configWarning for a malformed config', async () => {
    fs.writeFileSync(cfgFile, '{"mode":"on"}');
    const r = await result();
    expect(r.configWarning.file).toBe(cfgFile);
    expect(r.configWarning.problems).toEqual(["top-level 'mode' is ignored; use defaults.mode"]);
  });

  it('warns when JEV_CONFIG_FILE names a missing file', async () => {
    const r = await result();
    expect(r.configWarning.problems).toEqual(['JEV_CONFIG_FILE points at a file that does not exist']);
  });

  it('adds nothing for a valid config', async () => {
    fs.writeFileSync(cfgFile, '{"defaults":{"mode":"on"}}');
    clearConfigCacheForTesting();
    expect((await result()).configWarning).toBeUndefined();
  });
});

describe('inspectJevConfigFile: file-level hazards', () => {
  const MB = 1024 * 1024;
  const padded = (bytes: number) => {
    const head = '{"defaults":{"mode":"off"},"pad":"';
    const tail = '"}';
    return head + 'x'.repeat(bytes - head.length - tail.length) + tail;
  };
  let savedEnv: string | undefined;
  beforeEach(() => {
    savedEnv = process.env.JEV_CONFIG_FILE;
    delete process.env.JEV_CONFIG_FILE;
  });
  afterEach(() => {
    if (savedEnv === undefined) delete process.env.JEV_CONFIG_FILE;
    else process.env.JEV_CONFIG_FILE = savedEnv;
  });

  it('exactly 1 MB is valid', () => {
    fs.writeFileSync(cfgFile, padded(MB));
    expect(fs.statSync(cfgFile).size).toBe(MB);
    expect(inspectJevConfigFile(cfgFile).state).toBe('valid');
  });

  it('1 MB + 1 byte is malformed, naming the limit', () => {
    fs.writeFileSync(cfgFile, padded(MB + 1));
    const r = inspectJevConfigFile(cfgFile);
    expect(r.state).toBe('malformed');
    expect(r.problems).toEqual(['config is over the 1 MB limit']);
  });

  it('re-checks the size on the bytes read, not only on stat', () => {
    fs.writeFileSync(cfgFile, '{}');
    const big = Buffer.alloc(MB + 1, 0x20);
    const r = inspectJevConfigFile(cfgFile, {
      lstat: fs.lstatSync,
      stat: (f: string) => ({ ...fs.statSync(f), size: 2, isFile: () => true }) as fs.Stats,
      readFile: () => big
    });
    expect(r.state).toBe('malformed');
    expect(r.problems).toEqual(['config is over the 1 MB limit']);
  });

  it('a symlink to a valid file is valid', () => {
    const target = path.join(tmp, 'real.json');
    fs.writeFileSync(target, '{"defaults":{"mode":"on"}}');
    fs.symlinkSync(target, cfgFile);
    expect(inspectJevConfigFile(cfgFile).state).toBe('valid');
  });

  it('a dangling symlink is malformed ("broken symlink"), not missing', () => {
    fs.symlinkSync(path.join(tmp, 'nowhere.json'), cfgFile);
    const r = inspectJevConfigFile(cfgFile);
    expect(r.state).toBe('malformed');
    expect(r.problems).toEqual(['broken symlink: the target does not exist or cannot be read']);
  });

  it('a symlink loop is malformed', () => {
    fs.symlinkSync(cfgFile, cfgFile);
    expect(inspectJevConfigFile(cfgFile).problems[0]).toContain('broken symlink');
  });

  it('no entry at all stays missing', () => {
    expect(inspectJevConfigFile(cfgFile)).toEqual({ state: 'missing', file: cfgFile, problems: [] });
  });

  it('JEV_CONFIG_FILE set to a missing path is malformed', () => {
    process.env.JEV_CONFIG_FILE = cfgFile;
    const r = inspectJevConfigFile();
    expect(r.state).toBe('malformed');
    expect(r.file).toBe(cfgFile);
    expect(r.problems[0]).toContain('JEV_CONFIG_FILE points at a file that does not exist');
  });

  it('JEV_CONFIG_FILE set to an existing valid file is valid', () => {
    fs.writeFileSync(cfgFile, '{}');
    process.env.JEV_CONFIG_FILE = cfgFile;
    expect(inspectJevConfigFile().state).toBe('valid');
  });
});

describe('guard hook: file-level hazards ask loudly', () => {
  it('an over-limit config asks, naming the file and the limit', () => {
    fs.writeFileSync(cfgFile, '{"defaults":{"mode":"off"},"pad":"' + 'x'.repeat(1024 * 1024) + '"}');
    const r = runHook(plainAgent);
    expect(decisionOf(r.out)).toBe('ask');
    expect(reasonOf(r.out)).toContain(cfgFile);
    expect(reasonOf(r.out)).toContain('config is over the 1 MB limit');
    expect(audit().filter((e) => e.event === 'config_malformed')).toHaveLength(1);
  });

  it('a dangling symlink asks instead of reading as off', () => {
    fs.symlinkSync(path.join(tmp, 'nowhere.json'), cfgFile);
    const r = runHook(plainAgent);
    expect(decisionOf(r.out)).toBe('ask');
    expect(reasonOf(r.out)).toContain('broken symlink');
    expect(r.err).toContain(cfgFile);
  });

  it('a symlink to a valid off config is silent', () => {
    const target = path.join(tmp, 'real.json');
    fs.writeFileSync(target, '{"defaults":{"mode":"off"}}');
    fs.symlinkSync(target, cfgFile);
    expect(runHook(opusAgent).out).toBe('');
  });

  it('JEV_CONFIG_FILE naming a missing file asks (explicitly pointing at nothing is a mistake)', () => {
    const r = runHook(plainAgent);
    expect(decisionOf(r.out)).toBe('ask');
    expect(reasonOf(r.out)).toContain('JEV_CONFIG_FILE points at a file that does not exist');
    expect(reasonOf(r.out)).toContain(cfgFile);
  });
});
