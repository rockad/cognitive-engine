import { describe, it, expect, beforeEach, afterEach } from 'bun:test';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { evaluateClaude } from '../src/guard.ts';
import { readAgentDefinition } from '../src/agent-definitions.ts';
import { parseFrontmatterFields } from '../src/agent-frontmatter.ts';
import { clearConfigCacheForTesting } from '../src/core/config.ts';

/**
 * Claude Code registers each agent under the frontmatter `name:`, not the file name, and loads every
 * .md file of the agents directories, subdirectories included. The guard must find the definition the
 * same way, and ask whenever which file Claude Code runs is not certain. Session model: sonnet.
 */

let tmp = '';
let proj = '';
let savedEnv: NodeJS.ProcessEnv = {};

beforeEach(() => {
  savedEnv = { ...process.env };
  for (const v of ['JEV_HARNESS', 'ANTIGRAVITY_CLI', 'GEMINI_CLI', 'CLAUDE_CODE', 'CLAUDECODE', 'CLAUDE_PROJECT_DIR', 'OPENCODE_SESSION', 'OPENCODE_HOME', 'CORTEX_SESSION', 'CLAUDE_CODE_SUBAGENT_MODEL', 'CLAUDE_CODE_SUBAGENT_MODEL_FORCE', 'CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL']) delete process.env[v];
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-name-'));
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

const fm = (body: string) => ['---', ...body.split('\n'), '---', 'body', ''].join('\n');
const projectAgents = () => path.join(proj, '.claude', 'agents');
const userAgents = () => path.join(tmp, '.claude', 'agents');

function put(dir: string, rel: string, text: string): void {
  const file = path.join(dir, rel);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, text);
}

function transcript(model: string): string {
  const file = path.join(tmp, 't.jsonl');
  fs.writeFileSync(file, `${JSON.stringify({ type: 'assistant', message: { model } })}\n`);
  return file;
}

const askFor = (subagent: string) =>
  evaluateClaude({ tool_name: 'Agent', transcript_path: transcript('claude-sonnet-5-5'), cwd: proj, tool_input: { subagent_type: subagent } });

const isConflict = (model: string | null) => model !== null && model.includes('unreadable');

describe('the reviewer input: the file name and the declared name disagree', () => {
  beforeEach(() => {
    put(projectAgents(), 'helper.md', fm('name: other\nmodel: haiku'));
    put(projectAgents(), 'zz.md', fm('name: helper\nmodel: opus'));
  });

  it('asks instead of reading helper.md and allowing haiku', () => {
    expect(askFor('helper')?.decision).toBe('ask');
  });

  it('reports a conflict, not the model of helper.md', () => {
    const def = readAgentDefinition('helper', proj);
    expect(def.found).toBe(true);
    expect(isConflict(def.model)).toBe(true);
  });
});

describe('a definition is found by its declared name', () => {
  it('a file with another file name but the right name is the definition', () => {
    put(projectAgents(), 'zz.md', fm('name: helper\nmodel: opus'));
    expect(readAgentDefinition('helper', proj)).toEqual({ found: true, model: 'opus' });
    expect(askFor('helper')?.decision).toBe('ask');
  });

  it('a quoted name matches', () => {
    put(projectAgents(), 'zz.md', fm('name: "helper"\nmodel: haiku'));
    expect(readAgentDefinition('helper', proj)).toEqual({ found: true, model: 'haiku' });
  });

  it('subdirectories are scanned', () => {
    put(projectAgents(), 'team/deep/zz.md', fm('name: helper\nmodel: haiku'));
    expect(readAgentDefinition('helper', proj)).toEqual({ found: true, model: 'haiku' });
  });

  it('a file named like the agent with no name: key is still the definition (as before)', () => {
    put(projectAgents(), 'helper.md', fm('model: haiku'));
    expect(readAgentDefinition('helper', proj)).toEqual({ found: true, model: 'haiku' });
  });

  it('a file named like the agent that declares the same name is the definition', () => {
    put(projectAgents(), 'helper.md', fm('name: helper\nmodel: haiku'));
    expect(readAgentDefinition('helper', proj)).toEqual({ found: true, model: 'haiku' });
  });

  it('a file with another file name and no name: key is not a claimant', () => {
    put(projectAgents(), 'zz.md', fm('model: opus'));
    put(userAgents(), 'helper.md', fm('model: haiku'));
    expect(readAgentDefinition('helper', proj)).toEqual({ found: true, model: 'haiku' });
  });

  it('the nearest directory still decides: a project name match beats a user file name match', () => {
    put(projectAgents(), 'zz.md', fm('name: helper\nmodel: haiku'));
    put(userAgents(), 'helper.md', fm('model: opus'));
    expect(readAgentDefinition('helper', proj)).toEqual({ found: true, model: 'haiku' });
  });

  it('a user-level file with a different file name is found by its name', () => {
    put(userAgents(), 'zz.md', fm('name: helper\nmodel: opus'));
    expect(readAgentDefinition('helper', proj)).toEqual({ found: true, model: 'opus' });
  });

  it('an installed plugin agent is found by its name too', () => {
    const installPath = path.join(tmp, '.claude', 'plugins', 'cache', 'p-m');
    put(path.join(installPath, 'agents'), 'zz.md', fm('name: helper\nmodel: opus'));
    put(path.join(tmp, '.claude', 'plugins'), 'installed_plugins.json', JSON.stringify({ version: 2, plugins: { 'p@m': [{ scope: 'user', installPath, version: '1' }] } }));
    expect(readAgentDefinition('p:helper', proj)).toEqual({ found: true, model: 'opus' });
    expect(readAgentDefinition('helper', proj)).toEqual({ found: true, model: 'opus' });
  });

  it('a directory named like the definition is not a definition (as before)', () => {
    fs.mkdirSync(path.join(projectAgents(), 'helper.md'), { recursive: true });
    put(userAgents(), 'helper.md', fm('model: haiku'));
    expect(readAgentDefinition('helper', proj)).toEqual({ found: true, model: 'haiku' });
  });
});

describe('more than one file claims the name: conflict', () => {
  it('two files declare it, even with different file names', () => {
    put(projectAgents(), 'a.md', fm('name: helper\nmodel: haiku'));
    put(projectAgents(), 'b.md', fm('name: helper\nmodel: haiku'));
    expect(isConflict(readAgentDefinition('helper', proj).model)).toBe(true);
  });

  it('one in a subdirectory and one at the top', () => {
    put(projectAgents(), 'helper.md', fm('name: helper\nmodel: haiku'));
    put(projectAgents(), 'sub/x.md', fm('name: helper\nmodel: opus'));
    expect(isConflict(readAgentDefinition('helper', proj).model)).toBe(true);
    expect(askFor('helper')?.decision).toBe('ask');
  });

  it('a file by file name (no name key) and another by declared name', () => {
    put(projectAgents(), 'helper.md', fm('model: haiku'));
    put(projectAgents(), 'zz.md', fm('name: helper\nmodel: opus'));
    expect(isConflict(readAgentDefinition('helper', proj).model)).toBe(true);
  });

  it('the conflict names the files', () => {
    put(projectAgents(), 'a.md', fm('name: helper\nmodel: haiku'));
    put(projectAgents(), 'b.md', fm('name: helper\nmodel: opus'));
    const model = readAgentDefinition('helper', proj).model ?? '';
    expect(model).toContain('a.md');
    expect(model).toContain('b.md');
  });
});

describe('a name that cannot be read is a conflict, never a quiet skip', () => {
  it('the file named like the agent has a name with an escape', () => {
    put(projectAgents(), 'helper.md', fm('name: "hel\\u0070er"\nmodel: haiku'));
    expect(isConflict(readAgentDefinition('helper', proj).model)).toBe(true);
  });

  it('the file named like the agent has a duplicate name key', () => {
    put(projectAgents(), 'helper.md', fm('name: helper\nname: other\nmodel: haiku'));
    expect(isConflict(readAgentDefinition('helper', proj).model)).toBe(true);
  });

  it('the file named like the agent has an unreadable block', () => {
    put(projectAgents(), 'helper.md', `---\nname: helper\nmodel: haiku\nno closing delimiter\n`);
    expect(isConflict(readAgentDefinition('helper', proj).model)).toBe(true);
  });

  it('another file with an unreadable name that mentions the agent could be it: conflict', () => {
    put(projectAgents(), 'helper.md', fm('name: helper\nmodel: haiku'));
    put(projectAgents(), 'zz.md', fm('name: "hel\\u0070er"\nmodel: opus'));
    expect(isConflict(readAgentDefinition('helper', proj).model)).toBe(true);
  });

  it('another file with an unreadable name that never mentions the agent is ignored', () => {
    put(projectAgents(), 'helper.md', fm('name: helper\nmodel: haiku'));
    put(projectAgents(), 'zz.md', fm('name: "other\\u0021"\nmodel: opus'));
    expect(readAgentDefinition('helper', proj)).toEqual({ found: true, model: 'haiku' });
  });

  it('the same holds in the user directory', () => {
    put(userAgents(), 'helper.md', fm('name: other\nmodel: haiku'));
    expect(isConflict(readAgentDefinition('helper', proj).model)).toBe(true);
  });
});

describe('never fewer definitions than before', () => {
  it('a plain <name>.md with a model is read exactly as it was', () => {
    put(projectAgents(), 'q.md', fm('model: opus'));
    expect(readAgentDefinition('q', proj)).toEqual({ found: true, model: 'opus' });
  });

  it('nothing found stays not found', () => {
    put(projectAgents(), 'q.md', fm('name: q\nmodel: opus'));
    expect(readAgentDefinition('nope', proj)).toEqual({ found: false, model: null });
  });

  it('a path-like name still finds nothing', () => {
    put(projectAgents(), 'q.md', fm('name: q'));
    expect(readAgentDefinition('../q', proj)).toEqual({ found: false, model: null });
  });
});

describe('the declared name, read like the model', () => {
  const none = { kind: 'none' };
  it('plain, quoted and single-quoted names', () => {
    expect(parseFrontmatterFields(fm('name: helper'), null).name).toEqual({ kind: 'name', value: 'helper' });
    expect(parseFrontmatterFields(fm('"name": "helper"'), null).name).toEqual({ kind: 'name', value: 'helper' });
    expect(parseFrontmatterFields(fm("name: 'helper' # c")).name).toEqual({ kind: 'name', value: 'helper' });
  });

  it('no name, an empty name, no frontmatter', () => {
    expect(parseFrontmatterFields(fm('model: haiku'), null).name).toEqual(none);
    expect(parseFrontmatterFields(fm('name:'), null).name).toEqual(none);
    expect(parseFrontmatterFields('body only', null).name).toEqual(none);
  });

  it('a name that Jev and Claude Code read differently is unreadable', () => {
    // `bad: x: y` breaks the first parse; the fallback quotes `name: helper # c` whole.
    expect(parseFrontmatterFields(fm('bad: x: y\nname: helper # c')).name.kind).toBe('unreadable');
  });

  it('a name problem does not make the model unreadable', () => {
    const f = parseFrontmatterFields(fm('name: "x\\u0041"\nmodel: haiku'), null);
    expect(f.name.kind).toBe('unreadable');
    expect(f.model).toEqual({ kind: 'model', value: 'haiku' });
  });

  it('a block that cannot be read at all makes both unreadable', () => {
    const f = parseFrontmatterFields('---\nname: x\n', null);
    expect(f.name.kind).toBe('unreadable');
    expect(f.model.kind).toBe('unreadable');
  });
});
