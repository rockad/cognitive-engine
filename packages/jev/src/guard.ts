/**
 * Narrow subagent guard shared by the Claude Code and Antigravity PreToolUse hooks.
 *
 * This hook is the only enforcement point for the tier ceiling. It runs outside the agent, so the
 * agent cannot talk its way past it; only the user's answer to its prompt lets a call through.
 * It enforces two invariants and nothing else, deterministically and without network access:
 * - a subagent may not run on a stronger model than the session without user approval (Claude Code)
 * - the Pro tier always requires manual user approval (Antigravity)
 *
 * It answers `ask`, never allow: when it cannot decide (unreadable input, unknown session model for
 * a strong tier, an internal error) it asks rather than letting the call through silently.
 *
 * Everything else Jev does for a turn (advice, dispatch, telemetry) is advice from the MCP tools.
 */
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { modelTierRank } from './core/governance.js';

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

/** Rank from which a model counts as "strong" (opus and above) when the session model is unknown. */
const STRONG_RANK = 3;

/**
 * Claude Code Agent/Task call: ask when the model the subagent will run on is stronger than the
 * session's. That is the call's own `model`, or, when the call sets none, the `model` in the
 * subagent's definition file. "inherit" runs on the session model, so it passes.
 *
 * When the session model is unknown the two cannot be compared, so the guard fails closed for the
 * strong tiers (opus and above) and lets weaker requests through.
 */
export function decideClaudeAgentCall(
  call: AgentCall,
  sessionModel: string | null,
  definitionModel: string | null = null
): Verdict | null {
  const model = call.model !== undefined ? call.model : definitionModel;
  const requested = claudeModelRank(model);
  if (requested === null) return null;

  const ceiling = claudeModelRank(sessionModel);
  if (ceiling === null) {
    if (requested < STRONG_RANK) return null;
    return {
      decision: 'ask',
      reason:
        `[jev] This delegation runs on model "${model}" and the session model could not be determined. ` +
        'Approve to run it, or deny and re-run it on a cheaper model.'
    };
  }
  if (requested <= ceiling) return null;
  return {
    decision: 'ask',
    reason:
      `[jev] This delegation runs on model "${model}", stronger than the session model "${sessionModel}". ` +
      'Approve to run it on the stronger model, or deny and re-run it on the session model or a cheaper one.'
  };
}

/**
 * The `model:` from a subagent's definition (`<cwd>/.claude/agents/<name>.md`, then
 * `~/.claude/agents/<name>.md`), or null. The name is restricted to file-name characters.
 */
export function agentDefinitionModel(subagentType: string | undefined, cwd: string | undefined): string | null {
  if (!subagentType || !/^[A-Za-z0-9_.-]+$/.test(subagentType)) return null;
  const dirs = [cwd ? path.join(cwd, '.claude', 'agents') : null, path.join(process.env.HOME || os.homedir(), '.claude', 'agents')];
  for (const dir of dirs) {
    if (!dir) continue;
    try {
      const text = fs.readFileSync(path.join(dir, `${subagentType}.md`), 'utf8');
      const front = /^---\r?\n([\s\S]*?)\r?\n---/.exec(text)?.[1];
      const model = front && /^model:\s*(.+?)\s*$/m.exec(front)?.[1]?.replace(/^["']|["']$/g, '');
      if (model) return model;
    } catch {
      // no definition here
    }
  }
  return null;
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
  const call: AgentCall = input.tool_input ?? {};
  return decideClaudeAgentCall(
    call,
    sessionModelFromTranscript(input.transcript_path),
    call.model === undefined ? agentDefinitionModel(call.subagent_type, input.cwd) : null
  );
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
