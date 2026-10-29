#!/usr/bin/env bun
/**
 * PreToolUse hook entrypoint: `bun hook.ts claude|antigravity`, hook JSON on stdin.
 *
 * Active in Jev modes `on` and `on-demand` for both harnesses, silent only in `off` (plan 3.1.3
 * review decision of 2026-10-28). Once active it never lets a call it cannot evaluate through
 * silently: malformed or empty input and internal errors produce an `ask`, so the user decides.
 * A Jev config that cannot be trusted (validateJevConfig) is neither off nor on: every guarded
 * call asks, loudly, until the file is fixed or Jev is set off explicitly.
 * The matcher in each adapter limits the hook to calls that spawn subagents, so this only prompts
 * on those calls.
 */
import { inspectJevConfigFile, readJevMode } from './core/config.js';
import {
  configMalformedVerdict,
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

/** Whether the hook payload is one of the calls this guard is registered for. Unparseable input counts as guarded. */
function isGuardedCall(harnessArg: string, stdin: string): boolean {
  try {
    const parsed = JSON.parse(stdin);
    if (harnessArg === 'claude') {
      const name = parsed?.tool_name;
      return typeof name !== 'string' || name === 'Agent' || name === 'Task' || name === 'Workflow';
    }
    const name = parsed?.toolCall?.name;
    return typeof name !== 'string' || name === 'invoke_subagent';
  } catch {
    return true;
  }
}

/**
 * The Jev config exists but cannot be trusted (validateJevConfig). The loader would silently read
 * it as some default mode, possibly `off`, so the guard fails loudly on every guarded call: an
 * `ask` naming the file and the problems, a stderr line, and a `config_malformed` audit event.
 */
function configMalformedResponse(harnessArg: GuardHarness, file: string, problems: string[], stdin: string): string | null {
  if (!isGuardedCall(harnessArg, stdin)) return null;
  const verdict = configMalformedVerdict(file, problems);
  process.stderr.write(`[jev] config ${file} is malformed (${problems.join('; ')}): the subagent guard asks on every guarded call. Run /jev status, or set jev off.\n`);
  if (harnessArg === 'claude') {
    let toolName: unknown;
    let sessionId: unknown;
    try {
      const parsed = JSON.parse(stdin);
      toolName = parsed?.tool_name;
      sessionId = parsed?.session_id;
    } catch {
      // unparseable input: audit without the tool name
    }
    appendClaudeDecision({ event: 'config_malformed', tool_name: toolName, session_id: sessionId, file, problems });
  }
  return formatVerdict(harnessArg, verdict);
}

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
    const inspection = inspectJevConfigFile();
    if (inspection.state === 'malformed') {
      return configMalformedResponse(harnessArg as GuardHarness, inspection.file, inspection.problems, stdin);
    }
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
