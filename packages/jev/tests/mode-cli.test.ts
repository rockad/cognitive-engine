import { describe, it, expect, beforeEach, afterEach } from 'bun:test';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import {
  readJevMode,
  writeModeState,
  loadJevConfigSync,
  clearConfigCacheForTesting
} from '../src/core/config.ts';
import { handleSetModeTool, handleGetModeTool } from '../src/tools/mode.ts';

let tmp = '';
let home = '';
let origHome: string | undefined;
let origConfigFile: string | undefined;
let origEnv: NodeJS.ProcessEnv = {};

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

describe('In-process Mode Switching and Single Config Store', () => {
  beforeEach(() => {
    origEnv = { ...process.env };
    for (const v of HARNESS_ENV_VARS) delete process.env[v];
    origHome = process.env.HOME;
    origConfigFile = process.env.JEV_CONFIG_FILE;

    tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-mode-test-'));
    home = path.join(tmp, 'home');
    fs.mkdirSync(home, { recursive: true });

    process.env.HOME = home;
    process.env.JEV_CONFIG_FILE = path.join(home, '.config', 'jev', 'config.json');
    process.env.JEV_CACHE_FILE = ':memory:';
    clearConfigCacheForTesting();
  });

  afterEach(() => {
    if (origHome !== undefined) process.env.HOME = origHome;
    else delete process.env.HOME;

    if (origConfigFile !== undefined) process.env.JEV_CONFIG_FILE = origConfigFile;
    else delete process.env.JEV_CONFIG_FILE;

    process.env = origEnv;
    clearConfigCacheForTesting();
    fs.rmSync(tmp, { recursive: true, force: true });
  });

  it('defaults to "off" across all harnesses when unconfigured', async () => {
    expect(await readJevMode()).toBe('off');
    expect(await readJevMode('claude-code')).toBe('off');
    expect(await readJevMode('antigravity')).toBe('off');
    expect(await readJevMode('opencode')).toBe('off');
  });

  it('switches mode to "on" via writeModeState in config.json', async () => {
    await writeModeState('on');

    expect(await readJevMode()).toBe('on');
    expect(await readJevMode('claude-code')).toBe('on');

    const configPath = process.env.JEV_CONFIG_FILE!;
    expect(fs.existsSync(configPath)).toBe(true);

    const cfg = JSON.parse(fs.readFileSync(configPath, 'utf8'));
    expect(cfg.defaults?.mode).toBe('on');

    // Verifies legacy mode.json is never created
    const legacyPath = path.join(home, '.config', 'jev', 'mode.json');
    expect(fs.existsSync(legacyPath)).toBe(false);
  });

  it('switches mode to "on-demand" via writeModeState', async () => {
    await writeModeState('on-demand');

    expect(await readJevMode()).toBe('on-demand');
    expect(await readJevMode('claude-code')).toBe('on-demand');
  });

  it('global "off" aligns all existing harness entries to "off"', async () => {
    await writeModeState('on');
    await writeModeState('on', 'claude-code');
    await writeModeState('on', 'antigravity');

    expect(await readJevMode('claude-code')).toBe('on');
    expect(await readJevMode('antigravity')).toBe('on');

    // Global off
    await writeModeState('off');

    expect(await readJevMode()).toBe('off');
    expect(await readJevMode('claude-code')).toBe('off');
    expect(await readJevMode('antigravity')).toBe('off');

    const cfg = JSON.parse(fs.readFileSync(process.env.JEV_CONFIG_FILE!, 'utf8'));
    expect(cfg.defaults?.mode).toBe('off');
    expect(cfg.harnesses?.['claude-code']?.mode).toBe('off');
    expect(cfg.harnesses?.antigravity?.mode).toBe('off');
  });

  it('per-harness override takes precedence over defaults.mode = "off"', async () => {
    // defaults.mode is 'off', but claude-code is explicitly 'on'
    await writeModeState('off');
    await writeModeState('on', 'claude-code');

    expect(await readJevMode('claude-code')).toBe('on');
    expect(await readJevMode('antigravity')).toBe('off');
    expect(await readJevMode('opencode')).toBe('off');
  });

  it('global symmetry: writeModeState("off") then writeModeState("on") leaves existing harnesses "on"', async () => {
    // Start with defaults 'on' and antigravity 'on'
    await writeModeState('on');
    await writeModeState('on', 'antigravity');

    expect(await readJevMode('antigravity')).toBe('on');

    // Turn globally off
    await writeModeState('off');
    expect(await readJevMode('antigravity')).toBe('off');

    // Turn globally on
    await writeModeState('on');
    expect(await readJevMode('antigravity')).toBe('on');

    const cfg = JSON.parse(fs.readFileSync(process.env.JEV_CONFIG_FILE!, 'utf8'));
    expect(cfg.defaults?.mode).toBe('on');
    expect(cfg.harnesses?.antigravity?.mode).toBe('on');
  });

  it('supports per-harness mode isolation when global mode is on', async () => {
    await writeModeState('on');
    await writeModeState('on-demand', 'claude-code');

    expect(await readJevMode()).toBe('on');
    expect(await readJevMode('claude-code')).toBe('on-demand');
    expect(await readJevMode('antigravity')).toBe('on');
  });

  it('handleSetModeTool mutates config.json directly in-process', async () => {
    const res = await handleSetModeTool({ mode: 'on' });
    expect(res.content[0].text).toContain('"status": "success"');
    expect(res.content[0].text).toContain('"mode": "on"');

    expect(await readJevMode()).toBe('on');
  });

  it('handleGetModeTool reports active harness, active mode, defaultMode, and harnesses', async () => {
    await writeModeState('off');
    await writeModeState('on', 'antigravity');

    process.env.ANTIGRAVITY_CLI = '1';
    const res = await handleGetModeTool();
    const data = JSON.parse(res.content[0].text);

    expect(data.harness).toBe('antigravity');
    expect(data.mode).toBe('on');
    expect(data.defaultMode).toBe('off');
    expect(data.harnesses?.antigravity?.mode).toBe('on');

    delete process.env.ANTIGRAVITY_CLI;
  });
});
