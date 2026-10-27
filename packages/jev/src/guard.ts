/**
 * Narrow subagent guard shared by the Claude Code and Antigravity PreToolUse hooks.
 *
 * It enforces two invariants and nothing else, deterministically and without network access:
 * - a subagent may not run on a stronger model than the session without user approval (Claude Code)
 * - the Pro tier always requires manual user approval (Antigravity)
 *
 * Everything else Jev does for a turn (advice, dispatch, telemetry) stays in the MCP tools.
 */
import * as fs from 'node:fs';

export interface Verdict {
  decision: 'ask';
  reason: string;
}

export type GuardHarness = 'claude' | 'antigravity';

const CLAUDE_RANK: ReadonlyArray<readonly [RegExp, number]> = [
  [/haiku/i, 1],
  [/sonnet/i, 2],
  [/opus/i, 3],
  [/fable/i, 4]
];

/** Relative strength of a Claude model id or alias; null when unknown or "inherit". */
export function claudeModelRank(model: string | undefined | null): number | null {
  if (!model) return null;
  const hit = CLAUDE_RANK.find(([pattern]) => pattern.test(model));
  return hit ? hit[1] : null;
}

export interface AgentCall {
  description?: string;
  prompt?: string;
  subagent_type?: string;
  model?: string;
}

/**
 * Claude Code Agent/Task call: ask when the requested model is stronger than the session's.
 * A missing model or "inherit" runs on the session model, so it passes. An unknown session
 * model cannot be compared, so it passes too (fail open).
 */
export function decideClaudeAgentCall(call: AgentCall, sessionModel: string | null): Verdict | null {
  const requested = claudeModelRank(call.model);
  const ceiling = claudeModelRank(sessionModel);
  if (requested === null || ceiling === null || requested <= ceiling) return null;
  return {
    decision: 'ask',
    reason:
      `[jev] This delegation asks for model "${call.model}", stronger than the session model "${sessionModel}". ` +
      'Approve to run it on the stronger model, or deny and re-run it on the session model or a cheaper one.'
  };
}

export interface AntigravitySubagent {
  Model?: string;
  [key: string]: unknown;
}

/** Antigravity invoke_subagent call: any `pro` subagent needs manual approval. */
export function decideAntigravitySubagents(subagents: AntigravitySubagent[]): Verdict | null {
  const pro = subagents.some((s) => String(s?.Model ?? '').toLowerCase() === 'pro');
  if (!pro) return null;
  return {
    decision: 'ask',
    reason: 'Pro tier model requested for subagent. Manual user approval required to preserve Gemini Pro quota.'
  };
}

/** The model of the most recent assistant turn in a Claude Code transcript, or null. */
export function sessionModelFromTranscript(transcriptPath: string | undefined): string | null {
  if (!transcriptPath) return null;
  try {
    const TAIL_BYTES = 256 * 1024;
    const fd = fs.openSync(transcriptPath, 'r');
    try {
      const { size } = fs.fstatSync(fd);
      const start = Math.max(0, size - TAIL_BYTES);
      const buf = Buffer.alloc(size - start);
      fs.readSync(fd, buf, 0, buf.length, start);
      const lines = buf.toString('utf8').split('\n');
      if (start > 0) lines.shift(); // first line is likely cut mid-record
      for (let i = lines.length - 1; i >= 0; i--) {
        if (!lines[i]) continue;
        try {
          const entry = JSON.parse(lines[i]);
          const model = entry?.type === 'assistant' ? entry?.message?.model : undefined;
          if (typeof model === 'string' && model && !model.startsWith('<')) return model;
        } catch {
          // skip partial or non-JSON line
        }
      }
    } finally {
      fs.closeSync(fd);
    }
  } catch {
    // unreadable transcript: unknown model
  }
  return null;
}

/** Claude Code PreToolUse input -> verdict. */
export function evaluateClaude(input: any): Verdict | null {
  if (input?.tool_name !== 'Agent' && input?.tool_name !== 'Task') return null;
  return decideClaudeAgentCall(input.tool_input ?? {}, sessionModelFromTranscript(input.transcript_path));
}

/** Antigravity PreToolUse input -> verdict. */
export function evaluateAntigravity(input: any): Verdict | null {
  if (input?.toolCall?.name !== 'invoke_subagent') return null;
  const subagents = input.toolCall.args?.Subagents;
  return Array.isArray(subagents) ? decideAntigravitySubagents(subagents) : null;
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
