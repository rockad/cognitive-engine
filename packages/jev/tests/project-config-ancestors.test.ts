import { describe, it, expect, beforeEach, afterEach } from 'bun:test';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { run } from '../src/hook.ts';
import { inspectProjectConfig } from '../src/project-config.ts';
import { claudeDecisionsFile } from '../src/claude-audit.ts';
import { clearConfigCacheForTesting } from '../src/core/config.ts';

/**
 * Every project config from the hook cwd up to the filesystem root counts, not just the nearest one
 * (and, in a linked worktree, the main checkout's too): the STRICTEST valid ceiling and the highest
 * mode win, and a malformed file anywhere on the way asks. A nested file can therefore never cancel
 * an ancestor's ceiling, and the project root or git root is not where the search stops.
 */
let tmp = '';
let proj = '';
let sub = '';
let savedEnv: NodeJS.ProcessEnv = {};

beforeEach(() => {
  savedEnv = { ...process.env };
  for (const v of ['JEV_HARNESS', 'ANTIGRAVITY_CLI', 'GEMINI_CLI', 'CLAUDE_CODE', 'CLAUDECODE', 'CLAUDE_PROJECT_DIR', 'OPENCODE_SESSION', 'OPENCODE_HOME', 'CORTEX_SESSION', 'CLAUDE_CODE_SUBAGENT_MODEL', 'CLAUDE_CODE_SUBAGENT_MODEL_FORCE', 'CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL']) delete process.env[v];
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-anc-'));
  proj = path.join(tmp, 'proj');
  sub = path.join(proj, 'sub');
  fs.mkdirSync(path.join(sub, 'deep'), { recursive: true });
  process.env.HOME = tmp;
  process.env.JEV_CONFIG_FILE = path.join(tmp, 'config.json');
  process.env.JEV_CLAUDE_STATE_DIR = path.join(tmp, 'state');
  process.env.JEV_CLAUDE_DECISIONS_FILE = path.join(tmp, 'decisions.jsonl');
  fs.writeFileSync(process.env.JEV_CONFIG_FILE, JSON.stringify({ defaults: { mode: 'on-demand' } }));
  clearConfigCacheForTesting();
});

afterEach(() => {
  process.env = savedEnv;
  clearConfigCacheForTesting();
  fs.rmSync(tmp, { recursive: true, force: true });
});

function put(dir: string, body: unknown, rel = '.jev.json'): string {
  const file = path.join(dir, rel);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, typeof body === 'string' ? body : JSON.stringify(body));
  return file;
}
function transcript(model: string): string {
  const file = path.join(tmp, 't.jsonl');
  fs.writeFileSync(file, `${JSON.stringify({ type: 'assistant', message: { model } })}\n`);
  return file;
}
const agent = (model: string, session: string, cwd: string) =>
  JSON.stringify({ tool_name: 'Agent', session_id: 's1', transcript_path: transcript(session), cwd, tool_input: { model, prompt: 'p' } });
const decide = async (stdin: string) => {
  const out = await run('claude', stdin);
  return out ? (JSON.parse(out).hookSpecificOutput.permissionDecision as string) : null;
};
const reason = async (stdin: string) => JSON.parse((await run('claude', stdin))!).hookSpecificOutput.permissionDecisionReason as string;
const asks = () => (fs.existsSync(claudeDecisionsFile()) ? fs.readFileSync(claudeDecisionsFile(), 'utf8').trim().split('\n').map((l) => JSON.parse(l)) : []).filter((l) => l.event === 'ask');

describe('the strictest ceiling across ancestors wins', () => {
  const cases: Array<[string, Array<[string, unknown, string?]>, string | null]> = [
    ['a nested file with no ceiling does not cancel the root ceiling', [['proj', { guard: { ceiling: 'haiku' } }], ['sub', {}]], 'haiku'],
    ['a nested empty-guard file does not cancel it', [['proj', { guard: { ceiling: 'haiku' } }], ['sub', { guard: {} }]], 'haiku'],
    ['a nested file that only sets a mode does not cancel it', [['proj', { guard: { ceiling: 'haiku' } }], ['sub', { defaults: { mode: 'off' } }]], 'haiku'],
    ['a looser nested ceiling does not loosen the root', [['proj', { guard: { ceiling: 'haiku' } }], ['sub', { guard: { ceiling: 'opus' } }]], 'haiku'],
    ['a stricter nested ceiling tightens', [['proj', { guard: { ceiling: 'sonnet' } }], ['sub', { guard: { ceiling: 'haiku' } }]], 'haiku'],
    ['a ceiling only on the nested file counts', [['proj', {}], ['sub', { guard: { ceiling: 'sonnet' } }]], 'sonnet'],
    ['both file names in one directory: the lower wins', [['proj', { guard: { ceiling: 'sonnet' } }, '.agents/jev.json'], ['proj', { guard: { ceiling: 'haiku' } }, '.jev.json']], 'haiku'],
    ['a deeper ancestor than the nearest file counts', [['proj', { guard: { ceiling: 'haiku' } }], ['sub', {}], ['deep', {}]], 'haiku']
  ];
  for (const [name, files, expected] of cases) {
    it(name, async () => {
      for (const [where, body, rel] of files) put(where === 'proj' ? proj : where === 'sub' ? sub : path.join(sub, 'deep'), body, rel);
      const cwd = path.join(sub, 'deep');
      expect(inspectProjectConfig(cwd)).toMatchObject({ state: 'valid', ceiling: expected });
      // sonnet is above a haiku ceiling in an opus session and equal to a sonnet ceiling
      const verdict = await decide(agent('sonnet', 'claude-opus-5-5', cwd));
      expect(verdict).toBe(expected === 'haiku' ? 'ask' : null);
    });
  }

  it('the reason names the file whose ceiling applies', async () => {
    const rootFile = put(proj, { guard: { ceiling: 'haiku' } });
    put(sub, {});
    const r = await reason(agent('sonnet', 'claude-opus-5-5', sub));
    expect(r).toContain(rootFile);
    expect(asks()[0].projectCeiling).toBe('haiku');
  });

  it('the highest mode across the files is the effective one in the audit', async () => {
    put(proj, { defaults: { mode: 'on' } });
    put(sub, { defaults: { mode: 'on-demand' } });
    await run('claude', agent('opus', 'claude-sonnet-5-5', sub));
    expect(asks()[0].mode).toBe('on');
  });
});

describe('a malformed ancestor asks on every guarded call', () => {
  for (const body of ['{ not json', '[]', { guard: { ceiling: 'gpt-9' } }, { defaults: { mode: 'banana' } }]) {
    it(`root file ${JSON.stringify(body)}, valid nested file`, async () => {
      const bad = put(proj, body);
      put(sub, { guard: { ceiling: 'opus' } });
      const cwd = path.join(sub, 'deep');
      expect(inspectProjectConfig(cwd)).toMatchObject({ state: 'malformed', file: bad });
      expect(await decide(agent('haiku', 'claude-sonnet-5-5', cwd))).toBe('ask');
    });
  }

  it('a malformed nearest file still asks', async () => {
    put(proj, { guard: { ceiling: 'haiku' } });
    const bad = put(sub, '{ nope');
    expect(inspectProjectConfig(sub)).toMatchObject({ state: 'malformed', file: bad });
  });

  it('a dangling symlink in an ancestor asks', async () => {
    fs.symlinkSync(path.join(tmp, 'missing.json'), path.join(proj, '.jev.json'));
    put(sub, {});
    expect(inspectProjectConfig(sub)).toMatchObject({ state: 'malformed' });
  });
});

describe('the search does not stop at the project root: it runs up to the filesystem root', () => {
  it('a ceiling in a parent workspace above CLAUDE_PROJECT_DIR counts', () => {
    put(tmp, { guard: { ceiling: 'haiku' } });
    put(proj, {});
    process.env.CLAUDE_PROJECT_DIR = proj;
    expect(inspectProjectConfig(sub)).toMatchObject({ state: 'valid', ceiling: 'haiku' });
  });

  it('a malformed file above CLAUDE_PROJECT_DIR still asks', () => {
    const bad = put(tmp, '{ not json');
    process.env.CLAUDE_PROJECT_DIR = proj;
    expect(inspectProjectConfig(sub)).toMatchObject({ state: 'malformed', file: bad });
  });

  it('a ceiling above the git root counts when CLAUDE_PROJECT_DIR is unset', () => {
    fs.mkdirSync(path.join(proj, '.git'));
    put(tmp, { guard: { ceiling: 'haiku' } });
    put(proj, { guard: { ceiling: 'sonnet' } });
    expect(inspectProjectConfig(path.join(sub, 'deep'))).toMatchObject({ state: 'valid', ceiling: 'haiku' });
  });

  it('the strictest wins across the root and a parent workspace, and a looser parent loosens nothing', () => {
    fs.mkdirSync(path.join(proj, '.git'));
    put(tmp, { guard: { ceiling: 'opus' } });
    put(proj, { guard: { ceiling: 'sonnet' } });
    expect(inspectProjectConfig(sub)).toMatchObject({ state: 'valid', ceiling: 'sonnet' });
  });

  it('a malformed file above the git root asks', async () => {
    fs.mkdirSync(path.join(proj, '.git'));
    put(tmp, '{ not json');
    expect(await decide(agent('haiku', 'claude-sonnet-5-5', sub))).toBe('ask');
  });

  it('a planted sub/.git does not drop the ancestors ceiling', () => {
    put(proj, { guard: { ceiling: 'haiku' } });
    fs.mkdirSync(path.join(sub, '.git'));
    expect(inspectProjectConfig(path.join(sub, 'deep'))).toMatchObject({ state: 'valid', ceiling: 'haiku' });
  });

  it('a file at the project root itself is read', () => {
    process.env.CLAUDE_PROJECT_DIR = proj;
    put(proj, { guard: { ceiling: 'sonnet' } });
    expect(inspectProjectConfig(path.join(sub, 'deep'))).toMatchObject({ ceiling: 'sonnet' });
  });

  it('CLAUDE_PROJECT_DIR elsewhere than the cwd: its ancestors count too', () => {
    const other = path.join(tmp, 'elsewhere', 'proj2');
    fs.mkdirSync(other, { recursive: true });
    put(path.join(tmp, 'elsewhere'), { guard: { ceiling: 'haiku' } });
    process.env.CLAUDE_PROJECT_DIR = other;
    expect(inspectProjectConfig(sub)).toMatchObject({ state: 'valid', ceiling: 'haiku' });
  });
});

describe('a linked worktree also treats the main checkout as an ancestor', () => {
  let main = '';
  let wt = '';

  /** A main checkout with a real .git directory and a linked worktree whose `.git` is a file pointing into it. */
  function linkedWorktree(wtDir: string, opts: { commondir?: boolean } = {}) {
    main = path.join(tmp, 'main');
    fs.mkdirSync(path.join(main, '.git', 'worktrees', 'wt'), { recursive: true });
    if (opts.commondir !== false) fs.writeFileSync(path.join(main, '.git', 'worktrees', 'wt', 'commondir'), '../..\n');
    fs.mkdirSync(wtDir, { recursive: true });
    fs.writeFileSync(path.join(wtDir, '.git'), `gitdir: ${path.join(main, '.git', 'worktrees', 'wt')}\n`);
    wt = wtDir;
  }

  it('a worktree inside the main checkout (.claude/worktrees/X): the main checkout ceiling counts', () => {
    linkedWorktree(path.join(tmp, 'main', '.claude', 'worktrees', 'wt'));
    put(main, { guard: { ceiling: 'haiku' } });
    expect(inspectProjectConfig(wt)).toMatchObject({ state: 'valid', ceiling: 'haiku' });
  });

  it('a worktree next to the main checkout: the main checkout ceiling counts', () => {
    linkedWorktree(path.join(tmp, 'sibling-wt'));
    put(main, { guard: { ceiling: 'haiku' } });
    expect(inspectProjectConfig(path.join(wt, 'deep-not-yet'))).toMatchObject({ state: 'valid', ceiling: 'haiku' });
  });

  it('with CLAUDE_PROJECT_DIR set to the worktree the main checkout still counts', () => {
    linkedWorktree(path.join(tmp, 'sibling-wt'));
    put(main, { guard: { ceiling: 'haiku' } });
    process.env.CLAUDE_PROJECT_DIR = wt;
    expect(inspectProjectConfig(wt)).toMatchObject({ state: 'valid', ceiling: 'haiku' });
  });

  it('the strictest of the worktree and the main checkout wins', () => {
    linkedWorktree(path.join(tmp, 'sibling-wt'));
    put(main, { guard: { ceiling: 'opus' } });
    put(wt, { guard: { ceiling: 'sonnet' } });
    expect(inspectProjectConfig(wt)).toMatchObject({ state: 'valid', ceiling: 'sonnet' });
    put(main, { guard: { ceiling: 'haiku' } });
    expect(inspectProjectConfig(wt)).toMatchObject({ state: 'valid', ceiling: 'haiku' });
  });

  it('a malformed file in the main checkout asks', () => {
    linkedWorktree(path.join(tmp, 'sibling-wt'));
    const bad = put(main, '{ nope');
    expect(inspectProjectConfig(wt)).toMatchObject({ state: 'malformed', file: bad });
  });

  it('a main checkout found without a commondir file (gitdir/../..)', () => {
    linkedWorktree(path.join(tmp, 'sibling-wt'), { commondir: false });
    put(main, { guard: { ceiling: 'haiku' } });
    expect(inspectProjectConfig(wt)).toMatchObject({ ceiling: 'haiku' });
  });

  it('a relative gitdir pointer is resolved against the worktree', () => {
    main = path.join(tmp, 'main');
    wt = path.join(tmp, 'rel-wt');
    fs.mkdirSync(path.join(main, '.git', 'worktrees', 'wt'), { recursive: true });
    fs.mkdirSync(wt, { recursive: true });
    fs.writeFileSync(path.join(wt, '.git'), 'gitdir: ../main/.git/worktrees/wt\n');
    put(main, { guard: { ceiling: 'haiku' } });
    expect(inspectProjectConfig(wt)).toMatchObject({ ceiling: 'haiku' });
  });

  it('a garbage .git file is not an error and adds nothing', () => {
    wt = path.join(tmp, 'garbage-wt');
    fs.mkdirSync(wt, { recursive: true });
    fs.writeFileSync(path.join(wt, '.git'), 'not a pointer');
    expect(inspectProjectConfig(wt)).toEqual({ state: 'none' });
  });

  it('a pointer to a missing directory is not an error and adds nothing', () => {
    wt = path.join(tmp, 'gone-wt');
    fs.mkdirSync(wt, { recursive: true });
    fs.writeFileSync(path.join(wt, '.git'), `gitdir: ${path.join(tmp, 'nowhere', '.git', 'worktrees', 'x')}\n`);
    expect(inspectProjectConfig(wt)).toEqual({ state: 'none' });
  });
});
