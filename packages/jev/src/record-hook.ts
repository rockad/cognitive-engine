#!/usr/bin/env bun
/**
 * Log-only recorder hook for Claude Code: `bun record-hook.ts SessionStart|PostModelSwitch`.
 *
 * - PostModelSwitch: writes the session's current model to the per-session state file the guard
 *   reads, and audits the change with its source. The payload fields come from the Claude Code
 *   binary's schema (the event never fired headless): `to_model`, `from_model`, `requested_model`,
 *   `source`. It reads `to_model` and falls back to `model`.
 * - SessionStart: carries no model. A fresh session (`startup`, `clear`) drops any stale state for
 *   its id; `resume` keeps it. Old state files are pruned.
 *
 * It only observes. It never blocks, never prints, and always exits 0: a failure here must not
 * affect the session, and the guard's transcript fallback covers a missing state file. It also
 * never interferes with Claude Code's own "switch models when a message is flagged" fallbacks;
 * those arrive as ordinary model changes (source `auto`) and are recorded like any other.
 */
import { appendClaudeDecision } from './claude-audit.js';
import { clearSessionState, pruneSessionState, writeSessionState } from './session-model.js';

const str = (v: unknown): string | undefined => (typeof v === 'string' && v ? v : undefined);

/** Handle one hook payload. Returns what it did, for tests; never throws. */
export function record(event: string | undefined, stdin: string): string {
  try {
    const input = JSON.parse(stdin);
    if (event === 'PostModelSwitch') {
      const to = str(input?.to_model) ?? str(input?.model);
      if (!to) return 'no-model';
      writeSessionState(input?.session_id, to, str(input?.source));
      appendClaudeDecision({
        event: 'model_change',
        session_id: str(input?.session_id),
        from_model: str(input?.from_model),
        to_model: to,
        requested_model: str(input?.requested_model),
        source: str(input?.source)
      });
      return 'recorded';
    }
    if (event === 'SessionStart') {
      const source = str(input?.source);
      if (source === 'startup' || source === 'clear') clearSessionState(input?.session_id);
      pruneSessionState();
      return 'session-start';
    }
    return 'ignored';
  } catch {
    return 'error';
  }
}

if (import.meta.main) {
  try {
    record(process.argv[2], await Bun.stdin.text());
  } catch {
    // log-only: nothing to do
  }
  process.exit(0);
}
