/**
 * Append-only JSONL audit of the Claude Code escalation guard.
 *
 * One line per `ask` and per observed model change. Records carry decisions and model names only:
 * never prompt text, script text or environment values. Writing is best effort and can never
 * change a decision, so a full disk or unwritable path does not turn into an allow or a crash.
 */
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';

export interface ClaudeDecisionRecord {
  event: 'ask' | 'model_change';
  [key: string]: unknown;
}

/** Path of the audit file; JEV_CLAUDE_DECISIONS_FILE overrides it (tests). */
export function claudeDecisionsFile(): string {
  return process.env.JEV_CLAUDE_DECISIONS_FILE || path.join(process.env.HOME || os.homedir(), '.config', 'jev', 'claude_decisions.jsonl');
}

export function appendClaudeDecision(record: ClaudeDecisionRecord): boolean {
  try {
    const file = claudeDecisionsFile();
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.appendFileSync(file, `${JSON.stringify({ ts: new Date().toISOString(), ...record })}\n`);
    return true;
  } catch {
    return false;
  }
}
