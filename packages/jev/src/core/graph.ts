import type { CognitiveTier } from './config.js';

export interface SubTask {
  taskId: string;
  task: string;
  subagent: string;
  tier: string;
  cognitiveTier?: CognitiveTier;
  concreteModel?: string;
  executionTool?: 'jev_execute_free' | 'jev_agent_free' | 'invoke_subagent';
  executionGuidance?: string;
  dependencies: string[];
  canRunParallel: boolean;
  scaffoldFirst?: boolean;
  boilerplateType?: string | null;
  effort?: string;
  requiresUserApproval: boolean;
  estimatedCostUsd: number;
}

export interface BatchDispatchPlan {
  planName: string;
  totalTasks: number;
  freeTierTasks: number;
  activeSessionModel: string;
  maxConcurrentFreeWorkers: number;
  tasks: SubTask[];
  stages: SubTask[][];
  parallelExecutionOrder?: string[][];
  estimatedCostUsd: number;
  requiresUserApproval: boolean;
}

/**
 * Kahn's topological sort over task dependencies, outputting stages: SubTask[][]
 * where each stage represents an execution wave of independent tasks that can run in parallel.
 */
export function topologicalSortStages(tasks: SubTask[]): SubTask[][] {
  const taskMap = new Map<string, SubTask>();
  const inDegree = new Map<string, number>();
  const graph = new Map<string, string[]>();

  for (const t of tasks) {
    taskMap.set(t.taskId, t);
    inDegree.set(t.taskId, 0);
    graph.set(t.taskId, []);
  }

  for (const t of tasks) {
    for (const dep of t.dependencies || []) {
      if (taskMap.has(dep)) {
        inDegree.set(t.taskId, (inDegree.get(t.taskId) || 0) + 1);
        graph.get(dep)!.push(t.taskId);
      }
    }
  }

  const stages: SubTask[][] = [];
  let currentStage: SubTask[] = [];

  for (const [id, deg] of inDegree.entries()) {
    if (deg === 0) {
      currentStage.push(taskMap.get(id)!);
    }
  }

  const processed = new Set<string>();

  while (currentStage.length > 0) {
    stages.push(currentStage);
    const nextStage: SubTask[] = [];

    for (const task of currentStage) {
      processed.add(task.taskId);
      for (const neighborId of graph.get(task.taskId) || []) {
        const newDeg = (inDegree.get(neighborId) || 1) - 1;
        inDegree.set(neighborId, newDeg);
        if (newDeg === 0) {
          nextStage.push(taskMap.get(neighborId)!);
        }
      }
    }

    currentStage = nextStage;
  }

  if (processed.size < tasks.length) {
    const remaining: SubTask[] = [];
    for (const t of tasks) {
      if (!processed.has(t.taskId)) {
        remaining.push(t);
      }
    }
    if (remaining.length > 0) {
      stages.push(remaining);
    }
  }

  return stages;
}

/**
 * Flattened topological sort of tasks in dependency order.
 */
export function topologicalSortTasks(tasks: SubTask[]): SubTask[] {
  return topologicalSortStages(tasks).flat();
}
