/**
 * Strict tool definitions for the Jev MCP server.
 *
 * ⚠️ `defineTool` takes a raw zod SHAPE and builds the object schema itself, always
 * `.strict()`. That is the point: the SDK's own `registerTool` accepts a raw shape
 * and wraps it in a NON-strict object, which silently drops unknown keys. A
 * misspelled `approvedTier`, `zdr` or `visibility` then becomes "not provided" and
 * the call succeeds with the default — the failure this module exists to prevent.
 * Taking the shape rather than a finished schema makes forgetting `.strict()`
 * impossible. Nested objects must use `strictObject` for the same reason.
 *
 * Before this module the handlers cast `args as {...}` and nothing enforced the
 * advertised `inputSchema` at all: a typo'd key, a bad enum value, a wrong type and
 * a missing required argument were all accepted (RFC 001 D30; plan §3.5).
 */
import { z } from 'zod';
import type { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';

export type ToolResult = { content: Array<{ type: 'text'; text: string }> };
export type ToolInput<S extends z.ZodRawShape> = z.infer<z.ZodObject<S>>;

/** An object schema that rejects unknown keys. Use for every nested object too. */
export function strictObject<S extends z.ZodRawShape>(shape: S) {
  return z.object(shape).strict();
}

export interface JevTool {
  readonly name: string;
  readonly description: string;
  readonly input: z.ZodObject<z.ZodRawShape, 'strict'>;
  // Heterogeneous registry: each handler is typed against its own shape at the
  // definition site, and the server only ever calls it with already-validated input.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  readonly handler: (args: any) => Promise<ToolResult>;
}

export function defineTool<S extends z.ZodRawShape>(def: {
  name: string;
  description: string;
  shape: S;
  handler: (args: ToolInput<S>) => Promise<ToolResult>;
}): JevTool {
  return {
    name: def.name,
    description: def.description,
    input: strictObject(def.shape) as unknown as z.ZodObject<z.ZodRawShape, 'strict'>,
    handler: def.handler
  };
}

// `registerTool` is generic over each tool's schema; instantiating it over a
// heterogeneous list recurses past TypeScript's depth limit (TS2589). The runtime
// call is identical, so bridge through the one signature this module relies on.
type RegisterTool = (
  name: string,
  config: { description: string; inputSchema: unknown },
  callback: (args: unknown) => Promise<ToolResult>
) => unknown;

export function registerTools(server: McpServer, tools: readonly JevTool[]): void {
  const register = server.registerTool.bind(server) as unknown as RegisterTool;
  for (const tool of tools) {
    register(
      tool.name,
      { description: tool.description, inputSchema: tool.input },
      async (args) => tool.handler(args)
    );
  }
}
