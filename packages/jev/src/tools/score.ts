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

export const scoreToolDefinition: Tool = {
  name: 'jev_score',
  description: 'Rate relevance, quality, or cognitive complexity on an ordinal scale using OpenRouter native Decisions API (question type "score") with criteria buckets.',
  inputSchema: {
    type: 'object',
    properties: {
      context: { type: 'string', description: 'Content to rate' },
      rubric: { type: 'string', description: 'Rubric or question for rating' },
      criteria: {
        type: 'array',
        items: { type: 'string' },
        description: 'Optional array of criteria descriptions (defaults to standard 5-point quality scale)'
      }
    },
    required: ['context']
  }
};

export async function handleScoreTool(args: unknown): Promise<{ content: Array<{ type: 'text'; text: string }> }> {
  const start = performance.now();
  const { context, rubric, criteria } = (args || {}) as {
    context: string;
    rubric?: string;
    criteria?: string[];
  };

  if (!context) {
    throw new Error('jev_score requires "context"');
  }

  const criteriaList = Array.isArray(criteria) && criteria.length > 0
    ? criteria
    : ['Poor', 'Fair', 'Good', 'Very Good', 'Exceptional'];

  let score = 3;
  let confidence = 0.9;
  let probabilities: Record<string, number> = {};
  let legend: Record<string, string> = {};
  let modelUsed = JEV_MODEL;
  let fallbackUsed = false;
  let promptTokens = 0;
  let costUsd = 0;

  // Tier 1: Jev Decisions API with question type 'score'
  try {
    const data = await callJevDecision(context, {
      score: {
        type: 'score',
        instructions: rubric || 'Rate content quality according to the criteria',
        criteria: criteriaList
      }
    });
    const scoreAnswer = data.answers?.score;
    score = typeof scoreAnswer?.score === 'number' ? scoreAnswer.score : 3;
    confidence = scoreAnswer?.confidence ?? 0.95;
    probabilities = scoreAnswer?.probabilities ?? {};
    legend = scoreAnswer?.legend ?? {};
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
        score: {
          type: 'score',
          instructions: rubric || 'Rate content quality according to the criteria',
          criteria: criteriaList
        }
      });
      fallbackUsed = true;
      modelUsed = fallbackData.model || FALLBACK_MODEL;
      promptTokens = fallbackData.usage?.prompt_tokens ?? fallbackData.usage?.input_tokens ?? 0;
      costUsd = (promptTokens / 1_000_000) * FALLBACK_INPUT_COST_PER_MILLION;

      const scoreAnswer = fallbackData.answers?.score;
      score = typeof scoreAnswer?.score === 'number' ? scoreAnswer.score : 3;
      confidence = scoreAnswer?.confidence ?? 0.85;
      probabilities = scoreAnswer?.probabilities ?? {};
      legend = scoreAnswer?.legend ?? {};
    } catch {
      const passthroughPayload = {
        score: null,
        confidence: 0,
        fallbackToAgent: true,
        message: 'Rating models unavailable. Please score directly using your own reasoning.'
      };
      return {
        content: [{ type: 'text', text: JSON.stringify(passthroughPayload, null, 2) }]
      };
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
    tool: 'jev_score',
    score,
    confidence,
    ...telemetry
  });

  return {
    content: [{
      type: 'text',
      text: JSON.stringify({
        score,
        legend,
        probabilities,
        confidence,
        telemetry
      }, null, 2)
    }]
  };
}
