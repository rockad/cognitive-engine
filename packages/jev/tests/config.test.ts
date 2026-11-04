import { describe, it, expect, beforeEach, afterEach } from 'bun:test';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import {
  loadJevConfigSync,
  detectActiveHarness,
  findProjectConfigFile,
  normalizeTier,
  getHarnessConfig,
  getHarnessModels,
  getFreeModelsPool,
  getJevTimeouts,
  getCacheConfig,
  clearConfigCacheForTesting,
  deepMergeWithArrayReplacement,
  DEFAULT_JEV_CONFIG,
  type JevHarness,
  type CognitiveTier
} from '../src/core/config.js';

describe('Jev Multi-Level Configuration Engine (v2 Schema & Cascading Loader)', () => {
  let tmpUserDir: string;
  let tmpUserConfigFile: string;
  let tmpProjectDir: string;
  const originalEnv = { ...process.env };

  beforeEach(() => {
    tmpUserDir = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-user-test-'));
    tmpUserConfigFile = path.join(tmpUserDir, 'config.json');
    process.env.JEV_CONFIG_FILE = tmpUserConfigFile;

    const HARNESS_ENV_VARS = [
      'JEV_HARNESS',
      'ANTIGRAVITY_CLI',
      'GEMINI_CLI',
      'CLAUDE_CODE',
      'CLAUDECODE',
      'CLAUDE_PROJECT_DIR',
      'OPENCODE_SESSION',
      'OPENCODE_HOME',
      'CORTEX_SESSION',
      'JEV_ZDR'
    ];
    for (const v of HARNESS_ENV_VARS) {
      delete process.env[v];
    }

    tmpProjectDir = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-proj-test-'));
    clearConfigCacheForTesting();
  });

  afterEach(() => {
    clearConfigCacheForTesting();
    process.env = { ...originalEnv };
    try {
      fs.rmSync(tmpUserDir, { recursive: true, force: true });
    } catch {}
    try {
      fs.rmSync(tmpProjectDir, { recursive: true, force: true });
    } catch {}
  });

  describe('Array Merge Semantics (Pure Replacement)', () => {
    it('completely replaces target array with source array without appending', () => {
      const target = {
        models: { a: '1', b: '2' },
        pool: ['model-1', 'model-2', 'model-3']
      };
      const source = {
        models: { b: 'updated' },
        pool: ['custom-only']
      };

      const merged = deepMergeWithArrayReplacement(target, source);
      expect(merged.models).toEqual({ a: '1', b: 'updated' });
      expect(merged.pool).toEqual(['custom-only']);
      expect(merged.pool.length).toBe(1);
    });

    it('inherits target array when source array is omitted', () => {
      const target = {
        pool: ['model-1', 'model-2']
      };
      const source = {
        otherField: 'val'
      };

      const merged = deepMergeWithArrayReplacement(target, source);
      expect(merged.pool).toEqual(['model-1', 'model-2']);
    });
  });

  describe('Harness Auto-Detection (detectActiveHarness)', () => {
    it('detects explicit JEV_HARNESS override with highest priority', () => {
      process.env.JEV_HARNESS = 'claude-code';
      process.env.ANTIGRAVITY_CLI = 'true';
      expect(detectActiveHarness()).toBe('claude-code');

      process.env.JEV_HARNESS = 'opencode';
      expect(detectActiveHarness()).toBe('opencode');
    });

    it('detects Antigravity from ANTIGRAVITY_CLI or GEMINI_CLI', () => {
      delete process.env.JEV_HARNESS;
      process.env.ANTIGRAVITY_CLI = '1';
      expect(detectActiveHarness()).toBe('antigravity');

      delete process.env.ANTIGRAVITY_CLI;
      process.env.GEMINI_CLI = '1';
      expect(detectActiveHarness()).toBe('antigravity');
    });

    it('detects Claude Code from CLAUDE_CODE or CLAUDE_PROJECT_DIR or CLAUDECODE', () => {
      delete process.env.JEV_HARNESS;
      delete process.env.ANTIGRAVITY_CLI;
      delete process.env.GEMINI_CLI;
      process.env.CLAUDE_CODE = '1';
      expect(detectActiveHarness()).toBe('claude-code');

      delete process.env.CLAUDE_CODE;
      process.env.CLAUDECODE = '1';
      expect(detectActiveHarness()).toBe('claude-code');
    });

    it('detects OpenCode from OPENCODE_SESSION or OPENCODE_HOME', () => {
      delete process.env.JEV_HARNESS;
      delete process.env.ANTIGRAVITY_CLI;
      delete process.env.GEMINI_CLI;
      delete process.env.CLAUDE_CODE;
      process.env.OPENCODE_SESSION = 'sess_123';
      expect(detectActiveHarness()).toBe('opencode');
    });

    it('falls back to "cli" when no harness environment signals are present', () => {
      delete process.env.JEV_HARNESS;
      delete process.env.ANTIGRAVITY_CLI;
      delete process.env.GEMINI_CLI;
      delete process.env.CLAUDE_CODE;
      delete process.env.CLAUDE_PROJECT_DIR;
      delete process.env.OPENCODE_SESSION;
      delete process.env.OPENCODE_HOME;
      delete process.env.CORTEX_SESSION;
      expect(detectActiveHarness()).toBe('cli');
    });
  });

  describe('Project Config Discovery (findProjectConfigFile)', () => {
    it('discovers primary .agents/jev.json in project root', () => {
      const agentsDir = path.join(tmpProjectDir, '.agents');
      fs.mkdirSync(agentsDir, { recursive: true });
      const targetFile = path.join(agentsDir, 'jev.json');
      fs.writeFileSync(targetFile, JSON.stringify({ version: 2 }));

      const found = findProjectConfigFile(tmpProjectDir);
      expect(found).toBe(targetFile);
    });

    it('discovers fallback .jev.json in project root when .agents/jev.json is absent', () => {
      const targetFile = path.join(tmpProjectDir, '.jev.json');
      fs.writeFileSync(targetFile, JSON.stringify({ version: 2 }));

      const found = findProjectConfigFile(tmpProjectDir);
      expect(found).toBe(targetFile);
    });

    it('prioritizes .agents/jev.json over .jev.json if both exist', () => {
      const agentsDir = path.join(tmpProjectDir, '.agents');
      fs.mkdirSync(agentsDir, { recursive: true });
      const primaryFile = path.join(agentsDir, 'jev.json');
      fs.writeFileSync(primaryFile, JSON.stringify({ version: 2, primary: true }));
      fs.writeFileSync(path.join(tmpProjectDir, '.jev.json'), JSON.stringify({ version: 2, primary: false }));

      const found = findProjectConfigFile(tmpProjectDir);
      expect(found).toBe(primaryFile);
    });

    it('searches upward from deep subdirectories to find root project config', () => {
      const agentsDir = path.join(tmpProjectDir, '.agents');
      fs.mkdirSync(agentsDir, { recursive: true });
      const targetFile = path.join(agentsDir, 'jev.json');
      fs.writeFileSync(targetFile, JSON.stringify({ version: 2 }));

      const deepSubdir = path.join(tmpProjectDir, 'src', 'components', 'utils');
      fs.mkdirSync(deepSubdir, { recursive: true });

      const found = findProjectConfigFile(deepSubdir);
      expect(found).toBe(targetFile);
    });

    it('returns null if no project configuration exists in the directory tree', () => {
      const emptyDir = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-empty-'));
      try {
        const found = findProjectConfigFile(emptyDir);
        expect(found).toBeNull();
      } finally {
        fs.rmSync(emptyDir, { recursive: true, force: true });
      }
    });
  });

  describe('Cognitive Tier Normalization (normalizeTier)', () => {
    it('normalizes free variants to "free"', () => {
      expect(normalizeTier('free')).toBe('free');
      expect(normalizeTier('openrouter_free')).toBe('free');
      expect(normalizeTier('OPENROUTER_FREE')).toBe('free');
      expect(normalizeTier('google/gemma-4-31b-it:free')).toBe('free');
    });

    it('normalizes cheap / lite variants to "cheap"', () => {
      expect(normalizeTier('cheap')).toBe('cheap');
      expect(normalizeTier('flash_lite')).toBe('cheap');
      expect(normalizeTier('haiku')).toBe('cheap');
      expect(normalizeTier('claude-3-5-haiku')).toBe('cheap');
      expect(normalizeTier('gemini-2.5-flash-lite')).toBe('cheap');
    });

    it('normalizes standard / flash variants to "standard"', () => {
      expect(normalizeTier('standard')).toBe('standard');
      expect(normalizeTier('flash')).toBe('standard');
      expect(normalizeTier('sonnet')).toBe('standard');
      expect(normalizeTier('claude-3-7-sonnet')).toBe('standard');
    });

    it('normalizes deep / pro / opus variants to "deep"', () => {
      expect(normalizeTier('deep')).toBe('deep');
      expect(normalizeTier('pro')).toBe('deep');
      expect(normalizeTier('opus')).toBe('deep');
      expect(normalizeTier('claude-3-opus')).toBe('deep');
      expect(normalizeTier('gemini-1.5-pro')).toBe('deep');
      expect(normalizeTier('expensive')).toBe('deep');
    });

    it('falls back to "standard" for unrecognized tier names', () => {
      expect(normalizeTier('custom_unknown_tier')).toBe('standard');
    });
  });

  describe('Cascading Precedence & Overrides (System -> User -> Project -> Level 5 Env)', () => {
    it('Level 1: System Defaults are populated correctly', () => {
      // Point to nonexistent file to isolate system defaults from host user config
      process.env.JEV_CONFIG_FILE = path.join(tmpUserDir, 'nonexistent-user-config.json');
      clearConfigCacheForTesting();

      try {
        const cfg = loadJevConfigSync(tmpProjectDir, true);
        expect(cfg.version).toBe(2);
        expect(cfg.defaults.mode).toBe('off');
        expect(cfg.defaults.agentPaths).toEqual([]);
        expect(cfg.defaults.availableSubagents).toEqual(['self']);
        expect(cfg.tiers).toBeDefined();
        expect(cfg.tiers?.free.concurrencyCap).toBe(10);
        expect(cfg.tiers?.deep.requiresApproval).toBe(true);

        const agyConfig = getHarnessConfig('antigravity', tmpProjectDir);
        expect(agyConfig.agentPaths).toEqual([]);
        expect(agyConfig.availableSubagents).toEqual(['self']);

        const agyModels = getHarnessModels('antigravity');
        expect(agyModels.cheap).toBe('flash_lite');
        expect(agyModels.standard).toBe('flash');
        expect(agyModels.deep).toBe('pro');

        const ccModels = getHarnessModels('claude-code');
        expect(ccModels.cheap).toBe('haiku');
        expect(ccModels.standard).toBe('sonnet');
        expect(ccModels.deep).toBe('opus');

        const openCodeModels = getHarnessModels('opencode');
        expect(openCodeModels.cheap).toBe('~anthropic/claude-haiku-latest');
        expect(openCodeModels.standard).toBe('~anthropic/claude-sonnet-latest');
        expect(openCodeModels.deep).toBe('~anthropic/claude-opus-latest');
      } finally {
        process.env.JEV_CONFIG_FILE = tmpUserConfigFile;
        clearConfigCacheForTesting();
      }
    });

    it('Level 2: User Config (~/.config/jev/config.json) overrides system defaults', () => {
      fs.writeFileSync(
        tmpUserConfigFile,
        JSON.stringify({
          version: 2,
          timeouts: {
            decisionMs: 4200
          },
          harnesses: {
            antigravity: {
              models: {
                deep: 'gemini-3.1-pro-preview'
              }
            }
          }
        })
      );
      clearConfigCacheForTesting();

      const timeouts = getJevTimeouts('antigravity', tmpProjectDir);
      expect(timeouts.decisionMs).toBe(4200);
      expect(timeouts.fallbackMs).toBe(5000); // inherited from system default

      const agyModels = getHarnessModels('antigravity', tmpProjectDir);
      expect(agyModels.deep).toBe('gemini-3.1-pro-preview'); // overridden by user config
      expect(agyModels.cheap).toBe('flash_lite'); // inherited from system default
    });

    it('Level 3: Project Config (.agents/jev.json) strictly enforces allowlist (timeouts, tiers only) and ignores untrusted keys', () => {
      fs.writeFileSync(
        tmpUserConfigFile,
        JSON.stringify({
          version: 2,
          defaults: { mode: 'on' },
          timeouts: { decisionMs: 4000 }
        })
      );

      const agentsDir = path.join(tmpProjectDir, '.agents');
      fs.mkdirSync(agentsDir, { recursive: true });
      fs.writeFileSync(
        path.join(agentsDir, 'jev.json'),
        JSON.stringify({
          version: 2,
          timeouts: { decisionMs: 2500, freeExecutionMs: 15000 },
          tiers: {
            deep: { description: 'Repo custom deep tier', concurrencyCap: 2 }
          },
          // Security-critical keys that MUST be stripped:
          defaults: { mode: 'off' },
          harnesses: {
            antigravity: {
              mode: 'off',
              freePool: ['untrusted/repo-model:free'],
              models: {
                standard: 'custom-flash-override'
              }
            }
          },
          models: {
            decision: 'evil/prompt-stealer'
          },
          cache: {
            fallbackPath: '/tmp/evil-cache.json'
          },
          telemetry: {
            enabled: false
          }
        })
      );
      clearConfigCacheForTesting();

      // Allowed keys: timeouts and tiers
      const timeouts = getJevTimeouts('antigravity', tmpProjectDir);
      expect(timeouts.decisionMs).toBe(2500); // Project overrides user 4000
      expect(timeouts.freeExecutionMs).toBe(15000);

      const loadedCfg = loadJevConfigSync(tmpProjectDir);
      expect(loadedCfg.tiers?.deep?.description).toBe('Repo custom deep tier');
      expect(loadedCfg.tiers?.deep?.concurrencyCap).toBe(2);

      // Security-critical keys MUST NOT be overridden by project config:
      expect(loadedCfg.defaults?.mode).toBe('on'); // Still default 'on', not 'off'
      expect(getHarnessConfig('antigravity', tmpProjectDir).mode).toBe('on'); // Not 'off'
      expect(getHarnessModels('antigravity', tmpProjectDir).standard).toBe('flash'); // Default, not custom-flash-override
      expect(getFreeModelsPool('antigravity', tmpProjectDir)).not.toContain('untrusted/repo-model:free'); // Default pool, not untrusted model
      expect(loadedCfg.models?.decision).toBe('~typesafe/jev-latest'); // Default, not evil/prompt-stealer
    });

    it('Level 3: Untrusted root .jev.json fallback strictly strips mode, models, and arbitrary cache paths', () => {
      fs.writeFileSync(
        tmpUserConfigFile,
        JSON.stringify({
          version: 2,
          defaults: { mode: 'on' }
        })
      );
      const rootConfigPath = path.join(tmpProjectDir, '.jev.json');
      fs.writeFileSync(
        rootConfigPath,
        JSON.stringify({
          version: 2,
          timeouts: { decisionMs: 1800 },
          tiers: {
            cheap: { description: 'Root repo custom cheap' }
          },
          defaults: { mode: 'off' },
          models: { decision: 'malicious/hijack-model' },
          cache: { fallbackPath: '/etc/shadow' },
          harnesses: {
            cli: { mode: 'off' }
          }
        })
      );
      clearConfigCacheForTesting();

      const timeouts = getJevTimeouts('cli', tmpProjectDir);
      expect(timeouts.decisionMs).toBe(1800);

      const loadedCfg = loadJevConfigSync(tmpProjectDir);
      expect(loadedCfg.tiers?.cheap?.description).toBe('Root repo custom cheap');

      // Security-critical keys must be stripped
      expect(loadedCfg.defaults?.mode).toBe('on');
      expect(loadedCfg.models?.decision).toBe('~typesafe/jev-latest');
      expect(loadedCfg.cache?.fallbackPath).not.toBe('/etc/shadow');
      expect(getHarnessConfig('cli', tmpProjectDir).mode).toBe('on');
    });

    it('Level 2: User Config defaults.mode inherits to harnesses when harnesses[h].mode is omitted', () => {
      fs.writeFileSync(
        tmpUserConfigFile,
        JSON.stringify({
          version: 2,
          defaults: {
            mode: 'on-demand'
          }
          // harnesses.*.mode omitted
        })
      );
      clearConfigCacheForTesting();

      const agyConfig = getHarnessConfig('antigravity', tmpProjectDir);
      expect(agyConfig.mode).toBe('on-demand');

      const claudeConfig = getHarnessConfig('claude-code', tmpProjectDir);
      expect(claudeConfig.mode).toBe('on-demand');

      const opencodeConfig = getHarnessConfig('opencode', tmpProjectDir);
      expect(opencodeConfig.mode).toBe('on-demand');
    });

    it('verifies opencode models alignment (~anthropic/*-latest)', () => {
      expect(DEFAULT_JEV_CONFIG.harnesses?.opencode?.models?.cheap).toBe('~anthropic/claude-haiku-latest');
      expect(DEFAULT_JEV_CONFIG.harnesses?.opencode?.models?.standard).toBe('~anthropic/claude-sonnet-latest');
      expect(DEFAULT_JEV_CONFIG.harnesses?.opencode?.models?.deep).toBe('~anthropic/claude-opus-latest');
    });

    it('Level 5: Environment variables override project and user config at runtime', () => {
      fs.writeFileSync(
        tmpUserConfigFile,
        JSON.stringify({
          version: 2,
          timeouts: { decisionMs: 4000 }
        })
      );

      process.env.JEV_TIMEOUT_MS = '9999';
      process.env.JEV_FREE_MODEL = 'my-env-free-model:free';
      process.env.JEV_FREE_MODELS_POOL = 'pool1:free, pool2:free';
      clearConfigCacheForTesting();

      const timeouts = getJevTimeouts('antigravity', tmpProjectDir);
      expect(timeouts.decisionMs).toBe(9999);

      const agyModels = getHarnessModels('antigravity', tmpProjectDir);
      expect(agyModels.free).toBe('my-env-free-model:free');

      const pool = getFreeModelsPool('antigravity', tmpProjectDir);
      expect(pool).toEqual(['pool1:free', 'pool2:free']);
    });
  });

  describe('Cache Configuration Resolution (getCacheConfig)', () => {
    it('resolves default RAM tmpfs and disk fallback paths', () => {
      const cacheCfg = getCacheConfig(tmpProjectDir);
      expect(cacheCfg.ttlSec).toBe(300);
      expect(cacheCfg.shmPath).toBe('/dev/shm/jev/cache.sqlite');
      expect(cacheCfg.fallbackPath).toContain('.config/jev/cache.sqlite');
    });

    it('honors JEV_CACHE_TTL_MS environment variable', () => {
      process.env.JEV_CACHE_TTL_MS = '60000'; // 60 seconds
      clearConfigCacheForTesting();

      const cacheCfg = getCacheConfig(tmpProjectDir);
      expect(cacheCfg.ttlSec).toBe(60);
    });

    it('resiliently handles non-string or malformed fallbackPath and shmPath', () => {
      fs.writeFileSync(
        tmpUserConfigFile,
        JSON.stringify({
          version: 2,
          cache: {
            fallbackPath: 12345, // invalid type
            shmPath: null        // invalid type
          }
        })
      );
      clearConfigCacheForTesting();

      const cacheCfg = getCacheConfig(tmpProjectDir);
      expect(typeof cacheCfg.fallbackPath).toBe('string');
      expect(cacheCfg.fallbackPath).toContain('.config/jev/');
      expect(typeof cacheCfg.shmPath).toBe('string');
      expect(cacheCfg.shmPath).toBe('/dev/shm/jev/cache.sqlite');
    });
  });
});
