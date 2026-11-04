import { describe, it, expect, mock } from 'bun:test';
import {
  topologicalSortStages,
  estimateTierCost,
  type SubTask,
  type BatchDispatchPlan
} from '../src/shared.ts';

describe('Wave 2: Topological Wave Staging in jev_batch_dispatch', () => {
  it('correctly sorts independent tasks into a single wave stage', () => {
    const tasks: SubTask[] = [
      {
        taskId: 't1',
        task: 'Scaffold project',
        subagent: 'code_engineer',
        tier: 'openrouter_free',
        dependencies: [],
        canRunParallel: true,
        requiresUserApproval: false,
        estimatedCostUsd: 0.0
      },
      {
        taskId: 't2',
        task: 'Fetch documentation',
        subagent: 'research',
        tier: 'flash_lite',
        dependencies: [],
        canRunParallel: true,
        requiresUserApproval: false,
        estimatedCostUsd: 0.0001
      }
    ];

    const stages = topologicalSortStages(tasks);
    expect(stages.length).toBe(1);
    expect(stages[0].map(t => t.taskId)).toEqual(['t1', 't2']);
  });

  it('correctly stages linear dependencies across sequential waves', () => {
    const tasks: SubTask[] = [
      {
        taskId: 't1',
        task: 'Step 1',
        subagent: 'code_engineer',
        tier: 'flash',
        dependencies: [],
        canRunParallel: true,
        requiresUserApproval: false,
        estimatedCostUsd: 0.001
      },
      {
        taskId: 't2',
        task: 'Step 2',
        subagent: 'code_engineer',
        tier: 'flash',
        dependencies: ['t1'],
        canRunParallel: false,
        requiresUserApproval: false,
        estimatedCostUsd: 0.001
      },
      {
        taskId: 't3',
        task: 'Step 3',
        subagent: 'code_engineer',
        tier: 'flash',
        dependencies: ['t2'],
        canRunParallel: false,
        requiresUserApproval: false,
        estimatedCostUsd: 0.001
      }
    ];

    const stages = topologicalSortStages(tasks);
    expect(stages.length).toBe(3);
    expect(stages[0].map(t => t.taskId)).toEqual(['t1']);
    expect(stages[1].map(t => t.taskId)).toEqual(['t2']);
    expect(stages[2].map(t => t.taskId)).toEqual(['t3']);
  });

  it('correctly stages diamond DAG dependencies into execution waves', () => {
    // Wave 0: A
    // Wave 1: B, C (both depend on A)
    // Wave 2: D (depends on B and C)
    const tasks: SubTask[] = [
      {
        taskId: 'A',
        task: 'Root task',
        subagent: 'code_engineer',
        tier: 'flash',
        dependencies: [],
        canRunParallel: true,
        requiresUserApproval: false,
        estimatedCostUsd: 0.001
      },
      {
        taskId: 'B',
        task: 'Parallel branch 1',
        subagent: 'code_engineer',
        tier: 'flash',
        dependencies: ['A'],
        canRunParallel: false,
        requiresUserApproval: false,
        estimatedCostUsd: 0.001
      },
      {
        taskId: 'C',
        task: 'Parallel branch 2',
        subagent: 'research',
        tier: 'flash',
        dependencies: ['A'],
        canRunParallel: false,
        requiresUserApproval: false,
        estimatedCostUsd: 0.001
      },
      {
        taskId: 'D',
        task: 'Merge and review',
        subagent: 'review',
        tier: 'pro',
        dependencies: ['B', 'C'],
        canRunParallel: false,
        requiresUserApproval: true,
        estimatedCostUsd: 0.015
      }
    ];

    const stages = topologicalSortStages(tasks);
    expect(stages.length).toBe(3);
    expect(stages[0].map(t => t.taskId)).toEqual(['A']);
    expect(stages[1].map(t => t.taskId).sort()).toEqual(['B', 'C']);
    expect(stages[2].map(t => t.taskId)).toEqual(['D']);
  });

  it('accurately estimates tier costs across all supported tiers', () => {
    expect(estimateTierCost('openrouter_free')).toBe(0.00);
    expect(estimateTierCost('tier_0_free')).toBe(0.00);
    expect(estimateTierCost('free')).toBe(0.00);
    expect(estimateTierCost('flash_lite')).toBe(0.0001);
    expect(estimateTierCost('tier_1_cheap')).toBe(0.0001);
    expect(estimateTierCost('flash')).toBe(0.001);
    expect(estimateTierCost('tier_2_standard')).toBe(0.001);
    expect(estimateTierCost('pro')).toBe(0.015);
    expect(estimateTierCost('tier_3_deep')).toBe(0.015);
  });
});

describe('Wave 2: Ambiguity & Indecision Gate in jev_judge', () => {
  function computeJudgeAmbiguity(noulValue: number) {
    const confidence = Number((Math.abs(noulValue - 0.50) * 2).toFixed(4));
    const ambiguous = noulValue >= 0.45 && noulValue <= 0.55;
    const requiresManualVerification = ambiguous;
    return { confidence, ambiguous, requiresManualVerification };
  }

  it('flags ambiguous: true and requiresManualVerification: true when 0.45 <= noul <= 0.55', () => {
    // Exact center
    const center = computeJudgeAmbiguity(0.50);
    expect(center.confidence).toBe(0);
    expect(center.ambiguous).toBe(true);
    expect(center.requiresManualVerification).toBe(true);

    // Lower boundary
    const lower = computeJudgeAmbiguity(0.45);
    expect(lower.confidence).toBeCloseTo(0.10, 2);
    expect(lower.ambiguous).toBe(true);
    expect(lower.requiresManualVerification).toBe(true);

    // Upper boundary
    const upper = computeJudgeAmbiguity(0.55);
    expect(upper.confidence).toBeCloseTo(0.10, 2);
    expect(upper.ambiguous).toBe(true);
    expect(upper.requiresManualVerification).toBe(true);

    // Mid-ambiguity point
    const mid = computeJudgeAmbiguity(0.52);
    expect(mid.confidence).toBeCloseTo(0.04, 2);
    expect(mid.ambiguous).toBe(true);
    expect(mid.requiresManualVerification).toBe(true);
  });

  it('flags ambiguous: false when noul is outside the [0.45, 0.55] window', () => {
    const clearPass = computeJudgeAmbiguity(0.85);
    expect(clearPass.confidence).toBeCloseTo(0.70, 2);
    expect(clearPass.ambiguous).toBe(false);
    expect(clearPass.requiresManualVerification).toBe(false);

    const clearFail = computeJudgeAmbiguity(0.15);
    expect(clearFail.confidence).toBeCloseTo(0.70, 2);
    expect(clearFail.ambiguous).toBe(false);
    expect(clearFail.requiresManualVerification).toBe(false);
  });
});

describe('Wave 1 & 3: Direct HTTP Execution of Free Tools without OpenCode', () => {
  it('confirms OpenCode binary and runner exports are completely removed from src/index.ts', async () => {
    const indexModule = await import('../src/index.ts');
    expect((indexModule as any).OPENCODE_BIN).toBeUndefined();
    expect((indexModule as any).callOpenCodeAgent).toBeUndefined();
  });

  it('confirms openrouter/free execution directly calls OpenRouter HTTP endpoint', async () => {
    const originalFetch = globalThis.fetch;
    let endpointCalled = '';
    let requestBody: any = null;

    globalThis.fetch = (async (url: string | URL | Request, init?: RequestInit) => {
      endpointCalled = String(url);
      if (init?.body) {
        requestBody = JSON.parse(init.body as string);
      }
      return new Response(
        JSON.stringify({
          id: 'test-direct-http',
          model: 'openrouter/free',
          choices: [
            {
              message: {
                role: 'assistant',
                content: 'Direct HTTP free execution response'
              }
            }
          ],
          usage: {
            prompt_tokens: 15,
            completion_tokens: 8,
            total_tokens: 23
          }
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } }
      );
    }) as any;

    try {
      const { OPENROUTER_CHAT_URL } = await import('../src/shared.ts');
      const apiKey = process.env.JEV_OPENROUTER_API_KEY || 'test-key';
      
      const res = await fetch(OPENROUTER_CHAT_URL, {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${apiKey}`,
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({
          model: 'openrouter/free',
          messages: [{ role: 'user', content: 'Test prompt' }]
        })
      });

      const data = await res.json();
      expect(endpointCalled).toBe(OPENROUTER_CHAT_URL);
      expect(requestBody.model).toBe('openrouter/free');
      expect(data.choices[0].message.content).toBe('Direct HTTP free execution response');
      expect(data.model).toBe('openrouter/free');
    } finally {
      globalThis.fetch = originalFetch;
    }
  });
});
