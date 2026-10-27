#!/usr/bin/env bun
/**
 * TypeSafe Jev MCP Server entrypoint.
 *
 * Transport selection:
 * - default: stdio, for CLI harnesses (Antigravity, Claude Code, OpenCode)
 * - `--http` or JEV_TRANSPORT=http: Streamable HTTP on 127.0.0.1:8115/mcp (`engine up`)
 *
 * Mode scheduling contract (implemented in src/tools/mode.ts):
 * When targetMode === 'off', setTimeout(() => execFile(JEV_MODE_BIN, ['off'], () => {}), 150);
 * timer.unref?.();
 * Returns: 'Jev mode set to OFF (MCP server disable scheduled in 150ms after response flush)'
 */
import { initTracing } from './tracing.js';
import { HTTP_HOST, HTTP_PATH, HTTP_PORT, startHttp, startStdio } from './server.js';

// Re-export for backward compatibility with existing tests
export { getModeDetails, type ModeDetails } from './tools/mode.js';

if (import.meta.main) {
  void initTracing();

  const useHttp = process.argv.includes('--http') || process.env.JEV_TRANSPORT === 'http';
  if (useHttp) {
    const port = Number(process.env.JEV_HTTP_PORT ?? HTTP_PORT);
    await startHttp({ port });
    console.error(`jev: streamable HTTP listening on http://${HTTP_HOST}:${port}${HTTP_PATH}`);
  } else {
    await startStdio();
  }
}
