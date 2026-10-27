import * as fsSync from 'node:fs';
import * as path from 'node:path';
import * as os from 'node:os';
import {
  callJev,
  ensureSecretsLoaded,
  JEV_MODEL
} from './client.js';
import {
  computeDecisionCacheKey,
  getCachedDecision,
  setCachedDecision
} from './cache.js';
import { JEV_CONFIG_FILE } from './config.js';
import { getTracer } from '../tracing.js';

// ── Antigravity quota view ───────────────────────────────────────────────────
export const QUOTA_STATE_FILE = path.join(os.homedir(), '.gemini', 'antigravity-cli', 'quota-state.json');
export const QUOTA_LEGACY_FILE = '/tmp/antigravity-quota.json';

export const DEFAULT_5H_NORMAL_MIN = 0.30;
export const DEFAULT_5H_CONSERVE_MIN = 0.10;
export const DEFAULT_WEEKLY_NORMAL_MIN = 0.20;
export const DEFAULT_WEEKLY_CONSERVE_MIN = 0.10;

export const QUOTA_NORMAL_MIN = DEFAULT_5H_NORMAL_MIN;
export const QUOTA_CONSERVE_MIN = DEFAULT_5H_CONSERVE_MIN;
export const QUOTA_5H_NORMAL_MIN = DEFAULT_5H_NORMAL_MIN;
export const QUOTA_5H_CONSERVE_MIN = DEFAULT_5H_CONSERVE_MIN;
export const QUOTA_WEEKLY_NORMAL_MIN = DEFAULT_WEEKLY_NORMAL_MIN;
export const QUOTA_WEEKLY_CONSERVE_MIN = DEFAULT_WEEKLY_CONSERVE_MIN;
export const QUOTA_STALE_SECONDS = Number(process.env.JEV_QUOTA_STALE_SECONDS || 15 * 60);

export interface QuotaThresholds {
  normal_min: number;
  conserve_min: number;
  five_hour_normal_min: number;
  five_hour_conserve_min: number;
  weekly_normal_min: number;
  weekly_conserve_min: number;
}

export function getQuotaThresholds(): QuotaThresholds {
  ensureSecretsLoaded();

  let fileConfig: any = {};
  try {
    if (fsSync.existsSync(JEV_CONFIG_FILE)) {
      fileConfig = JSON.parse(fsSync.readFileSync(JEV_CONFIG_FILE, 'utf8'));
    }
  } catch {
    // ignore
  }

  const fileThresholds = fileConfig.thresholds || fileConfig.quota || {};

  const parseNum = (val: any): number | undefined => {
    if (typeof val === 'number' && !Number.isNaN(val)) return val;
    if (typeof val === 'string' && val.trim() !== '') {
      const n = Number(val);
      if (!Number.isNaN(n)) return n;
    }
    return undefined;
  };

  const fiveHourNormalMin =
    parseNum(process.env.JEV_QUOTA_5H_NORMAL_MIN) ??
    parseNum(process.env.JEV_QUOTA_5H_THRESHOLD) ??
    parseNum(fileThresholds.five_hour_normal_min) ??
    parseNum(fileThresholds.quota_5h_threshold) ??
    parseNum(fileThresholds['5h_normal_min']) ??
    parseNum(fileThresholds['5h']) ??
    parseNum(process.env.JEV_QUOTA_NORMAL_MIN) ??
    DEFAULT_5H_NORMAL_MIN;

  const fiveHourConserveMin =
    parseNum(process.env.JEV_QUOTA_5H_CONSERVE_MIN) ??
    parseNum(process.env.JEV_QUOTA_5H_CRITICAL) ??
    parseNum(fileThresholds.five_hour_conserve_min) ??
    parseNum(fileThresholds.quota_5h_critical) ??
    parseNum(fileThresholds['5h_conserve_min']) ??
    parseNum(process.env.JEV_QUOTA_CONSERVE_MIN) ??
    DEFAULT_5H_CONSERVE_MIN;

  const weeklyNormalMin =
    parseNum(process.env.JEV_QUOTA_WEEKLY_NORMAL_MIN) ??
    parseNum(process.env.JEV_QUOTA_WEEKLY_THRESHOLD) ??
    parseNum(process.env.JEV_QUOTA_7D_NORMAL_MIN) ??
    parseNum(process.env.JEV_QUOTA_7D_THRESHOLD) ??
    parseNum(fileThresholds.weekly_normal_min) ??
    parseNum(fileThresholds.seven_day_normal_min) ??
    parseNum(fileThresholds['7d_normal_min']) ??
    parseNum(fileThresholds.quota_weekly_threshold) ??
    parseNum(fileThresholds.quota_7d_threshold) ??
    parseNum(fileThresholds['weekly_normal_min']) ??
    parseNum(fileThresholds['weekly']) ??
    parseNum(fileThresholds['7d']) ??
    DEFAULT_WEEKLY_NORMAL_MIN;

  const weeklyConserveMin =
    parseNum(process.env.JEV_QUOTA_WEEKLY_CONSERVE_MIN) ??
    parseNum(process.env.JEV_QUOTA_WEEKLY_CRITICAL) ??
    parseNum(process.env.JEV_QUOTA_7D_CONSERVE_MIN) ??
    parseNum(process.env.JEV_QUOTA_7D_CRITICAL) ??
    parseNum(fileThresholds.weekly_conserve_min) ??
    parseNum(fileThresholds.seven_day_conserve_min) ??
    parseNum(fileThresholds['7d_conserve_min']) ??
    parseNum(fileThresholds.quota_weekly_critical) ??
    parseNum(fileThresholds.quota_7d_critical) ??
    parseNum(fileThresholds['weekly_conserve_min']) ??
    DEFAULT_WEEKLY_CONSERVE_MIN;

  return {
    normal_min: fiveHourNormalMin,
    conserve_min: fiveHourConserveMin,
    five_hour_normal_min: fiveHourNormalMin,
    five_hour_conserve_min: fiveHourConserveMin,
    weekly_normal_min: weeklyNormalMin,
    weekly_conserve_min: weeklyConserveMin
  };
}

export interface QuotaBucket {
  remaining_fraction: number;
  used_fraction: number;
  used_pct: number;
  reset_at: number | null;
  reset_in_seconds: number | null;
  effective_remaining_fraction: number;
  refreshed: boolean;
}

export type QuotaStrategy = 'normal' | 'conserve' | 'free_only' | 'unknown';

export interface QuotaView {
  source: string;
  schema: number;
  stale: boolean;
  updated_at: number | null;
  plan_tier?: string;
  active_model?: string;
  buckets: Record<string, QuotaBucket>;
  gemini: { five_hour: number | null; weekly: number | null; min_remaining: number | null };
  strategy: QuotaStrategy;
  thresholds: QuotaThresholds;
  geminiRemainingFraction: number | null;
  /** Pool backing the currently selected model (Gemini buckets for Gemini models, 3p buckets otherwise). */
  active: { pool: 'gemini' | '3p'; five_hour: number | null; weekly: number | null; min_remaining: number | null; strategy: QuotaStrategy };
}

export function evaluatePoolStrategy(
  fiveHour: number | null,
  weekly: number | null,
  t: QuotaThresholds
): QuotaStrategy {
  if (fiveHour === null && weekly === null) return 'unknown';

  const is5hCritical = fiveHour !== null && fiveHour <= t.five_hour_conserve_min;
  const isWeeklyCritical = weekly !== null && weekly <= t.weekly_conserve_min;
  if (is5hCritical || isWeeklyCritical) {
    return 'free_only';
  }

  const is5hConserve = fiveHour !== null && fiveHour <= t.five_hour_normal_min;
  const isWeeklyConserve = weekly !== null && weekly <= t.weekly_normal_min;
  if (is5hConserve || isWeeklyConserve) {
    return 'conserve';
  }

  return 'normal';
}

export function normaliseBucket(nowSec: number, raw: any): QuotaBucket | null {
  if (!raw || typeof raw.remaining_fraction !== 'number') return null;
  const rem = raw.remaining_fraction;
  const resetAt =
    typeof raw.reset_at === 'number' ? raw.reset_at
    : typeof raw.quota_resets_at === 'number' ? raw.quota_resets_at
    : typeof raw.reset_in_seconds === 'number' ? nowSec + raw.reset_in_seconds
    : null;
  const refreshed = resetAt !== null && nowSec >= resetAt;
  return {
    remaining_fraction: rem,
    used_fraction: typeof raw.used_fraction === 'number' ? raw.used_fraction : 1 - rem,
    used_pct: typeof raw.used_pct === 'number' ? raw.used_pct : Math.round((1 - rem) * 1000) / 10,
    reset_at: resetAt,
    reset_in_seconds: typeof raw.reset_in_seconds === 'number' ? raw.reset_in_seconds : resetAt !== null ? resetAt - nowSec : null,
    effective_remaining_fraction: refreshed ? 1 : rem,
    refreshed
  };
}

/**
 * Read the multi-bucket quota state. Prefers schema v2, falls back to legacy single-bucket.
 */
export async function readQuotaView(): Promise<QuotaView> {
  const nowSec = Math.floor(Date.now() / 1000);
  let source = 'none';
  let schema = 0;
  let updatedAt: number | null = null;
  let planTier: string | undefined;
  let activeModel: string | undefined;
  let rawBuckets: Record<string, any> = {};

  const tryParse = (file: string): any | null => {
    try {
      if (fsSync.existsSync(file)) return JSON.parse(fsSync.readFileSync(file, 'utf8'));
    } catch {
      // ignore
    }
    return null;
  };

  const v2 = tryParse(QUOTA_STATE_FILE);
  if (v2 && typeof v2 === 'object') {
    if (v2.buckets && typeof v2.buckets === 'object') {
      source = QUOTA_STATE_FILE; schema = Number(v2.schema || 2); updatedAt = v2.updated_at ?? null;
      planTier = v2.plan_tier; activeModel = v2.active_model; rawBuckets = v2.buckets;
    } else if (typeof v2.remaining_fraction === 'number') {
      source = QUOTA_STATE_FILE; schema = 1; updatedAt = v2.updated_at ?? null;
      rawBuckets = { 'gemini-5h': v2 };
    }
  }

  if (schema === 0) {
    const legacy = tryParse(QUOTA_LEGACY_FILE);
    if (legacy && typeof legacy.remaining_fraction === 'number') {
      source = QUOTA_LEGACY_FILE; schema = 1; updatedAt = legacy.updated_at ?? null;
      rawBuckets = { 'gemini-5h': legacy };
    }
  }

  const buckets: Record<string, QuotaBucket> = {};
  for (const [k, v] of Object.entries(rawBuckets)) {
    const b = normaliseBucket(nowSec, v);
    if (b) buckets[k] = b;
  }

  const geminiFive = buckets['gemini-5h']?.effective_remaining_fraction ?? null;
  const geminiWeekly = buckets['gemini-weekly']?.effective_remaining_fraction ?? null;
  const p3Five = buckets['3p-5h']?.effective_remaining_fraction ?? null;
  const p3Weekly = buckets['3p-weekly']?.effective_remaining_fraction ?? null;

  const geminiCandidates = [geminiFive, geminiWeekly].filter((x): x is number => typeof x === 'number');
  const p3Candidates = [p3Five, p3Weekly].filter((x): x is number => typeof x === 'number');
  const geminiMin = geminiCandidates.length ? Math.min(...geminiCandidates) : null;
  const p3Min = p3Candidates.length ? Math.min(...p3Candidates) : null;

  const availablePools = [geminiMin, p3Min].filter((x): x is number => typeof x === 'number');
  const minRemaining = availablePools.length ? Math.max(...availablePools) : null;

  const thresholds = getQuotaThresholds();
  const geminiStrategy = evaluatePoolStrategy(geminiFive, geminiWeekly, thresholds);
  const p3Strategy = evaluatePoolStrategy(p3Five, p3Weekly, thresholds);

  let strategy: QuotaStrategy = 'unknown';
  if (geminiStrategy !== 'unknown' && p3Strategy !== 'unknown') {
    const rank = (s: QuotaStrategy) => s === 'normal' ? 3 : s === 'conserve' ? 2 : s === 'free_only' ? 1 : 0;
    strategy = rank(geminiStrategy) >= rank(p3Strategy) ? geminiStrategy : p3Strategy;
  } else if (geminiStrategy !== 'unknown') {
    strategy = geminiStrategy;
  } else if (p3Strategy !== 'unknown') {
    strategy = p3Strategy;
  }

  const stale = updatedAt === null || nowSec - updatedAt > QUOTA_STALE_SECONDS;

  // The selected model decides which pool is spent: non-Gemini models draw on the 3p buckets.
  const isGeminiActive = !activeModel || /gemini/i.test(activeModel) || p3Min === null;
  const active = isGeminiActive
    ? { pool: 'gemini' as const, five_hour: geminiFive, weekly: geminiWeekly, min_remaining: geminiMin ?? minRemaining, strategy: geminiStrategy }
    : { pool: '3p' as const, five_hour: p3Five, weekly: p3Weekly, min_remaining: p3Min, strategy: p3Strategy };

  return {
    source,
    schema,
    stale,
    updated_at: updatedAt,
    plan_tier: planTier,
    active_model: activeModel,
    buckets,
    gemini: { five_hour: geminiFive, weekly: geminiWeekly, min_remaining: geminiMin ?? minRemaining },
    strategy,
    thresholds,
    geminiRemainingFraction: geminiMin ?? minRemaining,
    active
  };
}

export function quotaRecommendation(strategy: string): string {
  switch (strategy) {
    case 'free_only':
      return 'Gemini quota critically low. Route non-trivial generation to openrouter/free via jev_execute_free/jev_agent_free; avoid pro/flash tiers.';
    case 'conserve':
      return 'Gemini quota limited. Prefer flash_lite and free-tier offloads; reserve flash/pro for high-value work.';
    case 'normal':
      return 'Gemini quota healthy. Normal tier selection applies.';
    default:
      return 'Quota state unknown or stale. Assume limited: prefer free-tier offloads until the statusline refreshes.';
  }
}

// ── Model Tier Ranking & Clamping ────────────────────────────────────────────
export function modelTierRank(tierOrModel: string): number {
  const t = (tierOrModel || '').toLowerCase();
  if (t.includes('free') || t === 'openrouter_free') return 0;
  if (t.includes('flash_lite') || t.includes('flash-lite')) return 1;
  if (t.includes('flash') || t.includes('cheap') || t.includes('standard')) return 2;
  if (t.includes('pro') || t.includes('opus') || t.includes('expensive')) return 3;
  return 2;
}

export function rankToTierName(rank: number): 'openrouter_free' | 'flash_lite' | 'flash' | 'pro' {
  if (rank <= 0) return 'openrouter_free';
  if (rank === 1) return 'flash_lite';
  if (rank === 2) return 'flash';
  return 'pro';
}

export function clampTierToSessionCeiling(
  recommendedTier: string,
  activeModel?: string,
  promptText: string = ''
): { clampedTier: string; wasClamped: boolean; ceilingTier: string } {
  if (!activeModel) {
    return { clampedTier: recommendedTier, wasClamped: false, ceilingTier: 'pro' };
  }

  const text = promptText.toLowerCase();
  const explicitProOverride =
    text.includes('deep reasoning') ||
    text.includes('cross-system architecture') ||
    text.includes('manual pro approval') ||
    text.includes('claude opus') ||
    text.includes('gemini pro');

  const ceilingRank = modelTierRank(activeModel);
  const ceilingTier = rankToTierName(ceilingRank);
  const recRank = modelTierRank(recommendedTier);

  if (recRank > ceilingRank && !explicitProOverride) {
    return {
      clampedTier: ceilingTier,
      wasClamped: true,
      ceilingTier
    };
  }

  return {
    clampedTier: recommendedTier,
    wasClamped: false,
    ceilingTier
  };
}

// ── Thinking Effort Governance ───────────────────────────────────────────────
export type ThinkingEffort = 'low' | 'medium' | 'high';

export function defaultEffortForSubagent(subagent: string, promptText: string = ''): ThinkingEffort {
  const sa = (subagent || '').toLowerCase();
  const text = (promptText || '').toLowerCase();
  const hasWord = (pattern: RegExp): boolean => pattern.test(text);

  // High effort keywords / roles
  if (
    sa.includes('architect') ||
    sa.includes('security') ||
    sa.includes('auditor') ||
    hasWord(/\barchitecture\b/) ||
    hasWord(/\bsecurity audit\b/) ||
    hasWord(/\brfc design\b/)
  ) {
    return 'high';
  }

  // Low effort mechanical roles / keywords
  if (
    sa.includes('inbox') ||
    sa.includes('processor') ||
    sa.includes('format') ||
    sa.includes('scribe') ||
    hasWord(/\b(re)?format(s|ted|ting|ter|ters)?\b/) ||
    hasWord(/\bfile into\b/) ||
    hasWord(/\bcommit message\b/)
  ) {
    return 'low';
  }

  // Bounded deterministic ports
  if (
    hasWord(/\bport(ing)?\b/) &&
    (hasWord(/\brust\b/) || hasWord(/\bpython\b/) || hasWord(/\bcli\b/))
  ) {
    return 'medium';
  }

  return 'medium';
}

export function applyEffortGovernance(effort: ThinkingEffort, quotaStrategy: string, targetTier: string = ''): ThinkingEffort {
  const isFree = targetTier.toLowerCase().includes('free');
  if (quotaStrategy === 'free_only') {
    if (isFree) {
      return effort;
    }
    return 'low';
  }
  if (quotaStrategy === 'conserve' && effort === 'high') {
    return 'medium';
  }
  return effort;
}

// ── Tier Cost Estimation ────────────────────────────────────────────────────
export function estimateTierCost(tier: string): number {
  const t = (tier || '').toLowerCase();
  if (t === 'pro' || t === 'tier_3_deep' || t === 'expensive') {
    return 0.015;
  }
  if (t === 'flash' || t === 'tier_2_standard' || t === 'standard') {
    return 0.001;
  }
  if (t === 'flash_lite' || t === 'tier_1_cheap' || t === 'cheap') {
    return 0.0001;
  }
  return 0.0;
}

// ── Cost-Tiered Model Selection ─────────────────────────────────────────────
export type ModelTier = 'free' | 'cheap' | 'standard' | 'expensive' | string;

export interface ModelCandidate {
  id: string;
  tier: ModelTier;
  providerModel: string;
  costInPerMTok: number;
  costOutPerMTok: number;
  label?: string;
}

export interface ScoredOption extends ModelCandidate {
  prob: number;
  suitable: boolean;
  costScore: number;
}

export type ModelRecommendation = 'cheapest_suitable' | 'ask_user' | 'unknown';

export interface ModelSelection {
  options: ScoredOption[];
  cheapestOptionId: string;
  cheapestSuitableOptionId: string | null;
  recommendedOptionId: string | null;
  expensiveOptionIds: string[];
  upgradeOptionId: string | null;
  recommendation: ModelRecommendation;
  cheapestSufficient: boolean;
  cheapestSufficientProb: number;
  quotaStrategy: string;
  recommendedEffort?: ThinkingEffort;
  confidence: number;
  modelUsed: string;
  fallbackUsed: boolean;
  error?: string;
}

export const JEV_SELECT_NOUL_MIN = Number(process.env.JEV_SELECT_NOUL_MIN || 0.5);
const EXPENSIVE_ABOVE_USD_PER_MTOK = Number(process.env.JEV_SELECT_EXPENSIVE_ABOVE || 20);
const ASK_ABOVE_EXPENSIVE_PROB = Number(process.env.JEV_SELECT_ASK_EXPENSIVE_PROB || 0.35);

export const DEFAULT_MODEL_CANDIDATES: ModelCandidate[] = [
  { id: 'free', tier: 'free', providerModel: 'openrouter/free', costInPerMTok: 0, costOutPerMTok: 0, label: 'Free tier' },
  { id: 'cheap', tier: 'cheap', providerModel: '(configured cheap tier)', costInPerMTok: 0.15, costOutPerMTok: 0.6, label: 'Cheap tier' },
  { id: 'standard', tier: 'standard', providerModel: '(configured standard tier)', costInPerMTok: 1, costOutPerMTok: 4, label: 'Standard tier' },
  { id: 'expensive', tier: 'expensive', providerModel: '(configured expensive tier)', costInPerMTok: 3, costOutPerMTok: 15, label: 'Expensive tier' }
];

function costScore(c: ModelCandidate): number {
  return (Number(c.costInPerMTok) || 0) + 3 * (Number(c.costOutPerMTok) || 0);
}

function isExpensive(c: ModelCandidate): boolean {
  return c.tier === 'expensive' || costScore(c) > EXPENSIVE_ABOVE_USD_PER_MTOK;
}

export async function selectModel(
  task: string,
  candidates: ModelCandidate[],
  quotaStrategy = 'unknown'
): Promise<ModelSelection> {
  const cacheKey = computeDecisionCacheKey(task, candidates, quotaStrategy);
  const cached = getCachedDecision(cacheKey);
  if (cached) {
    const span = getTracer().startSpan('jev.select_model', {
      'task.length': task.length,
      'candidates.count': candidates.length,
      'quota.strategy': quotaStrategy,
      'decision.cached': true
    });
    span.setAttribute('decision.latency_ms', 0);
    if (cached.recommendedOptionId) span.setAttribute('decision.verdict', cached.recommendedOptionId);
    span.end();
    return cached;
  }

  const startTime = Date.now();
  const span = getTracer().startSpan('jev.select_model', {
    'task.length': task.length,
    'candidates.count': candidates.length,
    'quota.strategy': quotaStrategy,
    'decision.cached': false
  });

  const ranked = [...candidates].sort((a, b) => costScore(a) - costScore(b));
  const cheapest = ranked[0];

  const base = (extra: Partial<ModelSelection>): ModelSelection => ({
    options: ranked.map((c) => ({ ...c, prob: 0, suitable: false, costScore: costScore(c) })),
    cheapestOptionId: cheapest.id,
    cheapestSuitableOptionId: null,
    recommendedOptionId: null,
    expensiveOptionIds: ranked.filter(isExpensive).map((c) => c.id),
    upgradeOptionId: null,
    recommendation: 'unknown',
    cheapestSufficient: false,
    cheapestSufficientProb: 0,
    quotaStrategy,
    confidence: 0,
    modelUsed: JEV_MODEL,
    fallbackUsed: false,
    ...extra
  });

  try {
    const criteria: Record<string, string> = {};
    for (const c of ranked) {
      criteria[c.id] = `${c.label || c.providerModel} — tier ${c.tier}, ~$${c.costInPerMTok}/$${c.costOutPerMTok} per Mtok`;
    }

    const data: any = await callJev(
      `Task: ${task}\n\nQuota strategy for this machine: ${quotaStrategy}. ` +
        `Choose the cheapest model tier that can complete this task to a high standard.`,
      {
        selectedOption: {
          type: 'choice',
          instructions: 'Select the best option balancing task suitability against cost (prefer cheaper).',
          criteria
        },
        cheapestSufficient: {
          type: 'noul',
          instructions:
            `Is the cheapest option ("${cheapest.id}", tier ${cheapest.tier}, ~$${cheapest.costInPerMTok}/$${cheapest.costOutPerMTok} per Mtok) ` +
            `sufficient to complete this task to a high standard?`
        }
      }
    );

    const choiceAnswer = data?.answers?.selectedOption;
    const noulAnswer = data?.answers?.cheapestSufficient;
    const probs: Record<string, number> = choiceAnswer?.probabilities || {};
    const noul = typeof noulAnswer?.noul === 'number' ? noulAnswer.noul : 0.5;
    const cheapestSufficient = noul >= JEV_SELECT_NOUL_MIN;

    let required = cheapest;
    if (!cheapestSufficient) {
      const chosenId = typeof choiceAnswer?.choice === 'string' ? choiceAnswer.choice : cheapest.id;
      required = ranked.find((c) => c.id === chosenId) || cheapest;
      if (costScore(required) < costScore(cheapest)) required = cheapest;
    }

    const options: ScoredOption[] = ranked.map((c) => ({
      ...c,
      prob: probs[c.id] ?? 0,
      suitable: costScore(c) >= costScore(required),
      costScore: costScore(c)
    }));

    const upgrade = options.filter((o) => isExpensive(o)).sort((a, b) => b.prob - a.prob)[0];
    const maxExpensiveProb = upgrade?.prob ?? 0;

    let recommendation: ModelRecommendation = cheapestSufficient ? 'cheapest_suitable'
      : isExpensive(required) ? 'ask_user'
      : 'cheapest_suitable';
    if (!cheapestSufficient && recommendation !== 'ask_user' && maxExpensiveProb >= ASK_ABOVE_EXPENSIVE_PROB) {
      recommendation = 'ask_user';
    }

    const confidence = choiceAnswer?.confidence ?? 0.9;

    const result = base({
      options,
      cheapestSuitableOptionId: required.id,
      recommendedOptionId: required.id,
      upgradeOptionId: upgrade?.id ?? null,
      recommendation,
      cheapestSufficient,
      cheapestSufficientProb: noul,
      confidence: Number.isFinite(confidence) ? confidence : 0.9,
      modelUsed: data?.model || JEV_MODEL,
      fallbackUsed: false
    });

    const latencyMs = Date.now() - startTime;
    span.setAttribute('decision.latency_ms', latencyMs);
    if (result.recommendedOptionId) span.setAttribute('decision.verdict', result.recommendedOptionId);
    if (result.recommendation) span.setAttribute('decision.recommendation', result.recommendation);
    span.setAttribute('decision.cheapest_sufficient', result.cheapestSufficient);
    span.end();

    setCachedDecision(cacheKey, result);
    return result;
  } catch (err: any) {
    const latencyMs = Date.now() - startTime;
    span.setError(err);
    span.setAttribute('decision.latency_ms', latencyMs);
    span.setAttribute('decision.verdict', cheapest?.id || 'cheapest');
    span.end();

    const options: ScoredOption[] = ranked.map((c) => ({
      ...c,
      prob: 0,
      suitable: c.id === cheapest.id,
      costScore: costScore(c)
    }));
    return base({
      options,
      cheapestSuitableOptionId: cheapest.id,
      recommendedOptionId: cheapest.id,
      recommendation: 'cheapest_suitable',
      confidence: 0,
      fallbackUsed: true,
      error: String(err?.message || err)
    });
  }
}
