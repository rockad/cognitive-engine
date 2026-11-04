#!/usr/bin/env bun
/**
 * PreInvocation hook entrypoint for Antigravity:
 * `timeout 3.5 bun pre-invocation.ts antigravity || exit 0`, hook JSON on stdin.
 *
 * Runs before the model is called in a turn:
 * 1. Reads PreInvocation JSON input from stdin (with fast non-blocking fallback if empty or TTY).
 * 2. Evaluates active Jev mode (readJevMode / resolveJevModeFromConfig). If 'off', exits 0 with {}.
 * 3. Reads quota view (readQuotaView).
 * 4. If mode is 'on', runs pre-flight dispatch with withTimeout and explicit AbortController signal.
 * 5. Aborts any local daemon HTTP fetch in a mandatory finally block immediately after payload parsing
 *    so SSE streams never keep Bun's event loop open.
 * 6. Formats output JSON with { injectSteps: [{ ephemeralMessage: message }] }.
 * 7. Calls process.exit(0) explicitly on all code paths (success and fail-soft error recovery).
 */
import * as fsSync from 'node:fs';
import * as path from 'node:path';
import * as os from 'node:os';
import {
  inspectJevConfigFile,
  readJevMode,
  resolveJevModeFromConfig,
  type JevHarness,
  type JevMode
} from './core/config.js';
import {
  readQuotaView,
  clampTierToSessionCeiling,
  applyEffortGovernance,
  defaultEffortForSubagent,
  type QuotaView
} from './core/governance.js';
import { callJev, fetchWithTimeout } from './core/client.js';

export interface PreInvocationInput {
  conversationId?: string;
  workspacePaths?: string[];
  modelName?: string;
  lastUserInput?: string;
  invocationNum?: number;
  initialNumSteps?: number;
}

export interface PreFlightTelemetryEntry {
  timestamp: string;
  harness: 'antigravity';
  tool: 'jev_dispatch';
  toolOrHook: 'PreInvocation';
  sessionId: string | null;
  task: string;
  recommendedTier: 'openrouter_free' | 'flash_lite' | 'flash' | 'pro';
  confidence: number;
  [key: string]: unknown;
}

export const TIER_CRITERIA: Record<string, string> = {
  openrouter_free: 'Lightweight summaries, drafts, syntax transformations, autonomous free agent tasks, and boilerplate ($0 cost)',
  flash_lite: 'Trivial triage, fast simple lookups, file moves, quick formatting',
  flash: 'Standard agentic steps, routine edits, content synthesis, local commands',
  pro: 'Deep reasoning, architectural planning, complex multi-file refactoring, high-stakes writing'
};

export const DEFAULT_SUBAGENTS: Record<string, string> = {
  'code-engineer': 'Software engineering, automation scripting, unit testing, refactoring, and tooling specialist',
  'research': 'Deep research, comprehensive investigations, and information gathering',
  'review': 'Code review, architectural verification, diff inspection, and critique',
  'vault-librarian': 'Obsidian vault organization, note structure, wikilinks, indexing, and filing',
  'ghostwriter': 'Authoring documentation, long-form content, prose, and communication',
  'career-advisor': 'Career strategy, job applications, resume tailoring, and interview prep',
  'inbox-processor': 'Triaging incoming notes, tasks, staging areas, and unstructured text',
  'qa-engineer': 'Test authoring, test execution, bug reproduction, and QA validation',
  'system-operator': 'Infrastructure, host system config, daemons, background services, and environment setups',
  'knowledge-researcher': 'Synthesizing knowledge archives, transcripts, and domain notes',
  'self': 'Direct execution in main session, simple single-file edits, or conversational responses without delegation'
};

export function cleanUserInput(raw: string): string {
  if (!raw) return '';
  let text = raw;
  text = text.replace(/<ADDITIONAL_METADATA>[\s\S]*?<\/ADDITIONAL_METADATA>/gi, '');
  text = text.replace(/<\/?USER_REQUEST>/gi, '');
  return text.trim();
}

export function withTimeout<T>(
  pOrFn: Promise<T> | ((signal: AbortSignal) => Promise<T>),
  ms: number,
  externalController?: AbortController
): Promise<T | null> {
  const controller = externalController || new AbortController();
  let timer: any;
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

export async function readStdin(timeoutMs = 50): Promise<string> {
  if (process.stdin.isTTY) {
    return '';
  }
  return new Promise<string>((resolve) => {
    let data = '';
    const timer = setTimeout(() => {
      try {
        process.stdin.pause();
      } catch {}
      resolve(data.trim());
    }, timeoutMs);
    process.stdin.setEncoding('utf8');
    process.stdin.on('data', (chunk) => {
      data += chunk;
    });
    process.stdin.once('end', () => {
      clearTimeout(timer);
      resolve(data.trim());
    });
    process.stdin.once('error', () => {
      clearTimeout(timer);
      resolve(data.trim());
    });
    process.stdin.resume();
  });
}

export async function recordPreFlightTelemetry(
  entry: PreFlightTelemetryEntry,
  workspaceRoot?: string
): Promise<void> {
  const line = JSON.stringify(entry) + '\n';
  const targetFiles: string[] = [];

  if (workspaceRoot) {
    targetFiles.push(path.join(workspaceRoot, '.agents', 'memory', 'jev_telemetry.jsonl'));
  } else if (fsSync.existsSync(path.join(process.cwd(), '.agents'))) {
    targetFiles.push(path.join(process.cwd(), '.agents', 'memory', 'jev_telemetry.jsonl'));
  }
  targetFiles.push(path.join(os.homedir(), '.gemini', 'antigravity-cli', 'jev_telemetry.jsonl'));

  for (const target of targetFiles) {
    try {
      const dir = path.dirname(target);
      if (!fsSync.existsSync(dir)) {
        fsSync.mkdirSync(dir, { recursive: true });
      }
      fsSync.appendFileSync(target, line, 'utf8');
    } catch {
      // non-blocking best effort
    }
  }
}

/**
 * Fetch cross-harness memory state from local Recall daemon (:8110) if active.
 * Guarantees AbortController is aborted in a mandatory finally block immediately
 * after payload parsing so SSE sockets never keep Bun's event loop open.
 */
export async function fetchRecallMemoryFromDaemon(
  workspacePath?: string,
  externalSignal?: AbortSignal
): Promise<any> {
  const ws = workspacePath ? path.basename(workspacePath) : path.basename(process.cwd());
  const controller = new AbortController();
  if (externalSignal) {
    externalSignal.addEventListener('abort', () => controller.abort(), { once: true });
  }

  try {
    const res = await fetchWithTimeout(
      'http://127.0.0.1:8110/mcp',
      {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Accept': 'application/json, text/event-stream'
        },
        body: JSON.stringify({
          jsonrpc: '2.0',
          id: 1,
          method: 'tools/call',
          params: {
            name: 'read_note',
            arguments: {
              identifier: `session-memory:${ws}`,
              output_format: 'json',
              include_frontmatter: true
            }
          }
        }),
        signal: controller.signal
      },
      800
    );

    if (res.ok) {
      const text = await res.text();
      for (const line of text.split('\n')) {
        if (line.startsWith('data: ')) {
          try {
            const parsed = JSON.parse(line.slice(6));
            const fm = parsed?.result?.structuredContent?.result?.frontmatter;
            if (fm && fm.activeGoal) return fm;
          } catch {}
        }
      }
    }
  } catch {
    // daemon offline or unreachable - fail soft
  } finally {
    // Aborts any local daemon HTTP fetch in a mandatory finally block immediately after payload parsing
    // so SSE streams never keep Bun's event loop open.
    controller.abort();
  }
  return null;
}

export interface PreInvocationDeps {
  readMode?: (harness: JevHarness) => Promise<JevMode>;
  quotaView?: () => Promise<QuotaView>;
  dispatch?: (task: string, questions: Record<string, unknown>, signal?: AbortSignal) => Promise<any>;
  recallFetcher?: (workspacePath?: string, signal?: AbortSignal) => Promise<any>;
  workspaceRoot?: string;
}

export async function evaluatePreInvocation(
  rawInput: string,
  deps: PreInvocationDeps = {}
): Promise<Record<string, unknown>> {
  let hookInput: PreInvocationInput | null = null;
  if (rawInput) {
    try {
      hookInput = JSON.parse(rawInput);
    } catch {
      hookInput = null;
    }
  }

  // 1. Evaluate active Jev mode (readJevMode / resolveJevModeFromConfig)
  let mode: JevMode = 'off';
  if (deps.readMode) {
    mode = await deps.readMode('antigravity');
  } else {
    try {
      const inspection = inspectJevConfigFile();
      mode = inspection.state === 'valid'
        ? resolveJevModeFromConfig(inspection.config, 'antigravity')
        : await readJevMode('antigravity');
    } catch {
      mode = await readJevMode('antigravity').catch(() => 'off');
    }
  }

  if (mode === 'off') {
    return {};
  }

  // 2. Read quota view
  const quota = await (deps.quotaView ?? readQuotaView)();

  const conversationId = hookInput?.conversationId || null;
  const invocationNum = hookInput?.invocationNum;
  const rawUserInput = hookInput?.lastUserInput || '';
  const modelName = hookInput?.modelName || null;
  const cleanTask = cleanUserInput(rawUserInput);
  const activeModel = modelName || quota.active_model || 'gemini-pro';

  const activePool = quota.active;
  const remaining = activePool?.min_remaining ?? quota.gemini?.min_remaining ?? quota.geminiRemainingFraction ?? 1;
  const poolStrategy = activePool?.strategy ?? quota.strategy;
  const isEmergency = remaining < 0.10 || poolStrategy === 'free_only';
  const isConserve = remaining <= 0.25 || poolStrategy === 'conserve';
  const pct = (remaining * 100).toFixed(1);
  const poolLabel = activePool?.pool === '3p' ? `3p (${quota.active_model ?? 'non-Gemini'})` : 'Gemini';

  let quotaDirective = '';
  if (isEmergency) {
    quotaDirective = `[QUOTA EMERGENCY: ${poolLabel} quota is critically low (<10% remaining: ${pct}%). Subagents are locked out. ALL generation, drafting, and boilerplate MUST be offloaded to free models via jev_execute_free or jev_agent_free.]`;
  } else if (isConserve) {
    const fivePct = quota.active?.five_hour !== null && quota.active?.five_hour !== undefined ? `${Math.round(quota.active.five_hour * 100)}%` : 'n/a';
    const weeklyPct = quota.active?.weekly !== null && quota.active?.weekly !== undefined ? `${Math.round(quota.active.weekly * 100)}%` : 'n/a';
    quotaDirective = `[QUOTA CONSERVE: ${poolLabel} quota is limited (<=25% remaining: ${pct}%, 5h: ${fivePct}, weekly: ${weeklyPct}). All subagents are throttled strictly to Model: "flash_lite" (Pro tier prohibited). Offload routine generation, boilerplate, formatting, and draft text to free models via jev_execute_free or jev_agent_free.]`;
  }

  let modeDirective = '';
  if (mode === 'on') {
    modeDirective = `[JEV MODE: ON - AGGRESSIVE ENFORCEMENT]
- Pre-Flight Decision Flow: Run jev_select_model or jev_dispatch before starting complex tasks or spawning subagents.
- Free-Tier Offloading: Offload boilerplate generation, markdown formatting, draft summaries, and commit messages to jev_execute_free at $0.00 cost (0 Gemini quota).
- Quality Gates: Verify diffs and multi-step changes with jev_judge before completing turns.
- Subagent Tier Guard: Use flash_lite or flash for subagents. Pro tier (pro) strictly requires explicit manual user approval via ask_question.
- Post-Approval Transition Gate: Upon plan approval or /grill-me completion, IMMEDIATELY call jev_batch_dispatch and manage_subagents(Action: "list") to decompose and delegate parallel subagents. Do NOT edit >=2 files sequentially in the main orchestrator context without dispatching first.`;

    const shouldDispatch =
      cleanTask.length >= 8 &&
      (invocationNum === 1 || invocationNum === undefined) &&
      !cleanTask.startsWith('/jev');

    if (shouldDispatch) {
      const questions = {
        recommendedSubagent: {
          type: 'choice',
          instructions: 'Select the best specialized subagent for this task (or self for direct work in main session)',
          criteria: DEFAULT_SUBAGENTS
        },
        recommendedTier: {
          type: 'choice',
          instructions: 'Determine the required model tier from cognitive load and complexity',
          criteria: TIER_CRITERIA
        }
      };

      const dispatchController = new AbortController();
      try {
        const dispatchData = await withTimeout(
          (signal) =>
            (deps.dispatch ?? callJev)(
              `Task: ${cleanTask}\n\nQuota strategy for this machine: ${poolStrategy}.`,
              questions,
              signal
            ),
          1500,
          dispatchController
        );

        if (dispatchData?.answers) {
          const saChoice = dispatchData.answers.recommendedSubagent?.choice;
          const rawSubagent = typeof saChoice === 'string' && (saChoice in DEFAULT_SUBAGENTS || saChoice === 'self') ? saChoice : 'self';

          const tierChoice = dispatchData.answers.recommendedTier?.choice;
          const rawTier = typeof tierChoice === 'string' && tierChoice in TIER_CRITERIA ? tierChoice : 'flash';
          const confidence = typeof dispatchData.answers.recommendedTier?.confidence === 'number'
            ? dispatchData.answers.recommendedTier.confidence
            : 0.95;

          const { clampedTier } = clampTierToSessionCeiling(rawTier, activeModel, cleanTask);
          const recommendedTier = clampedTier;
          const recommendedSubagent = rawSubagent;

          const rawEffort = defaultEffortForSubagent(recommendedSubagent, cleanTask);
          const effort = applyEffortGovernance(rawEffort, poolStrategy, recommendedTier);

          await recordPreFlightTelemetry(
            {
              timestamp: new Date().toISOString(),
              harness: 'antigravity',
              tool: 'jev_dispatch',
              toolOrHook: 'PreInvocation',
              sessionId: conversationId ?? null,
              task: cleanTask.slice(0, 120),
              recommendedTier: (['openrouter_free', 'flash_lite', 'flash', 'pro'].includes(recommendedTier)
                ? recommendedTier
                : 'flash') as 'openrouter_free' | 'flash_lite' | 'flash' | 'pro',
              confidence
            },
            hookInput?.workspacePaths?.[0] || deps.workspaceRoot
          );

          const preflightLine = `[JEV PRE-FLIGHT] cost-tiered recommendation (quota=${poolStrategy}, model=${activeModel}): subagent="${recommendedSubagent}", tier="${recommendedTier}", effort="${effort}".`;
          modeDirective = `${preflightLine}\n\n${modeDirective}`;
        }
      } catch {
        // preflight dispatch is best-effort: on error or timeout keep standard modeDirective
      } finally {
        dispatchController.abort();
      }
    }
  }

  // 3. Optional Recall cross-harness memory fetch with mandatory AbortController cleanup
  let memoryMsg = '';
  const daemonController = new AbortController();
  try {
    const fetcher = deps.recallFetcher ?? fetchRecallMemoryFromDaemon;
    const recallData = await withTimeout(
      (signal) => fetcher(hookInput?.workspacePaths?.[0] || deps.workspaceRoot, signal),
      800,
      daemonController
    );
    if (recallData && recallData.activeGoal) {
      memoryMsg =
        `[CROSS-HARNESS MEMORY (${recallData.updatedAt || 'recent'})]\n` +
        `- Active Goal: ${recallData.activeGoal}\n` +
        `- Next Action: ${recallData.nextAction || 'none'}\n` +
        `- Current Milestone: ${recallData.currentMilestone || 'none'}`;
    }
  } catch {
    // best-effort
  } finally {
    // Aborts any local daemon HTTP fetch in a mandatory finally block immediately after payload parsing
    // so SSE streams never keep Bun's event loop open.
    daemonController.abort();
  }

  const parts: string[] = [];
  if (quotaDirective) parts.push(quotaDirective);
  if (modeDirective) parts.push(modeDirective);
  if (memoryMsg) parts.push(memoryMsg);

  if (!parts.length) {
    return {};
  }

  const message = parts.join('\n\n');
  return { injectSteps: [{ ephemeralMessage: message }] };
}

if (import.meta.main) {
  try {
    const rawInput = await readStdin(50);
    const result = await evaluatePreInvocation(rawInput);
    process.stdout.write(JSON.stringify(result) + '\n');
  } catch {
    process.stdout.write(JSON.stringify({}) + '\n');
  } finally {
    process.exit(0);
  }
}
