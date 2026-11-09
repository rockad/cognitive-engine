/**
 * Runs one dispatch tool handler in its own process with a stubbed Decisions API, so tests never
 * touch the real network, the real decision cache or the real telemetry file (all of which resolve
 * under HOME when the module loads). Input on stdin: { tool, args, answers, offline }.
 * `answers` maps a question key (sa_<id>, tier_<id>, recommendedTier, ...) to the choice to return;
 * a key not listed gets the first criteria key. `offline` makes every fetch fail.
 * Output on stdout: the tool's JSON text.
 */
import { handleBatchDispatchTool, handleDispatchTool } from '../../src/tools/dispatch.ts';

const { tool, args, answers = {}, offline = false } = JSON.parse(await Bun.stdin.text());

globalThis.fetch = (async (_url: unknown, init?: RequestInit) => {
  if (offline) throw new Error('offline');
  const body = JSON.parse(String(init?.body));
  const out: Record<string, unknown> = {};
  for (const [key, q] of Object.entries<any>(body.questions ?? {})) {
    out[key] = { choice: answers[key] ?? Object.keys(q.criteria)[0], confidence: 0.9, probabilities: {} };
  }
  return new Response(JSON.stringify({ model: 'stub', answers: out, usage: { input_tokens: 1 } }), {
    status: 200,
    headers: { 'Content-Type': 'application/json' }
  });
}) as typeof fetch;

const res = tool === 'batch' ? await handleBatchDispatchTool(args) : await handleDispatchTool(args);
process.stdout.write(res.content[0].text);
process.exit(0);
