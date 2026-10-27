import type { Tool } from '@modelcontextprotocol/sdk/types.js';
import { routeToolDefinition, handleRouteTool } from './route.js';
import { judgeToolDefinition, handleJudgeTool } from './judge.js';
import { scoreToolDefinition, handleScoreTool } from './score.js';
import {
  dispatchToolDefinition,
  batchDispatchToolDefinition,
  handleDispatchTool,
  handleBatchDispatchTool
} from './dispatch.js';
import {
  agentFreeToolDefinition,
  executeFreeToolDefinition,
  executeFreeGuardedToolDefinition,
  handleAgentFreeTool,
  handleExecuteFreeTool,
  handleExecuteFreeGuardedTool
} from './execute-free.js';
import {
  setModeToolDefinition,
  getModeToolDefinition,
  handleSetModeTool,
  handleGetModeTool,
  getModeDetails,
  readCurrentMode,
  writeModeState,
  type ModeDetails
} from './mode.js';
import {
  quotaToolDefinition,
  selectModelToolDefinition,
  handleQuotaTool,
  handleSelectModelTool
} from './quota.js';

export * from './route.js';
export * from './judge.js';
export * from './score.js';
export * from './dispatch.js';
export * from './execute-free.js';
export * from './mode.js';
export * from './quota.js';

export type ToolHandler = (args: unknown) => Promise<{ content: Array<{ type: 'text'; text: string }> }>;

export const allTools: Tool[] = [
  routeToolDefinition,
  judgeToolDefinition,
  scoreToolDefinition,
  dispatchToolDefinition,
  batchDispatchToolDefinition,
  agentFreeToolDefinition,
  executeFreeToolDefinition,
  executeFreeGuardedToolDefinition,
  setModeToolDefinition,
  getModeToolDefinition,
  quotaToolDefinition,
  selectModelToolDefinition
];

export const toolHandlers: Record<string, ToolHandler> = {
  jev_route: handleRouteTool,
  jev_judge: handleJudgeTool,
  jev_score: handleScoreTool,
  jev_dispatch: handleDispatchTool,
  jev_batch_dispatch: handleBatchDispatchTool,
  jev_agent_free: handleAgentFreeTool,
  jev_execute_free: handleExecuteFreeTool,
  jev_execute_free_guarded: handleExecuteFreeGuardedTool,
  jev_set_mode: handleSetModeTool,
  jev_get_mode: handleGetModeTool,
  jev_quota: handleQuotaTool,
  jev_select_model: handleSelectModelTool
};
