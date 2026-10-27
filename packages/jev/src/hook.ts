#!/usr/bin/env bun
/**
 * PreToolUse hook entrypoint: `bun hook.ts claude|antigravity`, hook JSON on stdin.
 *
 * Active only when the Jev mode for the harness is `on`. Fail-open by design: on any error, an
 * unknown harness or no verdict it prints nothing and exits 0, so the harness carries on as if
 * Jev were not installed.
 */
import { readJevMode } from './core/config.js';
import { evaluateAntigravity, evaluateClaude, formatVerdict, type GuardHarness, type Verdict } from './guard.js';

const HARNESSES: Record<GuardHarness, { jevHarness: 'claude-code' | 'antigravity'; evaluate: (input: any) => Verdict | null }> = {
  claude: { jevHarness: 'claude-code', evaluate: evaluateClaude },
  antigravity: { jevHarness: 'antigravity', evaluate: evaluateAntigravity }
};

export async function run(harnessArg: string | undefined, stdin: string): Promise<string | null> {
  const harness = HARNESSES[harnessArg as GuardHarness];
  if (!harness) return null;
  const verdict = harness.evaluate(JSON.parse(stdin));
  if (!verdict) return null;
  if ((await readJevMode(harness.jevHarness)) !== 'on') return null;
  return formatVerdict(harnessArg as GuardHarness, verdict);
}

if (import.meta.main) {
  try {
    const out = await run(process.argv[2], await Bun.stdin.text());
    if (out) process.stdout.write(`${out}\n`);
  } catch {
    // fail open
  }
}
