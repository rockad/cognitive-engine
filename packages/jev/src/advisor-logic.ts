/**
 * Claude Code advisor for Jev: the decision logic for the UserPromptSubmit hook, kept free of I/O
 * so it can be tested offline. `advisor-hook.ts` wires it to stdin/stdout and the real Jev calls.
 *
 * Ported from the agentic-hub Jev plugin (claude/logic.ts). It mirrors the OpenCode adapter
 * (adapters/opencode/src/logic.ts): advise a subagent, model and effort before each turn. It is
 * advice only. The enforcement point for the model ceiling is the PreToolUse guard (guard.ts),
 * which this module neither replaces nor imports. A Jev failure never fails a turn.
 */
import { defaultEffortForSubagent, applyEffortGovernance, discoverWorkspaceAgents, editCapableSubagent, type ThinkingEffort } from './core/index.js';
import { compareModels, parseModel } from './models.js';
import { lowerModel } from './project-config.js';
import { STAGE_RUBRIC_TEXT, TIER_CRITERIA, type ClaudeModelAlias, type JevTier } from './stage-rubric.js';

export { TIER_CRITERIA };
export type { JevTier, ClaudeModelAlias };

/** Claude Code's built-in subagents (plus `self` for doing the work directly). */
export const CLAUDE_SUBAGENT_CRITERIA: Readonly<Record<string, string>> = {
  Explore: 'Read-only codebase exploration: find files, search code, answer questions about the codebase',
  'general-purpose': 'General multi-step tasks, research, and implementation work',
  Plan: 'Designing an implementation plan before writing code',
  self: 'Direct answer or edit in the main session without delegation'
};

/** The kebab-case form discovery registers every agent under (core/discovery.ts, registerAgent). */
const kebab = (name: string): string => name.toLowerCase().replace(/[\s_]+/g, '-');

/**
 * Drop display-name aliases such as "Code Engineer" when discovery also registered the kebab-case
 * name ("code-engineer"): only the latter is a Claude Code `subagent_type`, and the router otherwise
 * sees the same agent twice under different spellings (case study 5.6). A name with no alias is kept
 * as is, and so is a lower-case name with no whitespace (`code_engineer`, a file-name alias): the
 * filter looks only at names Claude Code could not have been given, a space or a capital letter.
 */
export function withoutDisplayNames(discovered: Record<string, string>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [name, desc] of Object.entries(discovered)) {
    const isDisplayName = /\s|[A-Z]/.test(name);
    if (isDisplayName && kebab(name) !== name && Object.hasOwn(discovered, kebab(name))) continue;
    out[name] = desc;
  }
  return out;
}

/**
 * Resolve Claude subagent criteria dynamically by discovering custom workspace agents
 * and merging them with the Claude built-ins.
 */
export async function resolveClaudeSubagentCriteria(workspaceDir?: string): Promise<Record<string, string>> {
  const discovered = await discoverWorkspaceAgents(workspaceDir, 'claude-code');
  const criteria: Record<string, string> = { ...CLAUDE_SUBAGENT_CRITERIA };
  for (const [name, desc] of Object.entries(withoutDisplayNames(discovered))) {
    if (name === 'none') continue;
    criteria[name] = desc;
  }
  criteria.Explore = CLAUDE_SUBAGENT_CRITERIA.Explore;
  criteria['general-purpose'] = CLAUDE_SUBAGENT_CRITERIA['general-purpose'];
  criteria.Plan = CLAUDE_SUBAGENT_CRITERIA.Plan;
  criteria.self = CLAUDE_SUBAGENT_CRITERIA.self;
  return criteria;
}


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
  /** The model the user named when the one-step cap kept the tier short of it. */
  asked?: 'haiku' | 'sonnet' | 'opus';
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
  const chosen = typeof saChoice === 'string' && saChoice in criteria ? saChoice : 'self';
  // Plan and Explore have no Edit or Write tool: an editing task goes to general-purpose instead.
  const subagent = editCapableSubagent(chosen, task, 'general-purpose');
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

// ── Precedence (5.9) ────────────────────────────────────────────────────────────────────────────

/**
 * Which rule decided the model, in this order:
 * 1. `instruction`: the user's own words in this turn ("use opus", "minimal viable model").
 * 2. `guard`: the Jev mode guard, which gates the opus tier behind the user's approval when the
 *    session model is below it. An explicit request for opus (1) satisfies it.
 * 3. `standing`: the user's standing rule (default sonnet, escalate only where a wrong judgement is
 *    expensive). Jev cannot read the user's CLAUDE.md, so this layer is the router's tier as shaped
 *    by the stage rubric (stage-rubric.ts), which is a copy of that rule.
 */
export type AppliedRule = 'instruction' | 'guard' | 'standing';

export const RULE_LABEL: Readonly<Record<AppliedRule, string>> = {
  instruction: 'rule: per your instruction',
  guard: 'rule: per the Jev guard',
  standing: 'rule: per standing rule'
};

/**
 * The model the guard compares a delegation against: the lower of the session model and the project
 * ceiling (the same `lowerModel` the guard uses, guard.ts). No ceiling, an unknown session model or
 * one Jev cannot rank leaves the session model as it is.
 */
export function effectiveSessionModel(sessionModel: string | null, ceiling?: string | null): string | null {
  if (!ceiling) return sessionModel;
  try {
    return lowerModel(sessionModel, ceiling);
  } catch {
    return sessionModel;
  }
}

/** The rule that decided an already-resolved recommendation. `sessionModel` is the effective one (ceiling applied). */
export function appliedRule(rec: Dispatch, sessionModel: string | null): AppliedRule {
  if (rec.by) return 'instruction';
  return rec.tier === 'pro' && !sessionAllowsOpus(sessionModel) ? 'guard' : 'standing';
}

export interface Resolution {
  /** The recommendation to advise, after the user's instruction was applied. */
  advised: Dispatch;
  rule: AppliedRule;
  /** The user has to approve before the model is used: only the guard rule asks. */
  needsApproval: boolean;
}

/**
 * Resolve the model for a turn from the user's instruction, Jev's recommendation and the session
 * model, by the order above. The PreToolUse guard (guard.ts) cannot read the prompt and is not
 * changed by this: above the session model it still shows its one prompt, and that prompt is the
 * user's approval, so the advice says not to ask a second time.
 */
export function resolveModel(args: {
  userIntent: UserIntent;
  rec: Dispatch;
  sessionModel: string | null;
  /** A project's `guard.ceiling`: the model the guard compares against is the lower of it and the session model. */
  ceiling?: string | null;
}): Resolution {
  const advised = applyUserIntent(args.rec, args.userIntent);
  const rule = appliedRule(advised, effectiveSessionModel(args.sessionModel, args.ceiling));
  return { advised, rule, needsApproval: rule === 'guard' };
}

/** Whether the session model is opus or stronger, so an opus delegation is within the ceiling. Unknown counts as no. */
function sessionAllowsOpus(sessionModel: string | null): boolean {
  const session = parseModel(sessionModel);
  const opus = parseModel('opus');
  return session !== 'unknown' && opus !== 'unknown' && compareModels(session, opus) >= 0;
}

/**
 * Whether `model` is stronger than the (effective) session model, so the guard would ask about it.
 * An unknown or unrankable session model counts as "stronger" for opus only, as before: it is what
 * the opus-only branches did, and a cheaper model is not called an escalation on no evidence.
 */
function exceedsSession(model: string, sessionModel: string | null): boolean {
  const wanted = parseModel(model);
  if (wanted === 'unknown') return false;
  const session = parseModel(sessionModel);
  if (session === 'unknown') return model === 'opus' && !sessionAllowsOpus(sessionModel);
  return compareModels(wanted, session) > 0;
}

/** `sessionModel` is the effective one (project ceiling applied); `lowered` says what the ceiling lowered it from. */
function directiveBody(
  rec: Dispatch,
  sessionModel: string | null,
  quotaStrategy: string,
  intent: UserIntent = {},
  lowered?: { session: string | null }
): string {
  const head =
    `[JEV] cost-tiered recommendation (quota=${quotaStrategy}; ${RULE_LABEL[appliedRule(rec, sessionModel)]}): subagent="${rec.subagent}", ` +
    `tier="${rec.tier}", model="${rec.model}", effort="${rec.effort}".`;
  // The limit as the user knows it: the session model, or the project ceiling when that is the lower of the two.
  const limit = lowered
    ? `The project ceiling "${sessionModel}" (below the session model "${lowered.session}")`
    : `The session model${sessionModel ? ` "${sessionModel}"` : ''}`;
  const ceiling = sessionModel
    ? `${limit} is the hard ceiling: never delegate to a stronger model without explicit user approval.`
    : 'Never delegate to a stronger model than the current session without explicit user approval.';
  const subagent = rec.subagent === 'self' ? 'general-purpose' : rec.subagent;

  if (rec.by === 'instruction-model') {
    const asked = rec.asked
      ? `You asked for ${rec.asked} (explicit user request: "${intent.phrase ?? rec.asked}"), but Jev moves at most one tier step from its own recommendation (${rec.routerTier}): delegate to ${subagent} with model "${rec.model}".`
      : `You asked for ${rec.model} (explicit user request: "${intent.phrase ?? rec.model}"): delegate to ${subagent} with model "${rec.model}".`;
    // The PreToolUse guard cannot read the prompt, so above the ceiling it still shows its one prompt.
    // Tell the assistant not to ask first: that prompt is the user's confirmation.
    return exceedsSession(rec.model, sessionModel)
      ? `${head} ${asked} The Jev guard shows the user one approval prompt for a model above the session model; do not ask separately before it.`
      : `${head} ${ceiling} ${asked}`;
  }

  const costNote =
    rec.by === 'instruction-cost' && rec.routerTier && rec.routerTier !== rec.tier
      ? `You asked for the minimal viable model (explicit user request: "${intent.phrase ?? 'minimal viable'}"): Jev lowered the tier from ${rec.routerTier} to ${rec.tier}. `
      : '';
  const isCustom = !['Explore', 'Plan', 'general-purpose', 'self'].includes(rec.subagent);

  if (rec.tier === 'pro') {
    // One sentence, never the ceiling line plus an ask line that contradict each other (case study 5.2).
    const pro = sessionAllowsOpus(sessionModel)
      ? `Jev rates this opus-level and the session model "${sessionModel}" allows it: delegate to ${subagent} with model "opus".`
      : `Jev rates this opus-level; ${limit.replace(/^The/, 'the')} is the hard ceiling, so ask the user before delegating on opus: offer the cheaper option and let the user choose.`;
    return `${head} ${pro}`;
  }

  // A paid delegation on a model above the session model: one ceiling sentence, not the hard-ceiling
  // line followed by a "delegate to <stronger model>" action that contradicts it.
  if (rec.tier !== 'openrouter_free' && rec.subagent !== 'self' && exceedsSession(rec.model, sessionModel)) {
    return `${head} ${costNote}Jev rates this ${rec.model}-level; ${limit.replace(/^The/, 'the')} is the hard ceiling, so ask the user before delegating on ${rec.model}: offer a model within the ceiling and let the user choose.`;
  }

  const action =
    rec.tier === 'openrouter_free'
      ? 'Consider offloading it to the jev MCP tool jev_execute_free ($0) instead of a paid subagent.'
      : rec.subagent === 'self'
        ? 'Handle it directly.'
        : isCustom
          ? `Action: Delegate to custom persona via Agent(subagent_type: "${rec.subagent}", model: "${rec.model}").`
          : `Delegate to the ${rec.subagent} subagent with model "${rec.model}".`;
  return `${head} ${costNote}${ceiling} ${action}`;
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

/** Longest prefix of a prompt that is scanned for an instruction (a model named further down is prose or a paste). */
export const INTENT_SCAN_CHARS = 1500;

// An imperative that selects a model. Prose that merely mentions a model ("sonnet is cheaper than
// opus", "compare with opus") does not match: a verb of choosing must come right before the name.
// "with sonnet" counts only as a clause opener ("With sonnet, please") or after a verb that sets
// the work going ("run it with sonnet", "go with haiku"), never as "compare with opus". A full id
// ("use claude-opus-5-5") names its family.
const MODEL_IMPERATIVE =
  /\b(?:(?:use|using|run|launch|spawn|start|delegate(?:\s+(?:it|this|that|them))?\s+to|dispatch(?:\s+(?:it|this|that|them))?\s+to|go\s+with|switch\s+to|want|need|prefer|choose|pick|select)|(?:(?:do|run|launch|start|proceed|go)\s+(?:it|this|that|them|these)?\s*with)|(?<=^|[.!?\n,;:]\s*)with)\s+(?:an?\s+|the\s+|only\s+)?(?:claude-)?(haiku|sonnet|opus)(?:-\d+(?:-\d+)?)?(?:-\d{8})?\b/gi;
// `--model opus` is an instruction only in an imperative clause ("run with --model opus"), never in prose about the flag.
const MODEL_FLAG = /(?<![\w-])--model(?:=|\s+)(?:claude-)?(haiku|sonnet|opus)(?:-\d+(?:-\d+)?)?(?:-\d{8})?\b/gi;
const FLAG_VERB = /\b(?:use|using|run|launch|start|spawn|invoke|execute|pass|call|go)\b[^.!?\n]{0,40}$/i;
const ANY_MODEL = /\b(?:claude-)?(haiku|sonnet|opus)\b/gi;
// A negator within a couple of words before the instruction: "do not use opus", "not opus", "instead of",
// "without". Anywhere further back ("I have no idea, use opus") it is a different clause.
const NEGATION = /\b(?:don'?t|do\s+not|never|not|no|without|avoid|stop|instead\s+of|rather\s+than)\s+(?:[\w'-]+\s+){0,2}$/i;
// A second model named after the first only replaces it ("use sonnet instead of opus", "opus over sonnet").
const REPLACED = /\b(?:over|instead\s+of|rather\s+than)\s+(?:[\w'-]+\s+){0,2}$/i;
// A statement about what someone will do, not an instruction to do it ("I will use opus", "the agent can use haiku").
const STATEMENT_SUBJECT =
  /\b(?:i|we|you|he|she|they|it|agent|assistant|claude)\s*(?:'ll|'d|\s+(?:will|would|shall|can|could|might|may|did|does|had|has|have|am\s+going\s+to|are\s+going\s+to|is\s+going\s+to|was\s+going\s+to|'m\s+going\s+to))\s+(?:\w+\s+){0,1}$/i;
// A line a speaker label or a quote marker says was said by someone else (a pasted log or reply).
const PASTED_LINE = /^\s*(?:>|(?:[-*]\s*)?(?:agent|assistant|claude|ai|bot|model|jev|system|tool|log|output|stdout|stderr)\s*[:>])/i;
// A sentence that asks or supposes rather than instructs.
const NON_INSTRUCTION_SENTENCE = /^\W*(?:if|should|what|why|how)\s/i;
// Literary uses of the two model names that are poetic forms: "a haiku about spring", "use haiku format", "sonnet 130".
const LITERARY_TAIL = /^(?:[-\s]*(?:about|by|format|formats|style|styled|poem|poems|poetry|form|stanza|stanzas|syllable|syllables|contest|collection|sequence|cycle|writing|verse)\b|\s+\d{2,}\b(?!\.\d))/i;
const MINIMAL_COST =
  /\b(?:minimal|minimum)[\s-]+viable\b(?![\s-]+products?\b)|\b(?:minimal|minimum|smallest|lowest)[\s-]+(?:cost|tier|model)\b|\bcheapest\b|\bleast\s+expensive\b|\bas\s+cheap(?:ly)?\s+as\b/gi;

/** Blank out `re` matches with spaces, keeping every index and newline where it was. */
function blank(text: string, re: RegExp): string {
  return text.replace(re, (m) => m.replace(/[^\n]/g, ' '));
}

/**
 * The prompt with what is not the user's own instruction blanked out, index for index: fenced code
 * blocks, inline code spans and quoted strings ("..." and '...': a quote after a letter is an
 * apostrophe, not an opening quote).
 */
function withoutQuotedText(text: string): string {
  let out = blank(text, /(```|~~~)[\s\S]*?(?:\1|$)/g);
  out = blank(out, /`[^`\n]*`/g);
  out = blank(out, /"[^"\n]{0,300}"|“[^”\n]{0,300}”/g);
  return blank(out, /(?<![\w])'[^'\n]{1,300}'(?![\w])/g);
}

/** The sentence around `index`: from after the previous sentence end or newline to the next one, terminator included. */
function sentenceAround(text: string, index: number): string {
  const before = text.slice(0, index);
  let start = 0;
  for (const b of before.matchAll(/[.!?]+\s|\n/g)) start = b.index! + b[0].length;
  const rest = text.slice(index);
  const end = /[.!?]+(?=\s|$)|\n/.exec(rest);
  return text.slice(start, end ? index + end.index + end[0].length : text.length);
}

/** Whether a match at `index` is the user's own instruction, going by where it sits: not in a question, a supposition or a pasted line. */
function instructive(text: string, index: number): boolean {
  const sentence = sentenceAround(text, index).trim();
  if (sentence.endsWith('?') || NON_INSTRUCTION_SENTENCE.test(sentence)) return false;
  const lineStart = text.lastIndexOf('\n', index - 1) + 1;
  if (PASTED_LINE.test(text.slice(lineStart, index + 1))) return false;
  return true;
}

/**
 * Read an explicit model or cost instruction from the prompt. Conservative by design: imperative
 * patterns only; nothing inside code, a quotation or a pasted line; nothing in a question or a
 * supposition; a negated or contradictory mention yields nothing, and two different models in one
 * clause are ambiguous. It is pure and offline.
 */
export function parseUserIntent(prompt: string | undefined | null): UserIntent {
  const text = withoutQuotedText((prompt ?? '').slice(0, INTENT_SCAN_CHARS));
  const intent: UserIntent = {};
  const named = new Map<string, string>();
  const candidates = [
    ...[...text.matchAll(MODEL_IMPERATIVE)].map((m) => ({ m, flag: false })),
    ...[...text.matchAll(MODEL_FLAG)].map((m) => ({ m, flag: true }))
  ];
  for (const { m, flag } of candidates) {
    const at = m.index!;
    const family = m[1].toLowerCase();
    const after = text.slice(at + m[0].length);
    if (!instructive(text, at)) continue;
    if (NEGATION.test(text.slice(Math.max(0, at - 30), at))) continue;
    if (STATEMENT_SUBJECT.test(text.slice(Math.max(0, at - 40), at))) continue;
    if (flag && !FLAG_VERB.test(text.slice(Math.max(0, at - 60), at))) continue;
    if ((family === 'haiku' || family === 'sonnet') && LITERARY_TAIL.test(after)) continue;
    named.set(family, m[0].trim());
    // Another model named later in the same clause ("use opus or sonnet", "sonnet for A and opus for B") leaves the choice open.
    const clause = /^[^.!?;\n]{0,80}/.exec(after)![0];
    for (const other of clause.matchAll(ANY_MODEL)) {
      const o = other[1].toLowerCase();
      if (o === family) continue;
      const lead = text.slice(Math.max(0, at + m[0].length + other.index! - 30), at + m[0].length + other.index!);
      if (NEGATION.test(lead) || REPLACED.test(lead)) continue;
      named.set(o, other[0].trim());
    }
  }
  if (named.size === 1) {
    const [model, phrase] = [...named][0];
    intent.model = model as 'haiku' | 'sonnet' | 'opus';
    intent.phrase = phrase;
  }
  for (const cost of text.matchAll(MINIMAL_COST)) {
    if (!instructive(text, cost.index!) || NEGATION.test(text.slice(Math.max(0, cost.index! - 30), cost.index!))) continue;
    intent.cost = 'minimal';
    intent.phrase ??= cost[0];
    break;
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
 * Apply the user's own words to Jev's recommendation, moving the tier by at most ONE step from the
 * router's (a parser that misreads a prompt can then cost one tier, not three). An explicit model
 * moves the tier towards that model's tier, `minimal` lowers it by one step, never below
 * `flash_lite`. When both are present the explicit model wins and `minimal` is ignored, so the
 * effect is capped at one instruction. Returns the input when there is nothing to apply.
 */
export function applyUserIntent(rec: Dispatch, intent: UserIntent): Dispatch {
  if (intent.model) {
    const wanted = MODEL_TO_TIER[intent.model];
    const from = TIER_ORDER.indexOf(rec.routerTier ?? rec.tier);
    const to = TIER_ORDER.indexOf(wanted);
    const tier = TIER_ORDER[Math.max(from - 1, Math.min(from + 1, to))];
    const model = tier === wanted ? intent.model : TIER_TO_CLAUDE[tier];
    return {
      ...rec,
      tier,
      model,
      routerTier: rec.routerTier ?? rec.tier,
      by: 'instruction-model',
      ...(tier === wanted ? {} : { asked: intent.model })
    };
  }
  if (intent.cost === 'minimal') {
    const tier = oneStepDown(rec.tier);
    if (tier === rec.tier) return { ...rec, by: 'instruction-cost' };
    return { ...rec, tier, model: TIER_TO_CLAUDE[tier], routerTier: rec.routerTier ?? rec.tier, by: 'instruction-cost' };
  }
  return rec;
}

/** A turn that delegates paid work: the rubric helps there and is noise on the rest. */
function isDelegationTurn(rec: Dispatch): boolean {
  return rec.subagent !== 'self' && rec.tier !== 'openrouter_free' && rec.by !== 'instruction-model';
}

/** The context line injected before the turn, with the stage rubric on delegation turns (5.3). */
export function buildDirective(
  rec: Dispatch,
  sessionModel: string | null,
  quotaStrategy: string,
  intent: UserIntent = {},
  ceiling?: string | null
): string {
  const effective = effectiveSessionModel(sessionModel, ceiling);
  const body = directiveBody(rec, effective, quotaStrategy, intent, effective !== sessionModel ? { session: sessionModel } : undefined);
  return isDelegationTurn(rec) ? `${body} ${STAGE_RUBRIC_TEXT}` : body;
}

// ── Turns that need no advice (5.5) ─────────────────────────────────────────────────────────────

/** The part of a prompt sent to Jev: pasted material must not dominate the decision. */
export const TASK_TRIM_CHARS = 2000;
/** A prompt this long is a paste, not a task: no advice at all. */
export const PASTE_SKIP_CHARS = 20_000;

/** Prompts the host injects on the user's behalf, never a task of the user's. */
const SYSTEM_TURN = /^\s*<(?:task-notification|local-command-[a-z]+|bash-(?:input|stdout|stderr)|system-reminder)\b/i;

/** True for a turn the host injected on the user's behalf (a task notification, a local command): not a new user turn. */
export function isHostInjectedPrompt(prompt: string): boolean {
  return SYSTEM_TURN.test(prompt);
}

/** True for a prompt that carries no delegation intent by its shape alone, before any Jev call. */
export function isNonTaskPrompt(prompt: string): boolean {
  return isHostInjectedPrompt(prompt) || prompt.length > PASTE_SKIP_CHARS;
}

/**
 * Generous on purpose: the cost of a missed hint is small, but the cost of a wrong silence is a
 * task that gets no advice. Anything that reads like work counts.
 */
const WORK_VERB =
  /\b(?:write|edit|fix|add|create|implement|refactor|run\s+(?:the|a|an|it|this|that|all|my|each|every|bun|npm|make|git)|build|port|update|delete|remove|rename|change|move|review|check|test|find|search|read|explain|plan|design|commit|push|open|start|fold|launch|spawn|delegate|proceed|go ahead|deploy|install|analy[sz]e|summari[sz]e|investigate|debug|migrate|draft|document|list|show|look|verify|clean|merge|rewrite|generate|convert|set up|workflow|workstreams?)\b/i;

/**
 * A status or acknowledgement turn: Jev says `self` on a cheap tier and the prompt holds no verb of
 * work ("A3 completed the run", "thanks, looks good"). The user's explicit instruction always counts.
 */
export function isStatusTurn(rec: Dispatch, task: string, intent: UserIntent = {}): boolean {
  if (intent.model || intent.cost) return false;
  const cheap = rec.tier === 'flash_lite' || rec.tier === 'openrouter_free';
  return rec.subagent === 'self' && cheap && !WORK_VERB.test(task);
}

/** What goes to Jev: the head of the prompt only. */
export function trimTask(task: string): string {
  return task.length > TASK_TRIM_CHARS ? task.slice(0, TASK_TRIM_CHARS) : task;
}

/** A short stable id of a turn, the same wherever the same prompt is seen. A hash, never the text. */
export function turnId(prompt: string): string {
  return new Bun.CryptoHasher('sha256').update(prompt.trim()).digest('hex').slice(0, 12);
}

// ── Stage plans (5.4) ───────────────────────────────────────────────────────────────────────────

/** A prompt that names a workflow or workstreams: staged work, where one tuple per turn is not enough. */
export function isStagedPrompt(prompt: string): boolean {
  return /\bwork-?flows?\b|\bwork-?streams?\b/i.test(prompt);
}

/**
 * The reminder for staged work. The router sees one prompt and returns one tuple; a plan with
 * several streams needs one tier per stream, which `jev_batch_dispatch` returns from the caller's own
 * classification (`kind`, `needsEdit`) and the stage rubric.
 */
export const STAGE_PLAN_REMINDER =
  'This looks like staged work. Before launching it, call jev_batch_dispatch with one task per stream ' +
  '(kind: mechanical | settled-edit | judgement; needsEdit: true when the stream edits files) and follow its tiers per stream, ' +
  'not the single recommendation above.';
