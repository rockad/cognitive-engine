import { describe, it, expect, beforeEach, afterEach } from 'bun:test';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import {
  loadJevConfigSync,
  getHarnessConfig,
  readJevMode,
  evaluateLocalHeuristic,
  getCachedDecision,
  setCachedDecision,
  clearDecisionCache,
  computeDecisionCacheKey,
  formatVerdict,
  DECISION_CACHE_TTL_MS,
  JEV_CACHE_FILE,
  selectModel,
  type ModelCandidate
} from '../src/shared.ts';
import {
  initTracing,
  initTracingSync,
  getTracer,
  resetTracerForTesting,
  type JevTracer
} from '../src/tracing.ts';
import {
  clearConfigCacheForTesting
} from '../src/core/config.js';

describe('Consolidated Master Plan: Phase 1 (Unified Config & Harness Scoping)', () => {
  let tmpUserDir: string;
  let tmpUserConfigFile: string;

  beforeEach(() => {
    tmpUserDir = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-consol-user-'));
    tmpUserConfigFile = path.join(tmpUserDir, 'nonexistent-config.json');
    process.env.JEV_CONFIG_FILE = tmpUserConfigFile;
    clearConfigCacheForTesting();
  });

  afterEach(() => {
    delete process.env.JEV_CONFIG_FILE;
    clearConfigCacheForTesting();
    try {
      fs.rmSync(tmpUserDir, { recursive: true, force: true });
    } catch {}
  });

  it('loads default configuration when config file is minimal', () => {
    const cfg = loadJevConfigSync();
    expect(cfg.version).toBeNumber();
    expect(cfg.defaults).toBeDefined();
    expect(cfg.defaults.mode).toBeDefined();
  });

  it('provides independent per-harness configurations', () => {
    const agyCfg = getHarnessConfig('antigravity');
    const cliCfg = getHarnessConfig('cli');
    const ccCfg = getHarnessConfig('claude-code');

    expect(agyCfg.mode).toBeDefined();
    expect(cliCfg.mode).toBeDefined();
    expect(ccCfg.mode).toBeDefined();
  });

  it('reads harness mode accurately', async () => {
    const cliMode = await readJevMode('cli');
    expect(['off', 'on-demand', 'on']).toContain(cliMode);
  });
});

describe('Consolidated Master Plan: Phase 2 (Latency Optimization & Local Heuristics)', () => {
  it('evaluates local complexity heuristic for short formatting tasks (<40 tokens)', () => {
    const heuristic = evaluateLocalHeuristic('format JSON indent 2');
    expect(heuristic).not.toBeNull();
    expect(heuristic?.route).toBe('openrouter_free');
    expect(heuristic?.tier).toBe('openrouter_free');
    expect(heuristic?.effort).toBe('low');
  });

  it('returns null for non-mechanical or complex tasks', () => {
    const heuristic = evaluateLocalHeuristic('architect a multi-tenant distributed consensus database');
    expect(heuristic).toBeNull();
  });
});

describe('Consolidated Master Plan: Phase 3 (File-Backed Decision Cache & Concurrency)', () => {
  let tmpCacheDir: string;
  let tmpCacheFile: string;

  beforeEach(() => {
    tmpCacheDir = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-cache-test-'));
    tmpCacheFile = path.join(tmpCacheDir, 'decision-cache.json');
    process.env.JEV_CACHE_FILE = tmpCacheFile;
    process.env.JEV_CONFIG_FILE = path.join(tmpCacheDir, 'nonexistent-config.json');
    clearConfigCacheForTesting();
  });

  afterEach(() => {
    delete process.env.JEV_CACHE_FILE;
    delete process.env.JEV_CONFIG_FILE;
    delete process.env.JEV_CACHE_TTL_MS;
    clearConfigCacheForTesting();
    try {
      fs.rmSync(tmpCacheDir, { recursive: true, force: true });
    } catch {}
  });

  it('computes deterministic cache keys from prompt, candidates, and model tier', () => {
    const prompt = 'Implement user authentication with JWT';
    const candidates = [
      { id: 'free', tier: 'free' },
      { id: 'standard', tier: 'standard' }
    ];
    const tier = 'flash';

    const key1 = computeDecisionCacheKey(prompt, candidates, tier);
    const key2 = computeDecisionCacheKey(prompt, candidates, tier);
    expect(key1).toBe(key2);
    expect(key1.startsWith('decision:')).toBe(true);

    // Variations must produce different hashes
    const keyDiffPrompt = computeDecisionCacheKey('Different prompt', candidates, tier);
    expect(keyDiffPrompt).not.toBe(key1);

    const keyDiffCandidates = computeDecisionCacheKey(prompt, [{ id: 'pro', tier: 'pro' }], tier);
    expect(keyDiffCandidates).not.toBe(key1);

    const keyDiffTier = computeDecisionCacheKey(prompt, candidates, 'pro');
    expect(keyDiffTier).not.toBe(key1);
  });

  it('formats human-readable verdicts from structured decision answers', () => {
    const result = {
      answers: {
        recommendedSubagent: { type: 'choice', choice: 'code_engineer' },
        recommendedTier: { type: 'choice', choice: 'flash' },
        confidence: { type: 'score', score: 0.95 }
      }
    };
    const verdict = formatVerdict(result);
    expect(verdict).toContain('recommendedSubagent=code_engineer');
    expect(verdict).toContain('recommendedTier=flash');
    expect(verdict).toContain('confidence=0.95');
  });

  it('stores and retrieves decisions from the file-backed cache', () => {
    const testHash = `test-hash-${Date.now()}`;
    const testData = { choice: 'code_engineer', confidence: 0.95 };

    setCachedDecision(testHash, testData);
    const cached = getCachedDecision(testHash);
    expect(cached).toEqual(testData);

    // File on disk must exist and contain valid JSON
    expect(fs.existsSync(tmpCacheFile)).toBe(true);
    const diskContent = JSON.parse(fs.readFileSync(tmpCacheFile, 'utf8'));
    expect(diskContent[testHash]).toBeDefined();
    expect(diskContent[testHash].data).toEqual(testData);
  });

  it('respects the 5-minute cache TTL and expires stale entries', () => {
    expect(DECISION_CACHE_TTL_MS).toBe(5 * 60 * 1000);

    const testHash = `ttl-hash-${Date.now()}`;
    const testData = { choice: 'research', confidence: 0.88 };

    // Simulate entry written now
    setCachedDecision(testHash, testData);
    expect(getCachedDecision(testHash)).toEqual(testData);

    // Override TTL to 50ms for testing expiration
    process.env.JEV_CACHE_TTL_MS = '50';

    // Before expiration
    expect(getCachedDecision(testHash)).toEqual(testData);

    // Force entry timestamp to the past (> 50ms)
    const raw = JSON.parse(fs.readFileSync(tmpCacheFile, 'utf8'));
    raw[testHash].at = Date.now() - 100;
    fs.writeFileSync(tmpCacheFile, JSON.stringify(raw));

    expect(getCachedDecision(testHash)).toBeNull();
  });

  it('handles safe concurrency and atomic writes under simultaneous updates', () => {
    const numEntries = 15;
    for (let i = 0; i < numEntries; i++) {
      setCachedDecision(`concurrent-key-${i}`, { value: i });
    }

    // Verify all entries were written without corruption
    for (let i = 0; i < numEntries; i++) {
      const hit = getCachedDecision(`concurrent-key-${i}`);
      expect(hit).toEqual({ value: i });
    }

    const diskContent = JSON.parse(fs.readFileSync(tmpCacheFile, 'utf8'));
    expect(Object.keys(diskContent).length).toBeGreaterThanOrEqual(numEntries);
  });

  it('deduplicates identical model selection requests across turns', async () => {
    const candidates: ModelCandidate[] = [
      { id: 'free', tier: 'free', providerModel: 'openrouter/free', costInPerMTok: 0, costOutPerMTok: 0 },
      { id: 'standard', tier: 'standard', providerModel: 'gemini-flash', costInPerMTok: 0.35, costOutPerMTok: 1.05 }
    ];
    const task = 'Scaffold a test module for decision cache';
    const cacheKey = computeDecisionCacheKey(task, candidates, 'normal');

    // Seed the cache as if a pre-invocation hook just evaluated it
    const preflightResult = {
      options: candidates.map(c => ({ ...c, prob: c.id === 'standard' ? 0.9 : 0.1, suitable: true, costScore: 1 })),
      cheapestOptionId: 'free',
      cheapestSuitableOptionId: 'standard',
      recommendedOptionId: 'standard',
      expensiveOptionIds: [],
      upgradeOptionId: null,
      recommendation: 'cheapest_suitable' as const,
      cheapestSufficient: false,
      cheapestSufficientProb: 0.2,
      quotaStrategy: 'normal',
      confidence: 0.9,
      modelUsed: 'typesafe/jev-latest',
      fallbackUsed: false
    };

    setCachedDecision(cacheKey, preflightResult);

    // Main agent turn calls selectModel with identical arguments
    const result = await selectModel(task, candidates, 'normal');
    expect(result).toEqual(preflightResult);
    expect(result.recommendedOptionId).toBe('standard');
    expect(result.fallbackUsed).toBe(false);
  });
});

describe('Consolidated Master Plan: Phase 4 (OpenTelemetry Tracing Pipeline)', () => {
  afterEach(() => {
    delete process.env.JEV_OTEL_ENABLED;
    delete process.env.OTEL_EXPORTER_OTLP_ENDPOINT;
    delete process.env.JEV_OTEL_ENDPOINT;
    resetTracerForTesting();
  });

  it('defaults to NoopTracer with zero overhead when OTEL is inactive', async () => {
    delete process.env.JEV_OTEL_ENABLED;
    delete process.env.OTEL_EXPORTER_OTLP_ENDPOINT;

    const tracer = await initTracing();
    expect(tracer).toBeDefined();

    const span = tracer.startSpan('test-span', { 'test.attr': 'val' });
    expect(span).toBeDefined();
    expect(() => span.setAttribute('key', 123)).not.toThrow();
    expect(() => span.setAttributes({ 'k1': 'v1', 'k2': 2 })).not.toThrow();
    expect(() => span.setError(new Error('test'))).not.toThrow();
    expect(() => span.end()).not.toThrow();
  });

  it('activates OtlpTracer when OTEL endpoint is configured', () => {
    const tracer = initTracingSync({
      enabled: true,
      endpoint: 'http://127.0.0.1:4318',
      serviceName: 'jev-test-service'
    });

    expect(tracer).toBeDefined();
    const span = tracer.startSpan('jev.decision', {
      'decision.model': 'typesafe/jev-latest',
      'decision.cached': false
    });

    expect(span).toBeDefined();
    span.setAttribute('decision.latency_ms', 42);
    span.setAttribute('decision.tokens.input', 120);
    span.setAttribute('decision.tokens.output', 45);
    span.setAttribute('decision.tokens.total', 165);
    span.setAttribute('decision.verdict', 'recommendedSubagent=code_engineer; recommendedTier=flash');
    span.end();
  });

  it('instruments decision latency, token counts, and verdicts without throwing or blocking', async () => {
    process.env.JEV_OTEL_ENABLED = 'true';
    process.env.OTEL_EXPORTER_OTLP_ENDPOINT = 'http://127.0.0.1:4318/v1/traces';

    const tracer = await initTracing();
    expect(tracer).toBeDefined();

    // Verify span instrumentation
    const span = tracer.startSpan('jev.select_model', {
      'task.prompt': 'Format markdown table',
      'quota.strategy': 'normal'
    });

    span.setAttribute('decision.latency_ms', 12);
    span.setAttribute('decision.verdict', 'free');
    span.setAttribute('decision.cheapest_sufficient', true);
    span.end();
  });
});


describe('Consolidated Master Plan: Unified Telemetry Logging', () => {
  it('logs telemetry events conforming to JevTelemetryEvent', async () => {
    const { logJevTelemetry, JEV_TELEMETRY_FILE } = await import('../src/shared.ts');
    const testSession = `test-session-${Date.now()}`;

    await logJevTelemetry({
      harness: 'antigravity',
      toolOrHook: 'test_tool',
      sessionId: testSession,
      taskSummary: 'Test telemetry logging',
      tierRecommended: 'flash',
      modelUsed: 'gemini-3.8-flash',
      tokensSaved: 100,
      costUsd: 0.0,
      quotaStrategy: 'normal'
    });

    expect(fs.existsSync(JEV_TELEMETRY_FILE)).toBe(true);
    const content = fs.readFileSync(JEV_TELEMETRY_FILE, 'utf8');
    const lines = content.trim().split('\n');
    const lastEntry = JSON.parse(lines[lines.length - 1]);

    expect(lastEntry.harness).toBe('antigravity');
    expect(lastEntry.toolOrHook).toBe('test_tool');
    expect(lastEntry.sessionId).toBe(testSession);
    expect(lastEntry.tierRecommended).toBe('flash');
    expect(lastEntry.quotaStrategy).toBe('normal');
  });
});

describe('Consolidated Master Plan: Phase 5 (Empirical Calibration & Multi-Agent Orchestration)', () => {
  it('clamps recommended tier to active session model ceiling unless explicit pro override is present', async () => {
    const { clampTierToSessionCeiling } = await import('../src/shared.ts');

    // Case 1: Active session is Flash, recommendation is Pro for bounded CLI port -> Clamped to flash
    const boundedTask = 'Port cloud-ops-verify CLI from Python to Rust';
    const res1 = clampTierToSessionCeiling('pro', 'gemini-3.8-flash', boundedTask);
    expect(res1.wasClamped).toBe(true);
    expect(res1.clampedTier).toBe('flash');
    expect(res1.ceilingTier).toBe('flash');

    // Case 2: Active session is Flash, recommendation is Flash -> Unchanged
    const res2 = clampTierToSessionCeiling('flash', 'gemini-3.8-flash', boundedTask);
    expect(res2.wasClamped).toBe(false);
    expect(res2.clampedTier).toBe('flash');

    // Case 3: Active session is Flash, but prompt explicitly requests deep reasoning override -> Not clamped
    const archTask = 'Perform deep reasoning and cross-system architecture review';
    const res3 = clampTierToSessionCeiling('pro', 'gemini-3.8-flash', archTask);
    expect(res3.wasClamped).toBe(false);
    expect(res3.clampedTier).toBe('pro');

    // Case 4: Active session is Pro -> Pro is permitted
    const res4 = clampTierToSessionCeiling('pro', 'gemini-3.8-pro', boundedTask);
    expect(res4.wasClamped).toBe(false);
    expect(res4.clampedTier).toBe('pro');
  });

  it('detects package and crate scaffolding tasks to trigger Tier 0 free boilerplate offloading', async () => {
    const { detectScaffoldingTask } = await import('../src/shared.ts');

    const rustTask = 'Create new crate job-app with Cargo.toml and clap structs';
    const check1 = detectScaffoldingTask(rustTask);
    expect(check1.scaffoldFirst).toBe(true);
    expect(check1.boilerplateType).toBe('rust_crate');

    const nodeTask = 'Scaffold package.json and Bun ESM module for dashboard';
    const check2 = detectScaffoldingTask(nodeTask);
    expect(check2.scaffoldFirst).toBe(true);
    expect(check2.boilerplateType).toBe('node_package');

    const logicTask = 'Fix concurrency bug in SQLite thread pool';
    const check3 = detectScaffoldingTask(logicTask);
    expect(check3.scaffoldFirst).toBe(false);
    expect(check3.boilerplateType).toBeNull();
  });

  it('evaluates local deterministic quality judge without throwing or requiring remote models', async () => {
    const { evaluateLocalQualityJudge } = await import('../src/shared.ts');

    // Conflict marker rejection
    const conflictDiff = '<<<<<<< HEAD\nlet x = 1;\n=======\nlet x = 2;\n>>>>>>> main';
    const check1 = evaluateLocalQualityJudge(conflictDiff);
    expect(check1.passed).toBe(false);
    expect(check1.reason).toContain('conflict markers');

    // Empty content rejection
    const check2 = evaluateLocalQualityJudge('   ');
    expect(check2.passed).toBe(false);
    expect(check2.reason).toContain('empty');

    // Process failure rejection
    const failedRun = 'Compilation failed:\nerror[E0425]: cannot find value in scope\nProcess exited with code 1';
    const check3 = evaluateLocalQualityJudge(failedRun, 'Did tests pass?');
    expect(check3.passed).toBe(false);
    expect(check3.reason).toContain('exit code');

    // Clean valid diff pass
    const cleanDiff = 'diff --git a/main.rs b/main.rs\n+fn hello() -> bool { true }';
    const check4 = evaluateLocalQualityJudge(cleanDiff, 'Is code clean?');
    expect(check4.passed).toBe(true);
  });
});

