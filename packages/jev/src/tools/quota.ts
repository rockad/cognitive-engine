import type { Tool } from '@modelcontextprotocol/sdk/types.js';
import {
  readQuotaView,
  quotaRecommendation,
  selectModel,
  DEFAULT_MODEL_CANDIDATES,
  type ModelCandidate,
  logTelemetry
} from '../core/index.js';

export const quotaToolDefinition: Tool = {
  name: 'jev_quota',
  description: 'Report the live Antigravity (Google AI Pro) quota view across all buckets (gemini-5h, gemini-weekly, 3p-5h, 3p-weekly), reset-aware, and return a spending strategy: "normal" | "conserve" | "free_only" | "unknown". Use it to decide whether to keep working on Gemini tiers or offload to openrouter/free. May be stale when Antigravity is closed.',
  inputSchema: {
    type: 'object',
    properties: {
      refresh: {
        type: 'boolean',
        description: 'Reserved: hint that the caller wants a fresh read (the statusline is the only live source; this flag does not force a refresh).'
      }
    }
  }
};

export const selectModelToolDefinition: Tool = {
  name: 'jev_select_model',
  description: 'Cost-tiered model selection. In one ~800ms Jev decision call, returns a ranked set of candidate models (with cost + calibrated probability), the cheapest option that is sufficient for the task, the expensive options, and a recommendation: "cheapest_suitable" (proceed on the cheap option) or "ask_user" (the cheapest sufficient option is expensive — present the options and let the user choose). Cost-first: always prefers cheaper; expensive requires manual approval.',
  inputSchema: {
    type: 'object',
    properties: {
      task: { type: 'string', description: 'The task, prompt, or sub-goal to size up' },
      candidates: {
        type: 'array',
        description: 'Optional cost-annotated candidate models. Each: { id, tier, providerModel, costInPerMTok, costOutPerMTok, label? }. Defaults to four abstract tiers if omitted.',
        items: {
          type: 'object',
          properties: {
            id: { type: 'string' },
            tier: { type: 'string', enum: ['free', 'cheap', 'standard', 'expensive'] },
            providerModel: { type: 'string' },
            costInPerMTok: { type: 'number' },
            costOutPerMTok: { type: 'number' },
            label: { type: 'string' }
          },
          required: ['id', 'providerModel', 'costInPerMTok', 'costOutPerMTok']
        }
      },
      quotaStrategy: {
        type: 'string',
        enum: ['normal', 'conserve', 'free_only', 'unknown'],
        description: 'Optional quota strategy from jev_quota; biases the ladder toward free when quota is tight.'
      }
    },
    required: ['task']
  }
};

export async function handleQuotaTool(_args?: unknown): Promise<{ content: Array<{ type: 'text'; text: string }> }> {
  const view = await readQuotaView();
  const pct = (v: number | null) => (v === null ? null : Math.round(v * 1000) / 10);
  const result = {
    source: view.source,
    schema: view.schema,
    stale: view.stale,
    planTier: view.plan_tier ?? null,
    activeModel: view.active_model ?? null,
    updatedAt: view.updated_at,
    strategy: view.strategy,
    thresholds: view.thresholds,
    gemini: {
      fiveHourRemainingPct: pct(view.gemini.five_hour),
      weeklyRemainingPct: pct(view.gemini.weekly),
      minRemainingPct: pct(view.gemini.min_remaining)
    },
    buckets: Object.fromEntries(
      Object.entries(view.buckets).map(([k, b]) => [k, {
        remainingPct: pct(b.remaining_fraction),
        usedPct: b.used_pct,
        resetInSeconds: b.reset_in_seconds,
        refreshed: b.refreshed
      }])
    ),
    recommendation: quotaRecommendation(view.strategy)
  };

  await logTelemetry({
    tool: 'jev_quota',
    strategy: view.strategy,
    minRemaining: view.gemini.min_remaining,
    stale: view.stale,
    source: view.source
  });

  return {
    content: [{
      type: 'text',
      text: JSON.stringify(result, null, 2)
    }]
  };
}

export async function handleSelectModelTool(args: unknown): Promise<{ content: Array<{ type: 'text'; text: string }> }> {
  const { task, candidates, quotaStrategy } = (args || {}) as {
    task: string;
    candidates?: ModelCandidate[];
    quotaStrategy?: string;
  };
  if (!task) {
    throw new Error('jev_select_model requires "task"');
  }

  const cands = Array.isArray(candidates) && candidates.length ? candidates : DEFAULT_MODEL_CANDIDATES;
  const strategy = quotaStrategy || (await readQuotaView()).strategy;
  const selection = await selectModel(task, cands, strategy);

  await logTelemetry({
    tool: 'jev_select_model',
    recommendation: selection.recommendation,
    recommendedOptionId: selection.recommendedOptionId,
    cheapestSufficient: selection.cheapestSufficient,
    quotaStrategy: strategy,
    confidence: selection.confidence,
    fallbackUsed: selection.fallbackUsed
  });

  return {
    content: [{
      type: 'text',
      text: JSON.stringify(selection, null, 2)
    }]
  };
}
