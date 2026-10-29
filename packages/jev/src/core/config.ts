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

/** Path of the user config the loader reads (JEV_CONFIG_FILE wins over the home default). */
export function jevUserConfigFile(): string {
  return process.env.JEV_CONFIG_FILE || JEV_CONFIG_FILE;
}

export const VALID_JEV_MODES: readonly JevMode[] = ['off', 'on-demand', 'on'];
const KNOWN_HARNESSES: readonly JevHarness[] = ['antigravity', 'claude-code', 'opencode', 'cli'];

export interface JevConfigValidation {
  ok: boolean;
  /** One human-readable line per problem; names the key path and the expected shape, never file contents. */
  problems: string[];
}

function describeType(v: unknown): string {
  if (v === null) return 'null';
  if (Array.isArray(v)) return 'an array';
  return `a ${typeof v}`;
}

/**
 * Whether a parsed user config (~/.config/jev/config.json) can be trusted. Single source of truth
 * for the guard hook and for `jev_get_mode`.
 *
 * Recognized keys are exactly the ones the loader and its getters read; every other key is left
 * alone, since other tools may add their own. A missing key is "unset" and never a problem. Where
 * the loader already merges a value, a wrong type or an unknown mode is what makes the resolved
 * mode untrustworthy, so those are reported; the only rejected key the loader does not read is a
 * top-level `mode`, which is the common mistake for `defaults.mode`.
 */
export function validateJevConfig(raw: unknown): JevConfigValidation {
  const problems: string[] = [];
  if (raw === null || typeof raw !== 'object' || Array.isArray(raw)) {
    problems.push(`the top level must be a JSON object, found ${describeType(raw)}`);
    return { ok: false, problems };
  }
  const cfg = raw as Record<string, unknown>;
  const isObj = (v: unknown): v is Record<string, unknown> => v !== null && typeof v === 'object' && !Array.isArray(v);

  const object = (at: string, v: unknown): Record<string, unknown> | null => {
    if (v === undefined) return null;
    if (isObj(v)) return v;
    problems.push(`${at} must be an object, found ${describeType(v)}`);
    return null;
  };
  const typed = (at: string, v: unknown, type: 'string' | 'number' | 'boolean') => {
    if (v !== undefined && typeof v !== type) problems.push(`${at} must be a ${type}, found ${describeType(v)}`);
  };
  const stringList = (at: string, v: unknown) => {
    if (v === undefined) return;
    if (!Array.isArray(v) || v.some((x) => typeof x !== 'string')) {
      problems.push(`${at} must be an array of strings, found ${Array.isArray(v) ? 'an array with non-string items' : describeType(v)}`);
    }
  };
  const stringMap = (at: string, v: unknown) => {
    const o = object(at, v);
    if (!o) return;
    for (const [k, val] of Object.entries(o)) typed(`${at}.${k}`, val, 'string');
  };
  const mode = (at: string, v: unknown) => {
    if (v === undefined) return;
    if (typeof v !== 'string') {
      problems.push(`${at} must be one of ${VALID_JEV_MODES.join(', ')}, found ${describeType(v)}`);
    } else if (!(VALID_JEV_MODES as readonly string[]).includes(v)) {
      problems.push(`${at} must be one of ${VALID_JEV_MODES.join(', ')}, found ${JSON.stringify(v.slice(0, 40))}`);
    }
  };
  const routing = (at: string, v: unknown, strategy: boolean) => {
    const r = object(at, v);
    if (!r) return;
    if (strategy && r.strategy !== undefined && r.strategy !== 'router' && r.strategy !== 'keyword') {
      problems.push(`${at}.strategy must be "router" or "keyword"`);
    }
    typed(`${at}.swap`, r.swap, 'boolean');
    stringMap(`${at}.models`, r.models);
  };

  if ('mode' in cfg) problems.push("top-level 'mode' is ignored; use defaults.mode");
  typed('version', cfg.version, 'number');

  const defaults = object('defaults', cfg.defaults);
  if (defaults) {
    mode('defaults.mode', defaults.mode);
    typed('defaults.requireUpgradeApproval', defaults.requireUpgradeApproval, 'boolean');
    typed('defaults.swap', defaults.swap, 'boolean');
    stringList('defaults.availableSubagents', defaults.availableSubagents);
    stringList('defaults.agentPaths', defaults.agentPaths);
  }

  routing('routing', cfg.routing, true);
  stringMap('models', cfg.models);

  const tiers = object('tiers', cfg.tiers);
  if (tiers) {
    for (const [name, t] of Object.entries(tiers)) {
      const entry = object(`tiers.${name}`, t);
      if (!entry) continue;
      typed(`tiers.${name}.description`, entry.description, 'string');
      typed(`tiers.${name}.concurrencyCap`, entry.concurrencyCap, 'number');
      typed(`tiers.${name}.requiresApproval`, entry.requiresApproval, 'boolean');
    }
  }

  const timeouts = object('timeouts', cfg.timeouts);
  if (timeouts) for (const k of ['decisionMs', 'fallbackMs', 'freeModelMs', 'freeExecutionMs']) typed(`timeouts.${k}`, timeouts[k], 'number');

  const cache = object('cache', cfg.cache);
  if (cache) {
    typed('cache.ttlSec', cache.ttlSec, 'number');
    typed('cache.shmPath', cache.shmPath, 'string');
    typed('cache.fallbackPath', cache.fallbackPath, 'string');
  }

  const thresholds = object('thresholds', cfg.thresholds);
  if (thresholds) for (const [k, val] of Object.entries(thresholds)) typed(`thresholds.${k}`, val, 'number');

  const harnesses = object('harnesses', cfg.harnesses);
  if (harnesses) {
    for (const h of KNOWN_HARNESSES) {
      const at = `harnesses.${h}`;
      const hc = object(at, harnesses[h]);
      if (!hc) continue;
      mode(`${at}.mode`, hc.mode);
      stringMap(`${at}.models`, hc.models);
      stringList(`${at}.freePool`, hc.freePool);
      routing(`${at}.routing`, hc.routing, false);
      typed(`${at}.requireUpgradeApproval`, hc.requireUpgradeApproval, 'boolean');
      stringList(`${at}.availableSubagents`, hc.availableSubagents);
      stringList(`${at}.agentPaths`, hc.agentPaths);
    }
  }

  return { ok: problems.length === 0, problems };
}

export type JevConfigFileInspection =
  | { state: 'missing'; file: string; problems: [] }
  | { state: 'valid'; file: string; problems: [] }
  | { state: 'malformed'; file: string; problems: string[] };

/** The config file is never legitimately larger than this; a bigger one is malformed, not parsed. */
export const MAX_JEV_CONFIG_BYTES = 1024 * 1024;

/** The filesystem calls the config reader makes, injectable so tests can simulate a file that changes under it. */
export interface JevConfigIo {
  lstat: (file: string) => fsSync.Stats;
  stat: (file: string) => fsSync.Stats;
  readFile: (file: string) => Buffer;
}

const DEFAULT_CONFIG_IO: JevConfigIo = {
  lstat: (f) => fsSync.lstatSync(f),
  stat: (f) => fsSync.statSync(f),
  readFile: (f) => fsSync.readFileSync(f)
};

type ConfigFileRead = { kind: 'missing' } | { kind: 'problem'; problem: string } | { kind: 'text'; text: string };

/**
 * Reads the config file once, defensively: lstat first so a dangling symlink is not mistaken for
 * "no file", a size check before and after the read, and a regular-file check so a FIFO or
 * directory is never read. A path with no entry at all is `missing`.
 */
function readConfigFile(file: string, io: JevConfigIo): ConfigFileRead {
  let isLink = false;
  try {
    isLink = io.lstat(file).isSymbolicLink();
  } catch (err) {
    if ((err as NodeJS.ErrnoException)?.code === 'ENOENT') return { kind: 'missing' };
    const code = (err as NodeJS.ErrnoException)?.code;
    return { kind: 'problem', problem: `the file could not be read${code ? ` (${code})` : ''}` };
  }
  let size: number;
  try {
    const st = io.stat(file);
    if (!st.isFile()) return { kind: 'problem', problem: 'the file could not be read (not a regular file)' };
    size = st.size;
  } catch (err) {
    const code = (err as NodeJS.ErrnoException)?.code;
    if (isLink) return { kind: 'problem', problem: 'broken symlink: the target does not exist or cannot be read' };
    return { kind: 'problem', problem: `the file could not be read${code ? ` (${code})` : ''}` };
  }
  if (size > MAX_JEV_CONFIG_BYTES) return { kind: 'problem', problem: 'config is over the 1 MB limit' };
  let bytes: Buffer;
  try {
    bytes = io.readFile(file);
  } catch (err) {
    const code = (err as NodeJS.ErrnoException)?.code;
    return { kind: 'problem', problem: `the file could not be read${code ? ` (${code})` : ''}` };
  }
  if (bytes.length > MAX_JEV_CONFIG_BYTES) return { kind: 'problem', problem: 'config is over the 1 MB limit' };
  return { kind: 'text', text: bytes.toString('utf8') };
}

/**
 * Reads and validates the user config file. `missing` means no file at all (the loader then uses
 * its defaults), except when JEV_CONFIG_FILE names the path: pointing at a file that is not there
 * is a mistake, not "unset". `malformed` means the file is there but cannot be trusted:
 * unreadable, a broken symlink, over the size limit, not valid JSON, or rejected by
 * {@link validateJevConfig}.
 */
export function inspectJevConfigFile(file: string = jevUserConfigFile(), io: JevConfigIo = DEFAULT_CONFIG_IO): JevConfigFileInspection {
  const read = readConfigFile(file, io);
  if (read.kind === 'missing') {
    if (process.env.JEV_CONFIG_FILE && process.env.JEV_CONFIG_FILE === file) {
      return { state: 'malformed', file, problems: ['JEV_CONFIG_FILE points at a file that does not exist'] };
    }
    return { state: 'missing', file, problems: [] };
  }
  if (read.kind === 'problem') return { state: 'malformed', file, problems: [read.problem] };
  const text = read.text;
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return { state: 'malformed', file, problems: ['the file is not valid JSON'] };
  }
  const v = validateJevConfig(parsed);
  return v.ok ? { state: 'valid', file, problems: [] } : { state: 'malformed', file, problems: v.problems };
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
    const read = readConfigFile(userConfigFile, DEFAULT_CONFIG_IO);
    if (read.kind === 'text') {
      const parsed = JSON.parse(read.text);
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
