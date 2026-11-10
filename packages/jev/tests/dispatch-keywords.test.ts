import { describe, it, expect, beforeEach, afterEach } from 'bun:test';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import {
  defaultEffortForSubagent,
  detectScaffoldingTask,
  editCapableSubagent,
  evaluateLocalHeuristic,
  isFormattingTask,
  isFreeTierMechanicalTask,
  isReadOnlySubagent,
  taskNeedsEdit
} from '../src/core/index.ts';
import { parseDispatch } from '../src/advisor-logic.ts';
import { classifyBatchTier, resolveEditCapableSubagent } from '../src/tools/dispatch.ts';

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

/** A dispatch handler end to end in a child process with its own HOME and a stubbed Decisions API (no network). */
function runHandler(harness: string, input: { tool: 'batch' | 'single'; args: unknown; answers?: Record<string, string>; offline?: boolean }): any {
  fs.writeFileSync(path.join(tmp, 'config.json'), JSON.stringify({ routing: { strategy: 'keyword' } }));
  const clean: Record<string, string> = {};
  for (const [k, v] of Object.entries(process.env)) if (v !== undefined && !MARKERS.includes(k) && !/KEY|TOKEN|SECRET/.test(k)) clean[k] = v;
  const proc = Bun.spawnSync(['bun', RUNNER], {
    stdin: new TextEncoder().encode(JSON.stringify(input)),
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

const runBatch = (harness: string, tasks: unknown[], availableSubagents: string[] = []): any =>
  runHandler(harness, { tool: 'batch', args: { tasks, availableSubagents }, offline: true });
const runSingle = (harness: string, args: { task: string }, answers: Record<string, string>): any => runHandler(harness, { tool: 'single', args, answers });

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

describe('read-only personas never own an editing task', () => {
  it('knows Plan and Explore are read-only, whatever the case', () => {
    expect([isReadOnlySubagent('Plan'), isReadOnlySubagent('explore'), isReadOnlySubagent('general-purpose'), isReadOnlySubagent('code-engineer')]).toEqual([true, true, false, false]);
  });

  it.each([
    ['Edit the tasklist'],
    ['Implement the parser'],
    ['Fix the quota bug'],
    ['Refactor the module'],
    ['Write the changelog entry'],
    ['Add a test for it'],
    ['Update the README'],
    ['Rename the field'],
    ['Migrate the table'],
    ['Delete the stale file'],
    ['Apply the patch'],
    ['Rewrite the intro']
  ])('%j is an editing task', (task) => {
    expect(taskNeedsEdit(task)).toBe(true);
  });

  it.each([
    ['Review the design'],
    ['Check the quota file'],
    ['Find where the quota file is read'],
    ['Explain how the guard works'],
    ['Summarise the plan'],
    ['Look at the written notes'],
    ['Create a picture of the flow'],
    ['Commit to a direction'],
    ['Outline the approach, no edits'],
    ['Plan it without writing anything'],
    ['Do not update the files, just read them']
  ])('%j is not an editing task (the verb list stays narrow)', (task) => {
    expect(taskNeedsEdit(task)).toBe(false);
  });

  it('swaps Plan for the fallback on an editing task and keeps it on a planning task', () => {
    expect([editCapableSubagent('Plan', 'Edit the plan document', 'general-purpose'), editCapableSubagent('Plan', 'Design the storage approach', 'general-purpose')]).toEqual(['general-purpose', 'Plan']);
  });

  it('leaves an editing-capable persona alone', () => {
    expect(editCapableSubagent('code-engineer', 'Edit the plan document', 'general-purpose')).toBe('code-engineer');
  });

  it('resolveEditCapableSubagent swaps on Claude Code only', () => {
    const out = [
      resolveEditCapableSubagent('claude-code', 'Explore', 'Rewrite the notes'),
      resolveEditCapableSubagent('antigravity', 'Explore', 'Rewrite the notes')
    ];

    expect(out).toEqual(['general-purpose', 'Explore']);
  });

  it('parseDispatch never returns Plan or Explore for an editing task', () => {
    const answer = (sa: string) => ({ answers: { recommendedSubagent: { choice: sa, confidence: 0.9 }, recommendedTier: { choice: 'flash', confidence: 0.9 } } });

    const out = [
      parseDispatch(answer('Plan'), 'Edit the tasklist and update the plan', 'unknown')?.subagent,
      parseDispatch(answer('Explore'), 'Find where the quota file is read', 'unknown')?.subagent
    ];

    expect(out).toEqual(['general-purpose', 'Explore']);
  });

  it('batch dispatch on Claude Code gives Plan no editing task, even without needsEdit', () => {
    const plan = runBatch('claude-code', [{ id: 'a', task: 'Plan: edit the tasklist and update the plan' }], ['Plan']);

    // No editable candidate matches the wording, so it falls to self (handled directly), never Plan.
    expect(['general-purpose', 'self']).toContain(plan.tasks[0].subagent);
  });

  it('batch dispatch honours needsEdit: false over the wording', () => {
    const plan = runBatch('claude-code', [{ id: 'a', task: 'Plan how to update the tasklist', needsEdit: false }], ['Plan']);

    expect(plan.tasks[0].subagent).toBe('Plan');
  });

  it('single dispatch on Claude Code swaps Plan for general-purpose on an editing task', () => {
    const res = runSingle('claude-code', { task: 'Edit the plan document' }, { recommendedSubagent: 'Plan', recommendedTier: 'flash' });

    expect(res.recommendedSubagent).toBe('general-purpose');
    expect(res.executionGuidance).toContain('Agent(subagent_type: "general-purpose", model: "sonnet")');
  });

  it('single dispatch outside Claude Code is unchanged', () => {
    const res = runSingle('antigravity', { task: 'Edit the plan document' }, { recommendedSubagent: 'Plan', recommendedTier: 'flash' });

    expect(res.recommendedSubagent).toBe('Plan');
  });
});
