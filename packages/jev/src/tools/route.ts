import type { Tool } from '@modelcontextprotocol/sdk/types.js';
import {
  callJevDecision,
  callJevRouter,
  JEV_MODEL,
  FALLBACK_MODEL,
  JEV_INPUT_COST_PER_MILLION,
  FALLBACK_INPUT_COST_PER_MILLION,
  logTelemetry
} from '../core/index.js';

export const routeToolDefinition: Tool = {
  name: 'jev_route',
  description: 'Ultra-fast (~800ms) choice selection and classification via OpenRouter native Decisions API (~typesafe/jev-latest). Evaluates candidate options and criteria mapping, returning calibrated probabilities and confidence.',
  inputSchema: {
    type: 'object',
    properties: {
      context: { type: 'string', description: 'The text snippet, note, or prompt to classify' },
      options: { type: 'array', items: { type: 'string' }, description: 'Candidate categories/options to choose from' },
      criteria: {
        type: 'object',
        additionalProperties: { type: 'string' },
        description: 'Optional dictionary mapping option keys to descriptive criteria'
      },
      instruction: { type: 'string', description: 'Optional instruction or question for classification' }
    },
    required: ['context', 'options']
  }
};

export async function handleRouteTool(args: unknown): Promise<{ content: Array<{ type: 'text'; text: string }> }> {
  const start = performance.now();
  const { context, options, criteria, instruction } = (args || {}) as {
    context: string;
    options: string[];
    criteria?: Record<string, string>;
    instruction?: string;
  };

  if (!context || !Array.isArray(options) || options.length === 0) {
    throw new Error('jev_route requires non-empty "context" and "options" array');
  }

  // Build criteria dictionary
  const criteriaMap: Record<string, string> = {};
  for (const opt of options) {
    criteriaMap[opt] = (criteria && criteria[opt]) ? criteria[opt] : opt;
  }

  let chosen: string | null = null;
  let confidence = 1.0;
  let probabilities: Record<string, number> = {};
  let modelUsed = JEV_MODEL;
  let fallbackUsed = false;
  let promptTokens = 0;
  let costUsd = 0;

  // Tier 1: Jev Decisions API
  try {
    const data = await callJevDecision(context, {
      route: {
        type: 'choice',
        instructions: instruction || 'Select the most applicable category',
        criteria: criteriaMap
      }
    });
    const routeAnswer = data.answers?.route;
    chosen = routeAnswer?.choice || options[0];
    confidence = routeAnswer?.confidence ?? 1.0;
    probabilities = routeAnswer?.probabilities ?? {};
    promptTokens = data.usage?.input_tokens ?? 0;
    costUsd = typeof data.usage?.cost === 'number'
      ? data.usage.cost
      : (promptTokens / 1_000_000) * JEV_INPUT_COST_PER_MILLION;
    modelUsed = data.model || JEV_MODEL;
  } catch (primaryErr: any) {
    console.error(`[jev warn] Tier 1 decisions failed: ${primaryErr.message}. Triggering Tier 2 (${FALLBACK_MODEL})...`);

    // Tier 2: Fallback Model
    try {
      const fallbackData = await callJevRouter(context, {
        route: {
          type: 'choice',
          instructions: instruction || 'Select the most applicable category',
          criteria: criteriaMap
        }
      });
      fallbackUsed = true;
      modelUsed = fallbackData.model || FALLBACK_MODEL;
      promptTokens = fallbackData.usage?.prompt_tokens ?? fallbackData.usage?.input_tokens ?? 0;
      costUsd = (promptTokens / 1_000_000) * FALLBACK_INPUT_COST_PER_MILLION;

      const routeAnswer = fallbackData.answers?.route;
      chosen = routeAnswer?.choice || options[0];
      confidence = routeAnswer?.confidence ?? 0.85;
      probabilities = routeAnswer?.probabilities ?? { [chosen || options[0]]: 1.0 };
    } catch (fallbackErr: any) {
      const latencyMs = Math.round(performance.now() - start);
      await logTelemetry({ tool: 'jev_route', status: 'error', error: String(fallbackErr), latencyMs });
      throw new Error(
        `jev_route: all routing tiers failed. Tier 1 (Jev Decisions API) and Tier 2 (${FALLBACK_MODEL}) both unavailable. ` +
        `Last error: ${fallbackErr.message}. Ensure OPENROUTER_API_KEY is set and valid.`
      );
    }
  }

  const latencyMs = Math.round(performance.now() - start);
  const telemetry = {
    modelUsed,
    fallbackUsed,
    latencyMs,
    promptTokens,
    costUsd: Number(costUsd.toFixed(8)),
    costFormatted: `$${costUsd.toFixed(6)}`
  };

  await logTelemetry({
    tool: 'jev_route',
    chosen,
    confidence,
    probabilities,
    ...telemetry
  });

  return {
    content: [{
      type: 'text',
      text: JSON.stringify({
        choice: chosen,
        confidence,
        probabilities,
        telemetry
      }, null, 2)
    }]
  };
}
