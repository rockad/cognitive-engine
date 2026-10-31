#!/usr/bin/env bun
/**
 * PreToolUse hook entrypoint: `bun hook.ts claude|antigravity`, hook JSON on stdin.
 *
 * Silent unless the Jev mode for the harness is `on`. Once on, it never lets a call it cannot
 * evaluate through silently: malformed input or an internal error produces an `ask`, so the user
 * decides. The matcher in each adapter limits the hook to subagent calls, so this only prompts
 * on calls that spawn subagents.
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

const HARNESSES: Record<GuardHarness, { jevHarness: 'claude-code' | 'antigravity'; evaluate: (input: any) => Verdict | null }> = {
  claude: { jevHarness: 'claude-code', evaluate: evaluateClaude },
  antigravity: { jevHarness: 'antigravity', evaluate: evaluateAntigravity }
};

export async function run(harnessArg: string | undefined, stdin: string): Promise<string | null> {
  const harness = HARNESSES[harnessArg as GuardHarness];
  if (!harness) return null;

  let verdict: Verdict | null;
  try {
    if ((await readJevMode(harness.jevHarness)) !== 'on') return null;
    verdict = harness.evaluate(JSON.parse(stdin));
  } catch (err) {
    verdict = guardErrorVerdict(err);
  }
  return verdict ? formatVerdict(harnessArg as GuardHarness, verdict) : null;
}

if (import.meta.main) {
  try {
    const out = await run(process.argv[2], await Bun.stdin.text());
    if (out) process.stdout.write(`${out}\n`);
  } catch {
    // stdin could not be read at all: nothing to evaluate
  }
}
