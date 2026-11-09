/**
 * Per-harness wording and tool names for the dispatch tools (case study 5.11).
 *
 * `jev_dispatch` and `jev_batch_dispatch` used to hard-code Antigravity's vocabulary
 * (`invoke_subagent`, "0 Gemini quota"). The table below keeps that text exactly for every harness
 * except Claude Code, which gets `Agent(subagent_type, model)` and, inside a Workflow, `agent()`.
 * Antigravity, OpenCode and the plain CLI are byte-for-byte what they were; tests pin both halves.
 */
import type { JevHarness } from '../core/index.js';

export interface DispatchStyle {
  /** The tool or call that spawns a paid subagent on this harness. */
  readonly delegationTool: 'invoke_subagent' | 'Agent';
  /** The router's description of the free tier. */
  readonly freeTierCriteria: string;
  /** Guidance for a task Jev routed to the free tier. `single` is `jev_dispatch`, else a batch task. */
  freeGuidance(single: boolean): string;
  /** Guidance for a task that needs a subagent. */
  delegateGuidance(role: string, model: string): string;
}

const ANTIGRAVITY_STYLE: DispatchStyle = {
  delegationTool: 'invoke_subagent',
  freeTierCriteria:
    'Lightweight summaries, drafts, syntax transformations, autonomous free agent tasks via jev_agent_free, and boilerplate ($0 cost, 0 Gemini quota)',
  freeGuidance: (single) =>
    single
      ? 'CRITICAL QUOTA RULE: Jev evaluated this task as free-tier eligible. Do NOT call invoke_subagent. Execute directly via jev_execute_free or jev_agent_free at $0.00 cost (0 Gemini quota).'
      : 'CRITICAL QUOTA RULE: Free-tier eligible. Do NOT call invoke_subagent. Execute directly via jev_execute_free or jev_agent_free at $0.00 cost (0 Gemini quota).',
  delegateGuidance: (role, model) => `Delegate to subagent via invoke_subagent(Role: "${role}", Model: "${model}").`
};

const CLAUDE_STYLE: DispatchStyle = {
  delegationTool: 'Agent',
  freeTierCriteria:
    'Lightweight summaries, drafts, syntax transformations, autonomous free agent tasks via jev_agent_free, and boilerplate ($0 cost, no Claude quota)',
  freeGuidance: () =>
    'Free-tier eligible. Do NOT spawn a paid Agent. Execute directly via jev_execute_free or jev_agent_free at $0.00 cost (no Claude quota); it returns text and cannot edit files.',
  // `self` is not a Claude Code subagent type: the closest real one is general-purpose.
  delegateGuidance: (role, model) => {
    const type = role === 'self' ? 'general-purpose' : role;
    return `Delegate via Agent(subagent_type: "${type}", model: "${model}") or, inside a Workflow, agent({ agentType: "${type}", model: "${model}" }).`;
  }
};

const STYLES: Readonly<Record<JevHarness, DispatchStyle>> = {
  antigravity: ANTIGRAVITY_STYLE,
  'claude-code': CLAUDE_STYLE,
  opencode: ANTIGRAVITY_STYLE,
  cli: ANTIGRAVITY_STYLE
};

export function dispatchStyle(harness: JevHarness): DispatchStyle {
  return STYLES[harness] ?? ANTIGRAVITY_STYLE;
}
