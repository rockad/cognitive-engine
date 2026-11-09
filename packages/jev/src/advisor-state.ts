/**
 * The last advice given in a session, so a later hook can join "recommended" with "used"
 * (case study 5.7 and 5.10). The UserPromptSubmit advisor writes it; the PreToolUse reminder reads it.
 *
 * It lives next to the session-model state (`<session_id>.advice.json` in `claudeStateDir()`), is
 * pruned with it, and holds model names, a tier and a prompt hash: never prompt text. Reading and
 * writing are best effort and can never fail a turn.
 */
import * as fs from 'node:fs';
import * as path from 'node:path';
import { claudeStateDir, safeSessionId } from './session-model.js';

export interface LastAdvice {
  /** `turnId` of the prompt the advice was given for. */
  turn: string;
  tier: string;
  /** The model recommended, after the user's own instruction was applied. */
  model: string;
  subagent: string;
  /** What set the tier: absent for the router, else the user's instruction. */
  by?: string;
  /** ISO time the advice was given. */
  at: string;
}

function adviceFile(sessionId: unknown): string | null {
  const id = safeSessionId(sessionId);
  return id ? path.join(claudeStateDir(), `${id}.advice.json`) : null;
}

export function writeLastAdvice(sessionId: unknown, advice: LastAdvice): boolean {
  const file = adviceFile(sessionId);
  if (!file) return false;
  try {
    fs.mkdirSync(path.dirname(file), { recursive: true });
    const tmp = `${file}.${process.pid}.tmp`;
    fs.writeFileSync(tmp, JSON.stringify(advice));
    fs.renameSync(tmp, file);
    return true;
  } catch {
    return false;
  }
}

export function readLastAdvice(sessionId: unknown): LastAdvice | null {
  const file = adviceFile(sessionId);
  if (!file) return null;
  try {
    const raw = JSON.parse(fs.readFileSync(file, 'utf8'));
    const ok = ['turn', 'tier', 'model', 'subagent', 'at'].every((k) => typeof raw?.[k] === 'string');
    return ok ? (raw as LastAdvice) : null;
  } catch {
    return null;
  }
}
