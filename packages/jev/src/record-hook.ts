#!/usr/bin/env bun
/**
 * Recorder hook for Claude Code: `bun record-hook.ts SessionStart|PostModelSwitch|PostToolUse`.
 *
 * - PostModelSwitch: writes the session's current model to the per-session state file the guard
 *   reads, and audits the change with its source. The payload fields come from the Claude Code
 *   binary's schema (the event never fired headless): `to_model`, `from_model`, `requested_model`,
 *   `source`. It reads `to_model` and falls back to `model`.
 * - SessionStart: carries no model. A fresh session (`startup`, `clear`) drops any stale state for
 *   its id. A `resume` keeps the state file but writes a resume marker: the model may have changed
 *   with no PostModelSwitch, so the guard discards evidence older than the marker. Old state files
 *   are pruned.
 * - PostToolUse (matcher `Workflow`): re-hashes the workflow files the PreToolUse guard analysed and
 *   reports a file that changed since (workflow-pin.ts). Silent unless there is drift or the guard
 *   did not run for the call (Jev off), in which case it does nothing.
 *
 * It only observes. It never blocks, prints only that drift message, and always exits 0: a failure
 * here must not affect the session, and the guard's transcript fallback covers a missing state file. It also
 * never interferes with Claude Code's own "switch models when a message is flagged" fallbacks;
 * those arrive as ordinary model changes (source `auto`) and are recorded like any other.
 */
import { appendClaudeDecision } from './claude-audit.js';
import { pruneWorkflowPins, recordPostToolUse } from './workflow-pin.js';
import { clearSessionState, markSessionResumed, pruneSessionState, writeSessionState } from './session-model.js';

const str = (v: unknown): string | undefined => (typeof v === 'string' && v ? v : undefined);

/** Handle one hook payload. Returns what it did, for tests; never throws. */
export function record(event: string | undefined, stdin: string, now: Date = new Date()): string {
  try {
    const input = JSON.parse(stdin);
    if (event === 'PostModelSwitch') {
      const to = str(input?.to_model) ?? str(input?.model);
      if (!to) return 'no-model';
      writeSessionState(input?.session_id, to, str(input?.source), now);
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
    if (event === 'PostToolUse') return recordPostToolUse(stdin).status;
    if (event === 'SessionStart') {
      const source = str(input?.source);
      if (source === 'startup' || source === 'clear') clearSessionState(input?.session_id);
      // A resume can change the model with no PostModelSwitch: mark it so older evidence is dropped.
      if (source === 'resume') markSessionResumed(input?.session_id, now);
      pruneSessionState();
      pruneWorkflowPins();
      return 'session-start';
    }
    return 'ignored';
  } catch {
    return 'error';
  }
}

if (import.meta.main) {
  try {
    const stdin = await Bun.stdin.text();
    if (process.argv[2] === 'PostToolUse') {
      const result = recordPostToolUse(stdin);
      if (result.output) process.stdout.write(`${result.output}\n`);
    } else {
      record(process.argv[2], stdin);
    }
  } catch {
    // log-only: nothing to do
  }
  process.exit(0);
}
