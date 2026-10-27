import type { Tool } from '@modelcontextprotocol/sdk/types.js';
import {
  callJevDecision,
  callJevRouter,
  evaluateLocalQualityJudge,
  JEV_MODEL,
  FALLBACK_MODEL,
  JEV_INPUT_COST_PER_MILLION,
  FALLBACK_INPUT_COST_PER_MILLION,
  logTelemetry
} from '../core/index.js';

export const judgeToolDefinition: Tool = {
  name: 'jev_judge',
  description: 'Fast binary condition testing and verification using OpenRouter native Decisions API (question type "noul"). Returns passed: true if noul >= 0.5.',
  inputSchema: {
    type: 'object',
    properties: {
      context: { type: 'string', description: 'The content or output to evaluate' },
      condition: { type: 'string', description: 'The condition to test (e.g. "Did this task succeed?")' }
    },
    required: ['context', 'condition']
  }
};

export async function handleJudgeTool(args: unknown): Promise<{ content: Array<{ type: 'text'; text: string }> }> {
  const start = performance.now();
  const { context, condition } = (args || {}) as { context: string; condition: string };
  if (!context || !condition) {
    throw new Error('jev_judge requires "context" and "condition"');
  }

  let passed = false;
  let noulValue = 0.5;
  let confidence = 0.5;
  let modelUsed = JEV_MODEL;
  let fallbackUsed = false;
  let promptTokens = 0;
  let costUsd = 0;

  // Tier 1: Jev Decisions API with question type 'noul'
  try {
    const data = await callJevDecision(context, {
      judgment: {
        type: 'noul',
        instructions: condition
      }
    });
    const judgmentAnswer = data.answers?.judgment;
    noulValue = typeof judgmentAnswer?.noul === 'number' ? judgmentAnswer.noul : 0.5;
    passed = noulValue >= 0.5;
    confidence = judgmentAnswer?.confidence ?? Number((Math.abs(noulValue - 0.5) * 2).toFixed(2));
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
        judgment: {
          type: 'noul',
          instructions: condition
        }
      });
      fallbackUsed = true;
      modelUsed = fallbackData.model || FALLBACK_MODEL;
      promptTokens = fallbackData.usage?.prompt_tokens ?? fallbackData.usage?.input_tokens ?? 0;
      costUsd = (promptTokens / 1_000_000) * FALLBACK_INPUT_COST_PER_MILLION;

      const judgmentAnswer = fallbackData.answers?.judgment;
      noulValue = typeof judgmentAnswer?.noul === 'number' ? judgmentAnswer.noul : 0.5;
      passed = noulValue >= 0.5;
      confidence = judgmentAnswer?.confidence ?? Number((Math.abs(noulValue - 0.5) * 2).toFixed(2));
    } catch (fallbackErr: any) {
      console.warn(`[jev warn] Tier 1 & 2 failed: ${fallbackErr.message}. Triggering Tier 3 Local Deterministic Judge...`);
      // Tier 3: Local Deterministic Judge (AST, regex, exit codes, conflict markers)
      const localResult = evaluateLocalQualityJudge(context, condition);
      passed = localResult.passed;
      noulValue = passed ? 0.95 : 0.05;
      confidence = 0.9;
      modelUsed = 'local_deterministic';
      fallbackUsed = true;
    }
  }

  // Calculate confidence and check ambiguity gate:
  // confidence = |noul - 0.50| * 2. When 0.45 <= noul <= 0.55 (confidence <= 0.10), flag ambiguous & requiresManualVerification
  confidence = Number((Math.abs(noulValue - 0.50) * 2).toFixed(4));
  const isAmbiguous = noulValue >= 0.45 && noulValue <= 0.55;

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
    tool: 'jev_judge',
    passed,
    noul: noulValue,
    confidence,
    ambiguous: isAmbiguous,
    requiresManualVerification: isAmbiguous,
    ...telemetry
  });

  return {
    content: [{
      type: 'text',
      text: JSON.stringify({
        passed,
        noul: noulValue,
        confidence,
        ambiguous: isAmbiguous,
        requiresManualVerification: isAmbiguous,
        telemetry
      }, null, 2)
    }]
  };
}
