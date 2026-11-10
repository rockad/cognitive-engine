/**
 * Version-aware ranking of Claude model ids and aliases, used by the escalation guard.
 *
 * Rank is family-major (fable > opus > sonnet > haiku), then numeric version (5.5 > 5 > 4.8).
 * The tables below are data: to support a new model, add a row to KNOWN_MODEL_IDS or move an
 * alias in ALIASES. Anything not in them is "unknown", and the guard turns unknown into an ask
 * rather than guessing a rank.
 */

export type ModelFamily = 'fable' | 'opus' | 'sonnet' | 'haiku';

export interface ParsedModel {
  family: ModelFamily;
  version: number[];
}

/** Families from weakest to strongest. */
const FAMILY_ORDER: readonly ModelFamily[] = ['haiku', 'sonnet', 'opus', 'fable'];

/** What each alias currently resolves to. */
export const ALIASES: Readonly<Record<string, ParsedModel>> = {
  fable: { family: 'fable', version: [5, 1] },
  opus: { family: 'opus', version: [5, 5] },
  sonnet: { family: 'sonnet', version: [5, 5] },
  haiku: { family: 'haiku', version: [5, 5] }
};

/** Canonical full ids (no date suffix, no context-window suffix). */
export const KNOWN_MODEL_IDS: Readonly<Record<string, ParsedModel>> = {
  'claude-fable-5-1': { family: 'fable', version: [5, 1] },
  'claude-opus-5-5': { family: 'opus', version: [5, 5] },
  'claude-opus-5': { family: 'opus', version: [5] },
  'claude-opus-4-8': { family: 'opus', version: [4, 8] },
  'claude-sonnet-5-5': { family: 'sonnet', version: [5, 5] },
  'claude-sonnet-5': { family: 'sonnet', version: [5] },
  'claude-haiku-5-5': { family: 'haiku', version: [5, 5] }
};

export type ParseResult = ParsedModel | 'unknown';

/** Parse a model id or alias. Date suffixes (`-20261028`) and `[1m]`-style suffixes are tolerated. */
export function parseModel(model: string | null | undefined): ParseResult {
  if (typeof model !== 'string') return 'unknown';
  let id = model.trim().toLowerCase();
  id = id.replace(/\[[^\]]*\]$/, '').trim();
  id = id.replace(/-\d{8}$/, '');
  if (!id) return 'unknown';
  const hit = Object.hasOwn(ALIASES, id) ? ALIASES[id] : Object.hasOwn(KNOWN_MODEL_IDS, id) ? KNOWN_MODEL_IDS[id] : undefined;
  return hit ? { family: hit.family, version: [...hit.version] } : 'unknown';
}

/** Numeric compare of two version arrays; a missing component counts as 0 (5 equals 5.0). */
export function compareVersions(a: readonly number[], b: readonly number[]): number {
  const len = Math.max(a.length, b.length);
  for (let i = 0; i < len; i++) {
    const d = (a[i] ?? 0) - (b[i] ?? 0);
    if (d !== 0) return d < 0 ? -1 : 1;
  }
  return 0;
}

/** Negative when a is weaker than b, zero when equal, positive when a is stronger. */
export function compareModels(a: ParsedModel, b: ParsedModel): number {
  const f = FAMILY_ORDER.indexOf(a.family) - FAMILY_ORDER.indexOf(b.family);
  if (f !== 0) return f < 0 ? -1 : 1;
  return compareVersions(a.version, b.version);
}

export function formatParsedModel(m: ParsedModel): string {
  return `${m.family} ${m.version.join('.')}`;
}

/** True for the values that mean "run on the session model": absent, empty or `inherit`. */
export function inheritsSessionModel(model: string | null | undefined): boolean {
  if (model === undefined || model === null) return true;
  const m = String(model).trim().toLowerCase();
  return m === '' || m === 'inherit';
}

export type ModelAssessment =
  | { kind: 'allowed' }
  | { kind: 'escalation'; reason: string }
  | { kind: 'unknown-model'; reason: string }
  | { kind: 'unknown-session'; reason: string };

/**
 * Policy: any request strictly above the session model asks. Equal or lower is allowed silently.
 * An unknown requested model, or an unknown session model, asks. "inherit" and an absent model
 * run on the session model and are allowed.
 */
export function assessModelRequest(requested: string | null | undefined, sessionModel: string | null | undefined): ModelAssessment {
  if (inheritsSessionModel(requested)) return { kind: 'allowed' };
  const req = parseModel(requested);
  if (req === 'unknown') {
    return { kind: 'unknown-model', reason: `model "${requested}" is not a model Jev can rank` };
  }
  const session = sessionModel ? parseModel(sessionModel) : 'unknown';
  if (session === 'unknown') {
    return {
      kind: 'unknown-session',
      reason: `model "${requested}" cannot be compared with the session model${sessionModel ? ` "${sessionModel}", which Jev cannot rank` : ', which could not be determined'}`
    };
  }
  if (compareModels(req, session) > 0) {
    return {
      kind: 'escalation',
      reason: `model "${requested}" (${formatParsedModel(req)}) is stronger than the session model "${sessionModel}" (${formatParsedModel(session)})`
    };
  }
  return { kind: 'allowed' };
}

/**
 * What a subagent that INHERITS the session model runs on, checked against a project ceiling: the
 * real session model and the ceiling a project file set.
 */
export interface InheritCheck {
  sessionModel: string | null;
  ceiling: string;
}

/**
 * Assess a subagent that runs on the session model (no model, `inherit`, a built-in type, a
 * definition without a model) against a project ceiling. Allowed when the session model is not above
 * the ceiling. A session model above it is an escalation. A session model that is unknown or that
 * Jev cannot rank cannot be compared with the ceiling, so it asks.
 */
export function assessInherited(check: InheritCheck): ModelAssessment {
  const session = check.sessionModel ? parseModel(check.sessionModel) : 'unknown';
  const ceiling = parseModel(check.ceiling);
  if (session === 'unknown' || ceiling === 'unknown') {
    return {
      kind: 'unknown-session',
      reason: `the subagent inherits the session model, which ${check.sessionModel ? `("${check.sessionModel}") Jev cannot rank` : 'could not be determined'}, so it cannot be checked against the project ceiling "${check.ceiling}"`
    };
  }
  if (compareModels(session, ceiling) > 0) {
    return {
      kind: 'escalation',
      reason: `the subagent inherits the session model "${check.sessionModel}" (${formatParsedModel(session)}), which is stronger than the project ceiling "${check.ceiling}" (${formatParsedModel(ceiling)})`
    };
  }
  return { kind: 'allowed' };
}

// ── Environment overrides of the subagent model ───────────────────────────────────────────────────
//
// Claude Code lets the environment set the model of a subagent: `CLAUDE_CODE_SUBAGENT_MODEL` (the
// default for subagents that name no model), `CLAUDE_CODE_SUBAGENT_MODEL_FORCE` (that model replaces
// the call's and the definition's) and, for agents spawned inside a Workflow,
// `CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL`. The guard reads the hook process's environment, which is
// assumed to match the one Claude Code runs with. The public documentation (https://code.claude.com/docs/en/sub-agents)
// orders the model as call > definition > CLAUDE_CODE_SUBAGENT_MODEL > session, and says FORCE alone
// gives the session model; the FORCE details below (workflow agents, the exact `inherit`) were read
// from the Claude Code 2.1.321 binary, so treat them as version-bound.

/** The subagent-model variables as the hook process sees them. `null` means unset, empty or `inherit`. */
export interface SubagentModelEnv {
  /** `CLAUDE_CODE_SUBAGENT_MODEL`. */
  model: string | null;
  /** `CLAUDE_CODE_SUBAGENT_MODEL_FORCE` is truthy (`1`, `true`, `yes`, `on`, as Claude Code reads it). */
  force: boolean;
  /** `CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL`. */
  workflowModel: string | null;
}

const envModel = (v: string | undefined): string | null => {
  const m = v?.trim() ?? '';
  return m === '' || m.toLowerCase() === 'inherit' ? null : m;
};

export function readSubagentModelEnv(env: Readonly<Record<string, string | undefined>> = process.env): SubagentModelEnv {
  return {
    model: envModel(env.CLAUDE_CODE_SUBAGENT_MODEL),
    force: ['1', 'true', 'yes', 'on'].includes((env.CLAUDE_CODE_SUBAGENT_MODEL_FORCE ?? '').trim().toLowerCase()),
    workflowModel: envModel(env.CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL)
  };
}

export type ModelSource = 'call' | 'definition' | 'env' | 'env-forced' | 'workflow-env' | 'session';

/** A model a subagent may run on, and where it comes from. `model` null means the session model. */
export interface ModelCandidate {
  model: string | null | undefined;
  source: ModelSource;
}

/** True when a call's `model` is set to something: Claude Code reads an empty or null one as unset. */
export function callSetsModel(model: unknown): boolean {
  return model !== undefined && model !== null && !(typeof model === 'string' && model.trim() === '');
}

/**
 * Every model a subagent call can run on, given what the call, its definition and the environment
 * say. Every candidate must be ranked; the list has one entry except when two forced sources
 * disagree about which wins (a Workflow agent with both a workflow model and a forced model set).
 *
 * - Workflow agents: `CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL` replaces the call's and the definition's
 *   model.
 * - `CLAUDE_CODE_SUBAGENT_MODEL_FORCE` on: the forced model replaces the call's and the definition's;
 *   only the exact string `model: "inherit"` on an Agent-tool call (not in a workflow) still gives the
 *   session model. With no model set, the session model.
 * - Otherwise: the call's model, else the definition's (an explicit `inherit` there is the session
 *   model, not the variable), else `CLAUDE_CODE_SUBAGENT_MODEL`, else the session model.
 */
export function subagentModelCandidates(
  input: { call?: unknown; definition?: string | null; workflow?: boolean },
  env: SubagentModelEnv = readSubagentModelEnv()
): ModelCandidate[] {
  if (input.workflow && env.workflowModel) {
    const out: ModelCandidate[] = [{ model: env.workflowModel, source: 'workflow-env' }];
    if (env.force && env.model && env.model !== env.workflowModel) out.push({ model: env.model, source: 'env-forced' });
    return out;
  }
  if (env.force) {
    // Claude Code checks the exact string 'inherit' (no trim, no case folding), and only on the Agent
    // tool: a Workflow agent() drops its model under FORCE, `inherit` included, and gets the forced one.
    if (!input.workflow && input.call === 'inherit') return [{ model: null, source: 'call' }];
    return env.model ? [{ model: env.model, source: 'env-forced' }] : [{ model: null, source: 'session' }];
  }
  if (callSetsModel(input.call)) return [{ model: input.call as string, source: 'call' }];
  if (input.definition !== undefined && input.definition !== null && input.definition.trim() !== '') {
    return [{ model: input.definition, source: 'definition' }];
  }
  return env.model ? [{ model: env.model, source: 'env' }] : [{ model: null, source: 'session' }];
}

/** The clause an ask reason adds when the model being ranked came from the environment, else ''. */
export function modelSourceNote(source: ModelSource): string {
  switch (source) {
    case 'env-forced':
      return ' (CLAUDE_CODE_SUBAGENT_MODEL_FORCE is on, so the subagent runs on CLAUDE_CODE_SUBAGENT_MODEL and the call\'s own model is ignored)';
    case 'env':
      return ' (this is CLAUDE_CODE_SUBAGENT_MODEL, which applies because neither the call nor its definition sets a model)';
    case 'workflow-env':
      return ' (this is CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL, which replaces the model of every agent in a workflow)';
    default:
      return '';
  }
}
