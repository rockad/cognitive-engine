/**
 * Content binding for the Workflow guard (script TOCTOU).
 *
 * The PreToolUse hook analyses the workflow text it reads; Claude Code reads the file again to run
 * it, and the hook cannot hold the file still in between. What it can do, and does:
 * - re-analyse the CURRENT content on every call, so a scriptPath that was edited since an earlier
 *   call (or a resume of it) is judged on what it says now, never on an earlier verdict;
 * - bind each decision to the sha256 of the text it analysed: the ask text, the audit record and a
 *   pin (below) all carry the hashes, never the text;
 * - pin those hashes under the call's `tool_use_id`, and let a PostToolUse recorder re-hash the
 *   files after the run and report any drift (an audit event and a user-visible message).
 *
 * Limits: the drift check is detection after the fact, not prevention; a file swapped and put back
 * inside the window is not seen; an inline `script` has no file to re-check; what Claude Code runs
 * on a `resumeFromRunId` (the file or a saved copy of the earlier run) is not observable here.
 *
 * Nothing in this module reports unless the guard ran and Jev is still on: with Jev off the hook
 * returns before it evaluates anything, so no pin exists, and a pin written while Jev was on is
 * dropped without a word if Jev was switched off before the run ended (the recorder reads the
 * current mode).
 */
import * as crypto from 'node:crypto';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { appendClaudeDecision } from './claude-audit.js';
import { inspectJevConfigFile, resolveJevModeFromConfig, type JevConfigFileInspection } from './core/config.js';
import { claudeStateDir, safeSessionId } from './session-model.js';
import { MAX_SCRIPT_BYTES, type ScriptFact } from './workflow-guard.js';

export interface WorkflowPin {
  tool_use_id: string;
  session_id?: string;
  decision: 'allow' | 'ask';
  at: string;
  scripts: ScriptFact[];
}

export function pinDir(): string {
  return path.join(claudeStateDir(), 'workflow-pins');
}

function pinFile(toolUseId: unknown): string | null {
  const id = safeSessionId(toolUseId);
  return id ? path.join(pinDir(), `${id}.json`) : null;
}

/** Write the pin for a Workflow call. Best effort: false when there is no usable tool_use_id or the write failed. */
export function writeWorkflowPin(toolUseId: unknown, sessionId: unknown, decision: 'allow' | 'ask', scripts: ScriptFact[]): boolean {
  const file = pinFile(toolUseId);
  if (!file || typeof toolUseId !== 'string') return false;
  try {
    fs.mkdirSync(path.dirname(file), { recursive: true });
    const pin: WorkflowPin = { tool_use_id: toolUseId, ...(typeof sessionId === 'string' ? { session_id: sessionId } : {}), decision, at: new Date().toISOString(), scripts };
    const tmp = `${file}.${process.pid}.tmp`;
    fs.writeFileSync(tmp, JSON.stringify(pin));
    fs.renameSync(tmp, file);
    return true;
  } catch {
    return false;
  }
}

function readPin(toolUseId: unknown): WorkflowPin | null {
  const file = pinFile(toolUseId);
  if (!file) return null;
  try {
    const parsed = JSON.parse(fs.readFileSync(file, 'utf8'));
    return Array.isArray(parsed?.scripts) ? (parsed as WorkflowPin) : null;
  } catch {
    return null;
  }
}

function dropPin(toolUseId: unknown): void {
  const file = pinFile(toolUseId);
  if (!file) return;
  try {
    fs.rmSync(file, { force: true });
  } catch {
    // nothing to drop
  }
}

/** Delete pins not touched for two weeks (a run that never reached PostToolUse). Best effort. */
export function pruneWorkflowPins(now = Date.now(), maxAgeMs = 14 * 24 * 60 * 60 * 1000): void {
  try {
    for (const name of fs.readdirSync(pinDir())) {
      const file = path.join(pinDir(), name);
      try {
        if (now - fs.statSync(file).mtimeMs > maxAgeMs) fs.rmSync(file, { force: true });
      } catch {
        // skip
      }
    }
  } catch {
    // no pin dir yet
  }
}

/** sha256 of a file's current content, or null when it is gone, unreadable or over the analysis size limit. */
function currentHash(file: string): string | null {
  try {
    if (fs.statSync(file).size > MAX_SCRIPT_BYTES) return null;
    return crypto.createHash('sha256').update(fs.readFileSync(file, 'utf8')).digest('hex');
  } catch {
    return null;
  }
}

export interface PostToolUseResult {
  status: 'unchanged' | 'changed' | 'no-pin' | 'off' | 'ignored' | 'error';
  /** JSON for the hook's stdout; present only on drift. */
  output?: string;
}

/**
 * Whether Jev is off for Claude Code right now, as the guard hook derives it from the user config
 * (a missing file is off). A config that cannot be trusted is NOT off: the guard asks loudly in
 * that state, so a drift report is not suppressed either.
 */
function jevIsOff(inspect: () => JevConfigFileInspection): boolean {
  const inspection = inspect();
  if (inspection.state === 'missing') return true;
  if (inspection.state === 'malformed') return false;
  return resolveJevModeFromConfig(inspection.config, 'claude-code') === 'off';
}

/**
 * PostToolUse for Workflow: re-hash every file the PreToolUse analysis read and compare. Consumes
 * the pin. Never throws. Silent (`no-pin`) when the guard did not run for this call, and silent
 * (`off`, the pin dropped) when Jev has been switched off since: a pin written while Jev was on
 * is not reported after the user turned it off.
 */
export function recordPostToolUse(stdin: string, inspect: () => JevConfigFileInspection = inspectJevConfigFile): PostToolUseResult {
  try {
    const input = JSON.parse(stdin);
    if (input?.tool_name !== 'Workflow') return { status: 'ignored' };
    const pin = readPin(input?.tool_use_id);
    if (!pin) return { status: 'no-pin' };
    dropPin(input?.tool_use_id);
    if (jevIsOff(inspect)) return { status: 'off' };
    const drift: Array<{ file: string; analysed: string; current: string | null }> = [];
    for (const s of pin.scripts) {
      if (typeof s?.label !== 'string' || !path.isAbsolute(s.label)) continue; // inline: no file to re-check
      const current = currentHash(s.label);
      if (current !== s.sha256) drift.push({ file: s.label, analysed: s.sha256, current });
    }
    if (drift.length === 0) return { status: 'unchanged' };
    for (const d of drift) {
      appendClaudeDecision({
        event: 'workflow_changed',
        tool_name: 'Workflow',
        tool_use_id: pin.tool_use_id,
        session_id: pin.session_id,
        file: d.file,
        analysed_sha256: d.analysed,
        current_sha256: d.current,
        decision: pin.decision
      });
    }
    const lines = drift.map((d) => `- ${d.file}: analysed sha256:${d.analysed.slice(0, 16)}, now ${d.current ? `sha256:${d.current.slice(0, 16)}` : 'missing or unreadable'}`);
    const message =
      `[jev] A workflow file changed between the guard's analysis (${pin.decision}) and the end of the run, so what ran may not be what was checked:\n${lines.join('\n')}\n` +
      'Review the file, and re-run the workflow if it should be checked again.';
    return { status: 'changed', output: JSON.stringify({ systemMessage: message }) };
  } catch {
    return { status: 'error' };
  }
}
