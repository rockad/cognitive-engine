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
import { readAgentDefinition } from './agent-definitions.js';
import { assessModelRequest } from './models.js';
import { evaluateWorkflowInput } from './workflow-guard.js';
import { resolveSessionModel, sessionModelFromTranscript, type SessionModelSource } from './session-model.js';

export { sessionModelFromTranscript };

export interface Verdict {
  decision: 'ask';
  reason: string;
  /** Facts for the decision audit (tool, models, call sites). Never prompt or script text. */
  audit?: Record<string, unknown>;
}

export type GuardHarness = 'claude' | 'antigravity';

export interface AgentCall {
  description?: string;
  prompt?: string;
  subagent_type?: string;
  model?: string;
}

/** Rank from which a model counts as Pro tier on Antigravity (opus and above). */
const STRONG_RANK = 3;

/**
 * Claude Code Agent/Task call: ask when the model the subagent will run on is above the session's.
 * That is the call's own `model`, or, when the call sets none, the `model` in the subagent's
 * definition file. "inherit" or no model runs on the session model, so it passes.
 *
 * Ranking is version-aware (see models.ts). Equal or lower passes silently. A requested model Jev
 * cannot rank, or a session model that is unknown, asks.
 */
export function decideClaudeAgentCall(
  call: AgentCall,
  sessionModel: string | null,
  definitionModel: string | null = null,
  sessionModelSource: SessionModelSource = 'none'
): Verdict | null {
  const model = call.model !== undefined ? call.model : definitionModel;
  const assessment = assessModelRequest(model, sessionModel);
  if (assessment.kind === 'allowed') return null;
  const advice =
    assessment.kind === 'escalation'
      ? 'Approve to run it on the stronger model, or deny and re-run it on the session model or a cheaper one.'
      : 'Approve to run it, or deny and re-run it on a model Jev can rank.';
  return {
    decision: 'ask',
    reason: `[jev] This delegation uses ${assessment.reason}. ${advice}`,
    audit: { kind: assessment.kind, requested: model ?? null, sessionModel, sessionModelSource }
  };
}

/**
 * The `model:` from a subagent's definition (`<cwd>/.claude/agents/<name>.md`, then
 * `~/.claude/agents/<name>.md`), or null. The name is restricted to file-name characters.
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
export function evaluateClaudeWorkflow(input: any): Verdict | null {
  const session = resolveSessionModel(input);
  const decision = evaluateWorkflowInput(input.tool_input, input.cwd, session.model);
  if (decision.decision === 'allow') return null;
  return {
    decision: 'ask',
    reason: decision.message,
    audit: {
      kind: 'workflow',
      tool_name: 'Workflow',
      session_id: input.session_id,
      sessionModel: session.model,
      sessionModelSource: session.source,
      sites: decision.sites.map((s) => ({ file: s.file, line: s.line, col: s.col, reason: s.reason }))
    }
  };
}
/** Claude Code PreToolUse input -> verdict. */
export function evaluateClaude(input: any): Verdict | null {
  if (input?.tool_name === 'Workflow') return evaluateClaudeWorkflow(input);
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
  const verdict = decideClaudeAgentCall(
    call,
    session.model,
    call.model === undefined ? agentDefinitionModel(call.subagent_type, input.cwd) : null,
    session.source
  );
  return verdict ? { ...verdict, audit: { ...verdict.audit, tool_name: input.tool_name, session_id: input.session_id } } : null;
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
