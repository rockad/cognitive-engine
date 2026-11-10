#!/usr/bin/env bun
/**
 * Claude Code advisor hook for Jev: `bun advisor-hook.ts user-prompt-submit|pre-tool-use`, hook JSON on stdin.
 *
 * Ported from the agentic-hub Jev plugin (claude/hook.ts). Registered in adapters/claude/hooks/hooks.json:
 * - user-prompt-submit  advises subagent / model / effort through `additionalContext`.
 * - pre-tool-use        (Agent, Task, Workflow) logs the model requested next to the last recommendation
 *                       and may add a pre-flight reminder. It is not the guard: it never asks or denies.
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
import { STAGE_RUBRIC_TEXT } from './stage-rubric.js';
import {
  clearTurnState,
  currentTurn,
  hasRecentDispatch,
  readLastAdvice,
  readTurnState,
  recentAgentCalls,
  writeLastAdvice,
  writeTurnState,
  type LastAdvice,
  type TurnState
} from './advisor-state.js';
import {
  buildDirective,
  dispatchQuestions,
  isStagedPrompt,
  STAGE_PLAN_REMINDER,
  parseUserIntent,
  resolveModel,
  parseDispatch,
  resolveClaudeSubagentCriteria,
  isHostInjectedPrompt,
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
  tool_name?: string;
  tool_input?: { model?: unknown; subagent_type?: unknown };
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
  /** Store the turn marker (turn id, mode, intent) at the start of a real user turn. */
  markTurn?: (sessionId: string | null, state: TurnState) => void;
  /** Drop the turn marker when the turn runs with Jev off. */
  forgetTurn?: (sessionId: string | null) => void;
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
  const sessionId = typeof input.session_id === 'string' ? input.session_id : null;
  if ((await deps.readMode()) !== 'on') {
    // This turn runs without the advisor: nothing stored for an earlier turn may speak for it.
    deps.forgetTurn?.(sessionId);
    return null;
  }
  const prompt = (input.prompt || '').trim();
  // A host-injected turn (a task notification) is not a new user turn: it keeps the turn marker of the prompt it belongs to.
  if (isHostInjectedPrompt(prompt)) return null;
  // The user's own words first: an explicit model or cost instruction changes what is advised. The marker is
  // stored before anything can return early, so the PreToolUse hook of this turn never reads an earlier turn's.
  const intent = parseUserIntent(prompt);
  const turn = turnId(prompt);
  deps.markTurn?.(sessionId, {
    turn,
    mode: 'on',
    intent: { ...(intent.model ? { model: intent.model } : {}), ...(intent.cost ? { cost: intent.cost } : {}) },
    at: new Date().toISOString()
  });
  if (prompt.length < 8) return null;
  // Pasted blobs carry no task: no advice and no Jev call (5.5).
  if (isNonTaskPrompt(prompt)) return null;
  if (deps.egressAllowed && !deps.egressAllowed(input.cwd)) {
    deps.log({ hook: 'UserPromptSubmit', session_id: sessionId, egress: 'blocked' });
    return null;
  }
  const quotaStrategy = await deps.readQuotaStrategy();
  const criteria = await resolveClaudeSubagentCriteria(input.cwd);
  const rec = await deps.dispatch(trimTask(prompt), quotaStrategy, criteria);
  if (!rec) return null;
  // A status turn ("A3 completed the run") gets no line. An instruction in the prompt always does.
  if (isStatusTurn(rec, prompt, intent)) return null;
  const sessionModel = deps.readSessionModel(input);
  const { advised, rule } = resolveModel({ userIntent: intent, rec, sessionModel });
  const staged = isStagedPrompt(prompt);
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
    rule,
    quota: quotaStrategy,
    sessionModel
  });
  deps.remember?.(sessionId, {
    turn,
    tier: advised.tier,
    model: advised.model,
    subagent: advised.subagent,
    ...(advised.by ? { by: advised.by } : {}),
    ...(staged ? { staged: true } : {}),
    at: new Date().toISOString()
  });

  let additionalContext = buildDirective(advised, sessionModel, quotaStrategy, intent);
  if (staged) additionalContext += ` ${STAGE_PLAN_REMINDER}`;
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

// ── PreToolUse: pre-flight reminder and recommended-versus-used log (5.10) ──────────────────────

export type PreToolDeps = {
  readMode: () => Promise<JevMode>;
  /** The last advice given in this session, to join with what is requested now. */
  lastAdvice: (sessionId: string | null) => LastAdvice | null;
  /** The marker of the turn this call is in: advice or intent that does not belong to it is stale and ignored. */
  turnState: (sessionId: string | null) => TurnState | null;
  /** A `jev_dispatch` or `jev_batch_dispatch` call from Claude Code shows in the shared telemetry. */
  recentDispatch: () => boolean;
  /** This session's recent agent calls, and whether one already carried a reminder. */
  recentAgentCalls: (sessionId: string | null) => { count: number; reminded: boolean };
  log: (entry: Record<string, unknown>) => void;
};

/** Agent calls in the window before a lone one counts as part of a staged plan. */
export const STAGED_AGENT_CALLS = 2;

/** What the reminder says. It names the tool and the rubric and never blocks anything. */
export function preflightReminder(toolName: string): string {
  return (
    `[JEV] ${toolName} is about to delegate, and no jev_dispatch or jev_batch_dispatch call from Claude Code shows in the last 30 minutes. ` +
    `For staged work, call jev_batch_dispatch with one task per stream first (kind: mechanical | settled-edit | judgement; needsEdit: true when the stream edits files) and follow its tiers. ${STAGE_RUBRIC_TEXT} ` +
    'This is a reminder, not a block.'
  );
}

/**
 * Registered for Agent, Task and Workflow, next to the guard and independent of it. It never asks,
 * denies or blocks: its only outputs are an `agent_call` record (the model requested next to the
 * last recommendation, joined by `turn`) and, when it looks like staged work with no dispatch call,
 * one `additionalContext` reminder. Staged means a Workflow, the third agent call of the session
 * in ten minutes, or a session whose last prompt named a workflow or workstreams. A reminder is sent once per ten minutes per session, so a long run is not nagged.
 */
export async function onPreToolUse(input: HookInput, deps: PreToolDeps): Promise<HookOutput> {
  const tool = input.tool_name;
  if (tool !== 'Agent' && tool !== 'Task' && tool !== 'Workflow') return null;
  const mode = await deps.readMode();
  if (mode !== 'on') return null;
  const sessionId = typeof input.session_id === 'string' ? input.session_id : null;
  const call = input.tool_input || {};
  // Advice and intent from another turn, or from before a mode flip, are stale and ignored.
  const now = currentTurn(deps.turnState(sessionId), deps.lastAdvice(sessionId), mode);
  const advice = now.advice;
  const recent = deps.recentAgentCalls(sessionId);
  const staged = tool === 'Workflow' || recent.count >= STAGED_AGENT_CALLS || advice?.staged === true;
  const remind = staged && !recent.reminded && !deps.recentDispatch();
  deps.log({
    hook: 'PreToolUse',
    event: 'agent_call',
    session_id: sessionId,
    tool_name: tool,
    turn: now.turn,
    tier: advice?.tier ?? null,
    recommended_model: advice?.model ?? null,
    requested_model: typeof call.model === 'string' ? call.model : null,
    subagent_type: typeof call.subagent_type === 'string' ? call.subagent_type : null,
    user_intent: now.intent,
    preflight_reminder: remind
  });
  if (!remind) return null;
  return { hookSpecificOutput: { hookEventName: 'PreToolUse', additionalContext: preflightReminder(tool) } };
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
  // A PreToolUse record is the other half of the join: the model requested for a delegation.
  const used = entry.hook === 'PreToolUse' ? str(entry.requested_model) : recommended;
  return {
    harness: 'claude',
    toolOrHook: String(entry.hook || 'hook'),
    sessionId: str(entry.session_id) ?? null,
    turn: str(entry.turn),
    taskSummary: str(entry.subagent) ? `subagent: ${entry.subagent}` : str(entry.subagent_type) ? `subagent: ${entry.subagent_type}` : undefined,
    tierRecommended: str(entry.tier),
    modelRecommended: recommended,
    modelUsed: used,
    quotaStrategy: str(entry.quota)
  };
}

function appendLog(entry: Record<string, unknown>): void {
  try {
    // Never prompt text: the entry holds the recommendation, model names and a prompt hash only.
    appendClaudeDecision({ event: 'advice', ...entry });
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
  markTurn: writeTurnState,
  forgetTurn: clearTurnState,
  egressAllowed: (cwd) => checkEgress(egressContextDirs([cwd])).allowed
};

const livePreToolDeps: PreToolDeps = {
  readMode: () => readJevMode('claude-code'),
  lastAdvice: readLastAdvice,
  turnState: readTurnState,
  recentDispatch: () => hasRecentDispatch(),
  recentAgentCalls: (sessionId) => recentAgentCalls(sessionId),
  log: appendLog
};

async function main(event: string | undefined): Promise<void> {
  // The key is picked up the same way the MCP server and the OpenCode plugin do.
  loadEnvFile(path.join(JEV_DIR, 'env'));
  loadEnvFile(path.resolve(import.meta.dir, '..', '.env'));
  const input = JSON.parse((await Bun.stdin.text()) || '{}') as HookInput;
  const output =
    event === 'user-prompt-submit' ? await onUserPromptSubmit(input, liveDeps)
    : event === 'pre-tool-use' ? await onPreToolUse(input, livePreToolDeps)
    : null;
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
