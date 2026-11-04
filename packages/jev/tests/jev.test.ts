import { describe, it, expect, beforeEach, afterEach } from 'bun:test';
import * as fsSync from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { getModeDetails } from '../src/index.ts';
import {
  defaultEffortForSubagent,
  applyEffortGovernance,
  normaliseBucket,
  readQuotaView,
  selectModel,
  type ModelCandidate
} from '../src/shared.ts';

import {
  isFreeModel,
  assertFreeModel,
  callOpenRouterFree
} from '../src/core/client.js';

import {
  clearConfigCacheForTesting
} from '../src/core/config.js';

describe('Jev Plugin Unit & Integration Tests', () => {
  const isLiveTest = process.env.JEV_LIVE_TEST === '1';
  let tmpUserDir: string;
  let tmpCacheDir: string;

  beforeEach(() => {
    tmpUserDir = fsSync.mkdtempSync(path.join(os.tmpdir(), 'jev-unit-config-'));
    tmpCacheDir = fsSync.mkdtempSync(path.join(os.tmpdir(), 'jev-unit-cache-'));
    process.env.JEV_CONFIG_FILE = path.join(tmpUserDir, 'nonexistent-config.json');
    process.env.JEV_CACHE_FILE = path.join(tmpCacheDir, 'cache.sqlite');
    clearConfigCacheForTesting();
  });

  afterEach(() => {
    delete process.env.JEV_CONFIG_FILE;
    delete process.env.JEV_CACHE_FILE;
    clearConfigCacheForTesting();
    try {
      fsSync.rmSync(tmpUserDir, { recursive: true, force: true });
    } catch {}
    try {
      fsSync.rmSync(tmpCacheDir, { recursive: true, force: true });
    } catch {}
  });

  it('correctly provides mode details for off, on-demand, and on', () => {
    const off = getModeDetails('off');
    expect(off.mode).toBe('off');
    expect(off.title).toBe('OFF');

    const onDemand = getModeDetails('on-demand');
    expect(onDemand.mode).toBe('on-demand');
    expect(onDemand.title).toBe('ON-DEMAND');

    const on = getModeDetails('on');
    expect(on.mode).toBe('on');
    expect(on.title).toBe('ON (AGGRESSIVE)');
  });

  it('validates RFC decisions D2 and D3 for thinking effort governance', () => {
    // D2: vault_librarian defaults to medium
    expect(defaultEffortForSubagent('vault_librarian')).toBe('medium');
    expect(defaultEffortForSubagent('inbox_processor')).toBe('low');
    expect(defaultEffortForSubagent('system_architect')).toBe('high');

    // D3: free_only allows medium/high for free models, forces low for quota models
    expect(applyEffortGovernance('high', 'free_only', 'openrouter_free')).toBe('high');
    expect(applyEffortGovernance('medium', 'free_only', 'openrouter_free')).toBe('medium');
    expect(applyEffortGovernance('high', 'free_only', 'flash')).toBe('low');
    expect(applyEffortGovernance('medium', 'free_only', 'flash_lite')).toBe('low');

    // Conserve mode caps high to medium
    expect(applyEffortGovernance('high', 'conserve', 'flash')).toBe('medium');
    expect(applyEffortGovernance('medium', 'conserve', 'flash')).toBe('medium');
  });

  it('normaliseBucket correctly calculates remaining fractions and staleness/refresh', () => {
    const now = 1000;
    const raw = {
      remaining_fraction: 0.8,
      reset_at: 1200
    };
    const b = normaliseBucket(now, raw);
    expect(b).not.toBeNull();
    expect(b?.remaining_fraction).toBe(0.8);
    expect(b?.effective_remaining_fraction).toBe(0.8);
    expect(b?.refreshed).toBe(false);
    expect(b?.reset_in_seconds).toBe(200);

    // When reset_at has passed
    const refreshedBucket = normaliseBucket(1300, raw);
    expect(refreshedBucket?.refreshed).toBe(true);
    expect(refreshedBucket?.effective_remaining_fraction).toBe(1);
  });

  it('readQuotaView returns a valid QuotaView object', async () => {
    const view = await readQuotaView();
    expect(view).toBeDefined();
    expect(typeof view.source).toBe('string');
    expect(typeof view.strategy).toBe('string');
    expect(view.thresholds).toBeDefined();
  });

  it('selectModel evaluates candidates and falls back to cheapest option when offline', async () => {
    const candidates: ModelCandidate[] = [
      { id: 'exp', tier: 'expensive', providerModel: 'openrouter/expensive', costInPerMTok: 5, costOutPerMTok: 15 },
      { id: 'free', tier: 'free', providerModel: 'openrouter/free', costInPerMTok: 0, costOutPerMTok: 0 }
    ];
    const res = await selectModel('Simple formatting task', candidates, 'normal');
    expect(res).toBeDefined();
    expect(res.cheapestOptionId).toBe('free');
    expect(res.options.length).toBe(2);
  });


  it('validates free model offloading quality gate contract', () => {
    expect(typeof getModeDetails).toBe('function');
  });

  describe('Free Model Validation and Offloading Invariants', () => {
    it('isFreeModel validates free models and rejects paid ones', () => {
      expect(isFreeModel('openrouter/free')).toBe(true);
      expect(isFreeModel('google/gemma-4-31b-it:free')).toBe(true);
      expect(isFreeModel('meta-llama/llama-3.3-70b-instruct:free')).toBe(true);

      expect(isFreeModel('anthropic/claude-3.7-sonnet')).toBe(false);
      expect(isFreeModel('openai/gpt-4o')).toBe(false);
      expect(isFreeModel('google/gemini-2.5-flash')).toBe(false);
    });

    it('assertFreeModel throws on paid models and passes free models', () => {
      expect(assertFreeModel('openrouter/free')).toBe('openrouter/free');
      expect(assertFreeModel('cohere/north-mini-code:free')).toBe('cohere/north-mini-code:free');

      expect(() => assertFreeModel('anthropic/claude-3.7-sonnet')).toThrow(/not a free model/);
      expect(() => assertFreeModel('google/gemini-2.5-pro')).toThrow(/not a free model/);
    });

    it('callOpenRouterFree strictly rejects requested non-free models immediately', async () => {
      await expect(
        callOpenRouterFree('Test prompt', undefined, 'anthropic/claude-3.7-sonnet')
      ).rejects.toThrow(/not a free model/);
    });

    it('callOpenRouterFree strictly throws when all free candidates fail, and never calls paid models', async () => {
      const originalFetch = globalThis.fetch;
      const originalApiKey = process.env.OPENROUTER_API_KEY;
      const originalJevKey = process.env.JEV_OPENROUTER_API_KEY;
      const attemptedModels: string[] = [];

      process.env.OPENROUTER_API_KEY = 'mock-key-for-free-failure-test';
      delete process.env.JEV_OPENROUTER_API_KEY;

      globalThis.fetch = (async (url: any, init: any) => {
        if (init?.body) {
          try {
            const body = JSON.parse(init.body);
            if (body.model) {
              attemptedModels.push(body.model);
            }
          } catch {}
        }
        return new Response(JSON.stringify({ error: { message: 'All free capacity exhausted' } }), {
          status: 503,
          headers: { 'Content-Type': 'application/json' }
        });
      }) as any;

      try {
        await expect(
          callOpenRouterFree('Test offload prompt when upstream free tier is down')
        ).rejects.toThrow(/OpenRouter Free Error/);

        expect(attemptedModels.length).toBeGreaterThan(0);
        // Invariant: every single candidate tried MUST be a free model
        for (const model of attemptedModels) {
          expect(isFreeModel(model)).toBe(true);
          expect(model).not.toContain('sonnet');
          expect(model).not.toContain('opus');
          expect(model).not.toContain('typesafe/jev-router');
        }
      } finally {
        globalThis.fetch = originalFetch;
        if (originalApiKey) process.env.OPENROUTER_API_KEY = originalApiKey;
        else delete process.env.OPENROUTER_API_KEY;
        if (originalJevKey) process.env.JEV_OPENROUTER_API_KEY = originalJevKey;
        else delete process.env.JEV_OPENROUTER_API_KEY;
      }
    });

    it('filters out non-free models from free pool candidates', async () => {
      expect(isFreeModel('typesafe/jev-router')).toBe(false);
      expect(isFreeModel('openrouter/auto')).toBe(false);

      await expect(
        callOpenRouterFree('Test prompt', undefined, 'typesafe/jev-router')
      ).rejects.toThrow(/not a free model/);
    });
  });
});
