/**
 * OpenCode V2 plugin: Jev context advisory and delegation guard.
 *
 * - `session.hook('context')` appends a one-line routing directive to the system prompt.
 * - `tool.hook('execute.before')`, in `enforce` mode, blocks delegations that Jev routes to the
 *   Pro tier until the user approves.
 *
 * The OpenCode V2 plugin API is beta, so every hook is defensive: a Jev failure, a timeout or an
 * unexpected event shape no-ops instead of failing the turn. Only the deliberate `[jev]` approval
 * error is allowed to propagate.
 *
 * `opencode serve` caches plugin modules in memory; restart it (`pkill -f "opencode serve"`)
 * after changing this source.
 */
import { readJevMode } from '@cognitive-engine/jev/core';
import { handleDispatchTool } from '@cognitive-engine/jev/tools';
import { delegationBlock, directive, parseDispatch, taskText, type Dispatch } from './logic.ts';

export type PluginMode = 'off' | 'advisory' | 'enforce';

export interface PluginOptions {
  mode?: PluginMode;
  /** Tool name that spawns subagents. */
  delegationTool?: string;
  /** Skip the Pro approval gate in `enforce` mode. */
  allowPro?: boolean;
  timeoutMs?: number;
}

export interface PluginDeps {
  dispatch: (task: string) => Promise<Dispatch | null>;
  readMode: () => Promise<string>;
}

const MIN_TASK_CHARS = 8;
const ADVISORY_TTL_MS = 40_000;

const defaultDeps: PluginDeps = {
  dispatch: async (task) => {
    try {
      const res = await handleDispatchTool({ task });
      return parseDispatch(JSON.parse(res.content[0].text));
    } catch {
      return null;
    }
  },
  readMode: async () => String(await readJevMode('opencode'))
};

function withTimeout<T>(p: Promise<T>, ms: number): Promise<T> {
  let timer: ReturnType<typeof setTimeout>;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error('jev timeout')), ms);
  });
  return Promise.race([p, timeout]).finally(() => clearTimeout(timer));
}

export function createPlugin(deps: PluginDeps = defaultDeps) {
  async function setup(ctx: any, rawOptions?: PluginOptions): Promise<() => void> {
    try {
      const options: PluginOptions = { ...(ctx?.options ?? {}), ...(rawOptions ?? {}) };
      const mode: PluginMode = options.mode === 'off' || options.mode === 'enforce' ? options.mode : 'advisory';
      if (mode === 'off') return () => {};

      const timeoutMs = Number.isFinite(options.timeoutMs) ? (options.timeoutMs as number) : 1500;
      const delegationTool = options.delegationTool ?? 'task';
      const advised = new Map<string, number>();

      const dispatch = async (task: string): Promise<Dispatch | null> => {
        if (task.trim().length < MIN_TASK_CHARS) return null;
        if ((await deps.readMode()) === 'off') return null;
        return withTimeout(deps.dispatch(task), timeoutMs).catch(() => null);
      };

      if (typeof ctx?.session?.hook === 'function') {
        await ctx.session.hook('context', async (event: any) => {
          try {
            const text = taskText(event);
            const key = `${text.length}:${text.slice(0, 64)}:${text.slice(-64)}`;
            const last = advised.get(key) ?? 0;
            if (!text || Date.now() - last < ADVISORY_TTL_MS) return;
            advised.set(key, Date.now());

            const d = await dispatch(text);
            // OpenCode V2 requires system parts to be `{ type: 'text', text }` objects.
            if (d && Array.isArray(event?.system)) event.system.push({ type: 'text', text: directive(d) });
          } catch {
            // never fail a turn on Jev
          }
        });
      }

      if (mode === 'enforce' && !options.allowPro && typeof ctx?.tool?.hook === 'function') {
        await ctx.tool.hook('execute.before', async (a: any, b: any) => {
          const event = a ?? {};
          const out = b ?? event;
          if ((event.tool ?? event.name ?? out.tool) !== delegationTool) return;
          const args = out.args ?? event.args ?? {};
          const task = String(args.prompt ?? args.description ?? args.task ?? '');
          const block = delegationBlock(await dispatch(task), task);
          if (block) throw block;
        });
      }

      return () => advised.clear();
    } catch {
      // a broken Jev must never take OpenCode down: load as a no-op
      return () => {};
    }
  }

  return { id: 'jev', setup };
}

const plugin = createPlugin();
export default plugin;
export { plugin };
