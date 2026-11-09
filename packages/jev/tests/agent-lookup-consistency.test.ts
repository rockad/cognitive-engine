import { describe, it, expect, beforeEach, afterEach } from 'bun:test';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { evaluateClaude } from '../src/guard.ts';
import { agentLookupDirs, describeAgentLookup, readAgentDefinition } from '../src/agent-definitions.ts';
import { checkScriptText, fsResolver } from '../src/workflow-guard.ts';
import { clearConfigCacheForTesting } from '../src/core/config.ts';

let tmp = '';
let proj = '';
let savedEnv: NodeJS.ProcessEnv = {};

beforeEach(() => {
  savedEnv = { ...process.env };
  for (const v of ['JEV_HARNESS', 'CLAUDE_PROJECT_DIR']) delete process.env[v];
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-lookup-'));
  proj = path.join(tmp, 'proj');
  fs.mkdirSync(path.join(proj, '.git'), { recursive: true });
  process.env.HOME = tmp;
  process.env.JEV_CONFIG_FILE = path.join(tmp, 'config.json');
  process.env.JEV_CLAUDE_STATE_DIR = path.join(tmp, 'state');
  process.env.JEV_CLAUDE_DECISIONS_FILE = path.join(tmp, 'decisions.jsonl');
  clearConfigCacheForTesting();
});

afterEach(() => {
  process.env = savedEnv;
  clearConfigCacheForTesting();
  fs.rmSync(tmp, { recursive: true, force: true });
});

function installPlugin(key: string, agents: Record<string, string>): string {
  const installPath = path.join(tmp, '.claude', 'plugins', 'cache', key.replace('@', '-'));
  fs.mkdirSync(path.join(installPath, 'agents'), { recursive: true });
  for (const [name, front] of Object.entries(agents)) fs.writeFileSync(path.join(installPath, 'agents', `${name}.md`), `---\n${front}\n---\nbody\n`);
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
  return path.join(installPath, 'agents');
}

const writeSettings = (dir: string, name: string, body: unknown) => {
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, name), typeof body === 'string' ? body : JSON.stringify(body));
};

describe('workflow missing-definition message matches the Agent guard', () => {
  const asAgent = () => evaluateClaude({ tool_name: 'Agent', session_id: 's', tool_input: { subagent_type: 'ghost' }, cwd: proj })!;
  const asWorkflow = () => checkScriptText(`await agent('p', {agentType: 'ghost'})`, 'claude-sonnet-5-5', fsResolver(proj));

  it('names the agent and every lookup location, in the same words', () => {
    const wf = asWorkflow();
    expect(wf).toHaveLength(1);
    const reason = wf[0].reason;
    expect(reason).toContain('"ghost"');
    expect(reason).toContain(describeAgentLookup(proj));
    expect(reason).toContain(path.join(proj, '.claude', 'agents'));
    expect(reason).toContain('installed_plugins.json');
    // the sentence the two guards share
    const shared = 'no definition for it could be found, so the model it will run on cannot be checked against the session ceiling';
    expect(reason).toContain(shared);
    expect(asAgent().reason).toContain(shared);
  });

  it('still says "no definition" (the wording existing callers match on)', () => {
    expect(asWorkflow()[0].reason).toContain('no definition');
  });

  it('a resolver with no lookup description still produces the sentence', () => {
    const s = checkScriptText(`await agent('p', {agentType: 'ghost'})`, 'claude-sonnet-5-5');
    expect(s[0].reason).toContain('"ghost"');
    expect(s[0].reason).toContain('no definition for it could be found');
  });
});

describe('plugin lookup prefers enabled plugins, and still searches all', () => {
  const manifestOrder = () => {
    installPlugin('aaa@mk', { dup: 'model: opus' }); // first in the manifest, but disabled
    installPlugin('bbb@mk', { dup: 'model: haiku' }); // enabled
    installPlugin('ccc@mk', { only: 'model: sonnet' }); // not mentioned in settings
  };

  it('without readable settings the order is the manifest order, as before', () => {
    manifestOrder();
    expect(readAgentDefinition('dup', proj).model).toBe('opus');
  });

  it('an enabled plugin is searched before a disabled one', () => {
    manifestOrder();
    writeSettings(path.join(tmp, '.claude'), 'settings.json', { enabledPlugins: { 'aaa@mk': false, 'bbb@mk': true } });
    expect(readAgentDefinition('dup', proj).model).toBe('haiku');
  });

  it('never finds fewer definitions: disabled and unlisted plugins are still searched', () => {
    manifestOrder();
    writeSettings(path.join(tmp, '.claude'), 'settings.json', { enabledPlugins: { 'aaa@mk': false, 'bbb@mk': true } });
    expect(readAgentDefinition('only', proj)).toEqual({ found: true, model: 'sonnet' });
    expect(readAgentDefinition('aaa:dup', proj)).toEqual({ found: true, model: 'opus' });
    const dirs = agentLookupDirs('dup', proj);
    for (const k of ['aaa-mk', 'bbb-mk', 'ccc-mk']) expect(dirs.some((d) => d.includes(k))).toBe(true);
  });

  it('project settings and settings.local.json count, the later layer winning', () => {
    manifestOrder();
    writeSettings(path.join(tmp, '.claude'), 'settings.json', { enabledPlugins: { 'bbb@mk': true } });
    writeSettings(path.join(proj, '.claude'), 'settings.json', { enabledPlugins: { 'aaa@mk': true } });
    writeSettings(path.join(proj, '.claude'), 'settings.local.json', { enabledPlugins: { 'bbb@mk': false } });
    // aaa enabled (project), bbb disabled again (local): aaa first
    expect(readAgentDefinition('dup', proj).model).toBe('opus');
  });

  it('unreadable or odd settings fall back to the manifest order and never throw', () => {
    manifestOrder();
    for (const body of ['{ not json', '[]', '"x"', JSON.stringify({ enabledPlugins: 'yes' }), JSON.stringify({ enabledPlugins: ['bbb@mk'] })]) {
      writeSettings(path.join(tmp, '.claude'), 'settings.json', body);
      expect(readAgentDefinition('dup', proj)).toEqual({ found: true, model: 'opus' });
    }
  });

  it('only true enables: a string "true" or 1 does not', () => {
    manifestOrder();
    writeSettings(path.join(tmp, '.claude'), 'settings.json', { enabledPlugins: { 'bbb@mk': 'true' } });
    expect(readAgentDefinition('dup', proj).model).toBe('opus');
  });

  it('project and user directories still come before every plugin', () => {
    manifestOrder();
    writeSettings(path.join(tmp, '.claude'), 'settings.json', { enabledPlugins: { 'bbb@mk': true } });
    writeSettings(path.join(proj, '.claude', 'agents'), 'dup.md', '---\nmodel: sonnet\n---\n');
    expect(readAgentDefinition('dup', proj).model).toBe('sonnet');
  });
});
