import * as fs from 'node:fs/promises';
import * as fsSync from 'node:fs';
import * as path from 'node:path';
import * as os from 'node:os';
import { loadJevConfig, detectActiveHarness, type JevHarness } from './config.js';

interface AgentCacheEntry {
  timestamp: number;
  key: string;
  compositeMtime: string;
  agents: Record<string, string>;
}

let agentCache: AgentCacheEntry | null = null;

export function clearAgentCache(): void {
  agentCache = null;
}

function expandHome(p: string): string {
  if (!p) return p;
  if (p === '~') return os.homedir();
  if (p.startsWith('~/') || p.startsWith('~\\')) {
    return path.join(os.homedir(), p.slice(2));
  }
  return p;
}

/**
 * Walks up from startDir to find project root:
 * a directory containing .git, .agents, .claude, or opencode.json.
 */
export function findWorkspaceRoot(startDir: string = process.cwd()): string {
  try {
    let curr = path.resolve(startDir);
    while (true) {
      if (
        fsSync.existsSync(path.join(curr, '.git')) ||
        fsSync.existsSync(path.join(curr, '.agents')) ||
        fsSync.existsSync(path.join(curr, '.claude')) ||
        fsSync.existsSync(path.join(curr, 'opencode.json'))
      ) {
        return curr;
      }
      const parent = path.dirname(curr);
      if (parent === curr) break;
      curr = parent;
    }
  } catch {
    // fallback to startDir
  }
  return path.resolve(startDir);
}

function parseAgentContent(content: string, defaultName: string): { name: string; description: string } | null {
  const match = content.match(/^---\r?\n([\s\S]*?)\r?\n---/);
  if (!match) {
    return null;
  }

  let name = defaultName;
  let description = '';

  const yamlBlock = match[1];
  for (const line of yamlBlock.split('\n')) {
    const trimmed = line.trim();
    if (trimmed.startsWith('name:')) {
      const val = trimmed.slice(5).trim().replace(/^['"]|['"]$/g, '');
      if (val) name = val;
    } else if (trimmed.startsWith('description:')) {
      const val = trimmed.slice(12).trim().replace(/^['"]|['"]$/g, '');
      if (val) description = val;
    }
  }

  return {
    name,
    description: description || `Agent specialized in ${name.replace(/[-_]/g, ' ')}`
  };
}

/**
 * Registers an agent in the results map:
 * 1. Primary name (e.g. human-readable "Career Advisor" or exact frontmatter name)
 * 2. Normalized kebab-case alias (e.g. "career-advisor") if different
 * 3. Fallback directory or file name alias if different
 */
function registerAgent(
  result: Record<string, string>,
  name: string,
  description: string,
  fallbackId?: string
): void {
  // 1. Primary name
  if (!result[name]) {
    result[name] = description;
  }

  // 2. Normalized kebab-case alias
  const kebab = name.toLowerCase().replace(/[\s_]+/g, '-');
  if (kebab && !result[kebab]) {
    result[kebab] = description;
  }

  // 3. Fallback directory or filename identifier if different
  if (fallbackId) {
    if (!result[fallbackId]) {
      result[fallbackId] = description;
    }
    const fallbackKebab = fallbackId.toLowerCase().replace(/[\s_]+/g, '-');
    if (fallbackKebab && !result[fallbackKebab]) {
      result[fallbackKebab] = description;
    }
  }
}

/**
 * Dynamic Subagent Discovery
 * Discovers subagents in hierarchical order of precedence:
 * 1. Workspace candidates (from findWorkspaceRoot(cwd)):
 *    - .agents/agents
 *    - .agents/subagents (backward compatibility)
 *    - .claude/agents
 *    - .opencode/agents
 * 2. Harness-specific config paths (config.harnesses?.[activeHarness]?.agentPaths)
 * 3. Global default config paths (config.defaults?.agentPaths)
 * 4. Generic env override (process.env.JEV_AGENTS_DIR)
 * 5. Standard global harness locations (~/.gemini/config/agents, ~/.claude/agents, ~/.config/opencode/agents)
 *
 * For each existing candidate directory, subdirectories with agent.md take precedence
 * over standalone .md files. First encountered agent name takes precedence.
 */
export async function discoverWorkspaceAgents(
  cwd: string = process.cwd(),
  harness?: JevHarness
): Promise<Record<string, string>> {
  try {
    const config = await loadJevConfig(cwd);
    const targetHarness = harness || detectActiveHarness();
    const root = findWorkspaceRoot(cwd);

    const rawCandidates: string[] = [];

    // Generic env override takes top priority if set
    if (process.env.JEV_AGENTS_DIR) {
      for (const p of process.env.JEV_AGENTS_DIR.split(path.delimiter)) {
        if (p.trim()) {
          rawCandidates.push(expandHome(p.trim()));
        }
      }
    }

    // Partition candidate directories per harness:
    if (targetHarness === 'antigravity') {
      rawCandidates.push(
        path.join(root, '.agents', 'agents'),
        path.join(root, '.agents', 'subagents'),
        path.join(os.homedir(), '.gemini', 'antigravity-cli', 'agents'),
        path.join(os.homedir(), '.gemini', 'config', 'agents')
      );
    } else if (targetHarness === 'claude-code') {
      rawCandidates.push(
        path.join(root, '.claude', 'agents'),
        path.join(os.homedir(), '.claude', 'agents')
      );
    } else if (targetHarness === 'opencode') {
      rawCandidates.push(
        path.join(root, '.opencode', 'agents'),
        path.join(os.homedir(), '.config', 'opencode', 'agents')
      );
    } else {
      // 'cli' or other: scan across candidate folders
      rawCandidates.push(
        path.join(root, '.agents', 'agents'),
        path.join(root, '.agents', 'subagents'),
        path.join(root, '.claude', 'agents'),
        path.join(root, '.opencode', 'agents'),
        path.join(os.homedir(), '.gemini', 'config', 'agents'),
        path.join(os.homedir(), '.claude', 'agents'),
        path.join(os.homedir(), '.config', 'opencode', 'agents')
      );
    }

    // Universal Shared Global Pool: for all harnesses, append ~/.agents/agents
    // (local workspace agents appear before this, so local takes precedence)
    rawCandidates.push(path.join(os.homedir(), '.agents', 'agents'));

    // Append harness config paths (config.harnesses?.[targetHarness]?.agentPaths)
    const harnessAgentPaths = config.harnesses?.[targetHarness]?.agentPaths || [];
    for (const p of harnessAgentPaths) {
      if (typeof p === 'string' && p.trim()) {
        rawCandidates.push(expandHome(p.trim()));
      }
    }

    // Append global paths (config.defaults?.agentPaths)
    const defaultAgentPaths = config.defaults?.agentPaths || [];
    for (const p of defaultAgentPaths) {
      if (typeof p === 'string' && p.trim()) {
        rawCandidates.push(expandHome(p.trim()));
      }
    }

    // Deduplicate candidate directories
    const seenPaths = new Set<string>();
    const candidateDirs: string[] = [];
    for (const raw of rawCandidates) {
      const resolved = path.resolve(raw);
      if (!seenPaths.has(resolved)) {
        seenPaths.add(resolved);
        candidateDirs.push(resolved);
      }
    }

    // Filter to existing directories and compute composite mtime for cache
    const existingDirs: string[] = [];
    let compositeMtime = '';
    for (const dir of candidateDirs) {
      try {
        const st = await fs.stat(dir);
        if (st.isDirectory()) {
          existingDirs.push(dir);
          compositeMtime += `${dir}:${st.mtimeMs};`;
        }
      } catch {
        // ignore missing directory
      }
    }

    const cacheKey = `${path.resolve(cwd)}:${targetHarness}`;
    const now = Date.now();
    if (
      agentCache &&
      agentCache.key === cacheKey &&
      now - agentCache.timestamp < 15000 &&
      agentCache.compositeMtime === compositeMtime
    ) {
      return { ...agentCache.agents };
    }

    const result: Record<string, string> = {};

    for (const dir of existingDirs) {
      try {
        const entries = await fs.readdir(dir, { withFileTypes: true });

        // 1. Directory-per-agent pass: check subdirectories with agent.md first
        for (const entry of entries) {
          let isDir = entry.isDirectory();
          if (entry.isSymbolicLink()) {
            try {
              const st = await fs.stat(path.join(dir, entry.name));
              isDir = st.isDirectory();
            } catch {
              // ignore broken link
            }
          }

          if (isDir) {
            const agentFile = path.join(dir, entry.name, 'agent.md');
            try {
              const st = await fs.stat(agentFile);
              if (st.isFile()) {
                const content = await fs.readFile(agentFile, 'utf8');
                const parsed = parseAgentContent(content, entry.name);
                if (parsed && parsed.name) {
                  registerAgent(result, parsed.name, parsed.description, entry.name);
                }
              }
            } catch {
              // agent.md doesn't exist or is not readable
            }
          }
        }

        // 2. Standalone .md files pass (fallback)
        for (const entry of entries) {
          let isFile = entry.isFile();
          if (entry.isSymbolicLink()) {
            try {
              const st = await fs.stat(path.join(dir, entry.name));
              isFile = st.isFile();
            } catch {
              // ignore broken link
            }
          }

          if (isFile && entry.name.endsWith('.md')) {
            const defaultName = path.basename(entry.name, '.md');
            const filePath = path.join(dir, entry.name);
            try {
              const content = await fs.readFile(filePath, 'utf8');
              const parsed = parseAgentContent(content, defaultName);
              if (parsed && parsed.name) {
                registerAgent(result, parsed.name, parsed.description, defaultName);
              }
            } catch {
              // ignore unreadable file
            }
          }
        }
      } catch {
        // ignore unreadable directory
      }
    }

    if (Object.keys(result).length === 0) {
      result.self = 'Direct execution in current context';
      result.none = 'Direct answer without subagent delegation';
    } else {
      if (!result.self) result.self = 'Direct execution in current context';
      if (!result.none) result.none = 'Direct answer without subagent delegation';
    }

    agentCache = {
      timestamp: now,
      key: cacheKey,
      compositeMtime,
      agents: result
    };

    return { ...result };
  } catch {
    return {
      self: 'Direct execution in current context',
      none: 'Direct answer without subagent delegation'
    };
  }
}
