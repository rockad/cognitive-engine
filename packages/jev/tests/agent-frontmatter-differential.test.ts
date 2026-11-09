import { describe, it, expect, beforeEach, afterEach } from 'bun:test';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { evaluateClaude } from '../src/guard.ts';
import { readAgentDefinition } from '../src/agent-definitions.ts';
import { parseFrontmatterModel } from '../src/agent-frontmatter.ts';
import { clearConfigCacheForTesting } from '../src/core/config.ts';

/**
 * Claude Code reads an agent file with /^---\s*\n([\s\S]*?)---\s*\n?/ and Bun.YAML.parse, and when
 * that throws it quotes every top-level `key: value` line whose value holds one of {}[]*&#!|>%@` or
 * ": " and parses again (2.1.312). Jev's strict reader and that pipeline can disagree about the
 * `model`; whenever they could, Jev must ask, never answer "no model".
 */

let tmp = '';
let proj = '';
let savedEnv: NodeJS.ProcessEnv = {};

beforeEach(() => {
  savedEnv = { ...process.env };
  for (const v of ['JEV_HARNESS', 'ANTIGRAVITY_CLI', 'GEMINI_CLI', 'CLAUDE_CODE', 'CLAUDECODE', 'CLAUDE_PROJECT_DIR', 'OPENCODE_SESSION', 'OPENCODE_HOME', 'CORTEX_SESSION']) delete process.env[v];
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-fm-diff-'));
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

function transcript(model: string): string {
  const file = path.join(tmp, 't.jsonl');
  fs.writeFileSync(file, `${JSON.stringify({ type: 'assistant', message: { model } })}\n`);
  return file;
}

function verdictFor(name: string, text: string) {
  const dir = path.join(proj, '.claude', 'agents');
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, `${name}.md`), text);
  return evaluateClaude({ tool_name: 'Agent', transcript_path: transcript('claude-sonnet-5-5'), cwd: proj, tool_input: { subagent_type: name } });
}

describe('the reviewer inputs: a quoted or flow value that the fallback re-reads', () => {
  const smuggled: Array<[string, string]> = [
    ['double-quoted description', '---\nname: smug\ndescription: "a#b\nmodel: opus\nz: c"\nbad: x: y\n---'],
    ['single-quoted description', "---\nname: smug\ndescription: 'a#b\nmodel: opus\nz: c'\nbad: x: y\n---"],
    ['flow-sequence description', '---\nname: smug\ndescription: [a#,\nmodel: opus\nz: c]\nbad: x: y\n---']
  ];
  for (const [name, text] of smuggled) {
    it(`is unreadable, never "no model": ${name}`, () => {
      expect(parseFrontmatterModel(text).kind).toBe('unreadable');
    });
    it(`asks through the Agent guard: ${name}`, () => {
      expect(verdictFor('smug', text)?.decision).toBe('ask');
    });
    it(`reads as a model Jev cannot rank, not as inherit: ${name}`, () => {
      const dir = path.join(proj, '.claude', 'agents');
      fs.mkdirSync(dir, { recursive: true });
      fs.writeFileSync(path.join(dir, 'smug.md'), text);
      const def = readAgentDefinition('smug', proj);
      expect(def.found).toBe(true);
      expect(def.model).not.toBeNull();
      expect(def.model).toContain('unreadable');
    });
  }
});

describe('a non-model key with a quoted or flow value over several lines is unreadable', () => {
  const multiline: Array<[string, string]> = [
    ['double-quoted', fm('description: "line one\n  line two"\nmodel: haiku')],
    ['single-quoted', fm("description: 'line one\n  line two'\nmodel: haiku")],
    ['flow sequence', fm('tools: [Read,\n  Grep]\nmodel: haiku')],
    ['flow mapping', fm('meta: {a: 1,\n  b: 2}\nmodel: haiku')],
    ['quoted, with a top-level looking line inside', fm('description: "x\nmodel: haiku"\nmodel: opus')]
  ];
  for (const [name, text] of multiline) {
    it(`asks: ${name}`, () => {
      expect(parseFrontmatterModel(text).kind).toBe('unreadable');
      expect(verdictFor('q', text)?.decision).toBe('ask');
    });
  }

  it('single-line quoted and flow values are still read exactly', () => {
    expect(parseFrontmatterModel(fm('description: "one line"\ntools: [Read, Grep]\nmodel: haiku'))).toEqual({ kind: 'model', value: 'haiku' });
  });
});

describe('Jev and the Claude Code pipeline must agree on the model', () => {
  it('the fallback quoting turns a trailing comment into part of the value: unreadable', () => {
    // `bad: x: y` breaks the first parse; the fallback quotes `model: haiku # cheap` whole.
    const text = fm('bad: x: y\nmodel: haiku # cheap');
    expect(parseFrontmatterModel(text).kind).toBe('unreadable');
    expect(verdictFor('q', text)?.decision).toBe('ask');
  });

  it('a block neither parse can read is unreadable (Claude Code would see no frontmatter at all)', () => {
    const text = fm('model: haiku\nbad: [x\nother: y');
    expect(parseFrontmatterModel(text).kind).toBe('unreadable');
  });

  it('a document-end marker block is not frontmatter to Claude Code: unreadable', () => {
    expect(parseFrontmatterModel('---\nmodel: haiku\n...\nbody\n').kind).toBe('unreadable');
  });

  it('where both agree the model is read: plain, with a throwing first parse that the fallback repairs', () => {
    expect(parseFrontmatterModel(fm('description: Use this: when needed\nmodel: haiku'))).toEqual({ kind: 'model', value: 'haiku' });
  });

  it('where both agree there is no model, none', () => {
    expect(parseFrontmatterModel(fm('name: q\ndescription: does things'))).toEqual({ kind: 'none' });
  });

  it('without Bun.YAML the strict reader is used, but a line the fallback could rewrite is unreadable', () => {
    const plain = fm('name: q\nmodel: haiku');
    expect(parseFrontmatterModel(plain, null)).toEqual({ kind: 'model', value: 'haiku' });
    const rewritable = fm('description: Use this: when needed\nmodel: haiku');
    expect(parseFrontmatterModel(rewritable, null).kind).toBe('unreadable');
  });

  it('a YAML parser that throws on both attempts makes the frontmatter unreadable', () => {
    const boom = () => {
      throw new Error('nope');
    };
    expect(parseFrontmatterModel(fm('model: haiku'), boom).kind).toBe('unreadable');
  });

  it('a model Claude Code would read as a non-string is unreadable', () => {
    expect(parseFrontmatterModel(fm('model: 5'), (t) => ({ model: 5 })).kind).toBe('unreadable');
  });
});
