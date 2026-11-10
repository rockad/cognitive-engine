import { z } from 'zod';
import { defineTool, strictObject, type ToolInput, type ToolResult } from './define.js';
import {
  callJevDecision,
  callJevRouter,
  readQuotaView,
  clampTierToSessionCeiling,
  detectScaffoldingTask,
  isFreeTierMechanicalTask,
  topologicalSortStages,
  estimateTierCost,
  defaultEffortForSubagent,
  applyEffortGovernance,
  discoverWorkspaceAgents,
  detectActiveHarness,
  loadJevConfigSync,
  normalizeTier,
  getHarnessModels,
  JEV_MODEL,
  FALLBACK_MODEL,
  JEV_INPUT_COST_PER_MILLION,
  FALLBACK_INPUT_COST_PER_MILLION,
  logTelemetry,
  type SubTask,
  type BatchDispatchPlan
} from '../core/index.js';
import { dispatchStyle } from './harness.js';
import { activeClaudeSessionModel, claudeModelTier, sessionBelowOpus } from '../claude-active-session.js';
import { CLAUDE_SUBAGENT_CRITERIA, resolveClaudeSubagentCriteria } from '../advisor-logic.js';
import { READ_ONLY_SUBAGENTS, STAGE_RUBRIC, TIER_CRITERIA, isStageKind, type StageKind } from '../stage-rubric.js';

const dispatchShape = {
  task: z.string().describe('The task, prompt, or sub-goal to dispatch'),
  availableSubagents: z.array(z.string()).optional().describe('Optional candidate subagents (defaults to dynamically discovered workspace agents)')
};

/**
 * The shared telemetry file labels every record `antigravity` unless told otherwise. On Claude Code
 * the record says so, which is how the pre-flight reminder (advisor-state.ts) finds a recent
 * dispatch call. Other harnesses keep the existing label, unchanged.
 */
export function telemetryHarness(): { harness?: 'claude-code' } {
  return detectActiveHarness() === 'claude-code' ? { harness: 'claude-code' } : {};
}

/**
 * The session ceiling on Claude Code. Unlike the Antigravity clamp it never lowers the tier: the
 * user's standing rule lets an expensive judgement escalate to opus, and the PreToolUse guard (not
 * this advice) is what asks the user before a model above the session runs. So the tool reports
 * the ceiling (`sessionCeilingTier`, `activeSessionModel`) and flags the escalation, and
 * `wasClamped` is always false. An unknown session model is reported as the top tier.
 */
export function claudeCeiling(
  recommendedTier: string,
  sessionModel: string | null
): { clampedTier: string; wasClamped: boolean; ceilingTier: string } {
  return { clampedTier: recommendedTier, wasClamped: false, ceilingTier: claudeModelTier(sessionModel) ?? 'pro' };
}

/** Extra guidance when the tier is opus and the session is below it: Claude Code will ask once. */
export function escalationNote(isClaude: boolean, isPro: boolean, sessionModel: string | null): string {
  if (!isClaude || !isPro || !sessionBelowOpus(sessionModel)) return '';
  return ` Opus is above the session model${sessionModel ? ` (${sessionModel})` : ''}: the Jev guard will ask the user once before it runs.`;
}

type TierSource = 'kind' | 'router' | 'keyword';

/**
 * Tier from the wording of a stage, the offline fallback on Claude Code when the caller gave no
 * `kind` and the router is unreachable. Whole words only: the old substring test sent "information"
 * and "transformation" to the free tier because they contain "format".
 */
export function claudeKeywordTier(taskText: string): string {
  if (/\b(?:deep reasoning|architecture overhaul)\b/i.test(taskText)) return 'pro';
  if (isFreeTierMechanicalTask(taskText)) return 'openrouter_free';
  return 'flash';
}

/**
 * Batch tier rule for the other harnesses: the same whole-word free-tier rule (see
 * isFreeTierMechanicalTask), then pro only on the explicit phrases, else flash.
 */
export function classifyBatchTier(taskText: string): 'openrouter_free' | 'flash' | 'pro' {
  if (isFreeTierMechanicalTask(taskText)) return 'openrouter_free';
  if (/\b(?:deep reasoning|architecture overhaul)\b/i.test(taskText || '')) return 'pro';
  return 'flash';
}

export const dispatchTool = defineTool({
  name: 'jev_dispatch',
  description: 'Intelligently route a task to the optimal subagent and model tier ("openrouter_free" | "flash_lite" | "flash" | "pro") in a single ~800ms decision call using TypeSafe Jev. "pro" tier models require explicit manual user approval before invocation.',
  shape: dispatchShape,
  handler: handleDispatchTool
});

const batchDispatchShape = {
  tasks: z.array(strictObject({
    id: z.string().describe('Unique identifier for the task/phase'),
    task: z.string().describe('The description of the work'),
    dependencies: z.array(z.string()).optional().describe('Optional array of task IDs that must complete before this task can start'),
    kind: z.enum(['mechanical', 'settled-edit', 'judgement']).optional().describe(
      'Claude Code only: the caller\'s own classification of the stage, used instead of guessing. mechanical (edit or checklist update) gives haiku, settled-edit (spec or plan edit with a decision already made) gives sonnet, judgement (an error is expensive) gives opus'
    ),
    needsEdit: z.boolean().optional().describe(
      'Claude Code only: the stage edits files, so read-only subagent types (Plan, Explore) are never assigned and the free tier (text only) is not used'
    )
  })).describe('Array of task descriptions or phase items to plan and dispatch in parallel'),
  availableSubagents: z.array(z.string()).optional().describe('Optional list of candidate subagents')
};

export const batchDispatchTool = defineTool({
  name: 'jev_batch_dispatch',
  description: 'Decompose a multi-task roadmap or list of phases into a parallel DAG execution manifest with optimal tier assignments, dependency tracking, concurrency safety (max 10 for openrouter_free), and boilerplate pre-flight flags. On Claude Code, classify each task with kind (mechanical, settled-edit, judgement) and needsEdit to get the model per stage (haiku, sonnet, opus), Agent() guidance and no read-only persona for an edit stage.',
  shape: batchDispatchShape,
  handler: handleBatchDispatchTool
});

export async function handleDispatchTool(args: ToolInput<typeof dispatchShape>): Promise<ToolResult> {
  const start = performance.now();
  const { task, availableSubagents } = args;

  if (!task) {
    throw new Error('jev_dispatch requires a non-empty "task" string');
  }

  let subagentCriteria: Record<string, string> = {};
  if (Array.isArray(availableSubagents) && availableSubagents.length > 0) {
    const discovered = await discoverWorkspaceAgents(process.cwd(), detectActiveHarness());
    for (const sa of availableSubagents) {
      subagentCriteria[sa] = discovered[sa] || `Agent specialized in ${sa.replace(/[-_]/g, ' ')}`;
    }
  } else {
    subagentCriteria = await discoverWorkspaceAgents(process.cwd(), detectActiveHarness());
  }

  if (
    Object.keys(subagentCriteria).length === 0 ||
    (Object.keys(subagentCriteria).length <= 2 && subagentCriteria.self && subagentCriteria.none)
  ) {
    subagentCriteria.self = 'Generalist tasks, direct code changes, orchestrator execution';
  }

  const harness = detectActiveHarness();
  const isClaude = harness === 'claude-code';
  const style = dispatchStyle(harness);
  const tierCriteria: Record<string, string> = isClaude
    ? { ...TIER_CRITERIA, openrouter_free: style.freeTierCriteria }
    : {
        openrouter_free: style.freeTierCriteria,
        flash_lite: 'Trivial triage, fast simple lookups, file moves, quick formatting',
        flash: 'Standard agentic steps, routine edits, content synthesis, local commands',
        pro: 'Deep reasoning, architectural planning, complex multi-file refactoring, high-stakes writing'
      };

  let recommendedSubagent = 'self';
  let recommendedTier = 'flash';
  let confidence = 0.95;
  let subagentProbabilities: Record<string, number> = {};
  let tierProbabilities: Record<string, number> = {};
  let modelUsed = JEV_MODEL;
  let fallbackUsed = false;
  let promptTokens = 0;
  let costUsd = 0;

  // Tier 1: Single decision call with both questions
  try {
    const data = await callJevDecision(`Task: ${task}`, {
      recommendedSubagent: {
        type: 'choice',
        instructions: 'Select the best specialized subagent for this task',
        criteria: subagentCriteria
      },
      recommendedTier: {
        type: 'choice',
        instructions: 'Determine the required model tier based on cognitive load and task complexity',
        criteria: tierCriteria
      }
    });

    const saAnswer = data.answers?.recommendedSubagent;
    const tierAnswer = data.answers?.recommendedTier;

    recommendedSubagent = saAnswer?.choice || 'self';
    recommendedTier = tierAnswer?.choice || 'flash';
    subagentProbabilities = saAnswer?.probabilities || {};
    tierProbabilities = tierAnswer?.probabilities || {};
    confidence = Math.min(saAnswer?.confidence ?? 0.95, tierAnswer?.confidence ?? 0.95);

    promptTokens = data.usage?.input_tokens ?? 0;
    costUsd = typeof data.usage?.cost === 'number'
      ? data.usage.cost
      : (promptTokens / 1_000_000) * JEV_INPUT_COST_PER_MILLION;
    modelUsed = data.model || JEV_MODEL;
  } catch (primaryErr: any) {
    console.error(`[jev warn] Tier 1 dispatch failed: ${primaryErr.message}. Triggering Tier 2 (${FALLBACK_MODEL})...`);

    // Tier 2: Fallback Model
    try {
      const fallbackData = await callJevRouter(`Task: ${task}`, {
        recommendedSubagent: {
          type: 'choice',
          instructions: 'Select the best specialized subagent for this task',
          criteria: subagentCriteria
        },
        recommendedTier: {
          type: 'choice',
          instructions: 'Determine the required model tier based on cognitive load and task complexity',
          criteria: tierCriteria
        }
      });

      fallbackUsed = true;
      modelUsed = fallbackData.model || FALLBACK_MODEL;
      promptTokens = fallbackData.usage?.prompt_tokens ?? fallbackData.usage?.input_tokens ?? 0;
      costUsd = (promptTokens / 1_000_000) * FALLBACK_INPUT_COST_PER_MILLION;

      const saAnswer = fallbackData.answers?.recommendedSubagent;
      const tierAnswer = fallbackData.answers?.recommendedTier;

      recommendedSubagent = saAnswer?.choice || 'self';
      recommendedTier = tierAnswer?.choice || 'flash';
      subagentProbabilities = saAnswer?.probabilities || {};
      tierProbabilities = tierAnswer?.probabilities || {};
      confidence = Math.min(saAnswer?.confidence ?? 0.85, tierAnswer?.confidence ?? 0.85);
    } catch (fallbackErr: any) {
      const latencyMs = Math.round(performance.now() - start);
      await logTelemetry({ tool: 'jev_dispatch', status: 'error', error: String(fallbackErr), latencyMs, ...telemetryHarness() });
      throw new Error(
        `jev_dispatch: all dispatch tiers failed. Tier 1 (Jev Decisions API) and Tier 2 (${FALLBACK_MODEL}) both unavailable. ` +
        `Last error: ${fallbackErr.message}. Ensure OPENROUTER_API_KEY is set and valid in mcp_config.json.`
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

  const quotaView = await readQuotaView();
  const rawEffort = defaultEffortForSubagent(recommendedSubagent, task);
  const recommendedEffort = applyEffortGovernance(rawEffort, quotaView.strategy, recommendedTier);

  // Phase 5: Session Model Ceiling Clamping. On Claude Code the ceiling is the Claude session's own
  // model, not Antigravity's quota file, and it is reported rather than clamped (see claudeCeiling).
  const claudeModel = isClaude ? activeClaudeSessionModel() : null;
  const ceilingClamping = isClaude
    ? claudeCeiling(recommendedTier, claudeModel)
    : clampTierToSessionCeiling(recommendedTier, quotaView.active_model);
  const effectiveTier = ceilingClamping.clampedTier;
  const cognitiveTier = normalizeTier(effectiveTier);
  const harnessModels = getHarnessModels();
  const concreteModel = harnessModels[cognitiveTier] || effectiveTier;
  const isPro = effectiveTier === 'pro' || cognitiveTier === 'deep';

  const isFree = cognitiveTier === 'free' || effectiveTier === 'openrouter_free';
  const executionTool = isFree ? 'jev_execute_free' : style.delegationTool;
  const executionGuidance = isFree
    ? style.freeGuidance(true)
    : style.delegateGuidance(recommendedSubagent, concreteModel) + escalationNote(isClaude, isPro, claudeModel);

  const scaffoldingCheck = detectScaffoldingTask(task);
  const result = {
    recommendedSubagent,
    recommendedTier: effectiveTier,
    cognitiveTier,
    concreteModel,
    rawRecommendedTier: recommendedTier,
    executionTool,
    executionGuidance,
    sessionCeilingTier: ceilingClamping.ceilingTier,
    wasClampedByCeiling: ceilingClamping.wasClamped,
    ...(isClaude ? { activeSessionModel: claudeModel ?? 'unknown' } : {}),
    scaffoldFirst: scaffoldingCheck.scaffoldFirst,
    boilerplateType: scaffoldingCheck.boilerplateType,
    recommendedEffort,
    requiresUserApproval: isPro,
    ...(isPro
      ? {
          approvalReason:
            'Pro tier model recommended for deep reasoning. Explicit manual user approval required before proceeding.'
        }
      : {}),
    delegationRecommended: recommendedSubagent !== 'none',
    confidence,
    subagentProbabilities,
    tierProbabilities,
    telemetry
  };

  await logTelemetry({
    tool: 'jev_dispatch',
    task: task.slice(0, 100),
    ...result,
    ...telemetryHarness()
  });

  return {
    content: [{
      type: 'text',
      text: JSON.stringify(result, null, 2)
    }]
  };
}

export function matchSubagentByKeywords(
  taskText: string,
  workspaceAgents: Record<string, string>
): string {
  const tLower = taskText.toLowerCase();
  const taskTokens = tLower.replace(/[^a-z0-9]+/g, ' ').split(/\s+/).filter(w => w.length > 2);
  const taskTokenSet = new Set(taskTokens);

  let bestAgent = 'self';
  let bestScore = 0;

  for (const [agentKey, desc] of Object.entries(workspaceAgents)) {
    if (agentKey === 'none') continue;
    let score = 0;

    // Check direct normalized key match (hyphens, underscores, spaces)
    const keyNorm = agentKey.toLowerCase().replace(/[-_]+/g, ' ');
    if (tLower.includes(keyNorm)) {
      score += 10;
    }

    // Split agentKey into tokens
    const keyTokens = agentKey.toLowerCase().replace(/[^a-z0-9]+/g, ' ').split(/\s+/).filter(w => w.length > 2);
    for (const kt of keyTokens) {
      if (taskTokenSet.has(kt)) {
        score += 4;
      }
    }

    // Description tokens match
    const descTokens = (desc || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').split(/\s+/).filter(w => w.length > 3);
    for (const dt of descTokens) {
      if (taskTokenSet.has(dt)) {
        score += 1;
      }
    }

    if (score > bestScore) {
      bestScore = score;
      bestAgent = agentKey;
    }
  }

  return bestScore > 0 ? bestAgent : 'self';
}

export async function handleBatchDispatchTool(args: ToolInput<typeof batchDispatchShape>): Promise<ToolResult> {
  const start = performance.now();
  const { tasks, availableSubagents } = args;

  if (!Array.isArray(tasks) || tasks.length === 0) {
    throw new Error('jev_batch_dispatch requires a non-empty "tasks" array');
  }

  const quotaView = await readQuotaView();
  const activeHarness = detectActiveHarness();
  const isClaude = activeHarness === 'claude-code';
  const style = dispatchStyle(activeHarness);
  const claudeModel = isClaude ? activeClaudeSessionModel() : null;
  const cfg = loadJevConfigSync();
  const routingStrategy = cfg.routing?.strategy || 'router';

  let workspaceAgents: Record<string, string> = {};
  if (Array.isArray(availableSubagents) && availableSubagents.length > 0) {
    const discovered = await discoverWorkspaceAgents(process.cwd(), activeHarness);
    for (const sa of availableSubagents) {
      workspaceAgents[sa] = discovered[sa] || `Agent specialized in ${sa.replace(/[-_]/g, ' ')}`;
    }
  } else if (isClaude) {
    // The real Claude Code subagent types: built-ins plus custom agents by their kebab-case name.
    workspaceAgents = await resolveClaudeSubagentCriteria(process.cwd());
  } else {
    workspaceAgents = await discoverWorkspaceAgents(process.cwd(), activeHarness);
  }

  if (
    Object.keys(workspaceAgents).length === 0 ||
    (Object.keys(workspaceAgents).length <= 2 && workspaceAgents.self && workspaceAgents.none)
  ) {
    workspaceAgents.self = 'Generalist tasks, direct code changes, orchestrator execution';
  }

  // Candidate subagents for a task. On Claude Code a stage that edits files never gets a read-only type.
  const candidatesFor = (item: { needsEdit?: boolean }): Record<string, string> => {
    if (!isClaude || !item.needsEdit) return workspaceAgents;
    const editable = Object.fromEntries(Object.entries(workspaceAgents).filter(([name]) => !READ_ONLY_SUBAGENTS.has(name)));
    return Object.keys(editable).length > 0 ? editable : { 'general-purpose': CLAUDE_SUBAGENT_CRITERIA['general-purpose'] };
  };
  // On Claude Code the tier is asked of the router per stage, with the rubric as criteria, unless the caller classified it.
  const routerTiers: Record<string, string> = {};

  // Pre-route subagents via Decisions API in 'router' mode, or keyword fallback
  const subagentAssignments: Record<string, string> = {};
  if (routingStrategy === 'router') {
    try {
      const questions: Record<string, any> = {};
      for (const item of tasks) {
        questions[`sa_${item.id}`] = {
          type: 'choice',
          instructions: `Select the best specialized subagent for the task: ${item.task}`,
          criteria: candidatesFor(item)
        };
        if (isClaude && !item.kind) {
          questions[`tier_${item.id}`] = {
            type: 'choice',
            instructions: `Determine the cheapest Claude model tier that fits this stage, using the rubric in the criteria: ${item.task}`,
            criteria: { ...TIER_CRITERIA, openrouter_free: style.freeTierCriteria }
          };
        }
      }
      const data = await callJevDecision(`Batch routing for ${tasks.length} tasks`, questions);
      for (const item of tasks) {
        const candidates = candidatesFor(item);
        const choice = data.answers?.[`sa_${item.id}`]?.choice;
        if (choice && choice !== 'none' && candidates[choice]) {
          subagentAssignments[item.id] = choice;
        } else {
          subagentAssignments[item.id] = matchSubagentByKeywords(item.task || '', candidates);
        }
        const tierChoice = data.answers?.[`tier_${item.id}`]?.choice;
        if (typeof tierChoice === 'string' && Object.hasOwn(TIER_CRITERIA, tierChoice)) routerTiers[item.id] = tierChoice;
      }
    } catch {
      // Fall back to keyword scoring on network / timeout error
      for (const item of tasks) {
        subagentAssignments[item.id] = matchSubagentByKeywords(item.task || '', candidatesFor(item));
      }
    }
  } else {
    for (const item of tasks) {
      subagentAssignments[item.id] = matchSubagentByKeywords(item.task || '', candidatesFor(item));
    }
  }

  const plannedTasks: SubTask[] = [];
  let freeTierCount = 0;
  let totalEstimatedCost = 0;
  let anyProRequested = false;

  for (const item of tasks) {
    const taskText = item.task || '';
    const deps = Array.isArray(item.dependencies) ? item.dependencies : [];
    const scaffolding = detectScaffoldingTask(taskText);

    let subagent = subagentAssignments[item.id] || 'self';
    // A read-only type cannot carry out an edit stage, whatever routed it there.
    if (isClaude && item.needsEdit && READ_ONLY_SUBAGENTS.has(subagent)) subagent = 'general-purpose';

    let rawTier = 'flash';
    let tierSource: TierSource | undefined;
    if (isClaude) {
      // The caller's classification first, then the router's answer under the rubric, then whole words.
      if (isStageKind(item.kind)) {
        rawTier = STAGE_RUBRIC[item.kind].tier;
        tierSource = 'kind';
      } else if (routerTiers[item.id]) {
        rawTier = routerTiers[item.id];
        tierSource = 'router';
      } else {
        rawTier = claudeKeywordTier(taskText);
        tierSource = 'keyword';
      }
      // The free tier returns text and cannot edit files.
      if (item.needsEdit && rawTier === 'openrouter_free') rawTier = 'flash_lite';
    } else {
      rawTier = classifyBatchTier(taskText);
    }

    const clamped = isClaude ? claudeCeiling(rawTier, claudeModel) : clampTierToSessionCeiling(rawTier, quotaView.active_model);
    const tier = clamped.clampedTier;
    const cognitiveTier = normalizeTier(tier);
    const harnessModels = getHarnessModels();
    const concreteModel = harnessModels[cognitiveTier] || tier;

    if (tier === 'openrouter_free' || cognitiveTier === 'free') {
      freeTierCount++;
    }

    const taskCost = estimateTierCost(tier);
    totalEstimatedCost += taskCost;

    const requiresApproval = (tier === 'pro' || cognitiveTier === 'deep');
    if (requiresApproval) {
      anyProRequested = true;
    }

    const isTaskFree = tier === 'openrouter_free' || cognitiveTier === 'free';
    const executionTool = isTaskFree ? 'jev_execute_free' : style.delegationTool;
    const executionGuidance = isTaskFree
      ? style.freeGuidance(false)
      : style.delegateGuidance(subagent, concreteModel) + escalationNote(isClaude, requiresApproval, claudeModel);

    const rawEffort = defaultEffortForSubagent(subagent, taskText);
    const taskEffort = applyEffortGovernance(rawEffort, quotaView.strategy, tier);

    plannedTasks.push({
      taskId: item.id,
      task: taskText,
      subagent,
      tier,
      cognitiveTier,
      concreteModel,
      executionTool,
      executionGuidance,
      dependencies: deps,
      canRunParallel: deps.length === 0,
      scaffoldFirst: scaffolding.scaffoldFirst,
      boilerplateType: scaffolding.boilerplateType,
      effort: taskEffort,
      requiresUserApproval: requiresApproval,
      estimatedCostUsd: taskCost,
      ...(isClaude ? { kind: isStageKind(item.kind) ? item.kind : undefined, needsEdit: item.needsEdit === true, tierSource } : {})
    });
  }

  const concurrencyCap = Math.min(10, Math.max(1, freeTierCount));
  const stages = topologicalSortStages(plannedTasks);

  const manifest: BatchDispatchPlan = {
    planName: 'Multi-Subagent Parallel DAG Execution Manifest',
    totalTasks: plannedTasks.length,
    freeTierTasks: freeTierCount,
    activeSessionModel: (isClaude ? claudeModel : quotaView.active_model) || 'unknown',
    maxConcurrentFreeWorkers: concurrencyCap,
    tasks: plannedTasks,
    stages,
    parallelExecutionOrder: stages.map(stage => stage.map(t => t.taskId)),
    estimatedCostUsd: Number(totalEstimatedCost.toFixed(6)),
    requiresUserApproval: anyProRequested
  };

  const latencyMs = Math.round(performance.now() - start);
  await logTelemetry({
    tool: 'jev_batch_dispatch',
    taskCount: plannedTasks.length,
    estimatedCostUsd: manifest.estimatedCostUsd,
    requiresUserApproval: anyProRequested,
    latencyMs,
    ...telemetryHarness()
  });

  return {
    content: [{
      type: 'text',
      text: JSON.stringify(manifest, null, 2)
    }]
  };
}
