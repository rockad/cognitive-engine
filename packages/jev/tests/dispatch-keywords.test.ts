import { describe, it, expect, beforeEach, afterEach } from 'bun:test';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { defaultEffortForSubagent, detectScaffoldingTask, evaluateLocalHeuristic, isFormattingTask, isFreeTierMechanicalTask } from '../src/core/index.ts';
import { classifyBatchTier } from '../src/tools/dispatch.ts';

const RUNNER = path.join(import.meta.dir, 'fixtures', 'run-dispatch.ts');
const MARKERS = ['JEV_HARNESS', 'ANTIGRAVITY_CLI', 'GEMINI_CLI', 'CORTEX_SESSION', 'CLAUDE_CODE', 'CLAUDECODE', 'CLAUDE_PROJECT_DIR', 'CLAUDE_CODE_SESSION_ID', 'OPENCODE_SESSION', 'OPENCODE_HOME'];

/**
 * Whole-word tier rule (case study 4.11, ported from agentic-hub Jev 2.3.1). Documented choice: the
 * keyword means formatting WORK, so the bare word "format" does not count ("dump format decision"
 * is a noun). Only scaffold*, boilerplate, formatting, reformat*, auto-format, and "format <object>"
 * (the, this, all, code, files, json ...) do.
 */
describe('free-tier keyword rule (whole words, documented phrase list)', () => {
  it.each([
    ['Gather information about the storage layer'],
    ['Plan the data transformation for the import'],
    ['Edit a spec to apply a dump format decision'],
    ['Pick the output format for the report'],
    ['Check the platform conformance notes']
  ])('keeps %j on flash', (task) => {
    expect(classifyBatchTier(task)).toBe('flash');
  });

  it.each([
    ['Scaffold a new package for the bridge'],
    ['Write boilerplate for the CLI entry point'],
    ['Format the changelog'],
    ['Reformatting the tables'],
    ['Fix formatting in the README'],
    ['Please format this table'],
    ['Auto-format the sources'],
    ['format JSON'],
    ['Scaffolding the adapter']
  ])('sends %j to the free tier', (task) => {
    expect(classifyBatchTier(task)).toBe('openrouter_free');
  });

  it('still routes explicit deep reasoning to pro', () => {
    expect(classifyBatchTier('This needs deep reasoning about consistency')).toBe('pro');
  });

  it('does not treat substrings of other words as scaffold or boilerplate', () => {
    expect([isFreeTierMechanicalTask('The scaffoldless design'), isFreeTierMechanicalTask('boilerplates are fine')]).toEqual([false, false]);
  });

  it('shares the formatting forms with the effort heuristic, not the scaffold words', () => {
    expect([isFormattingTask('Format the changelog'), isFormattingTask('Scaffold a package')]).toEqual([true, false]);
  });
});

describe('every substring site uses the whole-word rule', () => {
  it('effort heuristic: "information" and a noun "format" stay medium', () => {
    const efforts = [
      defaultEffortForSubagent('information-desk', 'Review the user information endpoint'),
      defaultEffortForSubagent('code_engineer', 'Apply a dump format decision to the spec'),
      defaultEffortForSubagent('code_engineer', 'Format the changelog'),
      defaultEffortForSubagent('doc-formatter', 'Tidy it')
    ];

    expect(efforts).toEqual(['medium', 'medium', 'low', 'low']);
  });

  it('local pre-filter: a short "dump format" prompt is not free-tier, "format JSON" is', () => {
    const noun = evaluateLocalHeuristic('decide the dump format');
    const work = evaluateLocalHeuristic('format JSON indent 2');

    expect([noun, work?.tier]).toEqual([null, 'openrouter_free']);
  });

  it('scaffolding detector: whole word for scaffold and boilerplate, file names still match', () => {
    const results = [
      detectScaffoldingTask('Scaffolding the adapter').scaffoldFirst,
      detectScaffoldingTask('Write the boilerplate').scaffoldFirst,
      detectScaffoldingTask('Update package.json').scaffoldFirst,
      detectScaffoldingTask('Fix the scaffoldless design').scaffoldFirst
    ];

    expect(results).toEqual([true, true, true, false]);
  });
});

let tmp = '';
beforeEach(() => {
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-keywords-'));
});
afterEach(() => fs.rmSync(tmp, { recursive: true, force: true }));

/** Batch dispatch end to end in a child process with its own HOME and keyword routing (no network). */
function runBatch(harness: string, tasks: unknown[], availableSubagents: string[] = []): any {
  fs.writeFileSync(path.join(tmp, 'config.json'), JSON.stringify({ routing: { strategy: 'keyword' } }));
  const clean: Record<string, string> = {};
  for (const [k, v] of Object.entries(process.env)) if (v !== undefined && !MARKERS.includes(k) && !/KEY|TOKEN|SECRET/.test(k)) clean[k] = v;
  const proc = Bun.spawnSync(['bun', RUNNER], {
    stdin: new TextEncoder().encode(JSON.stringify({ tool: 'batch', args: { tasks, availableSubagents }, offline: true })),
    env: {
      ...clean,
      HOME: tmp,
      JEV_HARNESS: harness,
      JEV_CONFIG_FILE: path.join(tmp, 'config.json'),
      JEV_CACHE_FILE: path.join(tmp, 'cache.sqlite'),
      JEV_CLAUDE_STATE_DIR: path.join(tmp, 'state'),
      JEV_OPENROUTER_API_KEY: 'test-key'
    }
  });
  if (proc.exitCode !== 0) throw new Error(`runner failed: ${proc.stderr.toString()}`);
  return JSON.parse(proc.stdout.toString());
}

describe('jev_batch_dispatch tier rule end to end', () => {
  it.each(['claude-code', 'antigravity', 'opencode', 'cli'])('%s: the dump-format sentence is flash and the scaffold sentence is free', (harness) => {
    const plan = runBatch(harness, [
      { id: 'a', task: 'Edit a spec to apply a dump format decision' },
      { id: 'b', task: 'Scaffold a new package' },
      { id: 'c', task: 'Collect information about the transformation' }
    ]);

    expect(plan.tasks.map((t: any) => t.tier)).toEqual(['flash', 'openrouter_free', 'flash']);
  });
});
