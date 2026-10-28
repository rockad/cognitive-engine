/**
 * Subagent definition lookup: `<cwd>/.claude/agents/<name>.md`, then `~/.claude/agents/<name>.md`.
 * The name is restricted to file-name characters, so a path-like name never escapes the directory.
 */
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';

export interface AgentDefinition {
  /** A definition file exists in one of the two directories. */
  found: boolean;
  /** The `model:` from its frontmatter (first definition that sets one), or null. */
  model: string | null;
}

export function readAgentDefinition(subagentType: string | undefined, cwd: string | undefined): AgentDefinition {
  const result: AgentDefinition = { found: false, model: null };
  if (!subagentType || !/^[A-Za-z0-9_.-]+$/.test(subagentType) || subagentType.startsWith('.')) return result;
  const dirs = [cwd ? path.join(cwd, '.claude', 'agents') : null, path.join(process.env.HOME || os.homedir(), '.claude', 'agents')];
  for (const dir of dirs) {
    if (!dir) continue;
    try {
      const text = fs.readFileSync(path.join(dir, `${subagentType}.md`), 'utf8');
      result.found = true;
      const front = /^---\r?\n([\s\S]*?)\r?\n---/.exec(text)?.[1];
      const model = front && /^model:\s*(.+?)\s*$/m.exec(front)?.[1]?.replace(/^["']|["']$/g, '');
      if (model) return { found: true, model };
    } catch {
      // no definition here
    }
  }
  return result;
}
