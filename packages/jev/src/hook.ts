#!/usr/bin/env bun
/**
 * PreToolUse hook entrypoint: `bun hook.ts claude|antigravity`, hook JSON on stdin.
 *
 * Active in Jev modes `on` and `on-demand` for both harnesses, silent only in `off` (plan 3.1.3
 * review decision of 2026-10-28). Once active it never lets a call it cannot evaluate through
 * silently: malformed or empty input and internal errors produce an `ask`, so the user decides.
 * The matcher in each adapter limits the hook to calls that spawn subagents, so this only prompts
 * on those calls.
 */
import { readJevMode } from './core/config.js';
import {
  evaluateAntigravity,
  evaluateClaude,
  formatVerdict,
  guardErrorVerdict,
  type GuardHarness,
  type Verdict
} from './guard.js';
import { appendClaudeDecision } from './claude-audit.js';

const HARNESSES: Record<
  GuardHarness,
  { jevHarness: 'claude-code' | 'antigravity'; evaluate: (input: any) => Verdict | null }
> = {
  claude: { jevHarness: 'claude-code', evaluate: evaluateClaude },
  antigravity: { jevHarness: 'antigravity', evaluate: evaluateAntigravity }
};

function auditAsk(verdict: Verdict, stdin: string): void {
  let toolName: unknown;
  let sessionId: unknown;
  try {
    const parsed = JSON.parse(stdin);
    toolName = parsed?.tool_name;
    sessionId = parsed?.session_id;
  } catch {
    // unparseable input: audit without the tool name
  }
  appendClaudeDecision({ event: 'ask', tool_name: toolName, session_id: sessionId, ...verdict.audit, reason: verdict.reason });
}

export async function run(harnessArg: string | undefined, stdin: string): Promise<string | null> {
  const harness = HARNESSES[harnessArg as GuardHarness];
  if (!harness) return null;

  let verdict: Verdict | null;
  try {
    if ((await readJevMode(harness.jevHarness)) === 'off') return null;
    verdict = harness.evaluate(JSON.parse(stdin));
  } catch (err) {
    verdict = guardErrorVerdict(err);
    verdict.audit = { kind: 'guard-error' };
  }
  if (!verdict) return null;
  if (harnessArg === 'claude') auditAsk(verdict, stdin);
  return formatVerdict(harnessArg as GuardHarness, verdict);
}

if (import.meta.main) {
  let stdin = '';
  try {
    stdin = await Bun.stdin.text();
  } catch {
    // unreadable stdin: run() treats empty input as unevaluable and asks
  }
  const out = await run(process.argv[2], stdin).catch((err) => {
    const harness = process.argv[2] === 'antigravity' ? 'antigravity' : 'claude';
    return formatVerdict(harness, guardErrorVerdict(err));
  });
  if (out) process.stdout.write(`${out}\n`);
}
