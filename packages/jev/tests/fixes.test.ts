import { describe, it, expect } from 'bun:test';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import {
  defaultEffortForSubagent,
  isFreeModel,
  assertFreeModel,
  buildRouterCompletionMessages,
  readQuotaView,
  modelTierRank,
  clampTierToSessionCeiling
} from '../src/shared.ts';

const PLUGIN_ROOT = path.resolve(import.meta.dir, '..');

/**
 * QUOTA_STATE_FILE is resolved from the home directory at import time, so the
 * quota view is exercised in a child process with its own HOME.
 */
function quotaViewUnderHome(home: string, extraEnv: Record<string, string> = {}): { ok: boolean; strategy?: string; fiveHour?: number | null; weekly?: number | null; error?: string } {
  const script =
    "const m = await import('./src/shared.ts');" +
    'try { const v = await m.readQuotaView();' +
    ' console.log(JSON.stringify({ ok: true, strategy: v.strategy, fiveHour: v.gemini.five_hour, weekly: v.gemini.weekly })); }' +
    ' catch (e) { console.log(JSON.stringify({ ok: false, error: String(e) })); }';
  const proc = Bun.spawnSync(['bun', '-e', script], { cwd: PLUGIN_ROOT, env: { ...process.env, ...extraEnv, HOME: home } });
  return JSON.parse(proc.stdout.toString().trim());
}

function writeQuotaState(home: string, buckets: Record<string, unknown>, activeModel?: string): void {
  const dir = path.join(home, '.gemini', 'antigravity-cli');
  fs.mkdirSync(dir, { recursive: true });
  const now = Math.floor(Date.now() / 1000);
  fs.writeFileSync(path.join(dir, 'quota-state.json'), JSON.stringify({ schema: 2, updated_at: now, buckets, active_model: activeModel }));
}

describe('readQuotaView', () => {
  it('does not throw when no quota state exists', () => {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-home-'));

    const view = quotaViewUnderHome(home);

    expect(view.ok).toBe(true);
  });

  it('reports the gemini five-hour bucket from schema v2 state', () => {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-home-'));
    const resetAt = Math.floor(Date.now() / 1000) + 3600;
    writeQuotaState(home, {
      'gemini-5h': { remaining_fraction: 0.2, reset_at: resetAt },
      'gemini-weekly': { remaining_fraction: 0.5, reset_at: resetAt }
    });

    const view = quotaViewUnderHome(home);

    expect(view.fiveHour).toBe(0.2);
  });

  it('derives the conserve strategy when 5h quota is <= 30%', () => {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-home-'));
    const resetAt = Math.floor(Date.now() / 1000) + 3600;
    writeQuotaState(home, {
      'gemini-5h': { remaining_fraction: 0.28, reset_at: resetAt },
      'gemini-weekly': { remaining_fraction: 0.50, reset_at: resetAt }
    });

    const view = quotaViewUnderHome(home);

    expect(view.strategy).toBe('conserve');
  });

  it('derives the conserve strategy when weekly (7d) quota is <= 20%', () => {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-home-'));
    const resetAt = Math.floor(Date.now() / 1000) + 3600;
    writeQuotaState(home, {
      'gemini-5h': { remaining_fraction: 0.60, reset_at: resetAt },
      'gemini-weekly': { remaining_fraction: 0.18, reset_at: resetAt }
    });

    const view = quotaViewUnderHome(home);

    expect(view.strategy).toBe('conserve');
  });

  it('derives normal strategy when both 5h > 30% and weekly > 20%', () => {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-home-'));
    const resetAt = Math.floor(Date.now() / 1000) + 3600;
    writeQuotaState(home, {
      'gemini-5h': { remaining_fraction: 0.35, reset_at: resetAt },
      'gemini-weekly': { remaining_fraction: 0.25, reset_at: resetAt }
    });

    const view = quotaViewUnderHome(home);

    expect(view.strategy).toBe('normal');
  });

  it('derives free_only when quota is critically exhausted (5h <= 10% or weekly <= 10%)', () => {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-home-'));
    const resetAt = Math.floor(Date.now() / 1000) + 3600;
    writeQuotaState(home, {
      'gemini-5h': { remaining_fraction: 0.08, reset_at: resetAt },
      'gemini-weekly': { remaining_fraction: 0.50, reset_at: resetAt }
    });

    const view = quotaViewUnderHome(home);

    expect(view.strategy).toBe('free_only');
  });

  it('derives free_only when weekly quota is critically exhausted (<= 10%)', () => {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-home-'));
    const resetAt = Math.floor(Date.now() / 1000) + 3600;
    writeQuotaState(home, {
      'gemini-5h': { remaining_fraction: 0.50, reset_at: resetAt },
      'gemini-weekly': { remaining_fraction: 0.09, reset_at: resetAt }
    });

    const view = quotaViewUnderHome(home);

    expect(view.strategy).toBe('free_only');
  });

  it('honours environment variable threshold overrides', () => {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-home-'));
    const resetAt = Math.floor(Date.now() / 1000) + 3600;
    writeQuotaState(home, {
      'gemini-5h': { remaining_fraction: 0.45, reset_at: resetAt },
      'gemini-weekly': { remaining_fraction: 0.50, reset_at: resetAt }
    });

    // Default 5h threshold is 0.30 (so 0.45 is normal). Override to 0.50 -> should conserve.
    const view = quotaViewUnderHome(home, { JEV_QUOTA_5H_NORMAL_MIN: '0.50' });

    expect(view.strategy).toBe('conserve');
  });

  it('honours config file threshold overrides (~/.config/jev/config.json)', () => {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-home-'));
    const configDir = path.join(home, '.config', 'jev');
    fs.mkdirSync(configDir, { recursive: true });
    fs.writeFileSync(path.join(configDir, 'config.json'), JSON.stringify({
      thresholds: {
        five_hour_normal_min: 0.60,
        weekly_normal_min: 0.40
      }
    }));

    const resetAt = Math.floor(Date.now() / 1000) + 3600;
    writeQuotaState(home, {
      'gemini-5h': { remaining_fraction: 0.50, reset_at: resetAt },
      'gemini-weekly': { remaining_fraction: 0.50, reset_at: resetAt }
    });

    const view = quotaViewUnderHome(home);

    expect(view.strategy).toBe('conserve');
  });

  it('checks gemini-argon-wtu-weekly bucket when activeModel is an argon model', async () => {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-home-'));
    const resetAt = Math.floor(Date.now() / 1000) + 3600;
    writeQuotaState(
      home,
      {
        'gemini-5h': { remaining_fraction: 0.8, reset_at: resetAt },
        'gemini-weekly': { remaining_fraction: 0.8, reset_at: resetAt },
        'gemini-argon-wtu-weekly': { remaining_fraction: 0.15, reset_at: resetAt }
      },
      'gemini-2.5-argon'
    );

    const view = await readQuotaView(home);
    expect(view.gemini.weekly).toBe(0.15);
    expect(view.strategy).toBe('conserve');
    expect(view.active.pool).toBe('gemini');
    expect(view.active.weekly).toBe(0.15);
  });
});

describe('defaultEffortForSubagent keyword matching', () => {
  it('does not treat "information" as a formatting task', () => {
    const task = 'Investigate why the user information endpoint leaks data';

    const effort = defaultEffortForSubagent('code_engineer', task);

    expect(effort).toBe('medium');
  });

  it('still treats an explicit formatting request as low effort', () => {
    const task = 'Please format this table';

    const effort = defaultEffortForSubagent('code_engineer', task);

    expect(effort).toBe('low');
  });
});

describe('free-tier guard', () => {
  it('accepts a model id with the :free suffix', () => {
    const model = 'google/gemma-4-31b-it:free';

    const free = isFreeModel(model);

    expect(free).toBe(true);
  });

  it('rejects a paid model', () => {
    const model = 'anthropic/claude-opus-5';

    const run = () => assertFreeModel(model);

    expect(run).toThrow('is not a free model');
  });
});

describe('escalation request', () => {
  it('sends the original prompt as the user message', () => {
    const prompt = 'Summarise the release notes';

    const messages = buildRouterCompletionMessages(prompt, 'Be brief');

    expect(messages.at(-1)).toEqual({ role: 'user', content: prompt });
  });

  it('does not wrap the prompt in a JSON classification instruction', () => {
    const prompt = 'Summarise the release notes';

    const messages = buildRouterCompletionMessages(prompt);

    expect(JSON.stringify(messages)).not.toContain('Respond ONLY with valid JSON');
  });
});


describe('WS2 Jev Plugin Core & CLI Fixes', () => {
  it('scripts/jev-mode is retired and mode state is managed via in-process writeModeState', () => {
    const jevModePath = path.join(PLUGIN_ROOT, 'scripts', 'jev-mode');
    expect(fs.existsSync(jevModePath)).toBe(false);
  });

  it('jev_set_mode with "off" schedules a detached timeout rather than blocking synchronously', async () => {
    const indexPath = path.join(PLUGIN_ROOT, 'src', 'index.ts');
    const content = fs.readFileSync(indexPath, 'utf-8');
    expect(content).toContain("targetMode === 'off'");
    expect(content).toContain('setTimeout(');
    expect(content).toContain('timer.unref?.()');
    expect(content).toContain('Jev mode set to OFF (MCP server disable scheduled in 150ms after response flush)');
  });
});

describe('modelTierRank', () => {
  it('returns rank 3 for argon, fable, pro, opus, expensive, deep', () => {
    expect(modelTierRank('argon')).toBe(3);
    expect(modelTierRank('gemini-2.5-argon')).toBe(3);
    expect(modelTierRank('fable')).toBe(3);
    expect(modelTierRank('claude-fable-5-1')).toBe(3);
    expect(modelTierRank('pro')).toBe(3);
    expect(modelTierRank('gemini-1.5-pro')).toBe(3);
    expect(modelTierRank('opus')).toBe(3);
    expect(modelTierRank('claude-3-opus')).toBe(3);
    expect(modelTierRank('expensive')).toBe(3);
    expect(modelTierRank('deep')).toBe(3);
  });

  it('returns rank 2 for flash, standard, cheap', () => {
    expect(modelTierRank('flash')).toBe(2);
    expect(modelTierRank('standard')).toBe(2);
    expect(modelTierRank('cheap')).toBe(2);
  });

  it('returns rank 1 for flash_lite', () => {
    expect(modelTierRank('flash_lite')).toBe(1);
    expect(modelTierRank('flash-lite')).toBe(1);
  });

  it('returns rank 0 for free tiers', () => {
    expect(modelTierRank('openrouter_free')).toBe(0);
    expect(modelTierRank('free')).toBe(0);
  });
});

describe('clampTierToSessionCeiling with argon and prompt overrides', () => {
  it('clamps pro down to flash when session ceiling is flash and no override is present', () => {
    const res = clampTierToSessionCeiling('pro', 'gemini-flash');
    expect(res.clampedTier).toBe('flash');
    expect(res.wasClamped).toBe(true);
    expect(res.ceilingTier).toBe('flash');
  });

  it('does not clamp when prompt contains explicit pro override keyword', () => {
    const keywords = [
      'deep reasoning',
      'cross-system architecture',
      'manual pro approval',
      'claude opus',
      'gemini pro',
      'gemini argon',
      'argon',
      'claude fable',
      'fable'
    ];
    for (const kw of keywords) {
      const res = clampTierToSessionCeiling('pro', 'gemini-flash', `Task requires ${kw} here`);
      expect(res.clampedTier).toBe('pro');
      expect(res.wasClamped).toBe(false);
      expect(res.ceilingTier).toBe('flash');
    }
  });

  it('does not clamp pro when active model is argon (tier 3)', () => {
    const res = clampTierToSessionCeiling('pro', 'gemini-2.5-argon');
    expect(res.clampedTier).toBe('pro');
    expect(res.wasClamped).toBe(false);
    expect(res.ceilingTier).toBe('pro');
  });
});


