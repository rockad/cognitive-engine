import { describe, it, expect, beforeEach, afterEach } from 'bun:test';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { evaluateClaude } from '../src/guard.ts';
import { readAgentDefinition } from '../src/agent-definitions.ts';
import { clearConfigCacheForTesting } from '../src/core/config.ts';

/**
 * Claude Code loads project agents from the project root; the guard walks from the hook cwd up to
 * it. A subdirectory's own `.claude/agents/x.md` must not shadow the root's definition for the
 * guard only: when the project defines the same agent in more than one place with different models,
 * which one runs is not known, so the guard asks. Never fewer definitions are found than before.
 * Session model in these tests: sonnet.
 */
let tmp = '';
let proj = '';
let sub = '';
let deep = '';
let savedEnv: NodeJS.ProcessEnv = {};

beforeEach(() => {
  savedEnv = { ...process.env };
  for (const v of ['JEV_HARNESS', 'ANTIGRAVITY_CLI', 'GEMINI_CLI', 'CLAUDE_CODE', 'CLAUDECODE', 'CLAUDE_PROJECT_DIR', 'OPENCODE_SESSION', 'OPENCODE_HOME', 'CORTEX_SESSION']) delete process.env[v];
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-agentroot-'));
  proj = path.join(tmp, 'proj');
  sub = path.join(proj, 'sub');
  deep = path.join(sub, 'deep');
  fs.mkdirSync(path.join(proj, '.git'), { recursive: true });
  fs.mkdirSync(deep, { recursive: true });
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

function define(dir: string, name: string, model: string | null) {
  const agents = path.join(dir, '.claude', 'agents');
  fs.mkdirSync(agents, { recursive: true });
  // the declared name must be the file's name: Claude Code registers a file under its `name:`
  fs.writeFileSync(path.join(agents, `${name}.md`), model === null ? `---\nname: ${name}\n---\nbody\n` : `---\nname: ${name}\nmodel: ${model}\n---\nbody\n`);
  return path.join(agents, `${name}.md`);
}
function transcript(model: string): string {
  const file = path.join(tmp, 't.jsonl');
  fs.writeFileSync(file, `${JSON.stringify({ type: 'assistant', message: { model } })}\n`);
  return file;
}
const verdict = (cwd: string, subagent_type = 'x') =>
  evaluateClaude({ tool_name: 'Agent', transcript_path: transcript('claude-sonnet-5-5'), cwd, tool_input: { subagent_type } });
const isConflict = (model: string | null) => typeof model === 'string' && model.includes('more than one place');

describe('a subdirectory definition cannot shadow the root definition', () => {
  it('root opus, subdirectory haiku, cwd in the subdirectory: asks', () => {
    const rootFile = define(proj, 'x', 'opus');
    const subFile = define(sub, 'x', 'haiku');
    const v = verdict(sub);
    expect(v?.decision).toBe('ask');
    expect(v?.reason).toContain(rootFile);
    expect(v?.reason).toContain(subFile);
    expect(isConflict(readAgentDefinition('x', sub).model)).toBe(true);
  });

  it('root haiku, subdirectory opus: asks too (either may be the one that runs)', () => {
    define(proj, 'x', 'haiku');
    define(sub, 'x', 'opus');
    expect(verdict(sub)?.decision).toBe('ask');
  });

  it('the cwd two levels down, definitions at the root and one level down', () => {
    define(proj, 'x', 'opus');
    define(sub, 'x', 'haiku');
    expect(verdict(deep)?.decision).toBe('ask');
  });

  it('a nearer definition with no model against a root definition that has one: asks', () => {
    define(proj, 'x', 'opus');
    define(sub, 'x', null);
    expect(verdict(sub)?.decision).toBe('ask');
  });

  it('a nearer unreadable frontmatter against a root definition: asks', () => {
    define(proj, 'x', 'haiku');
    const agents = path.join(sub, '.claude', 'agents');
    fs.mkdirSync(agents, { recursive: true });
    fs.writeFileSync(path.join(agents, 'x.md'), '---\n{model: haiku}\n---\n');
    expect(verdict(sub)?.decision).toBe('ask');
  });

  it('CLAUDE_PROJECT_DIR set: the root definition is found from a deep cwd', () => {
    process.env.CLAUDE_PROJECT_DIR = proj;
    define(proj, 'x', 'opus');
    expect(readAgentDefinition('x', deep)).toEqual({ found: true, model: 'opus' });
    expect(verdict(deep)?.decision).toBe('ask');
  });

  it('CLAUDE_PROJECT_DIR set and a conflicting subdirectory definition: asks', () => {
    process.env.CLAUDE_PROJECT_DIR = proj;
    define(proj, 'x', 'opus');
    define(deep, 'x', 'haiku');
    expect(verdict(deep)?.decision).toBe('ask');
  });

  it('a cwd outside CLAUDE_PROJECT_DIR: its own definition against the project root one', () => {
    process.env.CLAUDE_PROJECT_DIR = proj;
    const other = path.join(tmp, 'other');
    fs.mkdirSync(other, { recursive: true });
    define(proj, 'x', 'opus');
    define(other, 'x', 'haiku');
    expect(verdict(other)?.decision).toBe('ask');
  });
});

describe('definitions that agree, or that do not conflict, are unaffected', () => {
  it('the same model at both levels passes', () => {
    define(proj, 'x', 'haiku');
    define(sub, 'x', 'haiku');
    expect(verdict(sub)).toBeNull();
    expect(readAgentDefinition('x', sub)).toEqual({ found: true, model: 'haiku' });
  });

  it('the same model spelled differently (case, inherit versus none) is not a conflict', () => {
    define(proj, 'x', 'Haiku');
    define(sub, 'x', 'haiku');
    expect(verdict(sub)).toBeNull();
    define(proj, 'y', null);
    define(sub, 'y', 'inherit');
    expect(verdict(sub, 'y')).toBeNull();
  });

  it('only a subdirectory definition: it is found and read', () => {
    define(sub, 'x', 'opus');
    expect(readAgentDefinition('x', sub)).toEqual({ found: true, model: 'opus' });
    expect(verdict(sub)?.decision).toBe('ask');
  });

  it('only a root definition, cwd below it: found, nearest-first order unchanged', () => {
    define(proj, 'x', 'haiku');
    expect(verdict(deep)).toBeNull();
    expect(readAgentDefinition('x', deep)).toEqual({ found: true, model: 'haiku' });
  });

  it('a user-level definition is shadowed by the project one and is not a conflict', () => {
    define(proj, 'x', 'haiku');
    define(tmp, 'x', 'opus'); // ~/.claude/agents (HOME is tmp)
    expect(verdict(sub)).toBeNull();
    expect(readAgentDefinition('x', sub)).toEqual({ found: true, model: 'haiku' });
  });

  it('a user-level definition alone is still found', () => {
    define(tmp, 'x', 'haiku');
    expect(readAgentDefinition('x', sub)).toEqual({ found: true, model: 'haiku' });
  });

  it('no definition anywhere is still not found', () => {
    expect(readAgentDefinition('x', sub)).toEqual({ found: false, model: null });
  });
});
