import * as fs from 'node:fs/promises';
import * as fsSync from 'node:fs';
import * as path from 'node:path';
import * as os from 'node:os';
import { fetchWithTimeout } from './client.js';

export const JEV_TELEMETRY_FILE = path.join(os.homedir(), '.gemini', 'antigravity-cli', 'jev_telemetry.jsonl');
export const TELEMETRY_DIR = path.join(os.homedir(), '.gemini', 'antigravity-cli');
export const TELEMETRY_FILE = path.join(TELEMETRY_DIR, 'jev_telemetry.jsonl');

export interface JevTelemetryEvent {
  timestamp?: string;
  harness: 'antigravity' | 'claude' | 'cli';
  toolOrHook: string;
  sessionId?: string | null;
  taskSummary?: string;
  tierRecommended?: string;
  modelUsed?: string;
  latencyMs?: number;
  costUsd?: number;
  tokensSaved?: number;
  quotaStrategy?: string;
  [key: string]: unknown;
}

export async function logJevTelemetry(entry: JevTelemetryEvent): Promise<void> {
  try {
    const dir = path.dirname(JEV_TELEMETRY_FILE);
    if (!fsSync.existsSync(dir)) fsSync.mkdirSync(dir, { recursive: true });
    const payload = {
      timestamp: entry.timestamp || new Date().toISOString(),
      ...entry
    };
    await fs.appendFile(JEV_TELEMETRY_FILE, JSON.stringify(payload) + '\n', 'utf8');
  } catch {
    // telemetry is best-effort
  }
}

/**
 * Non-blocking append to local telemetry log (used by MCP tools)
 */
export async function logTelemetry(entry: Record<string, unknown>): Promise<void> {
  try {
    if (!fsSync.existsSync(TELEMETRY_DIR)) {
      await fs.mkdir(TELEMETRY_DIR, { recursive: true });
    }
    const payload = {
      timestamp: new Date().toISOString(),
      harness: 'antigravity',
      ...entry
    };
    await fs.appendFile(TELEMETRY_FILE, JSON.stringify(payload) + '\n', 'utf8');
  } catch {
    // Non-blocking telemetry
  }
}

/**
 * Shared Recall / durable session memory reader across harnesses.
 */
export async function fetchRecallMemory(workspace = path.basename(process.cwd()) || 'default'): Promise<any> {
  const tryReadRecall = async (identifier: string) => {
    try {
      const res = await fetchWithTimeout('http://127.0.0.1:8110/mcp', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Accept': 'application/json, text/event-stream'
        },
        body: JSON.stringify({
          jsonrpc: '2.0',
          id: 1,
          method: 'tools/call',
          params: {
            name: 'read_note',
            arguments: {
              identifier,
              output_format: 'json',
              include_frontmatter: true
            }
          }
        })
      }, 800);
      if (res.ok) {
        const text = await res.text();
        for (const line of text.split('\n')) {
          if (line.startsWith('data: ')) {
            const parsed = JSON.parse(line.slice(6));
            const fm = parsed?.result?.structuredContent?.result?.frontmatter;
            if (fm && fm.activeGoal) {
              return fm;
            }
          }
        }
      }
    } catch {
      // fallback
    }
    return null;
  };

  const remote = (await tryReadRecall(`session-memory:${workspace}`)) || (await tryReadRecall(`session-handoff:${workspace}`));
  if (remote) return remote;

  for (const file of ['memory.json', 'handoff.json']) {
    try {
      const filePath = path.join(os.homedir(), 'projects', workspace, '.agents', file);
      if (fsSync.existsSync(filePath)) {
        const raw = fsSync.readFileSync(filePath, 'utf8');
        return JSON.parse(raw);
      }
    } catch {
      // try next
    }
  }

  return null;
}
