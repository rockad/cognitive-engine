import { describe, it, expect } from 'bun:test';
import * as fs from 'node:fs';
import * as path from 'node:path';

const root = path.resolve(import.meta.dir, '..');
const { hooks } = JSON.parse(fs.readFileSync(path.join(root, 'hooks', 'hooks.json'), 'utf8'));

const handlers = (event: string): any[] => hooks[event].flatMap((entry: any) => entry.hooks);

describe('hooks.json failure policy', () => {
  it('guards the Agent|Task and Workflow tools with the same hook', () => {
    const matchers = hooks.PreToolUse.map((e: any) => e.matcher);
    expect(matchers).toEqual(['Agent|Task', 'Workflow']);
    for (const h of handlers('PreToolUse')) expect(h.command).toContain('packages/jev/src/hook.ts" claude');
  });

  it('every guard entry blocks on hook failure', () => {
    for (const h of handlers('PreToolUse')) expect(h.onFailure).toBe('block');
  });

  it('recorder hooks stay fail-open', () => {
    for (const event of ['SessionStart', 'PostModelSwitch']) {
      for (const h of handlers(event)) {
        expect(h.command).toContain('record-hook.ts"');
        expect(h.onFailure).toBeUndefined();
      }
    }
  });

  it('only PreToolUse guards carry onFailure at all', () => {
    for (const event of Object.keys(hooks)) {
      for (const h of handlers(event)) expect('onFailure' in h).toBe(event === 'PreToolUse');
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
