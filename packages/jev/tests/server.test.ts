import { describe, it, expect, beforeEach, afterEach } from 'bun:test';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import type { AddressInfo } from 'node:net';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';
import { jevTools } from '../src/tools/index.ts';
import {
  createJevServer,
  startHttp,
  isLoopbackHost,
  isAllowedOrigin,
  HTTP_HOST,
  HTTP_PATH,
  HTTP_PORT
} from '../src/server.ts';
import { clampTierToSessionCeiling, modelTierRank } from '../src/core/governance.ts';
import { clearConfigCacheForTesting } from '../src/core/config.ts';

const REQUIRED_TOOLS = [
  'jev_dispatch',
  'jev_route',
  'jev_select_model',
  'jev_judge',
  'jev_score',
  'jev_set_mode',
  'jev_get_mode',
  'jev_quota',
  'jev_execute_free',
  'jev_agent_free'
];

const HARNESS_ENV_VARS = [
  'JEV_HARNESS',
  'ANTIGRAVITY_CLI',
  'GEMINI_CLI',
  'CLAUDE_CODE',
  'CLAUDECODE',
  'CLAUDE_PROJECT_DIR',
  'OPENCODE_SESSION',
  'OPENCODE_HOME',
  'CORTEX_SESSION'
];

let tmp = '';
let savedEnv: NodeJS.ProcessEnv = {};

beforeEach(() => {
  savedEnv = { ...process.env };
  for (const v of HARNESS_ENV_VARS) delete process.env[v];
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-server-test-'));
  process.env.HOME = tmp;
  process.env.JEV_CONFIG_FILE = path.join(tmp, 'config.json');
  process.env.JEV_CACHE_FILE = ':memory:';
  clearConfigCacheForTesting();
});

afterEach(() => {
  process.env = savedEnv;
  clearConfigCacheForTesting();
  fs.rmSync(tmp, { recursive: true, force: true });
});

describe('tool registry schema', () => {
  it('registers every RFC 002 tool', () => {
    const names = jevTools.map((t) => t.name);
    for (const name of REQUIRED_TOOLS) expect(names).toContain(name);
  });

  it('gives every tool a handler, a description and a STRICT object input schema', () => {
    for (const tool of jevTools) {
      expect(typeof tool.handler).toBe('function');
      expect(tool.description.length).toBeGreaterThan(0);
      expect(tool.input._def.unknownKeys).toBe('strict');
    }
  });

  it('has unique tool names', () => {
    const names = jevTools.map((t) => t.name);
    expect(new Set(names).size).toBe(names.length);
  });
});

describe('MCP tool dispatch (in-memory transport)', () => {
  async function connect(): Promise<Client> {
    const [clientSide, serverSide] = InMemoryTransport.createLinkedPair();
    await createJevServer().connect(serverSide);
    const client = new Client({ name: 'test', version: '0.0.0' });
    await client.connect(clientSide);
    return client;
  }

  it('lists the registered tools', async () => {
    const client = await connect();
    const { tools } = await client.listTools();
    expect(tools.map((t) => t.name).sort()).toEqual(jevTools.map((t) => t.name).sort());
  });

  it('advertises every input schema as closed (additionalProperties: false)', async () => {
    const client = await connect();
    const { tools } = await client.listTools();
    for (const tool of tools) {
      expect(tool.inputSchema.type).toBe('object');
      expect(tool.inputSchema.additionalProperties).toBe(false);
    }
  });

  it('dispatches jev_get_mode through the server', async () => {
    const client = await connect();
    const res = await client.callTool({ name: 'jev_get_mode', arguments: {} });
    const text = (res.content as Array<{ type: string; text: string }>)[0].text;
    expect(JSON.parse(text).mode).toBe('off');
  });

  it('round-trips a mode change', async () => {
    const client = await connect();
    await client.callTool({ name: 'jev_set_mode', arguments: { mode: 'on-demand' } });
    const res = await client.callTool({ name: 'jev_get_mode', arguments: {} });
    const text = (res.content as Array<{ type: string; text: string }>)[0].text;
    expect(JSON.parse(text).mode).toBe('on-demand');
  });

  it('rejects an unknown tool', async () => {
    const client = await connect();
    const res = (await client.callTool({ name: 'jev_nope', arguments: {} })) as { isError?: boolean; content: Array<{ text: string }> };
    expect(res.isError).toBe(true);
    expect(res.content[0].text).toMatch(/not found/i);
  });
});

describe('Streamable HTTP transport', () => {
  it('defaults to 127.0.0.1:8115/mcp', () => {
    expect(HTTP_HOST).toBe('127.0.0.1');
    expect(HTTP_PORT).toBe(8115);
    expect(HTTP_PATH).toBe('/mcp');
  });

  it('only accepts loopback Host headers for the bound port', () => {
    expect(isLoopbackHost('127.0.0.1:8115', 8115)).toBe(true);
    expect(isLoopbackHost('localhost:8115', 8115)).toBe(true);
    expect(isLoopbackHost('evil.example:8115', 8115)).toBe(false);
    expect(isLoopbackHost('127.0.0.1:9999', 8115)).toBe(false);
    expect(isLoopbackHost(undefined, 8115)).toBe(false);
  });

  it('only accepts a missing or loopback Origin', () => {
    expect(isAllowedOrigin(undefined)).toBe(true);
    expect(isAllowedOrigin('http://127.0.0.1:8115')).toBe(true);
    expect(isAllowedOrigin('http://localhost:3000')).toBe(true);
    expect(isAllowedOrigin('http://evil.example')).toBe(false);
    expect(isAllowedOrigin('https://evil.example:8115')).toBe(false);
    expect(isAllowedOrigin('http://127.0.0.1.evil.example')).toBe(false);
    expect(isAllowedOrigin('null')).toBe(false);
    expect(isAllowedOrigin('not a url')).toBe(false);
  });

  it('serves tools/list and tools/call over HTTP', async () => {
    const http = await startHttp({ port: 0 });
    try {
      const { port } = http.address() as AddressInfo;
      const client = new Client({ name: 'test', version: '0.0.0' });
      await client.connect(new StreamableHTTPClientTransport(new URL(`http://127.0.0.1:${port}${HTTP_PATH}`)));

      const { tools } = await client.listTools();
      expect(tools.map((t) => t.name)).toContain('jev_dispatch');

      const res = await client.callTool({ name: 'jev_get_mode', arguments: {} });
      expect((res.content as Array<{ text: string }>)[0].text).toContain('"mode"');
      await client.close();
    } finally {
      http.close();
    }
  });

  it('binds loopback only and rejects other paths, methods and hosts', async () => {
    const http = await startHttp({ port: 0 });
    try {
      const addr = http.address() as AddressInfo;
      expect(addr.address).toBe('127.0.0.1');
      const base = `http://127.0.0.1:${addr.port}`;

      expect((await fetch(`${base}/other`, { method: 'POST' })).status).toBe(404);
      expect((await fetch(`${base}${HTTP_PATH}`)).status).toBe(405);

      const forged = await fetch(`${base}${HTTP_PATH}`, {
        method: 'POST',
        headers: { Host: 'evil.example', 'Content-Type': 'application/json' },
        body: '{}'
      });
      expect(forged.status).toBe(403);

      const foreign = await fetch(`${base}${HTTP_PATH}`, {
        method: 'POST',
        headers: { Origin: 'http://evil.example', 'Content-Type': 'application/json' },
        body: '{}'
      });
      expect(foreign.status).toBe(403);
    } finally {
      http.close();
    }
  });
});

describe('tier ceiling invariants', () => {
  it('ranks tiers from free to pro', () => {
    expect(modelTierRank('openrouter_free')).toBeLessThan(modelTierRank('flash_lite'));
    expect(modelTierRank('flash_lite')).toBeLessThan(modelTierRank('flash'));
    expect(modelTierRank('flash')).toBeLessThan(modelTierRank('pro'));
  });

  it('clamps a Pro recommendation to the parent session ceiling', () => {
    const out = clampTierToSessionCeiling('pro', 'flash');
    expect(out.wasClamped).toBe(true);
    expect(out.clampedTier).toBe('flash');
  });

  it('accepts no input that lifts the ceiling', () => {
    // dispatch is advice: the clamp takes only the recommendation and the session model
    expect(clampTierToSessionCeiling.length).toBe(2);
    for (const name of ['jev_dispatch', 'jev_batch_dispatch']) {
      const tool = jevTools.find((t) => t.name === name)!;
      expect(Object.keys(tool.input.shape)).not.toContain('approvedTier');
      // and, being strict, a caller who passes one anyway is refused rather than ignored
      expect(tool.input.safeParse({ task: 'x', tasks: [], approvedTier: 'pro' }).success).toBe(false);
    }
  });

  it('keeps a Pro session at Pro', () => {
    expect(clampTierToSessionCeiling('pro', 'gemini-3.8-pro').clampedTier).toBe('pro');
  });

  it('leaves a recommendation at or below the ceiling alone', () => {
    const out = clampTierToSessionCeiling('flash_lite', 'flash');
    expect(out.wasClamped).toBe(false);
    expect(out.clampedTier).toBe('flash_lite');
  });
});
