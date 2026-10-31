import { describe, expect, test, beforeAll, afterAll } from 'bun:test';
import * as fs from 'node:fs/promises';
import * as path from 'node:path';
import * as os from 'node:os';
import { evaluateDrift, type DriftEvent } from '../src/audit/drift-detector.js';
import { readQuotaView, discoverWorkspaceAgents, findWorkspaceRoot, clearConfigCacheForTesting } from '../src/shared.js';

describe('Stage 2: Dynamic Workspace Subagent Discovery', () => {
  let fixtureDir: string;
  let emptyTempDir: string;
  let origHarness: string | undefined;

  beforeAll(async () => {
    // Fixtures live under .agents/agents, which only the antigravity harness scans; pin it
    // so the suite does not depend on which harness is running the tests.
    origHarness = process.env.JEV_HARNESS;
    process.env.JEV_HARNESS = 'antigravity';
    clearConfigCacheForTesting();
    fixtureDir = await fs.mkdtemp(path.join(os.tmpdir(), 'jev-agents-fixture-'));
    const agentsDir = path.join(fixtureDir, '.agents', 'agents');
    await fs.mkdir(agentsDir, { recursive: true });

    // Standalone .md files (legacy fallback)
    await fs.writeFile(
      path.join(agentsDir, 'alpha_agent.md'),
      '---\nname: alpha_agent\ndescription: "Alpha specialist for testing"\n---\n# Alpha Agent'
    );
    await fs.writeFile(
      path.join(agentsDir, 'beta_agent.md'),
      '---\nname: beta_agent\ndescription: "Beta specialist for testing"\n---\n# Beta Agent'
    );
    await fs.writeFile(
      path.join(agentsDir, 'gamma_agent.md'),
      '---\nname: gamma_agent\ndescription: "Gamma specialist for testing"\n---\n# Gamma Agent'
    );

    // Directory-per-agent with kebab-case identifier
    const deltaDir = path.join(agentsDir, 'delta-agent');
    await fs.mkdir(deltaDir, { recursive: true });
    await fs.writeFile(
      path.join(deltaDir, 'agent.md'),
      '---\nname: delta-agent\ndescription: "Delta specialist for testing directory-per-agent"\n---\n# Delta Agent'
    );

    // Directory-per-agent defaulting to directory name when name is omitted in frontmatter
    const epsilonDir = path.join(agentsDir, 'epsilon-agent');
    await fs.mkdir(epsilonDir, { recursive: true });
    await fs.writeFile(
      path.join(epsilonDir, 'agent.md'),
      '---\ndescription: "Epsilon specialist with default directory name"\n---\n# Epsilon Agent'
    );

    // Precedence test: directory-per-agent takes precedence over standalone .md fallback file
    const zetaDir = path.join(agentsDir, 'zeta_agent');
    await fs.mkdir(zetaDir, { recursive: true });
    await fs.writeFile(
      path.join(zetaDir, 'agent.md'),
      '---\nname: zeta_agent\ndescription: "Zeta directory implementation"\n---\n# Zeta Agent'
    );
    await fs.writeFile(
      path.join(agentsDir, 'zeta_agent.md'),
      '---\nname: zeta_agent\ndescription: "Zeta file fallback implementation"\n---\n# Zeta Agent'
    );

    // Dual-indexing test: Title Case name in kebab-case directory
    const careerDir = path.join(agentsDir, 'career-advisor');
    await fs.mkdir(careerDir, { recursive: true });
    await fs.writeFile(
      path.join(careerDir, 'agent.md'),
      '---\nname: Career Advisor\ndescription: "Executive career partner and JD auditor"\n---\n# Career Advisor'
    );

    // Non-agent files without frontmatter (must be ignored)
    await fs.writeFile(
      path.join(agentsDir, 'README.md'),
      '# Agents Directory\nThis directory holds agent definitions.\n'
    );
    await fs.writeFile(
      path.join(agentsDir, 'scratchpad.md'),
      'Just a regular markdown file with notes and no frontmatter.'
    );
    const nonAgentDir = path.join(agentsDir, 'plain-notes');
    await fs.mkdir(nonAgentDir, { recursive: true });
    await fs.writeFile(
      path.join(nonAgentDir, 'agent.md'),
      '# Plain Notes\nNot an agent definition without YAML frontmatter.'
    );

    emptyTempDir = await fs.mkdtemp(path.join(os.tmpdir(), 'jev-empty-'));
  });

  afterAll(async () => {
    if (origHarness !== undefined) process.env.JEV_HARNESS = origHarness;
    else delete process.env.JEV_HARNESS;
    clearConfigCacheForTesting();
    if (fixtureDir) await fs.rm(fixtureDir, { recursive: true, force: true });
    if (emptyTempDir) await fs.rm(emptyTempDir, { recursive: true, force: true });
  });

  test('discovers all workspace agents dynamically from .agents/agents or .agents/subagents', async () => {
    const agents = await discoverWorkspaceAgents(fixtureDir);
    expect(agents).toBeDefined();
    expect(typeof agents).toBe('object');
    expect(agents.self).toBeDefined();
    expect(agents.none).toBeDefined();
    expect(agents.alpha_agent).toBeDefined();
    expect(agents.beta_agent).toBeDefined();
    expect(agents.gamma_agent).toBeDefined();
    expect(agents['delta-agent']).toBe('Delta specialist for testing directory-per-agent');
    expect(agents['epsilon-agent']).toBe('Epsilon specialist with default directory name');
    expect(agents.zeta_agent).toBe('Zeta directory implementation');
    expect(agents['Career Advisor']).toBe('Executive career partner and JD auditor');
    expect(agents['career-advisor']).toBe('Executive career partner and JD auditor');

    const keys = Object.keys(agents);
    expect(keys.length).toBeGreaterThanOrEqual(9);
  });

  test('dual-indexes human-readable Title Case names and normalized kebab-case aliases', async () => {
    const agents = await discoverWorkspaceAgents(fixtureDir);
    expect(agents['Career Advisor']).toBeDefined();
    expect(agents['career-advisor']).toBeDefined();
    expect(agents['Career Advisor']).toBe('Executive career partner and JD auditor');
    expect(agents['career-advisor']).toBe('Executive career partner and JD auditor');
  });

  test('supports kebab-case identifiers and directory-per-agent precedence over file fallbacks', async () => {
    const agents = await discoverWorkspaceAgents(fixtureDir);
    expect(agents['delta-agent']).toBeDefined();
    expect(agents['epsilon-agent']).toBeDefined();
    // Precedence: directory description must win over file fallback description
    expect(agents.zeta_agent).toBe('Zeta directory implementation');
  });

  test('ignores non-agent markdown files and notes that lack valid YAML frontmatter', async () => {
    const agents = await discoverWorkspaceAgents(fixtureDir);
    expect(agents.README).toBeUndefined();
    expect(agents.readme).toBeUndefined();
    expect(agents.scratchpad).toBeUndefined();
    expect(agents['plain-notes']).toBeUndefined();
  });

  test('falls back cleanly to at least self and none when workspace has no agents directory', async () => {
    const emptyAgents = await discoverWorkspaceAgents(emptyTempDir);
    expect(emptyAgents).toBeDefined();
    expect(emptyAgents.self).toBeDefined();
    expect(emptyAgents.none).toBeDefined();
    expect(Object.keys(emptyAgents).length).toBeGreaterThanOrEqual(2);
  });

  test('findWorkspaceRoot walks up to locate project root', async () => {
    const subSubDir = path.join(fixtureDir, 'deep', 'nested', 'subdir');
    await fs.mkdir(subSubDir, { recursive: true });

    const rootFromDeep = findWorkspaceRoot(subSubDir);
    expect(rootFromDeep).toBe(path.resolve(fixtureDir));

    const rootFromEmpty = findWorkspaceRoot(emptyTempDir);
    expect(rootFromEmpty).toBe(path.resolve(emptyTempDir));
  });

  test('discovers subagents via JEV_AGENTS_DIR environment override', async () => {
    const customDir = await fs.mkdtemp(path.join(os.tmpdir(), 'jev-custom-agents-'));
    try {
      await fs.writeFile(
        path.join(customDir, 'custom_env_agent.md'),
        '---\nname: custom_env_agent\ndescription: "Discovered via JEV_AGENTS_DIR"\n---\n# Custom Agent'
      );
      process.env.JEV_AGENTS_DIR = customDir;
      const agents = await discoverWorkspaceAgents(emptyTempDir);
      expect(agents.custom_env_agent).toBe('Discovered via JEV_AGENTS_DIR');
    } finally {
      delete process.env.JEV_AGENTS_DIR;
      await fs.rm(customDir, { recursive: true, force: true });
    }
  });

  test('discovers subagents via agentPaths in config', async () => {
    const configDir = await fs.mkdtemp(path.join(os.tmpdir(), 'jev-config-agents-'));
    const configFile = path.join(configDir, 'test-config.json');
    const agentsPath = path.join(configDir, 'custom-agents');
    await fs.mkdir(agentsPath, { recursive: true });

    try {
      await fs.writeFile(
        path.join(agentsPath, 'custom_cfg_agent.md'),
        '---\nname: custom_cfg_agent\ndescription: "Discovered via config agentPaths"\n---\n# Config Agent'
      );
      await fs.writeFile(
        configFile,
        JSON.stringify({
          version: 2,
          defaults: {
            agentPaths: [agentsPath]
          }
        })
      );
      process.env.JEV_CONFIG_FILE = configFile;
      clearConfigCacheForTesting();

      const testWorkspaceDir = await fs.mkdtemp(path.join(os.tmpdir(), 'jev-empty-ws-'));
      try {
        const agents = await discoverWorkspaceAgents(testWorkspaceDir);
        expect(agents.custom_cfg_agent).toBe('Discovered via config agentPaths');
      } finally {
        await fs.rm(testWorkspaceDir, { recursive: true, force: true });
      }
    } finally {
      delete process.env.JEV_CONFIG_FILE;
      clearConfigCacheForTesting();
      await fs.rm(configDir, { recursive: true, force: true });
    }
  });

  test('returns cached agents map within 15s TTL', async () => {
    const t0 = performance.now();
    const agents1 = await discoverWorkspaceAgents(fixtureDir);
    const t1 = performance.now();
    const agents2 = await discoverWorkspaceAgents(fixtureDir);
    const t2 = performance.now();

    expect(agents1).toEqual(agents2);
    // Cached call should be faster than initial discovery
    expect(t2 - t1).toBeLessThanOrEqual(t1 - t0 + 5);
  });
});

describe('Stage 3: Drift Detection & Compliance Engine', () => {
  test('flags free tier leakage when task is openrouter_free but paid subagent was called', () => {
    const telemetry = [
      {
        timestamp: '2026-10-17T12:00:00.000Z',
        tool: 'jev_dispatch',
        recommendedTier: 'openrouter_free',
        sessionId: 'session-123'
      }
    ];

    const invocations = [
      {
        timestamp: '2026-10-17T12:01:00.000Z',
        sessionId: 'session-123',
        toolName: 'invoke_subagent',
        model: 'flash',
        role: 'code_engineer'
      }
    ];

    const report = evaluateDrift(telemetry, invocations);
    expect(report.totalDispatches).toBe(1);
    expect(report.totalViolations).toBe(1);
    expect(report.complianceRate).toBe(0);
    expect(report.violationsByType.free_tier_leakage).toBe(1);
    expect(report.events[0].type).toBe('free_tier_leakage');
  });

  test('flags unapproved pro usage when model is pro without user confirmation', () => {
    const telemetry = [
      {
        timestamp: '2026-10-17T12:00:00.000Z',
        tool: 'jev_dispatch',
        recommendedTier: 'pro',
        sessionId: 'session-456'
      }
    ];

    const invocations = [
      {
        timestamp: '2026-10-17T12:00:30.000Z',
        sessionId: 'session-456',
        toolName: 'invoke_subagent',
        model: 'pro',
        userApproved: false
      }
    ];

    const report = evaluateDrift(telemetry, invocations);
    expect(report.totalViolations).toBe(1);
    expect(report.violationsByType.unapproved_pro).toBe(1);
    expect(report.events[0].severity).toBe('critical');
  });

  test('flags invalid model enums passed to invoke_subagent', () => {
    const invocations = [
      {
        timestamp: '2026-10-17T12:00:00.000Z',
        sessionId: 'session-789',
        toolName: 'invoke_subagent',
        model: 'openrouter_free'
      }
    ];

    const report = evaluateDrift([], invocations);
    expect(report.violationsByType.enum_mismatch).toBe(1);
    expect(report.events[0].details).toContain('invalid Model enum');
  });

  test('computes 100% compliance when all executions match recommendations', () => {
    const telemetry = [
      {
        timestamp: '2026-10-17T12:00:00.000Z',
        tool: 'jev_dispatch',
        recommendedTier: 'flash',
        sessionId: 'session-100'
      }
    ];

    const invocations = [
      {
        timestamp: '2026-10-17T12:01:00.000Z',
        sessionId: 'session-100',
        toolName: 'invoke_subagent',
        model: 'flash',
        role: 'code_engineer'
      }
    ];

    const report = evaluateDrift(telemetry, invocations);
    expect(report.totalDispatches).toBe(1);
    expect(report.totalViolations).toBe(0);
    expect(report.complianceRate).toBe(100.0);
  });
});
