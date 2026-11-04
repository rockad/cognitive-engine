import * as fs from 'node:fs/promises';
import * as fsSync from 'node:fs';
import * as path from 'node:path';
import * as os from 'node:os';

export type DriftType =
  | 'free_tier_leakage'
  | 'subagent_idle_miss'
  | 'burst_edit_violation'
  | 'unapproved_pro'
  | 'enum_mismatch';

export type DriftSeverity = 'low' | 'medium' | 'high' | 'critical';

export interface DriftEvent {
  timestamp: string;
  type: DriftType;
  severity: DriftSeverity;
  details: string;
  recommendation?: string;
  actualTool?: string;
  sessionId?: string;
}

export interface DriftReport {
  analyzedAt: string;
  totalDispatches: number;
  totalViolations: number;
  complianceRate: number; // 0.0 to 100.0
  violationsByType: Record<DriftType, number>;
  events: DriftEvent[];
}

export function evaluateDrift(
  telemetryLogs: any[],
  toolInvocations: any[] = []
): DriftReport {
  const events: DriftEvent[] = [];
  const violationsByType: Record<DriftType, number> = {
    free_tier_leakage: 0,
    subagent_idle_miss: 0,
    burst_edit_violation: 0,
    unapproved_pro: 0,
    enum_mismatch: 0
  };

  let totalDispatches = 0;

  for (const t of telemetryLogs) {
    if (t.tool === 'jev_dispatch' || t.tool === 'jev_batch_dispatch') {
      totalDispatches++;

      // Check Free Tier Leakage: Jev recommended openrouter_free, but subagent was invoked
      const recTier = t.recommendedTier || t.tier;
      if (recTier === 'openrouter_free') {
        const subsequentPaid = toolInvocations.find((inv) => {
          const invTs = new Date(inv.timestamp).getTime();
          const tTs = new Date(t.timestamp).getTime();
          return (
            invTs >= tTs &&
            invTs - tTs <= 300000 &&
            inv.toolName === 'invoke_subagent' &&
            inv.model &&
            inv.model !== 'openrouter_free'
          );
        });

        if (subsequentPaid) {
          const ev: DriftEvent = {
            timestamp: subsequentPaid.timestamp || t.timestamp,
            type: 'free_tier_leakage',
            severity: 'high',
            details: `Task was evaluated as free-tier eligible, but invoke_subagent was called on Model: "${subsequentPaid.model}".`,
            recommendation: 'jev_execute_free',
            actualTool: 'invoke_subagent',
            sessionId: t.sessionId
          };
          events.push(ev);
          violationsByType.free_tier_leakage++;
        }
      }

      // Check Unapproved Pro: Jev recommended pro, but user approval was not requested
      if (recTier === 'pro') {
        const proInvocation = toolInvocations.find((inv) => {
          const invTs = new Date(inv.timestamp).getTime();
          const tTs = new Date(t.timestamp).getTime();
          return (
            invTs >= tTs &&
            invTs - tTs <= 300000 &&
            inv.toolName === 'invoke_subagent' &&
            inv.model === 'pro' &&
            !inv.userApproved
          );
        });

        if (proInvocation) {
          const ev: DriftEvent = {
            timestamp: proInvocation.timestamp || t.timestamp,
            type: 'unapproved_pro',
            severity: 'critical',
            details: 'Subagent was spawned on Model: "pro" without explicit manual user approval.',
            recommendation: 'ask_question',
            actualTool: 'invoke_subagent',
            sessionId: t.sessionId
          };
          events.push(ev);
          violationsByType.unapproved_pro++;
        }
      }
    }
  }

  // Scan tool invocations directly for enum mismatches and burst edits
  for (const inv of toolInvocations) {
    if (inv.toolName === 'invoke_subagent' && inv.model) {
      const allowed = ['inherit', 'flash_lite', 'flash', 'pro'];
      if (!allowed.includes(inv.model.toLowerCase())) {
        const ev: DriftEvent = {
          timestamp: inv.timestamp || new Date().toISOString(),
          type: 'enum_mismatch',
          severity: 'high',
          details: `invoke_subagent called with invalid Model enum "${inv.model}". Valid models: ${allowed.join(', ')}.`,
          recommendation: inv.model.includes('free') ? 'jev_execute_free' : 'inherit',
          actualTool: 'invoke_subagent',
          sessionId: inv.sessionId
        };
        events.push(ev);
        violationsByType.enum_mismatch++;
      }
    }

    if (inv.hookDenied && inv.reason?.includes('Multi-File Write Guard')) {
      const ev: DriftEvent = {
        timestamp: inv.timestamp || new Date().toISOString(),
        type: 'burst_edit_violation',
        severity: 'medium',
        details: 'Attempted sequential multi-file edits in main orchestrator without prior pre-flight dispatch.',
        recommendation: 'jev_batch_dispatch',
        actualTool: inv.toolName,
        sessionId: inv.sessionId
      };
      events.push(ev);
      violationsByType.burst_edit_violation++;
    }
  }

  const totalViolations = events.length;
  const complianceRate =
    totalDispatches > 0
      ? Math.max(0, Math.min(100, Number(((1 - totalViolations / totalDispatches) * 100).toFixed(1))))
      : 100.0;

  return {
    analyzedAt: new Date().toISOString(),
    totalDispatches,
    totalViolations,
    complianceRate,
    violationsByType,
    events
  };
}
