import { describe, it, expect } from 'bun:test';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { JEV_VERSION } from '../src/server.ts';

const read = (rel: string) => JSON.parse(fs.readFileSync(path.resolve(import.meta.dir, '..', rel), 'utf8'));

describe('jev version', () => {
  it('is one number across the package, the server and the Claude plugin bundle', () => {
    const pkg = read('package.json').version;

    expect(JEV_VERSION).toBe(pkg);
    expect(read('../../adapters/claude/package.json').version).toBe(pkg);
    expect(read('../../adapters/claude/.claude-plugin/plugin.json').version).toBe(pkg);
  });

  it('is 2.4.0, the release that adds the Claude prompt advisor', () => {
    expect(JEV_VERSION).toBe('2.4.0');
  });
});
