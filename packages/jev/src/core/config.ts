import * as fs from 'node:fs/promises';
import * as fsSync from 'node:fs';
import * as path from 'node:path';
import * as os from 'node:os';

export type JevMode = 'off' | 'on-demand' | 'on';
export const JEV_STATE_FILE = path.join(os.homedir(), '.config', 'jev', 'mode.json');
export const JEV_CONFIG_FILE = process.env.JEV_CONFIG_FILE || path.join(os.homedir(), '.config', 'jev', 'config.json');
export type JevHarness = 'antigravity' | 'claude-code' | 'opencode' | 'cli';

import type { ZdrPolicy } from './egress.js';

export type CognitiveTier = 'free' | 'cheap' | 'standard' | 'deep';

export interface JevPrivacyConfig {
  zdr?: ZdrPolicy | boolean;
}

export interface JevTiersConfig {
  free: { description: string; concurrencyCap?: number; requiresApproval?: boolean };
  cheap: { description: string; concurrencyCap?: number; requiresApproval?: boolean };
  standard: { description: string; concurrencyCap?: number; requiresApproval?: boolean };
  deep: { description: string; concurrencyCap?: number; requiresApproval?: boolean };
  [key: string]: { description: string; concurrencyCap?: number; requiresApproval?: boolean };
}

export type HarnessModelMap = Record<CognitiveTier, string>;

export interface HarnessRoutingConfig {
  swap?: boolean;
  models?: Record<string, string>;
}

export interface HarnessConfig {
  mode: JevMode;
  models?: Partial<HarnessModelMap>;
  freePool?: string[];
  routing?: HarnessRoutingConfig;
  requireUpgradeApproval?: boolean;
  availableSubagents?: string[];
  agentPaths?: string[];
}

export interface JevTimeoutsConfig {
  decisionMs: number;
  fallbackMs: number;
  freeModelMs: number;
  freeExecutionMs: number;
}

export interface JevCacheConfig {
  ttlSec: number;
  shmPath: string;
  fallbackPath: string;
}

export interface JevConfigDefaults {
  mode: JevMode;
  requireUpgradeApproval?: boolean;
  swap?: boolean;
  availableSubagents?: string[];
  agentPaths?: string[];
}

export interface JevRoutingConfig {
  strategy?: 'router' | 'keyword';
  swap?: boolean;
  models?: Record<string, string>;
}

export interface JevConfig {
  version: number;
  defaults: JevConfigDefaults;
  routing?: JevRoutingConfig;
  tiers?: JevTiersConfig;
  models?: Record<string, string>;
  timeouts?: Partial<JevTimeoutsConfig>;
  cache?: Partial<JevCacheConfig>;
  thresholds?: Record<string, number>;
  harnesses?: Partial<Record<JevHarness, Partial<HarnessConfig>>>;
  privacy?: JevPrivacyConfig;
}

export const DEFAULT_JEV_CONFIG: JevConfig = {
  version: 2,
  defaults: {
    mode: 'off',
    requireUpgradeApproval: true,
    swap: true,
    agentPaths: [],
    availableSubagents: ['self']
  },
  routing: {
    strategy: 'router',
    swap: true
  },
  tiers: {
    free: {
      description: 'Boilerplate, syntax transforms, draft summaries ($0 cost)',
      concurrencyCap: 10
    },
    cheap: {
      description: 'Trivial triage, quick lookups, file moves, formatting'
    },
    standard: {
      description: 'Standard agentic steps, routine edits, code engineering'
    },
    deep: {
      description: 'Complex multi-file refactoring, deep architecture, high-stakes writing',
      requiresApproval: true
    }
  },
  models: {
    decision: '~typesafe/jev-latest',
    router: 'typesafe/jev-router',
    fallback: 'typesafe/jev-router'
  },
  timeouts: {
    decisionMs: 3500,
    fallbackMs: 5000,
    freeModelMs: 8000,
    freeExecutionMs: 25000
  },
  cache: {
    ttlSec: 300,
    shmPath: '/dev/shm/jev/cache.sqlite',
    fallbackPath: path.join(os.homedir(), '.config', 'jev', 'cache.sqlite')
  },
  thresholds: {
    five_hour_normal_min: 0.30,
    five_hour_conserve_min: 0.10,
    weekly_normal_min: 0.20,
    weekly_conserve_min: 0.10
  },
  harnesses: {
    antigravity: {
      mode: 'off',
      models: {
        free: 'openrouter/free',
        cheap: 'flash_lite',
        standard: 'flash',
        deep: 'pro'
      },
      freePool: [
        'openrouter/free',
        'google/gemma-4-31b-it:free',
        'cohere/north-mini-code:free',
        'nvidia/nemotron-3.5-lightning:free'
      ]
    },
    'claude-code': {
      mode: 'off',
      models: {
        free: 'openrouter/free',
        cheap: 'haiku',
        standard: 'sonnet',
        deep: 'opus'
      }
    },
    opencode: {
      mode: 'off',
      models: {
        free: 'openrouter/free',
        cheap: '~anthropic/claude-haiku-latest',
        standard: '~anthropic/claude-sonnet-latest',
        deep: '~anthropic/claude-opus-latest'
      }
    },
    cli: {
      mode: 'off',
      models: {
        free: 'openrouter/free',
        cheap: 'google/gemma-4-31b-it:free',
        standard: 'typesafe/jev-router',
        deep: 'typesafe/jev-router'
      }
    }
  }
};

/**
 * Pure Replacement recursive merge:
 * - Specified arrays in source completely replace target arrays.
 * - Objects are merged recursively.
 * - Primitives overwrite target.
 */
export function deepMergeWithArrayReplacement<T extends Record<string, any>>(target: T, source?: Record<string, any>): T {
  if (!source || typeof source !== 'object') return target;
  const result: any = Array.isArray(target) ? [...target] : { ...target };
  for (const key of Object.keys(source)) {
    const sVal = source[key];
    const tVal = result[key];
    if (sVal === undefined) continue;
    if (Array.isArray(sVal)) {
      result[key] = [...sVal];
    } else if (sVal !== null && typeof sVal === 'object' && !Array.isArray(sVal)) {
      if (tVal !== null && typeof tVal === 'object' && !Array.isArray(tVal)) {
        result[key] = deepMergeWithArrayReplacement(tVal, sVal);
      } else {
        result[key] = deepMergeWithArrayReplacement({}, sVal);
      }
    } else {
      result[key] = sVal;
    }
  }
  return result;
}

export function detectActiveHarness(): JevHarness {
  if (process.env.JEV_HARNESS) {
    const h = process.env.JEV_HARNESS.toLowerCase().trim();
    if (h === 'antigravity' || h === 'claude-code' || h === 'opencode' || h === 'cli') {
      return h as JevHarness;
    }
  }
  if (process.env.ANTIGRAVITY_CLI || process.env.GEMINI_CLI || process.env.CORTEX_SESSION) {
    return 'antigravity';
  }
  if (process.env.CLAUDE_CODE || process.env.CLAUDE_PROJECT_DIR || process.env.CLAUDECODE) {
    return 'claude-code';
  }
  if (process.env.OPENCODE_SESSION || process.env.OPENCODE_HOME) {
    return 'opencode';
  }
  return 'cli';
}

export function findProjectConfigFile(startDir: string = process.cwd()): string | null {
  try {
    let curr = path.resolve(startDir);
    while (true) {
      const agentsPath = path.join(curr, '.agents', 'jev.json');
      if (fsSync.existsSync(agentsPath)) return agentsPath;
      const rootPath = path.join(curr, '.jev.json');
      if (fsSync.existsSync(rootPath)) return rootPath;

      const parent = path.dirname(curr);
      if (parent === curr) break;
      curr = parent;
    }
  } catch {
    // ignore access issues
  }
  return null;
}

export function normalizeTier(rawTier: string): CognitiveTier {
  const t = (rawTier || '').toLowerCase().trim();
  if (t === 'free' || t === 'openrouter_free' || t.includes('free')) return 'free';
  if (t === 'cheap' || t === 'flash_lite' || t.includes('haiku') || t.includes('lite') || t.includes('cheap')) return 'cheap';
  if (t === 'deep' || t === 'pro' || t.includes('opus') || t.includes('expensive') || t.includes('deep') || t === 'pro' || /(?:^|[-_/:])pro(?:$|[-_/:])/.test(t)) return 'deep';
  if (t === 'standard' || t === 'flash' || t.includes('sonnet') || t.includes('standard') || t.includes('flash')) return 'standard';
  return 'standard';
}

let cachedConfig: { at: number; data: JevConfig; dirKey: string } | null = null;
const CONFIG_CACHE_TTL_MS = 2000;

export function clearConfigCacheForTesting(): void {
  cachedConfig = null;
}

/**
 * Strict allowlist for untrusted project configs (.agents/jev.json, .jev.json).
 * May ONLY configure:
 * - timeouts (number values)
 * - tiers (tier hints: description, concurrencyCap, requiresApproval)
 * Explicitly strips/ignores all security-critical keys:
 * - defaults.mode, harnesses.*.mode (cannot turn mode on/off)
 * - models (cannot redirect decision/router/fallback prompts)
 * - freePool (cannot inject paid models into free pool)
 * - cache.* (cannot redirect cache paths or overwrite files)
 * - telemetry.*
 */
export function sanitizeProjectConfig(parsed: any): Partial<JevConfig> {
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return {};
  }

  const sanitized: Partial<JevConfig> = {};

  // 1. Timeouts (number values only)
  if (parsed.timeouts && typeof parsed.timeouts === 'object' && !Array.isArray(parsed.timeouts)) {
    const timeouts: Partial<JevTimeoutsConfig> = {};
    for (const key of ['decisionMs', 'fallbackMs', 'freeModelMs', 'freeExecutionMs'] as const) {
      if (typeof parsed.timeouts[key] === 'number' && Number.isFinite(parsed.timeouts[key]) && parsed.timeouts[key] > 0) {
        timeouts[key] = parsed.timeouts[key];
      }
    }
    if (Object.keys(timeouts).length > 0) {
      sanitized.timeouts = timeouts;
    }
  }

  // 2. Tiers (tier hints only: description, concurrencyCap, requiresApproval)
  if (parsed.tiers && typeof parsed.tiers === 'object' && !Array.isArray(parsed.tiers)) {
    const tiers: JevTiersConfig = {} as any;
    for (const [tierName, tierVal] of Object.entries(parsed.tiers)) {
      if (tierVal && typeof tierVal === 'object' && !Array.isArray(tierVal)) {
        const tVal = tierVal as any;
        const entry: { description: string; concurrencyCap?: number; requiresApproval?: boolean } = {
          description: typeof tVal.description === 'string' ? tVal.description : ''
        };
        if (typeof tVal.concurrencyCap === 'number' && Number.isFinite(tVal.concurrencyCap) && tVal.concurrencyCap > 0) {
          entry.concurrencyCap = tVal.concurrencyCap;
        }
        if (typeof tVal.requiresApproval === 'boolean') {
          entry.requiresApproval = tVal.requiresApproval;
        }
        tiers[tierName] = entry;
      }
    }
    if (Object.keys(tiers).length > 0) {
      sanitized.tiers = tiers;
    }
  }

  return sanitized;
}

export function loadJevConfigSync(startDir?: string, bypassCache = false): JevConfig {
  const now = Date.now();
  const dirKey = startDir ? path.resolve(startDir) : '';
  if (!bypassCache && cachedConfig && cachedConfig.dirKey === dirKey && (now - cachedConfig.at < CONFIG_CACHE_TTL_MS)) {
    return cachedConfig.data;
  }

  // Level 1: System Defaults
  let config: JevConfig = JSON.parse(JSON.stringify(DEFAULT_JEV_CONFIG));

  // Level 2: User Config (~/.config/jev/config.json or process.env.JEV_CONFIG_FILE)
  const userConfigFile = process.env.JEV_CONFIG_FILE || JEV_CONFIG_FILE;
  try {
    if (fsSync.existsSync(userConfigFile)) {
      const parsed = JSON.parse(fsSync.readFileSync(userConfigFile, 'utf8'));
      if (parsed && typeof parsed === 'object') {
        config = deepMergeWithArrayReplacement(config, parsed);
        if (typeof parsed.version === 'number') {
          config.version = parsed.version;
        }

        // Mode inheritance: if user config specifies defaults.mode, but omits harnesses[h].mode,
        // the harness MUST inherit from defaults.mode.
        if (parsed.defaults && parsed.defaults.mode) {
          const userDefaultMode = parsed.defaults.mode as JevMode;
          if (config.harnesses) {
            for (const h of Object.keys(config.harnesses) as JevHarness[]) {
              if (!parsed.harnesses?.[h]?.mode) {
                if (!config.harnesses[h]) config.harnesses[h] = {} as any;
                config.harnesses[h]!.mode = userDefaultMode;
              }
            }
          }
        }
      }
    }
  } catch {
    // Return default on error
  }

  // Level 3: Project Config (.agents/jev.json primary, .jev.json fallback) - UNTRUSTED INPUT
  const projectConfigFile = findProjectConfigFile(startDir);
  if (projectConfigFile) {
    try {
      if (fsSync.existsSync(projectConfigFile)) {
        const parsed = JSON.parse(fsSync.readFileSync(projectConfigFile, 'utf8'));
        if (parsed && typeof parsed === 'object') {
          const sanitized = sanitizeProjectConfig(parsed);
          config = deepMergeWithArrayReplacement(config, sanitized);
        }
      }
    } catch {
      // Keep on error
    }
  }

  cachedConfig = { at: now, data: config, dirKey };
  return config;
}

export async function loadJevConfig(startDir?: string): Promise<JevConfig> {
  return loadJevConfigSync(startDir);
}

export function getHarnessConfig(harness?: JevHarness, startDir?: string): HarnessConfig {
  const targetHarness = harness || detectActiveHarness();
  const cfg = loadJevConfigSync(startDir);
  const overrides = cfg.harnesses?.[targetHarness] || {};
  const mode = (overrides.mode || cfg.defaults.mode || 'off') as JevMode;
  const requireUpgradeApproval =
    typeof overrides.requireUpgradeApproval === 'boolean'
      ? overrides.requireUpgradeApproval
      : cfg.defaults.requireUpgradeApproval ?? true;
  const availableSubagents =
    Array.isArray(overrides.availableSubagents) && overrides.availableSubagents.length
      ? overrides.availableSubagents
      : cfg.defaults.availableSubagents;
  const agentPaths =
    Array.isArray(overrides.agentPaths) && overrides.agentPaths.length
      ? overrides.agentPaths
      : cfg.defaults.agentPaths;

  return {
    mode,
    models: overrides.models,
    freePool: overrides.freePool,
    routing: overrides.routing,
    requireUpgradeApproval,
    availableSubagents,
    agentPaths
  };
}

export function getHarnessModels(harness?: JevHarness, startDir?: string): HarnessModelMap {
  const targetHarness = harness || detectActiveHarness();
  const cfg = loadJevConfigSync(startDir);
  const defaultModels: HarnessModelMap =
    DEFAULT_JEV_CONFIG.harnesses?.[targetHarness]?.models as HarnessModelMap ||
    DEFAULT_JEV_CONFIG.harnesses?.cli?.models as HarnessModelMap || {
      free: 'openrouter/free',
      cheap: 'google/gemma-4-31b-it:free',
      standard: 'typesafe/jev-router',
      deep: 'typesafe/jev-router'
    };

  const configuredModels = cfg.harnesses?.[targetHarness]?.models || {};

  const models: HarnessModelMap = {
    free: configuredModels.free || defaultModels.free,
    cheap: configuredModels.cheap || defaultModels.cheap,
    standard: configuredModels.standard || defaultModels.standard,
    deep: configuredModels.deep || defaultModels.deep
  };

  // Level 5 runtime env override
  if (process.env.JEV_FREE_MODEL) {
    models.free = process.env.JEV_FREE_MODEL;
  }

  return models;
}

export function getFreeModelsPool(harness?: JevHarness, startDir?: string): string[] {
  // Level 5 runtime override
  if (process.env.JEV_FREE_MODELS_POOL) {
    const list = process.env.JEV_FREE_MODELS_POOL.split(',').map((s) => s.trim()).filter(Boolean);
    if (list.length > 0) return list;
  }

  const targetHarness = harness || detectActiveHarness();
  const cfg = loadJevConfigSync(startDir);
  const harnessPool = cfg.harnesses?.[targetHarness]?.freePool;
  if (Array.isArray(harnessPool) && harnessPool.length > 0) {
    return [...harnessPool];
  }

  const defaultPool = DEFAULT_JEV_CONFIG.harnesses?.[targetHarness]?.freePool;
  if (Array.isArray(defaultPool) && defaultPool.length > 0) {
    return [...defaultPool];
  }

  const antigravityPool = DEFAULT_JEV_CONFIG.harnesses?.antigravity?.freePool;
  if (Array.isArray(antigravityPool) && antigravityPool.length > 0) {
    return [...antigravityPool];
  }

  return ['openrouter/free'];
}

export function getJevTimeouts(harness?: JevHarness, startDir?: string): JevTimeoutsConfig {
  const cfg = loadJevConfigSync(startDir);
  const def = DEFAULT_JEV_CONFIG.timeouts!;
  const configured = cfg.timeouts || {};

  const decisionMs = Number(process.env.JEV_TIMEOUT_MS) || configured.decisionMs || def.decisionMs || 3500;
  const fallbackMs = Number(process.env.JEV_FALLBACK_TIMEOUT_MS) || configured.fallbackMs || def.fallbackMs || 5000;
  const freeModelMs = Number(process.env.JEV_FREE_MODEL_TIMEOUT_MS) || configured.freeModelMs || def.freeModelMs || 8000;
  const freeExecutionMs = Number(process.env.JEV_FREE_EXEC_TIMEOUT_MS) || configured.freeExecutionMs || def.freeExecutionMs || 25000;

  return {
    decisionMs,
    fallbackMs,
    freeModelMs,
    freeExecutionMs
  };
}

export function getCacheConfig(startDir?: string): JevCacheConfig {
  const cfg = loadJevConfigSync(startDir);
  const def = DEFAULT_JEV_CONFIG.cache || {
    ttlSec: 300,
    shmPath: '/dev/shm/jev/cache.sqlite',
    fallbackPath: path.join(os.homedir(), '.config', 'jev', 'decision-cache.json')
  };
  const configured = cfg.cache || {};

  const ttlSec = process.env.JEV_CACHE_TTL_MS
    ? Math.max(1, Math.round(Number(process.env.JEV_CACHE_TTL_MS) / 1000))
    : (typeof configured.ttlSec === 'number' ? configured.ttlSec : (def.ttlSec ?? 300));

  const defaultFallback = path.join(os.homedir(), '.config', 'jev', 'decision-cache.json');
  let rawFallback = typeof configured.fallbackPath === 'string' && configured.fallbackPath.trim() !== ''
    ? configured.fallbackPath
    : (typeof def.fallbackPath === 'string' && def.fallbackPath.trim() !== '' ? def.fallbackPath : defaultFallback);

  if (typeof rawFallback !== 'string') {
    rawFallback = defaultFallback;
  }

  const fallbackPath = rawFallback.startsWith('~/')
    ? path.join(os.homedir(), rawFallback.slice(2))
    : rawFallback;

  const shmPath = typeof configured.shmPath === 'string' && configured.shmPath.trim() !== ''
    ? configured.shmPath
    : (typeof def.shmPath === 'string' ? def.shmPath : '/dev/shm/jev/cache.sqlite');

  return {
    ttlSec,
    shmPath,
    fallbackPath
  };
}

export function readJevModeSync(harness?: JevHarness, startDir?: string): JevMode {
  const targetHarness = harness || detectActiveHarness();
  return getHarnessConfig(targetHarness, startDir).mode;
}

export async function readJevMode(harness?: JevHarness, startDir?: string): Promise<JevMode> {
  return readJevModeSync(harness, startDir);
}

export async function writeModeState(mode: JevMode, harness?: string): Promise<string> {
  const configFile = process.env.JEV_CONFIG_FILE || path.join(os.homedir(), '.config', 'jev', 'config.json');
  const dir = path.dirname(configFile);
  await fs.mkdir(dir, { recursive: true });

  let cfg: any = {};
  if (fsSync.existsSync(configFile)) {
    try {
      cfg = JSON.parse(await fs.readFile(configFile, 'utf8'));
    } catch {
      cfg = {};
    }
  }
  if (!cfg || typeof cfg !== 'object' || Array.isArray(cfg)) {
    cfg = {};
  }
  if (!cfg.harnesses) cfg.harnesses = {};
  if (!cfg.defaults) cfg.defaults = {};

  if (harness) {
    if (!cfg.harnesses[harness]) cfg.harnesses[harness] = {};
    cfg.harnesses[harness].mode = mode;
  } else {
    cfg.defaults.mode = mode;
    if (cfg.harnesses && typeof cfg.harnesses === 'object') {
      for (const h of Object.keys(cfg.harnesses)) {
        if (cfg.harnesses[h] && typeof cfg.harnesses[h] === 'object') {
          cfg.harnesses[h].mode = mode;
        }
      }
    }
  }

  const updatedAt = new Date().toISOString();
  await fs.writeFile(configFile, JSON.stringify(cfg, null, 2) + '\n', 'utf8');
  clearConfigCacheForTesting();
  return updatedAt;
}
