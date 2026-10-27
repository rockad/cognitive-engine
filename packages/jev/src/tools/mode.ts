import type { Tool } from '@modelcontextprotocol/sdk/types.js';
import * as path from 'node:path';
import * as os from 'node:os';
import {
  type JevMode,
  logTelemetry,
  readJevMode,
  readJevModeSync,
  detectActiveHarness,
  writeModeState as coreWriteModeState,
  loadJevConfigSync
} from '../core/index.js';

export { coreWriteModeState as writeModeState };

export interface ModeDetails {
  mode: JevMode;
  title: string;
  explanation: string;
  characteristics: string[];
}

export function getModeDetails(mode: string): ModeDetails {
  const normalized = mode.trim().toLowerCase();
  if (normalized === 'off') {
    return {
      mode: 'off',
      title: 'OFF',
      explanation: 'MCP server disabled. Zero token overhead. Zero OpenRouter calls. Antigravity operates strictly with built-in native tools.',
      characteristics: [
        'MCP server disabled (zero token overhead)',
        'Zero external OpenRouter calls',
        'Antigravity operates strictly with built-in native tools',
        'No pre-flight routing or automatic quality evaluations'
      ]
    };
  }
  if (normalized === 'on-demand' || normalized === 'ondemand') {
    return {
      mode: 'on-demand',
      title: 'ON-DEMAND',
      explanation: 'MCP server enabled as a passive System 1 coprocessor. Invoked selectively for complex multi-role workflows and ad-hoc free-tier generation.',
      characteristics: [
        'Passive System 1 coprocessor',
        'Invoked selectively for complex multi-role workflows and routing',
        'Free-tier generative execution available on request (openrouter/free)',
        'No mandatory pre-flight dispatch on routine tasks'
      ]
    };
  }
  return {
    mode: 'on',
    title: 'ON (AGGRESSIVE)',
    explanation: 'Mandatory System 1 enforcement active: aggressive pre-flight routing, free-tier offloading, automated quality gates, and subagent tier clamping.',
    characteristics: [
      'Pre-Flight Everything: Non-trivial tasks dispatched via jev_dispatch to calibrate subagent role & tier',
      'Free-Tier Offloading: Boilerplate, text formatting, summaries, and commits offloaded to openrouter/free ($0.00 cost, 0 quota)',
      'Automated Quality Gates: Diff evaluations and pre-commit checks evaluated with jev_judge',
      'Subagent Tier Guard: Pro tier models strictly gated behind manual user approval'
    ]
  };
}

export async function readCurrentMode(): Promise<{ mode: JevMode; updatedAt?: string; raw?: Record<string, any> }> {
  const activeHarness = detectActiveHarness();
  const mode = readJevModeSync(activeHarness);
  const cfg = loadJevConfigSync();
  return { mode, raw: cfg };
}

export const setModeToolDefinition: Tool = {
  name: 'jev_set_mode',
  description: 'Switch TypeSafe Jev operating mode ("off" | "on-demand" | "on"). Updates ~/.config/jev/config.json directly in-process.',
  inputSchema: {
    type: 'object',
    properties: {
      mode: {
        type: 'string',
        enum: ['off', 'on-demand', 'on'],
        description: 'The operating mode to set: "off" (disabled, 0 overhead), "on-demand" (passive coprocessor), or "on" (aggressive System 1 enforcement)'
      }
    },
    required: ['mode']
  }
};

export const getModeToolDefinition: Tool = {
  name: 'jev_get_mode',
  description: 'Inspect current TypeSafe Jev operating mode, configuration state, and operating characteristics.',
  inputSchema: {
    type: 'object',
    properties: {}
  }
};

export async function handleSetModeTool(args: unknown): Promise<{ content: Array<{ type: 'text'; text: string }> }> {
  const { mode } = (args || {}) as { mode: string };
  if (!mode) {
    throw new Error('jev_set_mode requires "mode" ("off" | "on-demand" | "on")');
  }

  const norm = String(mode).trim().toLowerCase();
  let targetMode: JevMode;
  if (norm === 'off' || norm === 'disable') {
    targetMode = 'off';
  } else if (norm === 'on-demand' || norm === 'ondemand') {
    targetMode = 'on-demand';
  } else if (norm === 'on' || norm === 'aggressive' || norm === 'enable') {
    targetMode = 'on';
  } else {
    throw new Error(`Invalid mode "${mode}". Supported modes: "off", "on-demand", "on"`);
  }

  const updatedAt = await coreWriteModeState(targetMode);
  const details = getModeDetails(targetMode);

  const result = {
    mode: targetMode,
    status: 'success',
    title: details.title,
    explanation: details.explanation,
    characteristics: details.characteristics,
    configFile: process.env.JEV_CONFIG_FILE || path.join(os.homedir(), '.config', 'jev', 'config.json'),
    updatedAt
  };

  await logTelemetry({
    tool: 'jev_set_mode',
    mode: targetMode,
    status: 'success'
  });

  return {
    content: [{
      type: 'text',
      text: JSON.stringify(result, null, 2)
    }]
  };
}

export async function handleGetModeTool(_args?: unknown): Promise<{ content: Array<{ type: 'text'; text: string }> }> {
  const activeHarness = detectActiveHarness();
  const activeMode = readJevModeSync(activeHarness);
  const cfg = loadJevConfigSync();
  const details = getModeDetails(activeMode);

  const result = {
    harness: activeHarness,
    mode: activeMode,
    defaultMode: cfg.defaults?.mode || 'off',
    harnesses: cfg.harnesses || {},
    status: 'active',
    title: details.title,
    explanation: details.explanation,
    characteristics: details.characteristics,
    configFile: process.env.JEV_CONFIG_FILE || path.join(os.homedir(), '.config', 'jev', 'config.json'),
    updatedAt: undefined
  };

  await logTelemetry({
    tool: 'jev_get_mode',
    mode: activeMode,
    status: 'success'
  });

  return {
    content: [{
      type: 'text',
      text: JSON.stringify(result, null, 2)
    }]
  };
}
