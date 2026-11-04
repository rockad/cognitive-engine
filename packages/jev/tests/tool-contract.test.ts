/**
 * The MCP server contract for Jev's tools (RFC 001 D30; plan §3.5): an unknown,
 * mistyped or missing argument is a tool error, never silently ignored or defaulted.
 *
 * Driven through the real server over an in-memory transport for ALL tools, derived
 * from the registry, so a tool added later is covered without editing this file.
 * Every case here is rejected by validation before a handler runs, so none touches
 * the network.
 */
import { describe, it, expect, beforeEach, afterEach } from 'bun:test';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { z } from 'zod';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js';
import { createJevServer } from '../src/server.ts';
import { jevTools, type JevTool } from '../src/tools/index.ts';
import { clearConfigCacheForTesting } from '../src/core/config.ts';

let tmp = '';
let savedEnv: NodeJS.ProcessEnv = {};

beforeEach(() => {
  savedEnv = { ...process.env };
  for (const v of ['JEV_HARNESS', 'ANTIGRAVITY_CLI', 'GEMINI_CLI', 'CLAUDE_CODE', 'CLAUDECODE', 'CLAUDE_PROJECT_DIR', 'OPENCODE_SESSION', 'OPENCODE_HOME', 'CORTEX_SESSION']) {
    delete process.env[v];
  }
  // No key, so even a validation bug could not make a real request.
  delete process.env.JEV_OPENROUTER_API_KEY;
  delete process.env.OPENROUTER_API_KEY;
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-contract-test-'));
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

async function connect(): Promise<Client> {
  const [clientSide, serverSide] = InMemoryTransport.createLinkedPair();
  await createJevServer().connect(serverSide);
  const client = new Client({ name: 'contract', version: '0.0.0' });
  await client.connect(clientSide);
  return client;
}

type Call = { isError?: boolean; content: Array<{ type: string; text?: string }> };

async function call(client: Client, name: string, args?: Record<string, unknown>): Promise<Call> {
  return (await client.callTool({ name, arguments: args })) as Call;
}

/** A value of the right type for a zod field, used to build the smallest valid call. */
function sample(field: z.ZodTypeAny): unknown {
  switch (field._def.typeName) {
    case 'ZodString': return 'x';
    case 'ZodNumber': return 1;
    case 'ZodBoolean': return true;
    case 'ZodArray': return [];
    case 'ZodEnum': return field._def.values[0];
    case 'ZodRecord': return {};
    case 'ZodOptional': return sample(field._def.innerType);
    default: throw new Error(`no sample for ${field._def.typeName}`);
  }
}

function requiredKeys(tool: JevTool): string[] {
  return Object.entries(tool.input.shape)
    .filter(([, f]) => !(f as z.ZodTypeAny).isOptional())
    .map(([k]) => k);
}

function minimal(tool: JevTool): Record<string, unknown> {
  return Object.fromEntries(requiredKeys(tool).map((k) => [k, sample(tool.input.shape[k] as z.ZodTypeAny)]));
}

const WRONG = { wrong: true };

describe('every tool rejects an unknown argument', () => {
  for (const tool of jevTools) {
    it(tool.name, async () => {
      const client = await connect();
      const res = await call(client, tool.name, { ...minimal(tool), visiblity: 'open' });
      expect(res.isError).toBe(true);
      expect(res.content[0].text).toMatch(/invalid arguments|unrecognized/i);
    });
  }
});

describe('every tool rejects a missing required argument', () => {
  for (const tool of jevTools) {
    for (const key of requiredKeys(tool)) {
      it(`${tool.name} without "${key}"`, async () => {
        const client = await connect();
        const args = minimal(tool);
        delete args[key];
        const res = await call(client, tool.name, args);
        expect(res.isError).toBe(true);
      });
    }
  }
});

describe('every tool rejects a wrong-typed argument', () => {
  for (const tool of jevTools) {
    for (const key of Object.keys(tool.input.shape)) {
      it(`${tool.name} with a bad "${key}"`, async () => {
        const client = await connect();
        const res = await call(client, tool.name, { ...minimal(tool), [key]: WRONG });
        expect(res.isError).toBe(true);
      });
    }
  }
});

describe('nested objects and enums are strict too', () => {
  it('jev_batch_dispatch rejects a typo inside a task', async () => {
    const client = await connect();
    const res = await call(client, 'jev_batch_dispatch', {
      tasks: [{ id: 'a', task: 'do it', dependancies: ['b'] }]
    });
    expect(res.isError).toBe(true);
  });

  it('jev_select_model rejects a typo inside a candidate and a missing tier', async () => {
    const client = await connect();
    const base = { id: 'm', providerModel: 'p/m', costInPerMTok: 1, costOutPerMTok: 2 };
    expect((await call(client, 'jev_select_model', { task: 't', candidates: [{ ...base, tier: 'cheap', costin: 1 }] })).isError).toBe(true);
    expect((await call(client, 'jev_select_model', { task: 't', candidates: [base] })).isError).toBe(true);
  });

  it('jev_select_model rejects an unknown quotaStrategy', async () => {
    const client = await connect();
    expect((await call(client, 'jev_select_model', { task: 't', quotaStrategy: 'frugal' })).isError).toBe(true);
  });

  it('jev_set_mode rejects a value outside the enum and leaves the mode unchanged', async () => {
    const client = await connect();
    // 'disable' used to be accepted by the handler although the schema never advertised it
    for (const mode of ['disable', 'ON', 'maybe', '']) {
      expect((await call(client, 'jev_set_mode', { mode })).isError).toBe(true);
    }
    const got = await call(client, 'jev_get_mode', {});
    expect(JSON.parse(got.content[0].text!).mode).toBe('off');
  });
});

describe('argument-less tools', () => {
  it('jev_get_mode works with empty arguments and with none at all', async () => {
    const client = await connect();
    expect((await call(client, 'jev_get_mode', {})).isError).toBeFalsy();
    expect((await call(client, 'jev_get_mode')).isError).toBeFalsy();
  });

  it('jev_get_mode rejects any argument', async () => {
    const client = await connect();
    expect((await call(client, 'jev_get_mode', { mode: 'on' })).isError).toBe(true);
  });

  it('jev_quota accepts refresh and rejects anything else', async () => {
    const client = await connect();
    expect((await call(client, 'jev_quota', { refresh: true })).isError).toBeFalsy();
    expect((await call(client, 'jev_quota', { refesh: true })).isError).toBe(true);
  });
});
