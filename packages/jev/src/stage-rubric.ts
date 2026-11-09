/**
 * One deterministic per-stage rubric for choosing a Claude model (case study 5.3): a short, linked
 * copy of the standing rule "default sonnet, escalate only where a wrong judgement is expensive".
 * It is shared by the prompt advisor (advisor-logic.ts) and by `jev_batch_dispatch` on Claude Code
 * (tools/dispatch.ts), so the hook and the tool give the same answer for the same kind of work.
 *
 * Jev cannot read the user's own CLAUDE.md, so this table is the only place the rule lives here.
 * Keep it in step with that rule; the tests pin that all three lines are present.
 */
export type JevTier = 'openrouter_free' | 'flash_lite' | 'flash' | 'pro';
export type ClaudeModelAlias = 'haiku' | 'sonnet' | 'opus' | 'fable';

/** What a stage is, from the caller's own classification. */
export type StageKind = 'mechanical' | 'settled-edit' | 'judgement';

export interface StageRule {
  readonly tier: JevTier;
  readonly model: ClaudeModelAlias;
  /** The rubric line, as shown to the router and to the model. */
  readonly rule: string;
}

export const STAGE_RUBRIC: Readonly<Record<StageKind, StageRule>> = {
  mechanical: { tier: 'flash_lite', model: 'haiku', rule: 'mechanical edit or checklist update -> haiku' },
  'settled-edit': { tier: 'flash', model: 'sonnet', rule: 'spec or plan edit with a settled decision -> sonnet' },
  judgement: {
    tier: 'pro',
    model: 'opus',
    rule: 'judgement whose error is expensive (a reversal not yet decided, adversarial verification, synthesis across disagreeing sources) -> opus'
  }
};

export const STAGE_KINDS = Object.keys(STAGE_RUBRIC) as StageKind[];

/** The three rubric lines as one sentence, for the context line. */
export const STAGE_RUBRIC_TEXT = `Stage rubric, cheapest model that fits: ${STAGE_KINDS.map((k) => STAGE_RUBRIC[k].rule).join('; ')}.`;

export function isStageKind(value: unknown): value is StageKind {
  return typeof value === 'string' && Object.hasOwn(STAGE_RUBRIC, value);
}
