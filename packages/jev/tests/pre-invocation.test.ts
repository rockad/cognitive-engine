import { describe, it, expect, beforeEach, afterEach } from 'bun:test';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import {
  evaluatePreInvocation,
  cleanUserInput,
  withTimeout,
  recordPreFlightTelemetry,
  type PreFlightTelemetryEntry
} from '../src/pre-invocation.ts';

const PLUGIN_ROOT = path.resolve(import.meta.dir, '..');

describe('PreInvocation Hook Engine (pre-invocation.ts)', () => {
  let tmpDir: string;

  beforeEach(() => {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-preinv-test-'));
  });

  afterEach(() => {
    try {
      fs.rmSync(tmpDir, { recursive: true, force: true });
    } catch {}
  });

  describe('cleanUserInput', () => {
    it('strips <ADDITIONAL_METADATA> and <USER_REQUEST> tags', () => {
      const raw =
        '<ADDITIONAL_METADATA>\nsome metadata\n</ADDITIONAL_METADATA>\n' +
        '<USER_REQUEST>\nPlease implement feature X\n</USER_REQUEST>';
      expect(cleanUserInput(raw)).toBe('Please implement feature X');
    });

    it('returns empty string on empty input', () => {
      expect(cleanUserInput('')).toBe('');
    });
  });

  describe('withTimeout', () => {
    it('resolves promise before timeout', async () => {
      const res = await withTimeout(Promise.resolve('ok'), 500);
      expect(res).toBe('ok');
    });

    it('aborts and returns null on timeout', async () => {
      let aborted = false;
      const res = await withTimeout(
        (signal) =>
          new Promise((resolve) => {
            signal.addEventListener('abort', () => {
              aborted = true;
            });
            setTimeout(() => resolve('slow'), 200);
          }),
        50
      );
      expect(res).toBeNull();
      expect(aborted).toBe(true);
    });
  });

  describe('recordPreFlightTelemetry', () => {
    it('writes live telemetry entry matching Fix 2 schema to .agents/memory/jev_telemetry.jsonl', async () => {
      const entry: PreFlightTelemetryEntry = {
        timestamp: new Date().toISOString(),
        harness: 'antigravity',
        tool: 'jev_dispatch',
        toolOrHook: 'PreInvocation',
        sessionId: 'session_abc_123',
        task: 'Implement Argon hardening',
        recommendedTier: 'flash',
        confidence: 0.95
      };

      await recordPreFlightTelemetry(entry, tmpDir);

      const targetPath = path.join(tmpDir, '.agents', 'memory', 'jev_telemetry.jsonl');
      expect(fs.existsSync(targetPath)).toBe(true);
      const content = fs.readFileSync(targetPath, 'utf8').trim();
      const parsed = JSON.parse(content);
      expect(parsed.harness).toBe('antigravity');
      expect(parsed.tool).toBe('jev_dispatch');
      expect(parsed.toolOrHook).toBe('PreInvocation');
      expect(parsed.sessionId).toBe('session_abc_123');
      expect(parsed.recommendedTier).toBe('flash');
      expect(parsed.confidence).toBe(0.95);
    });
  });

  describe('evaluatePreInvocation', () => {
    it('exits with {} when mode is off', async () => {
      const input = JSON.stringify({
        conversationId: 'sess_1',
        lastUserInput: 'Implement complex feature',
        invocationNum: 1
      });

      const res = await evaluatePreInvocation(input, {
        readMode: async () => 'off'
      });

      expect(res).toEqual({});
    });

    it('injects JEV MODE: ON directive and runs pre-flight dispatch when mode is on', async () => {
      const input = JSON.stringify({
        conversationId: 'sess_1',
        lastUserInput: 'Refactor cognitive engine telemetry layer',
        invocationNum: 1
      });

      let dispatchCalled = false;
      const res = await evaluatePreInvocation(input, {
        readMode: async () => 'on',
        workspaceRoot: tmpDir,
        quotaView: async () => ({
          source: 'test',
          schema: 2,
          stale: false,
          updated_at: Date.now(),
          active_model: 'gemini-flash',
          buckets: {},
          gemini: { five_hour: 0.8, weekly: 0.8, min_remaining: 0.8 },
          strategy: 'normal',
          thresholds: {} as any,
          geminiRemainingFraction: 0.8,
          active: { pool: 'gemini', five_hour: 0.8, weekly: 0.8, min_remaining: 0.8, strategy: 'normal' }
        }),
        dispatch: async (_task, _questions, _signal) => {
          dispatchCalled = true;
          return {
            answers: {
              recommendedSubagent: { choice: 'code-engineer' },
              recommendedTier: { choice: 'flash', confidence: 0.92 }
            }
          };
        }
      });

      expect(dispatchCalled).toBe(true);
      const injectSteps = (res as any).injectSteps;
      expect(injectSteps).toBeDefined();
      expect(injectSteps).toHaveLength(1);
      const msg = injectSteps[0].ephemeralMessage;
      expect(msg).toContain('[JEV MODE: ON - AGGRESSIVE ENFORCEMENT]');
      expect(msg).toContain('subagent="code-engineer"');
      expect(msg).toContain('tier="flash"');

      // Verify telemetry entry was recorded
      const telePath = path.join(tmpDir, '.agents', 'memory', 'jev_telemetry.jsonl');
      expect(fs.existsSync(telePath)).toBe(true);
      const teleEntry = JSON.parse(fs.readFileSync(telePath, 'utf8').trim());
      expect(teleEntry.toolOrHook).toBe('PreInvocation');
      expect(teleEntry.tool).toBe('jev_dispatch');
      expect(teleEntry.sessionId).toBe('sess_1');
      expect(teleEntry.recommendedTier).toBe('flash');
    });

    it('injects QUOTA CONSERVE directive when quota is <= 25%', async () => {
      const input = JSON.stringify({
        conversationId: 'sess_2',
        lastUserInput: 'A quick question',
        invocationNum: 2
      });

      const res = await evaluatePreInvocation(input, {
        readMode: async () => 'on',
        quotaView: async () => ({
          source: 'test',
          schema: 2,
          stale: false,
          updated_at: Date.now(),
          active_model: 'gemini-flash',
          buckets: {},
          gemini: { five_hour: 0.22, weekly: 0.50, min_remaining: 0.22 },
          strategy: 'conserve',
          thresholds: {} as any,
          geminiRemainingFraction: 0.22,
          active: { pool: 'gemini', five_hour: 0.22, weekly: 0.50, min_remaining: 0.22, strategy: 'conserve' }
        })
      });

      const injectSteps = (res as any).injectSteps;
      expect(injectSteps).toBeDefined();
      const msg = injectSteps[0].ephemeralMessage;
      expect(msg).toContain('QUOTA CONSERVE');
      expect(msg).toContain('flash_lite');
    });

    it('aborts local daemon fetch in mandatory finally block so event-loop does not hang', async () => {
      const input = JSON.stringify({
        conversationId: 'sess_3',
        lastUserInput: 'Check memory status',
        invocationNum: 2
      });

      let recallAborted = false;
      const res = await evaluatePreInvocation(input, {
        readMode: async () => 'on',
        quotaView: async () => ({
          source: 'test',
          schema: 2,
          stale: false,
          updated_at: Date.now(),
          active_model: 'gemini-flash',
          buckets: {},
          gemini: { five_hour: 0.8, weekly: 0.8, min_remaining: 0.8 },
          strategy: 'normal',
          thresholds: {} as any,
          geminiRemainingFraction: 0.8,
          active: { pool: 'gemini', five_hour: 0.8, weekly: 0.8, min_remaining: 0.8, strategy: 'normal' }
        }),
        recallFetcher: async (_ws, signal) => {
          signal?.addEventListener('abort', () => {
            recallAborted = true;
          });
          return { activeGoal: 'Complete AG-5 Argon hardening', currentMilestone: 'Tests passing' };
        }
      });

      expect(recallAborted).toBe(true);
      const injectSteps = (res as any).injectSteps;
      expect(injectSteps[0].ephemeralMessage).toContain('CROSS-HARNESS MEMORY');
      expect(injectSteps[0].ephemeralMessage).toContain('Complete AG-5 Argon hardening');
    });
  });

  describe('CLI hook execution via bun', () => {
    it('executes pre-invocation.ts as a CLI script and exits 0', () => {
      const scriptPath = path.join(PLUGIN_ROOT, 'src', 'pre-invocation.ts');
      const input = JSON.stringify({
        conversationId: 'sess_cli',
        lastUserInput: 'test task',
        invocationNum: 2
      });

      const proc = Bun.spawnSync(['bun', scriptPath, 'antigravity'], {
        cwd: tmpDir,
        stdin: Buffer.from(input, 'utf8'),
        env: {
          ...process.env,
          JEV_HARNESS: 'antigravity'
        }
      });

      expect(proc.exitCode).toBe(0);
      const out = proc.stdout.toString().trim();
      expect(() => JSON.parse(out)).not.toThrow();
    });
  });
});
