import { describe, it, expect, beforeEach, afterEach } from 'bun:test';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import {
  HOOK_GROUP,
  SERVER_NAME,
  installMcpConfig,
  jevHooksEntry,
  jevServerEntry,
  mergeHooksConfig,
  mergeMcpConfig
} from '../src/index.ts';

let tmp = '';
let configPath = '';

beforeEach(() => {
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-agy-'));
  configPath = path.join(tmp, 'config', 'mcp_config.json');
});

afterEach(() => {
  fs.rmSync(tmp, { recursive: true, force: true });
});

describe('antigravity adapter', () => {
  const entry = jevServerEntry('/opt/cognitive-engine');

  it('runs the stdio entrypoint of the engine checkout', () => {
    expect(entry.command).toBe('bun');
    expect(entry.args).toEqual(['run', '/opt/cognitive-engine/packages/jev/src/index.ts']);
  });

  it('embeds no credentials', () => {
    expect(entry.env).toBeUndefined();
    expect(JSON.stringify(entry)).not.toMatch(/sk-or-|API_KEY/);
  });

  it('merges without touching other servers', () => {
    const merged = mergeMcpConfig({ mcpServers: { lookup: { command: 'x', args: [] } }, other: 1 }, entry);
    expect(Object.keys(merged.mcpServers ?? {}).sort()).toEqual([SERVER_NAME, 'lookup']);
    expect(merged.other).toBe(1);
  });

  it('replaces a stale jev entry', () => {
    const merged = mergeMcpConfig({ mcpServers: { jev: { command: 'old', args: [] } } }, entry);
    expect(merged.mcpServers?.jev).toEqual(entry);
  });

  it('creates the config when it does not exist', () => {
    const res = installMcpConfig(entry, configPath);
    expect(res).toEqual({ path: configPath, changed: true, backup: null });
    expect(JSON.parse(fs.readFileSync(configPath, 'utf8')).mcpServers.jev).toEqual(entry);
  });

  it('is idempotent', () => {
    installMcpConfig(entry, configPath);
    const again = installMcpConfig(entry, configPath);
    expect(again.changed).toBe(false);
    expect(fs.existsSync(`${configPath}.bak`)).toBe(false);
  });

  it('backs up the previous file when it changes the content', () => {
    fs.mkdirSync(path.dirname(configPath), { recursive: true });
    fs.writeFileSync(configPath, JSON.stringify({ mcpServers: { lookup: { command: 'x', args: [] } } }));

    const res = installMcpConfig(entry, configPath);

    expect(res.changed).toBe(true);
    expect(res.backup).toBe(`${configPath}.bak`);
    expect(JSON.parse(fs.readFileSync(`${configPath}.bak`, 'utf8')).mcpServers.jev).toBeUndefined();
    expect(JSON.parse(fs.readFileSync(configPath, 'utf8')).mcpServers.lookup).toBeDefined();
  });

  it('refuses to overwrite an unparseable config', () => {
    fs.mkdirSync(path.dirname(configPath), { recursive: true });
    fs.writeFileSync(configPath, '{ not json');

    expect(() => installMcpConfig(entry, configPath)).toThrow(/not valid JSON/);
    expect(fs.readFileSync(configPath, 'utf8')).toBe('{ not json');
  });
});

describe('antigravity hook entry', () => {
  const entry = jevHooksEntry('/opt/cognitive-engine') as any;

  it('guards only invoke_subagent with the shared hook script', () => {
    expect(entry.PreToolUse).toHaveLength(1);
    expect(entry.PreToolUse[0].matcher).toBe('invoke_subagent');
    expect(entry.PreToolUse[0].hooks[0].command).toBe('bun /opt/cognitive-engine/packages/jev/src/hook.ts antigravity');
  });

  it('registers no other hook events', () => {
    expect(Object.keys(entry).sort()).toEqual(['PreToolUse', 'enabled']);
  });

  it('merges into a workspace hooks config without touching other groups', () => {
    const merged = mergeHooksConfig({ other: { enabled: true } }, entry);
    expect(Object.keys(merged).sort()).toEqual([HOOK_GROUP, 'other']);
    expect(merged[HOOK_GROUP]).toEqual(entry);
  });
});
