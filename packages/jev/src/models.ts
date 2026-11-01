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
