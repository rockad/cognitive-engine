import { describe, it, expect } from 'bun:test';
import * as fs from 'node:fs';
import * as path from 'node:path';

const root = path.resolve(import.meta.dir, '..');
const { hooks } = JSON.parse(fs.readFileSync(path.join(root, 'hooks', 'hooks.json'), 'utf8'));

const handlers = (event: string): any[] => hooks[event].flatMap((entry: any) => entry.hooks);
// The PreToolUse entries that guard (hook.ts), as opposed to the advisory one (advisor-hook.ts).
const guardEntries = (): any[] => hooks.PreToolUse.filter((e: any) => e.hooks.every((h: any) => h.command.includes('src/hook.ts" claude')));
const guardHandlers = (): any[] => guardEntries().flatMap((e: any) => e.hooks);

describe('hooks.json failure policy', () => {
  it('guards the Agent|Task and Workflow tools with the same hook', () => {
    expect(guardEntries().map((e: any) => e.matcher)).toEqual(['Agent|Task', 'Workflow']);
    for (const h of guardHandlers()) expect(h.command).toContain('packages/jev/src/hook.ts" claude');
  });

  it('every guard entry blocks on hook failure', () => {
    expect(guardHandlers()).toHaveLength(2);
    for (const h of guardHandlers()) expect(h.onFailure).toBe('block');
  });

  it('the pre-flight reminder sits beside the guard, fail-open, on the same tools', () => {
    const advisory = hooks.PreToolUse.filter((e: any) => !guardEntries().includes(e));
    expect(advisory.map((e: any) => e.matcher)).toEqual(['Agent|Task|Workflow']);
    expect(advisory[0].hooks[0].command).toContain('advisor-hook.ts" pre-tool-use');
    expect(advisory[0].hooks[0].onFailure).toBeUndefined();
  });

  it('recorder hooks stay fail-open', () => {
    for (const event of ['SessionStart', 'PostModelSwitch', 'PostToolUse']) {
      for (const h of handlers(event)) {
        expect(h.command).toContain('record-hook.ts"');
        expect(h.onFailure).toBeUndefined();
      }
    }
  });

  it('the prompt advisor is advice only and stays fail-open', () => {
    const advisors = handlers('UserPromptSubmit');
    expect(advisors).toHaveLength(1);
    expect(advisors[0].command).toContain('advisor-hook.ts" user-prompt-submit');
    expect(advisors[0].onFailure).toBeUndefined();
  });

  it('only the guard entries carry onFailure at all', () => {
    for (const event of Object.keys(hooks)) {
      for (const h of handlers(event)) expect('onFailure' in h).toBe(h.command.includes('src/hook.ts" claude'));
    }
  });

  it('quotes every path and gives every hook a timeout', () => {
    for (const event of Object.keys(hooks)) {
      for (const h of handlers(event)) {
        expect(h.type).toBe('command');
        expect(h.command).toMatch(/^bun "\$COGNITIVE_ENGINE_HOME\/packages\/jev\/src\/[a-z-]+\.ts"/);
        expect(typeof h.timeout).toBe('number');
      }
    }
  });

  it('points at entrypoints that exist', () => {
    for (const event of Object.keys(hooks)) {
      for (const h of handlers(event)) {
        const file = /"\$COGNITIVE_ENGINE_HOME\/([^"]+)"/.exec(h.command)![1];
        expect(fs.existsSync(path.resolve(root, '..', '..', file))).toBe(true);
      }
    }
  });
});
