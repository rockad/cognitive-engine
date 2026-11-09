import { describe, it, expect, beforeEach, afterEach } from 'bun:test';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { evaluateClaude } from '../src/guard.ts';
import { readAgentDefinition } from '../src/agent-definitions.ts';
import { clearConfigCacheForTesting } from '../src/core/config.ts';

/**
 * The `model:` of an agent definition is read from its frontmatter. Whatever the file looks like,
 * a frontmatter block whose model cannot be established (or is not a plain string) must ASK, never
 * read as "no model, so inherit". Session model in these tests: sonnet.
 */

let tmp = '';
let proj = '';
let savedEnv: NodeJS.ProcessEnv = {};

beforeEach(() => {
  savedEnv = { ...process.env };
  for (const v of ['JEV_HARNESS', 'ANTIGRAVITY_CLI', 'GEMINI_CLI', 'CLAUDE_CODE', 'CLAUDECODE', 'CLAUDE_PROJECT_DIR', 'OPENCODE_SESSION', 'OPENCODE_HOME', 'CORTEX_SESSION', 'CLAUDE_CODE_SUBAGENT_MODEL', 'CLAUDE_CODE_SUBAGENT_MODEL_FORCE', 'CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL']) delete process.env[v];
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-frontmatter-'));
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

function transcript(model: string): string {
  const file = path.join(tmp, 't.jsonl');
  fs.writeFileSync(file, `${JSON.stringify({ type: 'assistant', message: { model } })}\n`);
  return file;
}

function writeAgent(dir: string, name: string, text: string): void {
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, `${name}.md`), text);
}

const projectAgents = () => path.join(proj, '.claude', 'agents');
const userAgents = () => path.join(tmp, '.claude', 'agents');

function verdictFor(text: string) {
  writeAgent(projectAgents(), 'q', text);
  return evaluateClaude({
    tool_name: 'Agent',
    transcript_path: transcript('claude-sonnet-5-5'),
    cwd: proj,
    tool_input: { subagent_type: 'q' }
  });
}

const fm = (body: string, eol = '\n') => ['---', ...body.split('\n'), '---', 'body', ''].join(eol);

describe('frontmatter shapes that name a model above the session model ask', () => {
  const asks: Array<[string, string]> = [
    ['plain key', fm('model: opus')],
    ['double-quoted key', fm('"model": opus')],
    ['single-quoted key', fm("'model': opus")],
    ['space before the colon', fm('model : opus')],
    ['quoted value', fm('model: "opus"')],
    ['single-quoted value', fm("model: 'opus'")],
    ['trailing comment', fm('model: opus # the big one')],
    ['UTF-8 BOM before the block', `﻿${fm('model: opus')}`],
    ['BOM and quoted key', `﻿${fm('"model": opus')}`],
    ['CRLF line endings', fm('name: q\nmodel: opus', '\r\n')],
    ['opening delimiter with trailing spaces', `---  \nmodel: opus\n---\nbody\n`],
    ['model after a multi-line block scalar that mentions another model', fm('description: |\n  model: haiku\n  more\nmodel: opus')],
    ['model after a description containing a colon', fm('description: Use this: when needed\nmodel: opus')]
  ];
  for (const [name, text] of asks) {
    it(`asks: ${name}`, () => {
      const v = verdictFor(text);
      expect(v?.decision).toBe('ask');
      expect(v?.reason).toContain('opus');
    });
  }
});

describe('frontmatter whose model cannot be established asks', () => {
  const asks: Array<[string, string]> = [
    ['flow-style mapping', fm('{model: opus}')],
    ['flow-style mapping with quoted keys', fm('{"model": "opus"}')],
    ['sequence value', fm('model:\n  - opus')],
    ['flow sequence value', fm('model: [opus]')],
    ['mapping value', fm('model:\n  name: opus')],
    ['flow mapping value', fm('model: {name: opus}')],
    ['block scalar value', fm('model: |\n  opus')],
    ['folded scalar value', fm('model: >\n  opus')],
    ['anchor on the value', fm('model: &m opus')],
    ['alias as the value', fm('x: &m opus\nmodel: *m')],
    ['tag on the value', fm('model: !!str opus')],
    ['merge key', fm('base: &b {model: opus}\n<<: *b')],
    ['duplicate model key', fm('model: haiku\nmodel: opus')],
    ['duplicate via a quoted key', fm('model: haiku\n"model": opus')],
    ['upper-case key', fm('Model: opus')],
    ['all-caps key', fm('MODEL: opus')],
    ['full-width lookalike key', fm('ｍｏｄｅｌ: opus')],
    ['key with an escape', fm('"m\\u006fdel": opus')],
    ['no space after the colon', fm('model:opus')],
    ['unterminated quoted value', fm('model: "opus')],
    ['value with an escape', fm('model: "op\\u0075s"')],
    ['sequence item at the top level', fm('- model: opus')],
    ['explicit key', fm('? model\n: opus')],
    ['unterminated frontmatter block', `---\nmodel: opus\nbody never closes\n`],
    ['blank line before the opening delimiter', `\n${fm('model: opus')}`],
    ['space before the opening delimiter', ` ${fm('model: opus')}`],
    ['Unicode line separator in the block', fm('name: q\u2028model: opus')],
    ['a model Jev cannot rank', fm('model: nonexistent-model')],
    // Claude Code's block regex needs `\n`, so with CR-only line endings it reads no frontmatter at all
    ['CR-only line endings', fm('name: q\nmodel: opus', '\r')],
    // a multi-line quoted value is read differently by Claude Code's fallback parser (see agent-frontmatter-differential.test.ts)
    ['model after a multi-line quoted description', fm('description: "line one\nmodel: haiku"\nmodel: opus')],
    // Claude Code only sees a block closed by `---`, so it reads no frontmatter at all here
    ['a document end marker closing the block', `---\nmodel: haiku\n...\nbody\n`]
  ];
  for (const [name, text] of asks) {
    it(`asks: ${name}`, () => {
      expect(verdictFor(text)?.decision).toBe('ask');
    });
  }

  it('the ask names the agent file problem through an unrankable model value, never as inherit', () => {
    writeAgent(projectAgents(), 'q', fm('{model: opus}'));
    const def = readAgentDefinition('q', proj);
    expect(def.found).toBe(true);
    expect(def.model).not.toBeNull();
    expect(def.model).not.toBe('inherit');
  });
});

describe('frontmatter that is read exactly allows what it should', () => {
  const allows: Array<[string, string]> = [
    ['a cheaper model', fm('model: haiku')],
    ['a cheaper model under a quoted key', fm('"model": haiku')],
    ['a cheaper model with a BOM', `﻿${fm('model: haiku')}`],
    ['a cheaper model with CRLF', fm('model: haiku', '\r\n')],
    ['a cheaper model, space before the colon', fm('model : haiku')],
    ['the session model', fm('model: sonnet')],
    ['inherit', fm('model: inherit')],
    ['an empty model value', fm('name: q\nmodel:')],
    ['no model key', fm('name: q\ndescription: does things')],
    ['no frontmatter at all', 'Just a body, with model: opus in it.\n'],
    ['a BOM and no frontmatter', '﻿Just a body.\n'],
    ['an empty file', ''],
    ['a nested model key', fm('tools:\n  model: opus\nname: q')],
    ['a model mentioned in a block scalar only', fm('description: |\n  model: opus\nname: q')],
    ['a model mentioned in a comment only', fm('# model: opus\nname: q')],
    ['a model mentioned in the body only', `---\nname: q\n---\nmodel: opus\n`]
  ];
  for (const [name, text] of allows) {
    it(`allows: ${name}`, () => {
      expect(verdictFor(text)).toBeNull();
    });
  }

  it('reads the value, not a lookalike from a block scalar', () => {
    writeAgent(projectAgents(), 'q', fm('description: |\n  model: haiku\nmodel: opus'));
    expect(readAgentDefinition('q', proj)).toEqual({ found: true, model: 'opus' });
  });

  it('reads quoted and BOM forms to the same model', () => {
    for (const text of [fm('model: opus'), fm('"model": "opus"'), `﻿${fm("'model' : 'opus'")}`, fm('model: opus', '\r\n')]) {
      writeAgent(projectAgents(), 'q', text);
      expect(readAgentDefinition('q', proj)).toEqual({ found: true, model: 'opus' });
    }
  });
});

describe('the first definition found decides, as in Claude Code', () => {
  it('a nearer definition with no model does not fall through to a farther one that has a model', () => {
    writeAgent(projectAgents(), 'q', fm('name: q'));
    writeAgent(userAgents(), 'q', fm('model: haiku'));
    expect(readAgentDefinition('q', proj)).toEqual({ found: true, model: null });
  });

  it('a nearer unreadable model is not replaced by a cheap model from a farther file', () => {
    writeAgent(projectAgents(), 'q', fm('{model: opus}'));
    writeAgent(userAgents(), 'q', fm('model: haiku'));
    expect(verdictFor(fm('{model: opus}'))?.decision).toBe('ask');
  });

  it('a nearer opus is not replaced by a cheaper definition farther away', () => {
    writeAgent(userAgents(), 'q', fm('model: haiku'));
    expect(verdictFor(fm('model: opus'))?.decision).toBe('ask');
  });

  it('a directory named like the definition is not a definition', () => {
    fs.mkdirSync(path.join(projectAgents(), 'q.md'), { recursive: true });
    writeAgent(userAgents(), 'q', fm('model: haiku'));
    expect(readAgentDefinition('q', proj)).toEqual({ found: true, model: 'haiku' });
  });
});
