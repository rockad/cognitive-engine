#!/usr/bin/env bun
/**
 * Claude Code advisor hook for Jev: `bun advisor-hook.ts user-prompt-submit`, hook JSON on stdin.
 *
 * Ported from the agentic-hub Jev plugin (claude/hook.ts). Registered for UserPromptSubmit in
 * adapters/claude/hooks/hooks.json. It advises subagent / model / effort through `additionalContext`.
 *
 * - Active only in Jev mode "on". The PreToolUse guard (hook.ts) is separate and independent: this
 *   hook never asks, never denies and never blocks.
 * - Every path is best-effort: on any error, or when Jev is unreachable within the budget, it
 *   prints nothing and exits 0, so Claude Code carries on as if Jev were not installed.
 * - The session model comes from the same evidence the guard uses (session-model.ts), written by
 *   the record-hook.ts recorders. This hook does not write session state.
 */
import * as os from 'node:os';
import * as path from 'node:path';
import {
  callJev,
  readJevMode,
  readQuotaView,
  loadEnvFile,
  fetchRecallMemory,
  logJevTelemetry,
  checkEgress,
  egressContextDirs,
  type JevMode,
  type JevTelemetryEvent
} from './core/index.js';
import { appendClaudeDecision } from './claude-audit.js';
import { resolveSessionModel } from './session-model.js';
import { writeLastAdvice, type LastAdvice } from './advisor-state.js';
import {
  applyUserIntent,
  buildDirective,
  dispatchQuestions,
  parseUserIntent,
  parseDispatch,
  resolveClaudeSubagentCriteria,
  isNonTaskPrompt,
  isStatusTurn,
  trimTask,
  turnId,
  type Dispatch
} from './advisor-logic.js';

const JEV_DIR = path.join(process.env.HOME || os.homedir(), '.config', 'jev');
/** Well inside Claude Code's 30 s UserPromptSubmit budget; a slow Jev must not stall the prompt. */
export const JEV_HOOK_TIMEOUT_MS = 1500;

export type HookInput = {
  session_id?: string;
  transcript_path?: string;
  cwd?: string;
  prompt?: string;
};

export type HookOutput = { hookSpecificOutput: Record<string, unknown> } | null;

export type HookDeps = {
  readMode: () => Promise<JevMode>;
  readSessionModel: (input: HookInput) => string | null;
  readQuotaStrategy: () => Promise<string>;
  dispatch: (task: string, quotaStrategy: string, criteria?: Record<string, string>) => Promise<Dispatch | null>;
  log: (entry: Record<string, unknown>) => void;
  /** Remember the advice for the session so a later hook can join it with what was used. */
  remember?: (sessionId: string | null, advice: LastAdvice) => void;
  /** Egress policy for the hook's working directory; absent means no policy. */
  egressAllowed?: (cwd: string | undefined) => boolean;
};

export function withTimeout<T>(
  pOrFn: Promise<T> | ((signal: AbortSignal) => Promise<T>),
  ms: number,
  externalController?: AbortController
): Promise<T | null> {
  const controller = externalController || new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeoutPromise = new Promise<null>((resolve) => {
    timer = setTimeout(() => {
      controller.abort();
      resolve(null);
    }, ms);
  });
  const promise = typeof pOrFn === 'function' ? pOrFn(controller.signal) : pOrFn;
  return Promise.race([promise, timeoutPromise]).finally(() => {
    if (timer) clearTimeout(timer);
  });
}

export async function onUserPromptSubmit(input: HookInput, deps: HookDeps): Promise<HookOutput> {
  if ((await deps.readMode()) !== 'on') return null;
  const prompt = (input.prompt || '').trim();
  if (prompt.length < 8) return null;
  // Host-injected turns and pasted blobs carry no task: no advice and no Jev call (5.5).
  if (isNonTaskPrompt(prompt)) return null;
  const sessionId = typeof input.session_id === 'string' ? input.session_id : null;
  if (deps.egressAllowed && !deps.egressAllowed(input.cwd)) {
    deps.log({ hook: 'UserPromptSubmit', session_id: sessionId, egress: 'blocked' });
    return null;
  }
  const quotaStrategy = await deps.readQuotaStrategy();
  const criteria = await resolveClaudeSubagentCriteria(input.cwd);
  const rec = await deps.dispatch(trimTask(prompt), quotaStrategy, criteria);
  if (!rec) return null;
  // The user's own words first: an explicit model or cost instruction changes what is advised.
  const intent = parseUserIntent(prompt);
  // A status turn ("A3 completed the run") gets no line. An instruction in the prompt always does.
  if (isStatusTurn(rec, prompt, intent)) return null;
  const sessionModel = deps.readSessionModel(input);
  const advised = applyUserIntent(rec, intent);
  const turn = turnId(prompt);
  deps.log({
    hook: 'UserPromptSubmit',
    session_id: sessionId,
    turn,
    subagent: advised.subagent,
    tier: advised.tier,
    effort: advised.effort,
    confidence: advised.confidence,
    ...(advised.routerTier ? { routerTier: advised.routerTier } : {}),
    ...(advised.by ? { by: advised.by } : {}),
    recommended_model: advised.model,
    quota: quotaStrategy,
    sessionModel
  });
  deps.remember?.(sessionId, {
    turn,
    tier: advised.tier,
    model: advised.model,
    subagent: advised.subagent,
    ...(advised.by ? { by: advised.by } : {}),
    at: new Date().toISOString()
  });

  let additionalContext = buildDirective(advised, sessionModel, quotaStrategy, intent);
  try {
    const memory = await withTimeout(fetchRecallMemory(), 600);
    if (memory && memory.activeGoal) {
      additionalContext += `\n\n[CROSS-HARNESS MEMORY]\nActive Goal: ${memory.activeGoal}\nCurrent Milestone: ${memory.currentMilestone || 'none'}\nNext Action: ${memory.nextAction || 'none'}`;
    }
  } catch {
    // best-effort
  }

  return {
    hookSpecificOutput: {
      hookEventName: 'UserPromptSubmit',
      additionalContext
    }
  };
}

async function liveDispatch(task: string, quotaStrategy: string, criteria?: Record<string, string>): Promise<Dispatch | null> {
  try {
    const data = await withTimeout(
      (signal) =>
        callJev(
          `Task: ${task}\n\nQuota strategy for this machine: ${quotaStrategy}.`,
          dispatchQuestions(criteria),
          signal
        ),
      JEV_HOOK_TIMEOUT_MS
    );
    return data ? parseDispatch(data, task, quotaStrategy, criteria) : null;
  } catch {
    return null;
  }
}

/**
 * The shared telemetry record for one advice (case study 5.7). `modelRecommended` is the field that
 * says what it is: UserPromptSubmit recommends a model, it does not use one. `modelUsed` is kept as
 * a deprecated alias of the same value for one release so existing readers of the file keep
 * working; drop it in 2.5. `turn` joins the record to the PreToolUse record of the same turn.
 */
export function adviceTelemetry(entry: Record<string, unknown>): JevTelemetryEvent {
  const str = (v: unknown) => (typeof v === 'string' ? v : undefined);
  const recommended = str(entry.recommended_model);
  return {
    harness: 'claude',
    toolOrHook: String(entry.hook || 'hook'),
    sessionId: str(entry.session_id) ?? null,
    turn: str(entry.turn),
    taskSummary: str(entry.subagent) ? `subagent: ${entry.subagent}` : undefined,
    tierRecommended: str(entry.tier),
    modelRecommended: recommended,
    modelUsed: recommended,
    quotaStrategy: str(entry.quota)
  };
}

function appendLog(entry: Record<string, unknown>): void {
  try {
    // Never prompt text: the entry holds the recommendation, model names and a prompt hash only.
    appendClaudeDecision({ ...entry, event: 'advice' });
    void logJevTelemetry(adviceTelemetry(entry));
  } catch {
    // logging is best-effort
  }
}

const liveDeps: HookDeps = {
  readMode: () => readJevMode('claude-code'),
  readSessionModel: (input) => resolveSessionModel(input).model,
  readQuotaStrategy: async () => {
    try {
      return (await readQuotaView()).strategy;
    } catch {
      return 'unknown';
    }
  },
  dispatch: liveDispatch,
  log: appendLog,
  remember: writeLastAdvice,
  egressAllowed: (cwd) => checkEgress(egressContextDirs([cwd])).allowed
};

async function main(event: string | undefined): Promise<void> {
  // The key is picked up the same way the MCP server and the OpenCode plugin do.
  loadEnvFile(path.join(JEV_DIR, 'env'));
  loadEnvFile(path.resolve(import.meta.dir, '..', '.env'));
  const input = JSON.parse((await Bun.stdin.text()) || '{}') as HookInput;
  const output = event === 'user-prompt-submit' ? await onUserPromptSubmit(input, liveDeps) : null;
  if (output) process.stdout.write(JSON.stringify(output));
}

if (import.meta.main) {
  try {
    await main(process.argv[2]);
  } catch {
    // never fail the host turn on Jev
  }
  process.exit(0);
}
