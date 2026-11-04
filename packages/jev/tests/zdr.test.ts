import { describe, it, expect, beforeEach, afterEach } from 'bun:test';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import {
  readZdrPolicy,
  parseZdrValue,
  callJev,
  callJevDecision,
  callJevRouter,
  callRouterCompletion,
  callOpenRouterFree,
  EGRESS_ZDR_UNAVAILABLE_CODE,
  clearDecisionCache,
  egressLogFile,
  assertEgressAllowed
} from '../src/shared.ts';

const ENV_KEYS = [
  'HOME',
  'JEV_ZDR',
  'JEV_CONFIG_FILE',
  'OPENROUTER_API_KEY',
  'JEV_CACHE_FILE',
  'CLAUDE_PROJECT_DIR',
  'JEV_HARNESS',
  'ANTIGRAVITY_CLI',
  'GEMINI_CLI',
  'CLAUDE_CODE',
  'CLAUDECODE',
  'OPENCODE_SESSION',
  'OPENCODE_HOME',
  'CORTEX_SESSION'
] as const;

let tmp = '';
let savedEnv: Record<string, string | undefined> = {};
let originalFetch: typeof fetch;
let fetchCalls: Array<{ url: string; body: any }> = [];

function writeEgress(content: Record<string, unknown>): void {
  const dir = path.join(process.env.HOME!, '.config', 'jev');
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, 'egress.json'), JSON.stringify(content));
}

function writeConfig(content: Record<string, unknown>): void {
  const dir = path.join(process.env.HOME!, '.config', 'jev');
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, 'config.json'), JSON.stringify(content));
}

beforeEach(() => {
  savedEnv = Object.fromEntries(ENV_KEYS.map((k) => [k, process.env[k]]));
  tmp = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'jev-zdr-')));
  process.env.HOME = path.join(tmp, 'home');
  delete process.env.JEV_ZDR;
  delete process.env.JEV_CONFIG_FILE;
  delete process.env.CLAUDE_PROJECT_DIR;
  for (const h of [
    'JEV_HARNESS',
    'ANTIGRAVITY_CLI',
    'GEMINI_CLI',
    'CLAUDE_CODE',
    'CLAUDECODE',
    'OPENCODE_SESSION',
    'OPENCODE_HOME',
    'CORTEX_SESSION'
  ]) {
    delete process.env[h];
  }
  process.env.OPENROUTER_API_KEY = 'sk-test-zdr';
  process.env.JEV_CACHE_FILE = ':memory:';
  clearDecisionCache();
  fetchCalls = [];
  originalFetch = globalThis.fetch;
  globalThis.fetch = (async (url: string | URL | Request, init?: RequestInit) => {
    let bodyObj: any = null;
    if (init?.body && typeof init.body === 'string') {
      try {
        bodyObj = JSON.parse(init.body);
      } catch {
        bodyObj = init.body;
      }
    }
    fetchCalls.push({ url: String(url), body: bodyObj });
    return new Response(
      JSON.stringify({
        choices: [{ message: { content: '{"answers":{"test":{"choice":"a"}}}' } }],
        usage: { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 }
      }),
      { status: 200, headers: { 'Content-Type': 'application/json' } }
    );
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

describe('parseZdrValue normalization & fail-closed', () => {
  it('returns null for undefined, null, or empty string', () => {
    expect(parseZdrValue(undefined)).toBeNull();
    expect(parseZdrValue(null)).toBeNull();
    expect(parseZdrValue('')).toBeNull();
    expect(parseZdrValue('   ')).toBeNull();
  });

  it('normalizes boolean values', () => {
    expect(parseZdrValue(true)).toBe('on');
    expect(parseZdrValue(false)).toBe('off');
  });

  it('normalizes affirmative strings to "on" case-insensitively', () => {
    expect(parseZdrValue('on')).toBe('on');
    expect(parseZdrValue('ON')).toBe('on');
    expect(parseZdrValue('On')).toBe('on');
    expect(parseZdrValue('true')).toBe('on');
    expect(parseZdrValue('TRUE')).toBe('on');
    expect(parseZdrValue('yes')).toBe('on');
    expect(parseZdrValue('YES')).toBe('on');
    expect(parseZdrValue('1')).toBe('on');
  });

  it('normalizes negative strings to "off" case-insensitively', () => {
    expect(parseZdrValue('off')).toBe('off');
    expect(parseZdrValue('OFF')).toBe('off');
    expect(parseZdrValue('Off')).toBe('off');
    expect(parseZdrValue('false')).toBe('off');
    expect(parseZdrValue('FALSE')).toBe('off');
    expect(parseZdrValue('no')).toBe('off');
    expect(parseZdrValue('NO')).toBe('off');
    expect(parseZdrValue('0')).toBe('off');
  });

  it('normalizes "strict" case-insensitively', () => {
    expect(parseZdrValue('strict')).toBe('strict');
    expect(parseZdrValue('STRICT')).toBe('strict');
    expect(parseZdrValue('Strict')).toBe('strict');
  });

  it('fails closed to "strict" on typos and unrecognized non-empty strings', () => {
    expect(parseZdrValue('strcit')).toBe('strict');
    expect(parseZdrValue('onn')).toBe('strict');
    expect(parseZdrValue('enabled')).toBe('strict');
    expect(parseZdrValue('unknown')).toBe('strict');
  });
});

describe('readZdrPolicy monotonic tightening', () => {
  it('defaults to "off" when no configs and no env vars exist', () => {
    expect(readZdrPolicy()).toBe('off');
  });

  it('reads JEV_ZDR when no config files exist', () => {
    process.env.JEV_ZDR = 'on';
    expect(readZdrPolicy()).toBe('on');

    process.env.JEV_ZDR = '1';
    expect(readZdrPolicy()).toBe('on');

    process.env.JEV_ZDR = 'true';
    expect(readZdrPolicy()).toBe('on');

    process.env.JEV_ZDR = 'strict';
    expect(readZdrPolicy()).toBe('strict');

    process.env.JEV_ZDR = 'STRICT';
    expect(readZdrPolicy()).toBe('strict');

    process.env.JEV_ZDR = 'off';
    expect(readZdrPolicy()).toBe('off');

    process.env.JEV_ZDR = '0';
    expect(readZdrPolicy()).toBe('off');

    process.env.JEV_ZDR = 'false';
    expect(readZdrPolicy()).toBe('off');
  });

  it('monotonic tightening: JEV_ZDR=strict tightens config.json with privacy.zdr: "off"', () => {
    writeConfig({ version: 2, privacy: { zdr: 'off' } });
    process.env.JEV_ZDR = 'strict';

    expect(readZdrPolicy()).toBe('strict');
  });

  it('monotonic tightening: config.json with privacy.zdr: "strict" tightens egress.json with zdr: "off"', () => {
    writeEgress({ zdr: 'off' });
    writeConfig({ version: 2, privacy: { zdr: 'strict' } });
    process.env.JEV_ZDR = 'off';

    expect(readZdrPolicy()).toBe('strict');
  });

  it('monotonic tightening: egress.json with zdr: "on" tightens config.json with privacy.zdr: "off"', () => {
    writeEgress({ zdr: 'on' });
    writeConfig({ version: 2, privacy: { zdr: 'off' } });
    process.env.JEV_ZDR = 'off';

    expect(readZdrPolicy()).toBe('on');
  });

  it('supports boolean true/false in config.json and egress.json', () => {
    writeConfig({ version: 2, privacy: { zdr: true } });
    expect(readZdrPolicy()).toBe('on');

    writeConfig({ version: 2, privacy: { zdr: false } });
    expect(readZdrPolicy()).toBe('off');

    writeEgress({ zdr: true });
    expect(readZdrPolicy()).toBe('on');
  });
});

describe('Client Payload Integration for ZDR', () => {
  const questions = { test: { type: 'choice', instructions: 'choose', criteria: { a: 'A' } } };

  it('callJevDecision injects provider.zdr and data_collection when ZDR is on', async () => {
    process.env.JEV_ZDR = 'on';

    await callJevDecision('test state', questions);

    expect(fetchCalls.length).toBe(1);
    expect(fetchCalls[0].body.provider).toEqual({ zdr: true, data_collection: 'deny' });
  });

  it('callJevDecision injects provider.zdr when ZDR is strict', async () => {
    process.env.JEV_ZDR = 'strict';

    await callJevDecision('test state', questions);

    expect(fetchCalls.length).toBe(1);
    expect(fetchCalls[0].body.provider).toEqual({ zdr: true, data_collection: 'deny' });
  });

  it('callJevDecision omits provider when ZDR is off', async () => {
    process.env.JEV_ZDR = 'off';

    await callJevDecision('test state', questions);

    expect(fetchCalls.length).toBe(1);
    expect(fetchCalls[0].body.provider).toBeUndefined();
  });

  it('callJevRouter injects provider.zdr when ZDR is on', async () => {
    process.env.JEV_ZDR = 'on';

    await callJevRouter('test state', questions);

    expect(fetchCalls.length).toBe(1);
    expect(fetchCalls[0].body.provider).toEqual({ zdr: true, data_collection: 'deny' });
  });

  it('callRouterCompletion injects provider.zdr when ZDR is on', async () => {
    process.env.JEV_ZDR = 'on';

    await callRouterCompletion('test prompt');

    expect(fetchCalls.length).toBe(1);
    expect(fetchCalls[0].body.provider).toEqual({ zdr: true, data_collection: 'deny' });
  });
});

describe('callOpenRouterFree ZDR gating', () => {
  it('throws JEV_ZDR_UNAVAILABLE before fetch when ZDR is strict', async () => {
    process.env.JEV_ZDR = 'strict';

    let thrown: any = null;
    try {
      await callOpenRouterFree('test free prompt');
    } catch (err: any) {
      thrown = err;
    }

    expect(thrown).not.toBeNull();
    expect(thrown.code).toBe(EGRESS_ZDR_UNAVAILABLE_CODE);
    expect(thrown.message).toContain('Zero Data Retention (ZDR)');
    expect(fetchCalls.length).toBe(0);
  });

  it('executes successfully and omits provider.zdr when ZDR is on', async () => {
    process.env.JEV_ZDR = 'on';

    const result = await callOpenRouterFree('test free prompt');

    expect(fetchCalls.length).toBe(1);
    expect(fetchCalls[0].body.provider?.zdr).toBeUndefined();
    expect(result.content).toBeDefined();
  });
});

describe('Upstream 404 Guardrail Rejections', () => {
  const questions = { test: { type: 'choice', instructions: 'choose', criteria: { a: 'A' } } };

  it('callJevDecision wraps 404 guardrail restrictions as JEV_ZDR_UNAVAILABLE', async () => {
    globalThis.fetch = (async () => {
      return new Response(
        JSON.stringify({ error: { message: 'Provider rejected request due to guardrail restrictions' } }),
        { status: 404 }
      );
    }) as typeof fetch;

    let thrown: any = null;
    try {
      await callJevDecision('test state', questions);
    } catch (err: any) {
      thrown = err;
    }

    expect(thrown).not.toBeNull();
    expect(thrown.code).toBe(EGRESS_ZDR_UNAVAILABLE_CODE);
  });

  it('callJevRouter wraps 404 data policy error as JEV_ZDR_UNAVAILABLE', async () => {
    globalThis.fetch = (async () => {
      return new Response(
        JSON.stringify({ error: { message: 'No router pool model is routable with current data policy' } }),
        { status: 404 }
      );
    }) as typeof fetch;

    let thrown: any = null;
    try {
      await callJevRouter('test state', questions);
    } catch (err: any) {
      thrown = err;
    }

    expect(thrown).not.toBeNull();
    expect(thrown.code).toBe(EGRESS_ZDR_UNAVAILABLE_CODE);
  });

  it('callJev rethrows immediately when signal is aborted without attempting router fallback', async () => {
    const controller = new AbortController();
    controller.abort();

    let routerCalled = false;
    globalThis.fetch = (async (url: string | URL | Request) => {
      const urlStr = String(url);
      if (urlStr.includes('chat/completions')) {
        routerCalled = true;
      }
      const err = new Error('The operation was aborted');
      (err as any).name = 'AbortError';
      throw err;
    }) as typeof fetch;

    let thrown: any = null;
    try {
      await callJev('test state', questions, controller.signal);
    } catch (err: any) {
      thrown = err;
    }

    expect(thrown).not.toBeNull();
    expect(routerCalled).toBe(false);
  });
});

describe('Audit Logging for ZDR', () => {
  it('records zdr_policy and zdr_applied in egress log with active egress policy', () => {
    writeEgress({ allowPaths: [process.cwd()], zdr: 'on' });

    assertEgressAllowed(
      'https://openrouter.ai/api/v1/chat/completions',
      JSON.stringify({ model: 'test/model', provider: { zdr: true, data_collection: 'deny' } })
    );

    const logFile = egressLogFile();
    expect(fs.existsSync(logFile)).toBe(true);
    const content = fs.readFileSync(logFile, 'utf8').trim();
    const entry = JSON.parse(content);
    expect(entry.zdr_policy).toBe('on');
    expect(entry.zdr_applied).toBe(true);
    expect(entry.decision).toBe('sent');
  });

  it('records egress log when ZDR is active even if egress policy is absent', () => {
    process.env.JEV_ZDR = 'on';

    assertEgressAllowed(
      'https://openrouter.ai/api/v1/chat/completions',
      JSON.stringify({ model: 'test/model', provider: { zdr: true, data_collection: 'deny' } })
    );

    const logFile = egressLogFile();
    expect(fs.existsSync(logFile)).toBe(true);
    const content = fs.readFileSync(logFile, 'utf8').trim();
    const entry = JSON.parse(content);
    expect(entry.zdr_policy).toBe('on');
    expect(entry.zdr_applied).toBe(true);
    expect(entry.decision).toBe('sent');
  });

  it('does not log egress when ZDR is off and egress policy is absent', () => {
    process.env.JEV_ZDR = 'off';

    assertEgressAllowed(
      'https://openrouter.ai/api/v1/chat/completions',
      JSON.stringify({ model: 'test/model' })
    );

    const logFile = egressLogFile();
    expect(fs.existsSync(logFile)).toBe(false);
  });
});
