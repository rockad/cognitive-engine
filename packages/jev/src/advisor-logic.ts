/**
 * Claude Code advisor for Jev: the decision logic for the UserPromptSubmit hook, kept free of I/O
 * so it can be tested offline. `advisor-hook.ts` wires it to stdin/stdout and the real Jev calls.
 *
 * Ported from the agentic-hub Jev plugin (claude/logic.ts). It mirrors the OpenCode adapter
 * (adapters/opencode/src/logic.ts): advise a subagent, model and effort before each turn. It is
 * advice only. The enforcement point for the model ceiling is the PreToolUse guard (guard.ts),
 * which this module neither replaces nor imports. A Jev failure never fails a turn.
 */
import { defaultEffortForSubagent, applyEffortGovernance, discoverWorkspaceAgents, type ThinkingEffort } from './core/index.js';

export type JevTier = 'openrouter_free' | 'flash_lite' | 'flash' | 'pro';
export type ClaudeModelAlias = 'haiku' | 'sonnet' | 'opus' | 'fable';

/** Claude Code's built-in subagents (plus `self` for doing the work directly). */
export const CLAUDE_SUBAGENT_CRITERIA: Readonly<Record<string, string>> = {
  Explore: 'Read-only codebase exploration: find files, search code, answer questions about the codebase',
  'general-purpose': 'General multi-step tasks, research, and implementation work',
  Plan: 'Designing an implementation plan before writing code',
  self: 'Direct answer or edit in the main session without delegation'
};

/**
 * Resolve Claude subagent criteria dynamically by discovering custom workspace agents
 * and merging them with the Claude built-ins.
 */
export async function resolveClaudeSubagentCriteria(workspaceDir?: string): Promise<Record<string, string>> {
  const discovered = await discoverWorkspaceAgents(workspaceDir, 'claude-code');
  const criteria: Record<string, string> = { ...CLAUDE_SUBAGENT_CRITERIA };
  for (const [name, desc] of Object.entries(discovered)) {
    if (name === 'none') continue;
    criteria[name] = desc;
  }
  criteria.Explore = CLAUDE_SUBAGENT_CRITERIA.Explore;
  criteria['general-purpose'] = CLAUDE_SUBAGENT_CRITERIA['general-purpose'];
  criteria.Plan = CLAUDE_SUBAGENT_CRITERIA.Plan;
  criteria.self = CLAUDE_SUBAGENT_CRITERIA.self;
  return criteria;
}

export const TIER_CRITERIA: Readonly<Record<JevTier, string>> = {
  openrouter_free: 'Lightweight summaries, drafts, syntax transforms, formatting, boilerplate ($0 cost)',
  flash_lite: 'Trivial triage, quick lookups, file moves, formatting',
  flash: 'Standard agentic steps, routine edits, content synthesis, local commands',
  pro: 'Deep reasoning, architecture, complex multi-file refactors, high-stakes writing'
};

/** Jev tiers mapped onto Claude model aliases accepted by the Agent tool's `model` field. */
export const TIER_TO_CLAUDE: Readonly<Record<JevTier, ClaudeModelAlias>> = {
  openrouter_free: 'haiku',
  flash_lite: 'haiku',
  flash: 'sonnet',
  pro: 'opus'
};

export type Dispatch = {
  subagent: string;
  tier: JevTier;
  model: ClaudeModelAlias;
  effort: ThinkingEffort;
  confidence: number;
};

/** Turn a Jev decision response into a dispatch, validating every choice against the offered keys. */
export function parseDispatch(
  data: unknown,
  task: string,
  quotaStrategy: string,
  criteria: Record<string, string> = CLAUDE_SUBAGENT_CRITERIA
): Dispatch | null {
  const answers = (data as { answers?: Record<string, { choice?: unknown; confidence?: unknown }> } | null)?.answers;
  if (!answers) return null;
  const sa = answers.recommendedSubagent;
  const tierAnswer = answers.recommendedTier;
  const tierChoice = tierAnswer?.choice;
  const saChoice = sa?.choice;
  const tier: JevTier =
    typeof tierChoice === 'string' && tierChoice in TIER_CRITERIA ? (tierChoice as JevTier) : 'flash';
  const subagent =
    typeof saChoice === 'string' && saChoice in criteria ? saChoice : 'self';
  const confidence = Math.min(
    typeof sa?.confidence === 'number' ? sa.confidence : 0.9,
    typeof tierAnswer?.confidence === 'number' ? tierAnswer.confidence : 0.9
  );
  const effort = applyEffortGovernance(defaultEffortForSubagent(subagent, task), quotaStrategy, tier);
  return { subagent, tier, model: TIER_TO_CLAUDE[tier], effort, confidence };
}

/** The questions sent to Jev for one turn. */
export function dispatchQuestions(criteria: Record<string, string> = CLAUDE_SUBAGENT_CRITERIA): Record<string, unknown> {
  return {
    recommendedSubagent: {
      type: 'choice',
      instructions: 'Select the best Claude Code subagent for this task (or self for direct work)',
      criteria
    },
    recommendedTier: {
      type: 'choice',
      instructions: 'Determine the required model tier from cognitive load and complexity',
      criteria: TIER_CRITERIA
    }
  };
}

/** The context line injected before the turn. */
export function buildDirective(rec: Dispatch, sessionModel: string | null, quotaStrategy: string): string {
  const ceiling = sessionModel
    ? `The session model "${sessionModel}" is the hard ceiling: never delegate to a stronger model without explicit user approval.`
    : 'Never delegate to a stronger model than the current session without explicit user approval.';
  const isCustom = !['Explore', 'Plan', 'general-purpose', 'self'].includes(rec.subagent);
  const action =
    rec.tier === 'pro'
      ? 'This task may need the strongest tier: present the cheaper and the stronger option and let the user choose before delegating on opus.'
      : rec.tier === 'openrouter_free'
        ? 'Consider offloading it to the jev MCP tool jev_execute_free ($0) instead of a paid subagent.'
        : rec.subagent === 'self'
          ? 'Handle it directly.'
          : isCustom
            ? `Action: Delegate to custom persona via Agent(subagent_type: "${rec.subagent}", model: "${rec.model}").`
            : `Delegate to the ${rec.subagent} subagent with model "${rec.model}".`;
  return (
    `[JEV] cost-tiered recommendation (quota=${quotaStrategy}): subagent="${rec.subagent}", ` +
    `tier="${rec.tier}", model="${rec.model}", effort="${rec.effort}". ${ceiling} ${action}`
  );
}

/** Keep only characters that are safe in a file name. */
export function safeSessionId(id: unknown): string | null {
  if (typeof id !== 'string') return null;
  const clean = id.replace(/[^A-Za-z0-9_-]/g, '');
  return clean.length ? clean : null;
}
