import { describe, it, expect, beforeEach, afterEach } from 'bun:test';
import { telemetryHarness } from '../src/tools/dispatch.ts';

const MARKERS = ['JEV_HARNESS', 'ANTIGRAVITY_CLI', 'GEMINI_CLI', 'CORTEX_SESSION', 'CLAUDE_CODE', 'CLAUDECODE', 'CLAUDE_PROJECT_DIR', 'OPENCODE_SESSION', 'OPENCODE_HOME'];
let saved: NodeJS.ProcessEnv = {};
beforeEach(() => {
  saved = { ...process.env };
  for (const v of MARKERS) delete process.env[v];
});
afterEach(() => {
  process.env = saved;
});

describe('dispatch telemetry harness label', () => {
  it('labels Claude Code records so the pre-flight reminder can find them', () => {
    process.env.JEV_HARNESS = 'claude-code';

    expect(telemetryHarness()).toEqual({ harness: 'claude-code' });
  });

  it.each(['antigravity', 'opencode', 'cli'])('adds nothing on %s, so the existing label stays', (harness) => {
    process.env.JEV_HARNESS = harness;

    expect(telemetryHarness()).toEqual({});
  });
});
