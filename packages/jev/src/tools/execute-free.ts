import type { Tool } from '@modelcontextprotocol/sdk/types.js';
import {
  assertFreeModel,
  FREE_MODEL,
  callOpenRouterFree,
  callJevDecision,
  callRouterCompletion,
  logTelemetry
} from '../core/index.js';

export const agentFreeToolDefinition: Tool = {
  name: 'jev_agent_free',
  description: 'Execute autonomous agentic coding, workspace modification, refactoring, and text tasks using free-tier models via OpenRouter Free pool at $0.00 cost (0 Gemini quota).',
  inputSchema: {
    type: 'object',
    properties: {
      prompt: { type: 'string', description: 'The coding task, refactoring request, or prompt for the autonomous agent' },
      cwd: { type: 'string', description: 'Optional working directory for context (defaults to current working directory)' },
      model: {
        type: 'string',
        description: 'Free model to use (defaults to "openrouter/free" [200k ctx, official router], also supports "google/gemma-4-31b-it:free", "cohere/north-mini-code:free", "nvidia/nemotron-3.5-lightning:free")'
      },
      timeoutMs: {
        type: 'number',
        description: 'Optional execution timeout in milliseconds (defaults to 120000ms)'
      }
    },
    required: ['prompt']
  }
};

export const executeFreeToolDefinition: Tool = {
  name: 'jev_execute_free',
  description: 'Execute lightweight generative tasks (summaries, formatting, drafting, boilerplate) using OpenRouter Free Tier (defaults to openrouter/free [200k ctx]) at $0.00 cost, saving Gemini quota.',
  inputSchema: {
    type: 'object',
    properties: {
      prompt: { type: 'string', description: 'The prompt or instructions to execute' },
      systemInstruction: { type: 'string', description: 'Optional system instructions' },
      model: { type: 'string', description: 'Optional OpenRouter free model (defaults to openrouter/free)' }
    },
    required: ['prompt']
  }
};

export const executeFreeGuardedToolDefinition: Tool = {
  name: 'jev_execute_free_guarded',
  description: 'Execute lightweight or trivial generative tasks (formatting, boilerplate, summaries, single-file edits, markdown transformations) on OpenRouter free-tier models ($0.00 cost) with an automatic jev_judge quality gate evaluation. Automatically escalates to a fallback tier if quality verification fails.',
  inputSchema: {
    type: 'object',
    properties: {
      prompt: { type: 'string', description: 'The task prompt or content to process' },
      systemInstruction: { type: 'string', description: 'Optional system instructions' },
      condition: { type: 'string', description: 'Optional quality gate evaluation condition (e.g. "Is the output complete, coherent, and non-empty?")' },
      model: { type: 'string', description: 'Optional free model candidate (defaults to openrouter/free)' }
    },
    required: ['prompt']
  }
};

export async function handleAgentFreeTool(args: unknown): Promise<{ content: Array<{ type: 'text'; text: string }> }> {
  const start = performance.now();
  const { prompt, cwd, model, timeoutMs } = (args || {}) as {
    prompt: string;
    cwd?: string;
    model?: string;
    timeoutMs?: number;
  };

  if (!prompt) {
    throw new Error('jev_agent_free requires "prompt"');
  }

  const freeModel = assertFreeModel(model || FREE_MODEL);
  let content = '';
  let actualModel = freeModel;
  let tokensSaved = 0;
  let costUsd = 0;
  let latencyMs = 0;

  try {
    const data = await callOpenRouterFree(prompt, undefined, freeModel, cwd, timeoutMs);
    content = data.choices?.[0]?.message?.content || data.content || '';
    actualModel = data.model || data.modelUsed || freeModel;
    const promptTokens = data.usage?.prompt_tokens ?? 0;
    const completionTokens = data.usage?.completion_tokens ?? 0;
    tokensSaved = (data.usage?.total_tokens ?? 0) || (promptTokens + completionTokens);
    costUsd = 0;
    latencyMs = Math.round(performance.now() - start);
  } catch (err: any) {
    console.error(`[jev error] jev_agent_free failed: ${err.message}`);
    throw new Error(`Free Agent execution failed: ${err.message}`);
  }

  const telemetry = {
    modelUsed: actualModel,
    backend: 'openrouter' as const,
    fallbackTriggered: false,
    latencyMs,
    tokensSaved,
    costUsd,
    costFormatted: '$0.00'
  };

  await logTelemetry({
    tool: 'jev_agent_free',
    ...telemetry
  });

  return {
    content: [{
      type: 'text',
      text: JSON.stringify({
        content,
        backend: 'openrouter',
        modelUsed: actualModel,
        tokensSaved,
        costUsd: 0,
        latencyMs,
        telemetry
      }, null, 2)
    }]
  };
}

export async function handleExecuteFreeTool(args: unknown): Promise<{ content: Array<{ type: 'text'; text: string }> }> {
  const start = performance.now();
  const { prompt, systemInstruction, model } = (args || {}) as {
    prompt: string;
    systemInstruction?: string;
    model?: string;
  };

  if (!prompt) {
    throw new Error('jev_execute_free requires "prompt"');
  }

  const freeModel = assertFreeModel(model || FREE_MODEL);
  let data: any;
  try {
    data = await callOpenRouterFree(prompt, systemInstruction, freeModel);
  } catch (err: any) {
    console.error(`[jev error] jev_execute_free failed: ${err.message}`);
    throw new Error(`OpenRouter Free execution failed: ${err.message}`);
  }

  const content = data.choices?.[0]?.message?.content || data.content || '';
  const actualModel = data.model || data.modelUsed || freeModel;
  const promptTokens = data.usage?.prompt_tokens ?? 0;
  const completionTokens = data.usage?.completion_tokens ?? 0;
  const tokensSaved = (data.usage?.total_tokens ?? 0) || (promptTokens + completionTokens);
  const latencyMs = Math.round(performance.now() - start);

  const telemetry = {
    modelUsed: actualModel,
    backend: 'openrouter' as const,
    fallbackUsed: false,
    latencyMs,
    tokensSaved,
    costUsd: 0.00,
    costFormatted: '$0.00'
  };

  await logTelemetry({
    tool: 'jev_execute_free',
    ...telemetry
  });

  return {
    content: [{
      type: 'text',
      text: JSON.stringify({
        content,
        backend: 'openrouter',
        modelUsed: actualModel,
        tokensSaved,
        costUsd: 0,
        latencyMs,
        telemetry
      }, null, 2)
    }]
  };
}

export async function handleExecuteFreeGuardedTool(args: unknown): Promise<{ content: Array<{ type: 'text'; text: string }> }> {
  const start = performance.now();
  const { prompt, systemInstruction, condition, model } = (args || {}) as {
    prompt: string;
    systemInstruction?: string;
    condition?: string;
    model?: string;
  };

  if (!prompt) {
    throw new Error('jev_execute_free_guarded requires "prompt"');
  }

  const freeModel = assertFreeModel(model || FREE_MODEL);
  let data: any;
  try {
    data = await callOpenRouterFree(prompt, systemInstruction, freeModel);
  } catch (err: any) {
    console.error(`[jev error] jev_execute_free_guarded execution failed: ${err.message}`);
    throw new Error(`OpenRouter Free execution failed: ${err.message}`);
  }

  const content = data.choices?.[0]?.message?.content || data.content || '';
  const actualModel = data.model || data.modelUsed || freeModel;
  const promptTokens = data.usage?.prompt_tokens ?? 0;
  const completionTokens = data.usage?.completion_tokens ?? 0;
  const tokensSaved = (data.usage?.total_tokens ?? 0) || (promptTokens + completionTokens);

  // Automated quality evaluation via jev_judge condition check
  const testCondition = condition || 'Is the output complete, valid, coherent, and non-empty?';
  let passed = false;
  let noulValue = 0.5;
  let confidence = 0.5;

  try {
    const judgeData = await callJevDecision(content, {
      judgment: {
        type: 'noul',
        instructions: testCondition
      }
    });
    const judgmentAnswer = judgeData.answers?.judgment;
    noulValue = typeof judgmentAnswer?.noul === 'number' ? judgmentAnswer.noul : 0.5;
    passed = noulValue >= 0.5;
    confidence = judgmentAnswer?.confidence ?? 0.85;
  } catch {
    passed = content.trim().length > 0;
    noulValue = passed ? 0.9 : 0.1;
    confidence = 0.7;
  }

  let escalated = false;
  let finalContent = content;

  if (!passed) {
    try {
      const escalatedContent = await callRouterCompletion(prompt, systemInstruction);
      if (escalatedContent) {
        finalContent = escalatedContent;
        escalated = true;
      }
    } catch {
      // Keep original content if fallback also fails
    }
  }

  const latencyMs = Math.round(performance.now() - start);
  const telemetry = {
    modelUsed: actualModel,
    passed,
    noul: noulValue,
    escalated,
    latencyMs,
    tokensSaved,
    costUsd: 0.00,
    costFormatted: '$0.00'
  };

  await logTelemetry({
    tool: 'jev_execute_free_guarded',
    ...telemetry
  });

  return {
    content: [{
      type: 'text',
      text: JSON.stringify({
        content: finalContent,
        passed,
        noul: noulValue,
        confidence,
        escalated,
        modelUsed: actualModel,
        tokensSaved,
        costUsd: 0,
        latencyMs,
        telemetry
      }, null, 2)
    }]
  };
}
