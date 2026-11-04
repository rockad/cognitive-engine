import { describe, it, expect } from 'bun:test';
import { createPlugin, type PluginDeps } from '../src/index.ts';
import { delegationBlock, directive, parseDispatch, taskText, type Dispatch } from '../src/logic.ts';

const flash: Dispatch = { recommendedSubagent: 'explore', recommendedTier: 'flash', requiresUserApproval: false };
const pro: Dispatch = { recommendedSubagent: 'general', recommendedTier: 'pro', requiresUserApproval: true };

function makeCtx() {
  const hooks: Record<string, (...a: any[]) => Promise<void>> = {};
  return {
    hooks,
    ctx: {
      session: { hook: async (name: string, fn: any) => void (hooks[`session.${name}`] = fn) },
      tool: { hook: async (name: string, fn: any) => void (hooks[`tool.${name}`] = fn) }
    }
  };
}

const deps = (d: Dispatch | null, mode = 'on'): PluginDeps => ({ dispatch: async () => d, readMode: async () => mode });
const contextEvent = (text: string) => ({ messages: [{ content: text }], system: [] as any[] });

describe('opencode logic', () => {
  it('extracts recent message text from string and part content', () => {
    const text = taskText({ messages: [{ content: 'one' }, { parts: [{ text: 'two' }, 'three'] }] });
    expect(text).toBe('one\ntwo\nthree');
    expect(taskText({})).toBe('');
  });

  it('parses a dispatch result and requires approval for pro', () => {
    expect(parseDispatch({ recommendedSubagent: 'x', recommendedTier: 'pro' })?.requiresUserApproval).toBe(true);
    expect(parseDispatch({ recommendedSubagent: 'x', recommendedTier: 'flash' })?.requiresUserApproval).toBe(false);
    expect(parseDispatch({ recommendedTier: 'flash' })).toBeNull();
    expect(parseDispatch(null)).toBeNull();
  });

  it('states the ceiling in the directive and asks for approval on pro', () => {
    expect(directive(flash)).toContain('hard ceiling');
    expect(directive(pro)).toContain('let the user choose');
    expect(directive(flash)).not.toContain('let the user choose');
  });

  it('blocks only approval-requiring delegations', () => {
    expect(delegationBlock(flash, 'task')).toBeNull();
    expect(delegationBlock(null, 'task')).toBeNull();
    expect(delegationBlock(pro, 'task')?.message).toStartWith('[jev]');
  });
});

describe('opencode plugin', () => {
  it('registers nothing in off mode', async () => {
    const { ctx, hooks } = makeCtx();
    await createPlugin(deps(flash)).setup(ctx, { mode: 'off' });
    expect(Object.keys(hooks)).toEqual([]);
  });

  it('registers only the context hook in advisory mode', async () => {
    const { ctx, hooks } = makeCtx();
    await createPlugin(deps(flash)).setup(ctx, { mode: 'advisory' });
    expect(Object.keys(hooks)).toEqual(['session.context']);
  });

  it('injects the directive as a text part', async () => {
    const { ctx, hooks } = makeCtx();
    await createPlugin(deps(flash)).setup(ctx, {});
    const event = contextEvent('Refactor the parser module');
    await hooks['session.context'](event);
    expect(event.system).toHaveLength(1);
    expect(event.system[0].type).toBe('text');
    expect(event.system[0].text).toContain('[JEV]');
  });

  it('advises once per unique turn', async () => {
    const { ctx, hooks } = makeCtx();
    await createPlugin(deps(flash)).setup(ctx, {});
    const event = contextEvent('Refactor the parser module');
    await hooks['session.context'](event);
    await hooks['session.context'](event);
    expect(event.system).toHaveLength(1);
  });

  it('does nothing when Jev mode is off, the task is short, or dispatch fails', async () => {
    for (const [d, mode, text] of [
      [flash, 'off', 'Refactor the parser module'],
      [flash, 'on', 'hi'],
      [null, 'on', 'Refactor the parser module']
    ] as const) {
      const { ctx, hooks } = makeCtx();
      await createPlugin(deps(d, mode)).setup(ctx, {});
      const event = contextEvent(text);
      await hooks['session.context'](event);
      expect(event.system).toHaveLength(0);
    }
  });

  it('times out a slow dispatch without failing the turn', async () => {
    const { ctx, hooks } = makeCtx();
    const slow: PluginDeps = { readMode: async () => 'on', dispatch: () => new Promise(() => {}) };
    await createPlugin(slow).setup(ctx, { timeoutMs: 20 });
    const event = contextEvent('Refactor the parser module');
    await hooks['session.context'](event);
    expect(event.system).toHaveLength(0);
  });

  it('guards delegation only in enforce mode', async () => {
    const { ctx, hooks } = makeCtx();
    await createPlugin(deps(pro)).setup(ctx, { mode: 'enforce' });
    expect(Object.keys(hooks).sort()).toEqual(['session.context', 'tool.execute.before']);

    const call = { tool: 'task', args: { prompt: 'Redesign the whole storage layer' } };
    await expect(hooks['tool.execute.before'](call, call)).rejects.toThrow(/^\[jev\]/);
    const other = { tool: 'read', args: { prompt: 'Redesign the whole storage layer' } };
    await expect(hooks['tool.execute.before'](other, other)).resolves.toBeUndefined();
  });

  it('lets delegation through when the route is not pro or allowPro is set', async () => {
    const a = makeCtx();
    await createPlugin(deps(flash)).setup(a.ctx, { mode: 'enforce' });
    const call = { tool: 'task', args: { prompt: 'Find the config loader' } };
    await expect(a.hooks['tool.execute.before'](call, call)).resolves.toBeUndefined();

    const b = makeCtx();
    await createPlugin(deps(pro)).setup(b.ctx, { mode: 'enforce', allowPro: true });
    expect(b.hooks['tool.execute.before']).toBeUndefined();
  });

  it('loads as a no-op when the host context is unusable', async () => {
    const dispose = await createPlugin(deps(flash)).setup(null, { mode: 'enforce' });
    expect(typeof dispose).toBe('function');
  });
});
