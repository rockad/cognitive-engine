/**
 * Narrow subagent guard shared by the Claude Code and Antigravity PreToolUse hooks.
 *
 * This hook is the only enforcement point for the tier ceiling. It runs outside the agent, so the
 * agent cannot talk its way past it; only the user's answer to its prompt lets a call through.
 * It enforces two invariants and nothing else, deterministically and without network access:
 * - a subagent may not run on a stronger model than the session without user approval (Claude Code)
 * - the Pro tier always requires manual user approval (Antigravity)
 *
 * It answers `ask`, never allow: when it cannot decide (unreadable input, an unknown model, an
 * unknown session model, an internal error) it asks rather than letting the call through silently.
 *
 * Everything else Jev does for a turn (advice, dispatch, telemetry) is advice from the MCP tools.
 */
import { modelTierRank } from './core/governance.js';
import { BUILTIN_AGENT_TYPES, describeAgentLookup, effortReason, missingAgentDetail, readAgentDefinition } from './agent-definitions.js';
import { assessInherited, assessModelRequest, inheritsSessionModel, modelSourceNote, readSubagentModelEnv, subagentModelCandidates, type InheritCheck, type ModelSource, type SubagentModelEnv } from './models.js';
import { evaluateWorkflowInput, type ScriptFact } from './workflow-guard.js';
import { resolveSessionModel, sessionModelFromTranscript, type SessionModel, type SessionModelSource } from './session-model.js';
import { lowerModel } from './project-config.js';

export { sessionModelFromTranscript };

export interface Verdict {
  decision: 'ask';
  reason: string;
  /** Facts for the decision audit (tool, models, call sites). Never prompt or script text. */
  audit?: Record<string, unknown>;
}

export type GuardHarness = 'claude' | 'antigravity';

/** Which config file could not be trusted: the user's or a project's. */
export type ConfigScope = 'user' | 'project';

/**
 * Facts the evaluation leaves for the hook entrypoint beyond the verdict: for a Workflow call, the
 * decision and the hashes of the script text analysed (see workflow-pin.ts). A caller that does not
 * pass one gets none, and `evaluateClaude` then has no side effects.
 */
export interface GuardContext {
  /** In: a valid project config's `guard.ceiling` (project-config.ts) and the file it came from. */
  projectCeiling?: string | null;
  projectCeilingFile?: string;
  /** Out: for a Workflow call, the decision and the script hashes (workflow-pin.ts). */
  workflow?: { decision: 'allow' | 'ask'; scripts: ScriptFact[] };
}

/**
 * The session model the call is compared against: the lower of the real session model and the
 * project ceiling, with a note for the ask text when the ceiling is what lowered it. Without a
 * ceiling (or when the session model is unknown or unrankable) it is the session model unchanged.
 */
function ceilingFor(session: SessionModel, ctx: GuardContext | undefined): { model: string | null; note: string; inherit: InheritCheck | null } {
  const ceiling = ctx?.projectCeiling;
  if (!ceiling) return { model: session.model, note: '', inherit: null };
  // What a subagent that inherits the session model runs on is the REAL session model, which the
  // ceiling can be below: it is ranked against the ceiling too.
  const inherit: InheritCheck = { sessionModel: session.model, ceiling };
  const model = lowerModel(session.model, ceiling);
  const note =
    model !== session.model
      ? ` The project ceiling "${ceiling}"${ctx?.projectCeilingFile ? ` (${ctx.projectCeilingFile})` : ''} is below the session model "${session.model}", so it is the limit here; a project file can only tighten the ceiling.`
      : '';
  return { model, note, inherit };
}

export interface AgentCall {
  description?: string;
  prompt?: string;
  subagent_type?: string;
  model?: string;
  /** The reasoning effort for this call: a level (`low` to `max`) or, on newer Claude Code, `lower`/`higher`. */
  effort?: unknown;
}

/** What else the Agent decision reads besides the call, its definition's model and the session model. */
export interface AgentDecisionContext {
  /** The `effort:` of the subagent's definition (agent-definitions.ts), when it has one. */
  definitionEffort?: string | null;
  /** The subagent-model environment variables; default: this process's (models.ts). */
  env?: SubagentModelEnv;
}

/** Rank from which a model counts as Pro tier on Antigravity (opus and above). */
const STRONG_RANK = 3;

/**
 * Claude Code Agent/Task call: ask when the model the subagent will run on is above the session's,
 * or when its effort is above the interim threshold.
 *
 * Model: the call's own `model`, or, when the call sets none, the `model` in the subagent's
 * definition file, or, when that sets none either, `CLAUDE_CODE_SUBAGENT_MODEL`. With
 * `CLAUDE_CODE_SUBAGENT_MODEL_FORCE` on, that variable's model replaces the call's and the
 * definition's, so a forced model is ranked like any other and never bypasses the ceiling
 * (`subagentModelCandidates`, models.ts). "inherit" or no model runs on the session model, so it
 * passes, unless a project ceiling is set (`inherit`): then the session model is ranked against the
 * ceiling. Ranking is version-aware (see models.ts). Equal or lower passes silently. A requested
 * model Jev cannot rank, or a session model that is unknown, asks.
 *
 * Effort: the call's `effort` and the definition's `effort:` (`ctx.definitionEffort`) are each
 * checked with `effortReason`, the same threshold the Workflow guard applies (RFC 007, interim).
 */
export function decideClaudeAgentCall(
  call: AgentCall,
  sessionModel: string | null,
  definitionModel: string | null = null,
  sessionModelSource: SessionModelSource = 'none',
  inherit: InheritCheck | null = null,
  ctx: AgentDecisionContext = {}
): Verdict | null {
  const candidates = subagentModelCandidates({ call: call.model, definition: definitionModel }, ctx.env ?? readSubagentModelEnv());
  let failed: { assessment: Exclude<ReturnType<typeof assessModelRequest>, { kind: 'allowed' }>; source: ModelSource; model: string | null | undefined } | null = null;
  for (const c of candidates) {
    // A call that runs on the session model is ranked against the project ceiling when there is one.
    const a = inherit && inheritsSessionModel(c.model) ? assessInherited(inherit) : assessModelRequest(c.model, sessionModel);
    if (a.kind !== 'allowed') {
      failed = { assessment: a, source: c.source, model: c.model };
      break;
    }
  }
  const effortReasons: string[] = [];
  if (call.effort !== undefined) {
    const r = effortReason(call.effort, 'Agent call', true);
    if (r) effortReasons.push(r);
  }
  if (ctx.definitionEffort !== undefined && ctx.definitionEffort !== null) {
    const r = effortReason(ctx.definitionEffort, `the definition of "${call.subagent_type}"`);
    if (r) effortReasons.push(r);
  }
  if (failed === null && effortReasons.length === 0) return null;
  const reasons = [...(failed ? [failed.assessment.reason + modelSourceNote(failed.source)] : []), ...effortReasons.map((r) => r.replace(/\.$/, ''))];
  const advice = [
    ...(failed
      ? [
          failed.assessment.kind === 'escalation'
            ? 'Approve to run it on the stronger model, or deny and re-run it on the session model or a cheaper one.'
            : 'Approve to run it, or deny and re-run it on a model Jev can rank.'
        ]
      : []),
    ...(effortReasons.length > 0 ? ['Approve to run it at that effort, or deny and re-run it at a lower one.'] : [])
  ];
  const shown = failed ?? { source: candidates[0].source, model: candidates[0].model };
  return {
    decision: 'ask',
    reason: `[jev] This delegation needs approval: ${reasons.join('; ')}. ${advice.join(' ')}`,
    audit: {
      kind: failed ? failed.assessment.kind : 'effort',
      requested: shown.model ?? null,
      modelSource: shown.source,
      sessionModel,
      sessionModelSource,
      ...(effortReasons.length > 0
        ? { effort: { call: call.effort === undefined ? null : typeof call.effort === 'string' ? call.effort : typeof call.effort, definition: ctx.definitionEffort ?? null } }
        : {})
    }
  };
}

/**
 * When the type is not a built-in and no definition can be found, its model and its `effort:` cannot
 * be read (it may come from `--agents <json>`, which Jev cannot see), so ask whatever model the call
 * names and whether or not CLAUDE_CODE_SUBAGENT_MODEL_FORCE is on (the Workflow guard does the same).
 * No `subagent_type` is the general-purpose default and passes.
 */
function unknownAgentVerdict(call: AgentCall, cwd: string | undefined): Verdict | null {
  const type: unknown = call.subagent_type;
  if (type === undefined || type === null) return null;
  const definition = typeof type === 'string' ? readAgentDefinition(type, cwd) : { found: false, model: null };
  if (definition.found) return null;
  if (typeof type === 'string' && BUILTIN_AGENT_TYPES.has(type)) return null;
  const label = typeof type === 'string' ? JSON.stringify(type) : `a non-string value (${typeof type})`;
  return {
    decision: 'ask',
    reason:
      `[jev] The subagent type ${label} is not a built-in agent and ${missingAgentDetail(describeAgentLookup(cwd))} ` +
      'Approve only if that agent is trusted to run on the session model or a cheaper one and not at a high effort, or deny and use an agent type Jev can read.',
    audit: { kind: 'unknown-agent', subagent_type: typeof type === 'string' ? type : null }
  };
}

/**
 * The `model:` from a subagent's definition (project `.claude/agents` from the cwd up to the project
 * root, then the user config dir (`CLAUDE_CONFIG_DIR`, else `~/.claude`) agents, then installed plugins), or null. See agent-definitions.ts.
 */
export function agentDefinitionModel(subagentType: string | undefined, cwd: string | undefined): string | null {
  return readAgentDefinition(subagentType, cwd).model;
}

export interface AntigravitySubagent {
  Model?: string;
  [key: string]: unknown;
}

/**
 * Antigravity invoke_subagent call: any subagent on the Pro tier needs manual approval. That is
 * `pro` or any Pro/Opus-class model name; a missing Model or `inherit` runs on the session model.
 */
export function decideAntigravitySubagents(subagents: AntigravitySubagent[]): Verdict | null {
  const pro = subagents.some((s) => {
    const model = String(s?.Model ?? '').trim().toLowerCase();
    return model !== '' && model !== 'inherit' && modelTierRank(model) >= STRONG_RANK;
  });
  if (!pro) return null;
  return {
    decision: 'ask',
    reason: 'Pro tier model requested for subagent. Manual user approval required to preserve Gemini Pro quota.'
  };
}

/** Claude Code Workflow call -> verdict (see workflow-guard.ts). Shape mismatches ask and are audited. */
export function evaluateClaudeWorkflow(input: any, sink?: GuardContext): Verdict | null {
  const session = resolveSessionModel(input);
  const effective = ceilingFor(session, sink);
  const decision = evaluateWorkflowInput(input.tool_input, input.cwd, effective.model, undefined, effective.inherit);
  if (sink) sink.workflow = { decision: decision.decision, scripts: decision.scripts };
  if (decision.decision === 'allow') return null;
  return {
    decision: 'ask',
    reason: decision.message + effective.note,
    audit: {
      kind: 'workflow',
      tool_name: 'Workflow',
      session_id: input.session_id,
      sessionModel: session.model,
      ...(sink?.projectCeiling ? { projectCeiling: sink.projectCeiling } : {}),
      sessionModelSource: session.source,
      scripts: decision.scripts,
      sites: decision.sites.map((s) => ({ file: s.file, line: s.line, col: s.col, reason: s.reason }))
    }
  };
}
/** Claude Code PreToolUse input -> verdict. */
export function evaluateClaude(input: any, sink?: GuardContext): Verdict | null {
  if (typeof input?.tool_name !== 'string') {
    return {
      decision: 'ask',
      reason: '[jev] The hook input has no tool name, so the call could not be evaluated. Approve only if you intended it.',
      audit: { kind: 'malformed-input' }
    };
  }
  if (input.tool_name === 'Workflow') return evaluateClaudeWorkflow(input, sink);
  if (input?.tool_name !== 'Agent' && input?.tool_name !== 'Task') return null;
  const raw = input.tool_input ?? {};
  if (typeof raw !== 'object' || Array.isArray(raw)) {
    return {
      decision: 'ask',
      reason: `[jev] The ${input.tool_name} call's input could not be read. Approve only if you intended it.`,
      audit: { kind: 'malformed-input', tool_name: input.tool_name }
    };
  }
  const call = raw as AgentCall;
  const session = resolveSessionModel(input);
  const effective = ceilingFor(session, sink);
  const facts = { tool_name: input.tool_name, session_id: input.session_id, ...(sink?.projectCeiling ? { projectCeiling: sink.projectCeiling } : {}) };
  const env = readSubagentModelEnv();
  // Asked whatever the call's model or FORCE: the definition's `effort:` applies either way and Claude
  // Code also loads agents from `--agents <json>`, which the guard cannot see.
  const unknown = unknownAgentVerdict(call, input.cwd);
  if (unknown) return { ...unknown, audit: { ...unknown.audit, ...facts } };
  // The definition is read whatever the call's model: its `effort:` applies either way.
  const definition = readAgentDefinition(call.subagent_type, input.cwd);
  const verdict = decideClaudeAgentCall(call, effective.model, definition.model, session.source, effective.inherit, { definitionEffort: definition.effort, env });
  return verdict ? { ...verdict, reason: verdict.reason + effective.note, audit: { ...verdict.audit, ...facts } } : null;
}

/** Antigravity PreToolUse input -> verdict. A malformed subagent list fails closed (ask). */
export function evaluateAntigravity(input: any): Verdict | null {
  if (input?.toolCall?.name !== 'invoke_subagent') return null;
  const subagents = input.toolCall.args?.Subagents;
  if (!Array.isArray(subagents)) {
    return { decision: 'ask', reason: '[jev] The subagent list of this call could not be read. Approve only if you intended it.' };
  }
  return decideAntigravitySubagents(subagents);
}

/** The verdict used when the guard itself fails on a call it is registered for. */
export function guardErrorVerdict(err: unknown): Verdict {
  return {
    decision: 'ask',
    reason: `[jev] The subagent guard could not evaluate this call (${err instanceof Error ? err.message : String(err)}). Approve only if you intended it.`
  };
}

/**
 * The verdict for every guarded call while the Jev config cannot be trusted. Never an allow and
 * never limited to escalations: the user cannot know whether Jev is meant to be on or off.
 */
export function configMalformedVerdict(file: string, problems: string[], scope: ConfigScope = 'user'): Verdict {
  const list = problems.map((p) => `- ${p}`).join('\n');
  if (scope === 'project') {
    return {
      decision: 'ask',
      reason:
        `[jev] The project Jev config ${file} cannot be read, so the subagent guard cannot tell what it tightens and asks on every guarded call.\n` +
        `Problems:\n${list}\n` +
        'Fix: repair the file or remove it. A project file can only tighten the user config (a lower `guard.ceiling` model such as ' +
        '{"guard":{"ceiling":"sonnet"}}, a mode that is not lower than yours); it can never turn the guard off or raise the ceiling. ' +
        'Approve this call only if you intended it.',
      audit: { kind: 'project-config-malformed', file, problems }
    };
  }
  const stray = problems.some((p) => p.startsWith("top-level 'mode'"));
  return {
    decision: 'ask',
    reason:
      `[jev] The Jev config ${file} is malformed, so the subagent guard cannot tell whether Jev is on or off and asks on every guarded call.\n` +
      `Problems:\n${list}\n` +
      `Fix: run \`/jev status\` to see what Jev reads, then repair the file to the expected shape ` +
      `{"defaults":{"mode":"on-demand"}}${stray ? ' (move the top-level "mode" under "defaults")' : ''}, ` +
      `or set \`/jev off\` explicitly to silence the guard. Approve this call only if you intended it.`,
    audit: { kind: 'config-malformed', file, problems }
  };
}

/** The JSON each harness expects on stdout for a verdict. */
export function formatVerdict(harness: GuardHarness, verdict: Verdict): string {
  if (harness === 'claude') {
    return JSON.stringify({
      hookSpecificOutput: {
        hookEventName: 'PreToolUse',
        permissionDecision: verdict.decision,
        permissionDecisionReason: verdict.reason
      }
    });
  }
  return JSON.stringify({ decision: 'force_ask', reason: verdict.reason });
}
