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
import { compareModels, parseModel } from './models.js';

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
  /** The tier Jev returned, kept when the user's instruction changed `tier`. */
  routerTier?: JevTier;
  /** What set `tier` and `model`: absent for the router, else the user's instruction. */
  by?: 'instruction-model' | 'instruction-cost';
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

/** Whether the session model is opus or stronger, so an opus delegation is within the ceiling. Unknown counts as no. */
function sessionAllowsOpus(sessionModel: string | null): boolean {
  const session = parseModel(sessionModel);
  const opus = parseModel('opus');
  return session !== 'unknown' && opus !== 'unknown' && compareModels(session, opus) >= 0;
}

/** The context line injected before the turn. */
export function buildDirective(rec: Dispatch, sessionModel: string | null, quotaStrategy: string, intent: UserIntent = {}): string {
  const head =
    `[JEV] cost-tiered recommendation (quota=${quotaStrategy}): subagent="${rec.subagent}", ` +
    `tier="${rec.tier}", model="${rec.model}", effort="${rec.effort}".`;
  const ceiling = sessionModel
    ? `The session model "${sessionModel}" is the hard ceiling: never delegate to a stronger model without explicit user approval.`
    : 'Never delegate to a stronger model than the current session without explicit user approval.';
  const subagent = rec.subagent === 'self' ? 'general-purpose' : rec.subagent;

  if (rec.by === 'instruction-model') {
    const asked = `You asked for ${rec.model} (explicit user request: "${intent.phrase ?? rec.model}"): delegate to ${subagent} with model "${rec.model}".`;
    // The PreToolUse guard cannot read the prompt, so above the ceiling it still shows its one prompt.
    // Tell the assistant not to ask first: that prompt is the user's confirmation.
    return rec.model === 'opus' && !sessionAllowsOpus(sessionModel)
      ? `${head} ${asked} The Jev guard shows the user one approval prompt for a model above the session model; do not ask separately before it.`
      : `${head} ${ceiling} ${asked}`;
  }

  const lowered =
    rec.by === 'instruction-cost' && rec.routerTier && rec.routerTier !== rec.tier
      ? `You asked for the minimal viable model (explicit user request: "${intent.phrase ?? 'minimal viable'}"): Jev lowered the tier from ${rec.routerTier} to ${rec.tier}. `
      : '';
  const isCustom = !['Explore', 'Plan', 'general-purpose', 'self'].includes(rec.subagent);

  if (rec.tier === 'pro') {
    // One sentence, never the ceiling line plus an ask line that contradict each other (case study 5.2).
    const pro = sessionAllowsOpus(sessionModel)
      ? `Jev rates this opus-level and the session model "${sessionModel}" allows it: delegate to ${subagent} with model "opus".`
      : `Jev rates this opus-level; the session model${sessionModel ? ` "${sessionModel}"` : ''} is the hard ceiling, so ask the user before delegating on opus: offer the cheaper option and let the user choose.`;
    return `${head} ${pro}`;
  }

  const action =
    rec.tier === 'openrouter_free'
      ? 'Consider offloading it to the jev MCP tool jev_execute_free ($0) instead of a paid subagent.'
      : rec.subagent === 'self'
        ? 'Handle it directly.'
        : isCustom
          ? `Action: Delegate to custom persona via Agent(subagent_type: "${rec.subagent}", model: "${rec.model}").`
          : `Delegate to the ${rec.subagent} subagent with model "${rec.model}".`;
  return `${head} ${lowered}${ceiling} ${action}`;
}

/** What the user said about the model or the cost in the current prompt (case study 5.1). */
export type UserIntent = {
  /** A model the user named in an imperative ("use opus", "run an opus subagent"). */
  model?: 'haiku' | 'sonnet' | 'opus';
  /** `minimal` for "minimal viable model", "cheapest" and the like. */
  cost?: 'minimal' | 'normal';
  /** The words that matched, for the "You asked for ..." text. Never logged. */
  phrase?: string;
};

/** Longest prefix of a prompt that is scanned for an instruction. */
export const INTENT_SCAN_CHARS = 2000;

// An imperative that selects a model. Prose that merely mentions a model ("sonnet is cheaper than
// opus", "compare with opus") does not match: a verb of choosing must come right before the name.
const MODEL_IMPERATIVE =
  /\b(?:use|using|run|launch|spawn|start|delegate(?:\s+(?:it|this|that))?\s+to|dispatch(?:\s+(?:it|this|that))?\s+to|go\s+with|switch\s+to|want|need|prefer|choose|pick|select)\s+(?:an?\s+|the\s+|only\s+)?(haiku|sonnet|opus)\b/gi;
const ALTERNATIVE_MODEL = /^\s*(?:,\s*)?(?:or|and|\/|vs\.?|versus)\s+(?:an?\s+|the\s+)?(haiku|sonnet|opus)\b/i;
const NEGATION =/\b(?:don'?t|do\s+not|never|not|no|without|avoid|stop|instead\s+of)\b[^.!?\n]{0,25}$/i;
const MINIMAL_COST =
  /\b(?:minimal|minimum)[\s-]+viable\b|\b(?:minimal|minimum|smallest|lowest)[\s-]+(?:cost|tier|model)\b|\bcheapest\b|\bleast\s+expensive\b|\bas\s+cheap(?:ly)?\s+as\b/i;

/**
 * Read an explicit model or cost instruction from the prompt. Conservative by design: imperative
 * patterns only, a negated or contradictory mention yields nothing, and two different models in
 * one prompt are ambiguous. It is pure and offline.
 */
export function parseUserIntent(prompt: string | undefined | null): UserIntent {
  const text = (prompt ?? '').slice(0, INTENT_SCAN_CHARS);
  const intent: UserIntent = {};
  const named = new Map<string, string>();
  for (const m of text.matchAll(MODEL_IMPERATIVE)) {
    if (NEGATION.test(text.slice(Math.max(0, m.index! - 30), m.index!))) continue;
    named.set(m[1].toLowerCase(), m[0].trim());
    // "use opus or sonnet" is a choice left open, not an instruction.
    const alt = ALTERNATIVE_MODEL.exec(text.slice(m.index! + m[0].length));
    if (alt) named.set(alt[1].toLowerCase(), alt[0].trim());
  }
  if (named.size === 1) {
    const [model, phrase] = [...named][0];
    intent.model = model as 'haiku' | 'sonnet' | 'opus';
    intent.phrase = phrase;
  }
  const cost = MINIMAL_COST.exec(text);
  if (cost && !NEGATION.test(text.slice(Math.max(0, cost.index - 30), cost.index))) {
    intent.cost = 'minimal';
    intent.phrase ??= cost[0];
  }
  return intent;
}

const MODEL_TO_TIER: Readonly<Record<'haiku' | 'sonnet' | 'opus', JevTier>> = { haiku: 'flash_lite', sonnet: 'flash', opus: 'pro' };
const TIER_ORDER: readonly JevTier[] = ['openrouter_free', 'flash_lite', 'flash', 'pro'];

/** `flash_lite` is the floor of a "minimal" step: it never pushes a paid tier into the free path. */
function oneStepDown(tier: JevTier): JevTier {
  const i = TIER_ORDER.indexOf(tier);
  return i <= 1 ? tier : TIER_ORDER[i - 1];
}

/**
 * Apply the user's own words to Jev's recommendation. An explicit model replaces the tier; `minimal`
 * lowers it by one step, never below `flash_lite`. When both are present the explicit model wins
 * and `minimal` is ignored, so the effect is capped at one instruction. Returns the input when
 * there is nothing to apply.
 */
export function applyUserIntent(rec: Dispatch, intent: UserIntent): Dispatch {
  if (intent.model) {
    const tier = MODEL_TO_TIER[intent.model];
    return { ...rec, tier, model: intent.model, routerTier: rec.routerTier ?? rec.tier, by: 'instruction-model' };
  }
  if (intent.cost === 'minimal') {
    const tier = oneStepDown(rec.tier);
    if (tier === rec.tier) return { ...rec, by: 'instruction-cost' };
    return { ...rec, tier, model: TIER_TO_CLAUDE[tier], routerTier: rec.routerTier ?? rec.tier, by: 'instruction-cost' };
  }
  return rec;
}

/** Keep only characters that are safe in a file name. */
export function safeSessionId(id: unknown): string | null {
  if (typeof id !== 'string') return null;
  const clean = id.replace(/[^A-Za-z0-9_-]/g, '');
  return clean.length ? clean : null;
}
