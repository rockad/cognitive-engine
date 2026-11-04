import { routeTool } from './route.js';
import { judgeTool } from './judge.js';
import { scoreTool } from './score.js';
import { dispatchTool, batchDispatchTool } from './dispatch.js';
import { agentFreeTool, executeFreeTool, executeFreeGuardedTool } from './execute-free.js';
import { setModeTool, getModeTool } from './mode.js';
import { quotaTool, selectModelTool } from './quota.js';
import type { JevTool } from './define.js';

export * from './define.js';
export * from './route.js';
export * from './judge.js';
export * from './score.js';
export * from './dispatch.js';
export * from './execute-free.js';
export * from './mode.js';
export * from './quota.js';

/**
 * Every Jev tool, each with a strict input schema (`defineTool`). The server
 * registers this list, and nothing else decides what is exposed.
 */
export const jevTools: readonly JevTool[] = [
  routeTool,
  judgeTool,
  scoreTool,
  dispatchTool,
  batchDispatchTool,
  agentFreeTool,
  executeFreeTool,
  executeFreeGuardedTool,
  setModeTool,
  getModeTool,
  quotaTool,
  selectModelTool
];
