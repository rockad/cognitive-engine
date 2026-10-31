import { describe, it, expect } from 'bun:test';
import * as fs from 'node:fs';
import * as path from 'node:path';

const root = path.resolve(import.meta.dir, '..');
const readJson = (rel: string) => JSON.parse(fs.readFileSync(path.join(root, rel), 'utf8'));

describe('claude plugin bundle', () => {
  const manifest = readJson('.claude-plugin/plugin.json');
  const mcp = readJson('.mcp.json');

  it('has a native manifest with a name, version and license', () => {
    expect(manifest.name).toBe('jev');
    expect(manifest.version).toMatch(/^\d+\.\d+\.\d+$/);
    expect(manifest.license).toBe('MIT');
  });

  it('registers exactly one hook: the PreToolUse subagent guard', () => {
    expect(manifest.hooks).toBe('./hooks/hooks.json');
    const { hooks } = readJson('hooks/hooks.json');
    expect(Object.keys(hooks)).toEqual(['PreToolUse']);
    expect(hooks.PreToolUse).toHaveLength(1);
    expect(hooks.PreToolUse[0].matcher).toBe('Agent|Task');
    expect(hooks.PreToolUse[0].hooks[0].command).toContain('packages/jev/src/hook.ts" claude');
  });

  it('registers the jev stdio server', () => {
    expect(mcp.mcpServers.jev.command).toBe('bun');
    expect(mcp.mcpServers.jev.args.at(-1)).toBe('${COGNITIVE_ENGINE_HOME}/packages/jev/src/index.ts');
  });

  it('points at an entrypoint that exists in the engine checkout', () => {
    const entry = mcp.mcpServers.jev.args.at(-1).replace('${COGNITIVE_ENGINE_HOME}', path.resolve(root, '..', '..'));
    expect(fs.existsSync(entry)).toBe(true);
  });

  it('carries no credentials', () => {
    expect(JSON.stringify(mcp)).not.toMatch(/sk-or-|API_KEY/);
  });
});
