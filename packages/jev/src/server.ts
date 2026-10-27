/**
 * Jev MCP server factory and transports.
 *
 * - stdio (default): one server per CLI harness process.
 * - Streamable HTTP: stateless daemon on 127.0.0.1:8115/mcp, supervised by `engine up`.
 *   A fresh Server + transport serves each request, so no session state is shared between callers.
 */
import { createServer as createHttpServer, type IncomingMessage, type Server as HttpServer, type ServerResponse } from 'node:http';
import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { StreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/streamableHttp.js';
import { CallToolRequestSchema, ListToolsRequestSchema } from '@modelcontextprotocol/sdk/types.js';
import { allTools, toolHandlers } from './tools/index.js';

export const JEV_VERSION = '1.0.0';
export const HTTP_HOST = '127.0.0.1';
export const HTTP_PORT = 8115;
export const HTTP_PATH = '/mcp';

export function createJevServer(): Server {
  const server = new Server({ name: 'jev', version: JEV_VERSION }, { capabilities: { tools: {} } });

  server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: allTools }));

  server.setRequestHandler(CallToolRequestSchema, async (request) => {
    const { name, arguments: args } = request.params;
    const handler = toolHandlers[name];
    if (!handler) {
      throw new Error(`Unknown tool: ${name}`);
    }
    return await handler(args);
  });

  return server;
}

export async function startStdio(): Promise<Server> {
  const server = createJevServer();
  await server.connect(new StdioServerTransport());
  return server;
}

/** Reject requests whose Host header is not a loopback name on the bound port (DNS-rebinding guard). */
export function isLoopbackHost(hostHeader: string | undefined, port: number): boolean {
  if (!hostHeader) return false;
  return [`127.0.0.1:${port}`, `localhost:${port}`, `[::1]:${port}`].includes(hostHeader.toLowerCase());
}

function sendJson(res: ServerResponse, status: number, body: unknown): void {
  res.writeHead(status, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify(body));
}

async function readBody(req: IncomingMessage): Promise<unknown> {
  const chunks: Buffer[] = [];
  for await (const chunk of req) chunks.push(chunk as Buffer);
  if (chunks.length === 0) return undefined;
  return JSON.parse(Buffer.concat(chunks).toString('utf8'));
}

export interface HttpOptions {
  host?: string;
  port?: number;
}

export async function startHttp(options: HttpOptions = {}): Promise<HttpServer> {
  const host = options.host ?? HTTP_HOST;
  const requested = options.port ?? HTTP_PORT;

  const http = createHttpServer(async (req, res) => {
    const boundPort = (http.address() as { port: number } | null)?.port ?? requested;
    const { pathname } = new URL(req.url ?? '/', `http://${HTTP_HOST}`);

    if (pathname !== HTTP_PATH) return sendJson(res, 404, { error: 'not found' });
    if (!isLoopbackHost(req.headers.host, boundPort)) return sendJson(res, 403, { error: 'forbidden host' });
    if (req.method !== 'POST') {
      res.setHeader('Allow', 'POST');
      return sendJson(res, 405, { jsonrpc: '2.0', error: { code: -32000, message: 'Method not allowed' }, id: null });
    }

    try {
      const body = await readBody(req);
      const server = createJevServer();
      const transport = new StreamableHTTPServerTransport({ sessionIdGenerator: undefined });
      res.on('close', () => {
        void transport.close();
        void server.close();
      });
      await server.connect(transport);
      await transport.handleRequest(req, res, body);
    } catch (err) {
      if (!res.headersSent) {
        sendJson(res, 500, { jsonrpc: '2.0', error: { code: -32603, message: (err as Error).message }, id: null });
      }
    }
  });

  await new Promise<void>((resolve, reject) => {
    http.once('error', reject);
    http.listen(requested, host, resolve);
  });
  return http;
}
