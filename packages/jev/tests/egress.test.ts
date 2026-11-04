import { describe, it, expect, beforeEach, afterEach } from 'bun:test';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import {
  readEgressPolicy,
  checkEgress,
  egressLogFile,
  fetchWithTimeout,
  callJevDecision,
  callJevRouter,
  callJev,
  callRouterCompletion,
  callOpenRouterFree,
  selectModel,
  DEFAULT_MODEL_CANDIDATES,
  clearDecisionCache,
  EGRESS_DENIED_CODE,
  type EgressPolicy
} from '../src/shared.ts';
import { initTracingSync } from '../src/tracing.ts';

const PLUGIN_ROOT = path.resolve(import.meta.dir, '..');
const ENV_KEYS = ['HOME', 'CLAUDE_PROJECT_DIR', 'JEV_EGRESS_DENY', 'OPENROUTER_API_KEY', 'JEV_CACHE_FILE'] as const;

let tmp = '';
let savedEnv: Record<string, string | undefined> = {};
let originalFetch: typeof fetch;
let fetchCalls: string[] = [];

function writePolicy(policy: unknown): void {
  const dir = path.join(tmp, 'home', '.config', 'jev');
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, 'egress.json'), typeof policy === 'string' ? policy : JSON.stringify(policy));
}

function mkdir(...parts: string[]): string {
  const dir = path.join(tmp, ...parts);
  fs.mkdirSync(dir, { recursive: true });
  return dir;
}

const active = (allowPaths: string[] | null, denyPaths: string[] = []): EgressPolicy => ({
  state: 'active',
  allowPaths,
  denyPaths
});

beforeEach(() => {
  savedEnv = Object.fromEntries(ENV_KEYS.map((k) => [k, process.env[k]]));
  tmp = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'jev-egress-')));
  process.env.HOME = path.join(tmp, 'home');
  delete process.env.CLAUDE_PROJECT_DIR;
  delete process.env.JEV_EGRESS_DENY;
  process.env.OPENROUTER_API_KEY = 'sk-test-egress';
  process.env.JEV_CACHE_FILE = ':memory:';
  clearDecisionCache();
  fetchCalls = [];
  originalFetch = globalThis.fetch;
  globalThis.fetch = (async (url: string | URL | Request) => {
    fetchCalls.push(String(url));
    return new Response(JSON.stringify({ choices: [{ message: { content: 'ok response' } }] }), { status: 200 });
  }) as typeof fetch;
});

afterEach(() => {
  globalThis.fetch = originalFetch;
  for (const k of ENV_KEYS) {
    if (savedEnv[k] === undefined) delete process.env[k];
    else process.env[k] = savedEnv[k];
  }
  fs.rmSync(tmp, { recursive: true, force: true });
});

describe('egress policy: reading', () => {
  it('has no policy when neither egress.json nor JEV_EGRESS_DENY exists', () => {
    const policy = readEgressPolicy();

    expect(policy.state).toBe('absent');
  });

  it('treats egress.json that is not JSON as broken', () => {
    writePolicy('{ "allowPaths": [');

    const policy = readEgressPolicy();

    expect(policy.state).toBe('broken');
  });

  it('treats allowPaths of the wrong type as broken', () => {
    writePolicy({ allowPaths: 'not-a-list' });

    const policy = readEgressPolicy();

    expect(policy.state).toBe('broken');
  });

  it('activates a deny policy from JEV_EGRESS_DENY alone', () => {
    process.env.JEV_EGRESS_DENY = mkdir('private');

    const policy = readEgressPolicy();

    expect(policy.state).toBe('active');
  });
});

describe('egress policy: directory checks', () => {
  it('allows a directory inside allowPaths', () => {
    const pub = mkdir('pub');

    const decision = checkEgress([path.join(pub, 'src')], active([pub]));

    expect(decision.allowed).toBe(true);
  });

  it('blocks a sibling that only shares a name prefix with an allowed directory', () => {
    const pub = mkdir('pub');
    const sibling = mkdir('pub-other');

    const decision = checkEgress([sibling], active([pub]));

    expect(decision.allowed).toBe(false);
  });

  it('blocks a directory under denyPaths even inside allowPaths', () => {
    const root = mkdir('work');
    const priv = mkdir('work', 'private');

    const decision = checkEgress([priv], active([root], [priv]));

    expect(decision.allowed).toBe(false);
  });

  it('blocks everything when allowPaths is empty', () => {
    const decision = checkEgress([mkdir('any')], active([]));

    expect(decision.allowed).toBe(false);
  });

  it('blocks when any one of several session directories is outside allowPaths', () => {
    const pub = mkdir('pub');

    const decision = checkEgress([pub, mkdir('elsewhere')], active([pub]));

    expect(decision.allowed).toBe(false);
  });

  it('blocks when there is no session directory to check', () => {
    const decision = checkEgress([], active(null, [mkdir('private')]));

    expect(decision.allowed).toBe(false);
  });

  it('blocks a broken policy', () => {
    const decision = checkEgress([mkdir('any')], { state: 'broken', problem: 'bad' });

    expect(decision.allowed).toBe(false);
  });

  it('applies a deny entry given through a symlink to the real directory', () => {
    const real = mkdir('real');
    const link = path.join(tmp, 'link');
    fs.symlinkSync(real, link);

    const decision = checkEgress([real], active(null, [link]));

    expect(decision.allowed).toBe(false);
  });

  it('applies a deny entry on the real directory to access through a symlink', () => {
    const real = mkdir('real');
    const link = path.join(tmp, 'link');
    fs.symlinkSync(real, link);

    const decision = checkEgress([link], active(null, [real]));

    expect(decision.allowed).toBe(false);
  });

  it('keeps a deny entry for a path that does not exist', () => {
    const missing = path.join(tmp, 'not-cloned-yet');

    const decision = checkEgress([path.join(missing, 'src')], active(null, [missing]));

    expect(decision.allowed).toBe(false);
  });

  it.skipIf(process.platform !== 'darwin')('matches a deny entry regardless of letter case on macOS', () => {
    const priv = mkdir('Private');

    const decision = checkEgress([priv], active(null, [path.join(tmp, 'private')]));

    expect(decision.allowed).toBe(false);
  });

  it('lets JEV_EGRESS_DENY tighten an allowing egress.json', () => {
    const root = mkdir('work');
    writePolicy({ allowPaths: [root] });
    process.env.JEV_EGRESS_DENY = mkdir('work', 'private');

    const decision = checkEgress([path.join(root, 'private')]);

    expect(decision.allowed).toBe(false);
  });
});

describe('egress policy: fetchWithTimeout choke point', () => {
  const blockPluginRoot = () => writePolicy({ allowPaths: [mkdir('pub')] });

  it('does not call fetch when the session is blocked', async () => {
    blockPluginRoot();

    await fetchWithTimeout('https://openrouter.ai/api/v1/chat/completions', { method: 'POST', body: '{}' }, 1000).catch(() => null);

    expect(fetchCalls.length).toBe(0);
  });

  it('marks the block with the JEV_EGRESS_DENIED code', async () => {
    blockPluginRoot();

    const err = await fetchWithTimeout('https://openrouter.ai/x', { method: 'POST' }, 1000).catch((e) => e);

    expect(err.code).toBe(EGRESS_DENIED_CODE);
  });

  it('keeps directory paths out of the error message', async () => {
    blockPluginRoot();

    const err = await fetchWithTimeout('https://openrouter.ai/x', { method: 'POST' }, 1000).catch((e) => e);

    expect(String(err.message)).not.toContain(PLUGIN_ROOT);
  });

  it('lets loopback requests through under a blocking policy', async () => {
    blockPluginRoot();

    await fetchWithTimeout('http://127.0.0.1:8110/mcp', { method: 'POST' }, 1000);

    expect(fetchCalls).toEqual(['http://127.0.0.1:8110/mcp']);
  });

  it('sends the request when the session is allowed', async () => {
    writePolicy({ allowPaths: [PLUGIN_ROOT, process.cwd()] });

    await fetchWithTimeout('https://openrouter.ai/x', { method: 'POST' }, 1000);

    expect(fetchCalls.length).toBe(1);
  });

  it('sends the request when there is no policy', async () => {
    await fetchWithTimeout('https://openrouter.ai/x', { method: 'POST' }, 1000);

    expect(fetchCalls.length).toBe(1);
  });
});

describe('egress policy: log', () => {
  const body = JSON.stringify({ model: 'test/model', messages: [{ role: 'user', content: 'SECRET-PROMPT-TEXT' }] });
  const readLog = () => fs.readFileSync(egressLogFile(), 'utf8');

  it('logs a blocked request without its body', async () => {
    writePolicy({ allowPaths: [mkdir('pub')] });

    await fetchWithTimeout('https://openrouter.ai/x', { method: 'POST', body }, 1000).catch(() => null);

    expect(readLog()).not.toContain('SECRET-PROMPT-TEXT');
  });

  it('logs a sent request with its model', async () => {
    writePolicy({ allowPaths: [PLUGIN_ROOT, process.cwd()] });

    await fetchWithTimeout('https://openrouter.ai/x', { method: 'POST', body }, 1000);

    expect(JSON.parse(readLog().trim()).model).toBe('test/model');
  });

  it('writes no log when there is no policy', async () => {
    await fetchWithTimeout('https://openrouter.ai/x', { method: 'POST', body }, 1000);

    expect(fs.existsSync(egressLogFile())).toBe(false);
  });
});

describe('egress policy: every network sink is gated', () => {
  const block = () => writePolicy({ allowPaths: [mkdir('pub')] });
  const questions = { verdict: { type: 'noul', instructions: 'x', criteria: { a: 'b' } } };

  it('callJevDecision sends nothing', async () => {
    block();

    await callJevDecision('state text', questions).catch(() => null);

    expect(fetchCalls.length).toBe(0);
  });

  it('callJevRouter sends nothing', async () => {
    block();

    await callJevRouter('state text', questions).catch(() => null);

    expect(fetchCalls.length).toBe(0);
  });

  it('callJev sends nothing through either leg', async () => {
    block();

    await callJev('state text', questions).catch(() => null);

    expect(fetchCalls.length).toBe(0);
  });

  it('callRouterCompletion sends nothing', async () => {
    block();

    await callRouterCompletion('prompt text').catch(() => null);

    expect(fetchCalls.length).toBe(0);
  });

  it('callOpenRouterFree sends nothing', async () => {
    block();

    await callOpenRouterFree('prompt text').catch(() => null);

    expect(fetchCalls.length).toBe(0);
  });

  it('callOpenRouterFree with denied cwd sends nothing', async () => {
    writePolicy({ allowPaths: [PLUGIN_ROOT] });
    const deniedDir = mkdir('private-sub');

    await callOpenRouterFree('prompt text', undefined, undefined, deniedDir).catch(() => null);

    expect(fetchCalls.length).toBe(0);
  });

  it('selectModel (jev_select_model) sends nothing', async () => {
    block();

    await selectModel('a task long enough to route', DEFAULT_MODEL_CANDIDATES).catch(() => null);

    expect(fetchCalls.length).toBe(0);
  });
});

describe('egress policy: session directories', () => {
  it('checks the working directory supplied by the harness', async () => {
    const pub = mkdir('pub');
    writePolicy({ allowPaths: [pub, PLUGIN_ROOT] });
    const { checkEgress: check, egressContextDirs } = await import('../src/shared.ts');

    const decision = check(egressContextDirs([mkdir('private')]));

    expect(decision.allowed).toBe(false);
  });
});

describe('egress policy: OTLP span export', () => {
  const emitSpan = (endpoint: string): void => {
    initTracingSync({ enabled: true, endpoint }).startSpan('egress-test').end();
    initTracingSync({ enabled: false });
  };

  it('drops a span bound for a remote collector when the policy blocks', () => {
    writePolicy({ allowPaths: [mkdir('elsewhere')] });

    emitSpan('https://otel.example.com');

    expect(fetchCalls).toEqual([]);
  });

  it('logs the dropped span as blocked', () => {
    writePolicy({ allowPaths: [mkdir('elsewhere')] });

    emitSpan('https://otel.example.com');

    expect(fs.readFileSync(egressLogFile(), 'utf8')).toContain('"decision":"blocked"');
  });

  it('still exports to a loopback collector when the policy blocks', () => {
    writePolicy({ allowPaths: [mkdir('elsewhere')] });

    emitSpan('http://127.0.0.1:4318');

    expect(fetchCalls).toEqual(['http://127.0.0.1:4318/v1/traces']);
  });

  it('exports to a remote collector when there is no policy', () => {
    emitSpan('https://otel.example.com');

    expect(fetchCalls).toEqual(['https://otel.example.com/v1/traces']);
  });
});

describe('egress policy: no bypass of the choke point', () => {
  it('checks the policy in every file that calls fetch directly', () => {
    const allowed = new Set(['src/core/client.ts', 'src/tracing.ts']);
    const unchecked = [...allowed].filter(
      (rel) => !/assertEgressAllowed\(/.test(fs.readFileSync(path.join(PLUGIN_ROOT, rel), 'utf8'))
    );

    expect(unchecked).toEqual([]);
  });

  it('calls fetch directly only from fetchWithTimeout and tracing', () => {
    const allowed = new Set(['src/core/client.ts', 'src/tracing.ts']);
    const offenders: string[] = [];
    for (const dir of ['src']) {
      for (const file of fs.readdirSync(path.join(PLUGIN_ROOT, dir), { recursive: true }) as string[]) {
        if (!file.endsWith('.ts')) continue;
        const rel = path.join(dir, file);
        const text = fs.readFileSync(path.join(PLUGIN_ROOT, rel), 'utf8');
        const direct = (text.match(/(?<![\w.])fetch\(/g) || []).length;
        if (direct > 0 && !(allowed.has(rel) && direct === 1)) offenders.push(`${rel} (${direct})`);
      }
    }

    expect(offenders).toEqual([]);
  });
});
