import { describe, it, expect, beforeEach, afterEach } from 'bun:test';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { evaluateClaude } from '../src/guard.ts';
import { readAgentDefinition } from '../src/agent-definitions.ts';
import { clearConfigCacheForTesting } from '../src/core/config.ts';

let tmp = '';
let savedEnv: NodeJS.ProcessEnv = {};

function transcript(model: string): string {
  const file = path.join(tmp, 't.jsonl');
  fs.writeFileSync(file, `${JSON.stringify({ type: 'assistant', message: { model } })}\n`);
  return file;
}

function agentFile(dir: string, name: string, front: string): string {
  const file = path.join(dir, `${name}.md`);
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(file, `---\n${front}\n---\nbody\n`);
  return file;
}

/** Register a plugin under ~/.claude/plugins/installed_plugins.json with an agents/ dir. */
function installPlugin(key: string, agents: Record<string, string>): string {
  const installPath = path.join(tmp, '.claude', 'plugins', 'cache', key.replace('@', '-'));
  for (const [name, front] of Object.entries(agents)) agentFile(path.join(installPath, 'agents'), name, front);
  const manifest = path.join(tmp, '.claude', 'plugins', 'installed_plugins.json');
  let doc: any = { version: 2, plugins: {} };
  try {
    doc = JSON.parse(fs.readFileSync(manifest, 'utf8'));
  } catch {
    // first plugin
  }
  doc.plugins[key] = [{ scope: 'user', installPath, version: '1.0.0' }];
  fs.mkdirSync(path.dirname(manifest), { recursive: true });
  fs.writeFileSync(manifest, JSON.stringify(doc));
  return installPath;
}

function call(subagentType: unknown, cwd: string | undefined, extra: Record<string, unknown> = {}): ReturnType<typeof evaluateClaude> {
  return evaluateClaude({
    tool_name: 'Agent',
    transcript_path: transcript('claude-sonnet-5-5'),
    cwd,
    tool_input: { subagent_type: subagentType, ...extra }
  });
}

beforeEach(() => {
  savedEnv = { ...process.env };
  for (const v of ['JEV_HARNESS', 'ANTIGRAVITY_CLI', 'GEMINI_CLI', 'CLAUDE_CODE', 'CLAUDECODE', 'CLAUDE_PROJECT_DIR', 'OPENCODE_SESSION', 'OPENCODE_HOME', 'CORTEX_SESSION', 'CLAUDE_CODE_SUBAGENT_MODEL', 'CLAUDE_CODE_SUBAGENT_MODEL_FORCE', 'CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL']) delete process.env[v];
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-agent-unknown-'));
  process.env.HOME = tmp;
  process.env.JEV_CONFIG_FILE = path.join(tmp, 'config.json');
  clearConfigCacheForTesting();
});

afterEach(() => {
  process.env = savedEnv;
  clearConfigCacheForTesting();
  fs.rmSync(tmp, { recursive: true, force: true });
});

describe('unknown subagent types ask instead of passing as inherit', () => {
  it('asks for a name with no definition anywhere, naming the agent and the lookup locations', () => {
    const v = call('ghost', path.join(tmp, 'proj'));
    expect(v?.decision).toBe('ask');
    expect(v?.reason).toContain('ghost');
    expect(v?.reason).toContain('.claude/agents');
    expect(v?.reason).toContain('plugin');
    expect(v?.audit?.kind).toBe('unknown-agent');
  });

  it.each(['general-purpose', 'Explore', 'Plan', 'statusline-setup', 'claude-code-guide'])('allows the built-in %s', (name) => {
    expect(call(name, path.join(tmp, 'proj'))).toBeNull();
  });

  it('allows a call with no subagent_type (the general-purpose default)', () => {
    expect(call(undefined, path.join(tmp, 'proj'))).toBeNull();
  });

  it('asks for a subagent_type that is not a string, or is empty', () => {
    expect(call(42, tmp)?.decision).toBe('ask');
    expect(call('', tmp)?.decision).toBe('ask');
  });

  it('asks about an unfindable agent whatever model the call sets (its effort cannot be read)', () => {
    expect(call('ghost', tmp, { model: 'haiku' })?.audit).toMatchObject({ kind: 'unknown-agent' });
    expect(call('ghost', tmp, { model: 'opus' })?.decision).toBe('ask');
  });

  it('allows a defined agent that sets no model (inherit)', () => {
    const project = path.join(tmp, 'proj');
    agentFile(path.join(project, '.claude', 'agents'), 'plain', 'name: plain');
    expect(call('plain', project)).toBeNull();
  });

  it('a project definition overrides a built-in name, so its model applies', () => {
    const project = path.join(tmp, 'proj');
    agentFile(path.join(project, '.claude', 'agents'), 'Plan', 'model: opus');
    expect(call('Plan', project)?.decision).toBe('ask');
  });

  it('still allows a defined agent whose model is not above the session', () => {
    const project = path.join(tmp, 'proj');
    agentFile(path.join(project, '.claude', 'agents'), 'cheap', 'model: haiku');
    expect(call('cheap', project)).toBeNull();
  });
});

describe('project-root agents found from a subdirectory cwd', () => {
  it('finds .claude/agents at the git root and applies the ceiling', () => {
    const root = path.join(tmp, 'repo');
    fs.mkdirSync(path.join(root, '.git'), { recursive: true });
    agentFile(path.join(root, '.claude', 'agents'), 'deep', 'model: opus');
    agentFile(path.join(root, '.claude', 'agents'), 'cheap', 'model: haiku');
    const sub = path.join(root, 'packages', 'a', 'src');
    fs.mkdirSync(sub, { recursive: true });

    expect(call('deep', sub)?.decision).toBe('ask');
    expect(call('deep', sub)?.reason).toContain('opus');
    expect(call('cheap', sub)).toBeNull();
    expect(readAgentDefinition('deep', sub)).toEqual({ found: true, model: 'opus' });
  });

  it('finds .claude/agents at CLAUDE_PROJECT_DIR when that is the root', () => {
    const root = path.join(tmp, 'work');
    agentFile(path.join(root, '.claude', 'agents'), 'deep', 'model: opus');
    const sub = path.join(root, 'x', 'y');
    fs.mkdirSync(sub, { recursive: true });
    process.env.CLAUDE_PROJECT_DIR = root;
    expect(call('deep', sub)?.decision).toBe('ask');
  });

  it('checks CLAUDE_PROJECT_DIR even when the hook cwd is outside it', () => {
    const root = path.join(tmp, 'work');
    agentFile(path.join(root, '.claude', 'agents'), 'deep', 'model: opus');
    const elsewhere = path.join(tmp, 'elsewhere');
    fs.mkdirSync(elsewhere, { recursive: true });
    process.env.CLAUDE_PROJECT_DIR = root;
    expect(call('deep', elsewhere)?.decision).toBe('ask');
  });

  it('a nearer definition does not shadow one at the project root: differing models ask', () => {
    const root = path.join(tmp, 'repo');
    fs.mkdirSync(path.join(root, '.git'), { recursive: true });
    agentFile(path.join(root, '.claude', 'agents'), 'deep', 'model: opus');
    const sub = path.join(root, 'pkg');
    agentFile(path.join(sub, '.claude', 'agents'), 'deep', 'model: haiku');
    // Claude Code loads project agents from the root, so the cheaper nearer file cannot be trusted
    expect(call('deep', sub)?.decision).toBe('ask');
  });

  it('does not walk above the git root', () => {
    const outer = path.join(tmp, 'outer');
    agentFile(path.join(outer, '.claude', 'agents'), 'deep', 'model: opus');
    const root = path.join(outer, 'repo');
    fs.mkdirSync(path.join(root, '.git'), { recursive: true });
    const v = call('deep', root);
    expect(v?.decision).toBe('ask');
    expect(v?.audit?.kind).toBe('unknown-agent');
  });
});

describe('plugin agents', () => {
  it('maps plugin:agent to the plugin agents dir and applies its model', () => {
    installPlugin('toolkit@market', { reviewer: 'model: opus', scribe: 'model: haiku', plain: 'name: plain' });
    expect(call('toolkit:reviewer', tmp)?.decision).toBe('ask');
    expect(call('toolkit:reviewer', tmp)?.reason).toContain('opus');
    expect(call('toolkit:scribe', tmp)).toBeNull();
    expect(call('toolkit:plain', tmp)).toBeNull();
  });

  it('asks when the namespaced agent is not found in that plugin', () => {
    installPlugin('toolkit@market', { reviewer: 'model: haiku' });
    const v = call('toolkit:missing', tmp);
    expect(v?.decision).toBe('ask');
    expect(v?.reason).toContain('toolkit:missing');
  });

  it('asks when the plugin is not installed, or the manifest is missing or malformed', () => {
    expect(call('toolkit:reviewer', tmp)?.decision).toBe('ask');
    const manifest = path.join(tmp, '.claude', 'plugins', 'installed_plugins.json');
    fs.mkdirSync(path.dirname(manifest), { recursive: true });
    fs.writeFileSync(manifest, '{not json');
    expect(call('toolkit:reviewer', tmp)?.decision).toBe('ask');
    fs.writeFileSync(manifest, JSON.stringify({ plugins: 'nope' }));
    expect(call('toolkit:reviewer', tmp)?.decision).toBe('ask');
  });

  it('does not take another plugin\'s agent for a namespaced name', () => {
    installPlugin('other@market', { reviewer: 'model: haiku' });
    expect(call('toolkit:reviewer', tmp)?.decision).toBe('ask');
  });

  it('finds a bare plugin agent name too', () => {
    installPlugin('toolkit@market', { reviewer: 'model: opus' });
    expect(call('reviewer', tmp)?.decision).toBe('ask');
  });

  it('a manifest installPath that is not a string is ignored', () => {
    const manifest = path.join(tmp, '.claude', 'plugins', 'installed_plugins.json');
    fs.mkdirSync(path.dirname(manifest), { recursive: true });
    fs.writeFileSync(manifest, JSON.stringify({ plugins: { 'toolkit@m': [{ installPath: 7 }, null, 'x'] } }));
    expect(call('toolkit:reviewer', tmp)?.decision).toBe('ask');
  });
});

describe('hostile names', () => {
  it.each([
    '../escape',
    '..',
    '.hidden',
    'a/b',
    'a\\b',
    'toolkit:../../escape',
    'toolkit:/abs',
    ':reviewer',
    'toolkit:',
    'a:b:c',
    'name with space',
    'name\u0000nul'
  ])('asks for %j and never reads outside the agents dirs', (name) => {
    fs.writeFileSync(path.join(tmp, 'escape.md'), '---\nmodel: haiku\n---\n');
    installPlugin('toolkit@market', { reviewer: 'model: haiku' });
    fs.writeFileSync(path.join(tmp, '.claude', 'plugins', 'escape.md'), '---\nmodel: haiku\n---\n');
    const v = call(name, path.join(tmp, 'proj'));
    expect(v?.decision).toBe('ask');
    expect(readAgentDefinition(name, path.join(tmp, 'proj')).found).toBe(false);
  });
});

describe('symlinked agent files', () => {
  it('follows a symlinked agent file and applies its model', () => {
    const real = path.join(tmp, 'shared', 'deep.md');
    fs.mkdirSync(path.dirname(real), { recursive: true });
    fs.writeFileSync(real, '---\nmodel: opus\n---\n');
    const project = path.join(tmp, 'proj');
    fs.mkdirSync(path.join(project, '.claude', 'agents'), { recursive: true });
    fs.symlinkSync(real, path.join(project, '.claude', 'agents', 'deep.md'));
    expect(call('deep', project)?.decision).toBe('ask');
    expect(call('deep', project)?.reason).toContain('opus');
  });

  it('asks for a dangling symlink (treated as no definition)', () => {
    const project = path.join(tmp, 'proj');
    fs.mkdirSync(path.join(project, '.claude', 'agents'), { recursive: true });
    fs.symlinkSync(path.join(tmp, 'nowhere.md'), path.join(project, '.claude', 'agents', 'deep.md'));
    const v = call('deep', project);
    expect(v?.decision).toBe('ask');
    expect(v?.audit?.kind).toBe('unknown-agent');
  });

  it('follows a symlinked agents directory', () => {
    const realDir = path.join(tmp, 'shared-agents');
    agentFile(realDir, 'deep', 'model: opus');
    const project = path.join(tmp, 'proj');
    fs.mkdirSync(path.join(project, '.claude'), { recursive: true });
    fs.symlinkSync(realDir, path.join(project, '.claude', 'agents'));
    expect(call('deep', project)?.decision).toBe('ask');
  });
});
