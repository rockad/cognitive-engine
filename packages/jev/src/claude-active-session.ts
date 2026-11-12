/**
 * The model of the Claude Code session an MCP tool call belongs to (case study 5.11).
 *
 * The MCP tools get no hook payload, so they cannot ask Claude Code for the session model the way
 * the guard does. Claude Code does put `CLAUDE_CODE_SESSION_ID` in the environment of the servers it
 * starts (checked on the running jev servers of two concurrent sessions: each held its own id), so
 * this reads the same evidence the guard uses (`resolveSessionModel`: the PostModelSwitch state
 * file, then the latest assistant turn of the session transcript), located from that id.
 *
 * Limits, all fail-safe because the tools only advise and the PreToolUse guard enforces:
 * - the id is fixed when the server starts, so a session that is cleared or resumed under a new id
 *   reads as unknown (no state, no transcript);
 * - the transcript is found by scanning `<config dir>/projects/*` for `<id>.jsonl`;
 * - nothing found means `null` and the caller does not clamp.
 */
import * as fs from 'node:fs';
import * as path from 'node:path';
import { userClaudeDir } from './agent-definitions.js';
import { compareModels, parseModel } from './models.js';
import { resolveSessionModel } from './session-model.js';
import type { JevTier } from './stage-rubric.js';

/** `<config dir>/projects/<encoded cwd>/<session id>.jsonl`, found without knowing the encoding. */
export function findTranscript(sessionId: string, env: NodeJS.ProcessEnv = process.env): string | null {
  if (!/^[A-Za-z0-9_-]{1,128}$/.test(sessionId)) return null;
  const projects = path.join(userClaudeDir(), 'projects');
  try {
    // The project dir is usually right: Claude Code names it after the path with every non-alphanumeric as "-".
    const hint = env.CLAUDE_PROJECT_DIR ? env.CLAUDE_PROJECT_DIR.replace(/[^A-Za-z0-9]/g, '-') : null;
    const names = fs.readdirSync(projects);
    for (const name of hint && names.includes(hint) ? [hint, ...names] : names) {
      const file = path.join(projects, name, `${sessionId}.jsonl`);
      if (fs.existsSync(file)) return file;
    }
  } catch {
    // no projects dir: unknown
  }
  return null;
}

/** The current Claude Code session's model, or null when it cannot be established. Never throws. */
export function activeClaudeSessionModel(env: NodeJS.ProcessEnv = process.env): string | null {
  try {
    const id = env.CLAUDE_CODE_SESSION_ID;
    if (!id) return null;
    return resolveSessionModel({ session_id: id, transcript_path: findTranscript(id, env) ?? undefined }).model;
  } catch {
    return null;
  }
}

/** The Jev tier a session model can run itself: haiku, sonnet, opus or fable; null when Jev cannot rank it. */
export function claudeModelTier(model: string | null | undefined): JevTier | null {
  const parsed = parseModel(model);
  if (parsed === 'unknown') return null;
  return parsed.family === 'haiku' ? 'flash_lite' : parsed.family === 'sonnet' ? 'flash' : 'pro';
}

/** Whether a session on this model is below opus (so an opus delegation is an escalation). Unknown counts as below. */
export function sessionBelowOpus(model: string | null | undefined): boolean {
  const parsed = parseModel(model);
  const opus = parseModel('opus');
  return parsed === 'unknown' || opus === 'unknown' || compareModels(parsed, opus) < 0;
}
