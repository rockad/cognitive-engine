/**
 * Pure decision logic for the OpenCode plugin: no OpenCode, network or filesystem access.
 */
export interface Dispatch {
  recommendedSubagent: string;
  recommendedTier: string;
  recommendedEffort?: string;
  requiresUserApproval: boolean;
  ceilingClamped?: boolean;
}

/** Best-effort extraction of recent message text from a `context` hook event. */
export function taskText(event: unknown): string {
  const messages = (event as { messages?: unknown })?.messages;
  if (!Array.isArray(messages)) return '';
  const out: string[] = [];
  for (const m of messages.slice(-6)) {
    const parts = m?.parts ?? m?.content;
    if (typeof parts === 'string') out.push(parts);
    else if (Array.isArray(parts)) {
      for (const p of parts) {
        if (typeof p === 'string') out.push(p);
        else if (p && typeof p.text === 'string') out.push(p.text);
      }
    }
  }
  return out.join('\n').slice(-4000);
}

/** Coerces the `jev_dispatch` tool result into a Dispatch, or null if it is not one. */
export function parseDispatch(raw: unknown): Dispatch | null {
  if (!raw || typeof raw !== 'object') return null;
  const r = raw as Record<string, unknown>;
  if (typeof r.recommendedTier !== 'string' || typeof r.recommendedSubagent !== 'string') return null;
  return {
    recommendedSubagent: r.recommendedSubagent,
    recommendedTier: r.recommendedTier,
    recommendedEffort: typeof r.recommendedEffort === 'string' ? r.recommendedEffort : undefined,
    requiresUserApproval: r.requiresUserApproval === true || r.recommendedTier === 'pro',
    ceilingClamped: r.ceilingClamped === true
  };
}

export function directive(d: Dispatch): string {
  const parts = [`subagent="${d.recommendedSubagent}"`, `tier="${d.recommendedTier}"`];
  if (d.recommendedEffort) parts.push(`effort="${d.recommendedEffort}"`);
  return (
    `[JEV] cost-tiered recommendation: ${parts.join(', ')}. ` +
    `The main session model is the hard ceiling: never delegate to a stronger model without explicit manual user approval. ` +
    (d.requiresUserApproval
      ? `This task may need the strongest tier: present the cheaper and the stronger option and let the user choose before delegating.`
      : `Delegate to the recommended subagent.`)
  );
}

/** The error that blocks a delegation, or null when it may proceed. */
export function delegationBlock(d: Dispatch | null, task: string): Error | null {
  if (!d?.requiresUserApproval) return null;
  return new Error(
    `[jev] The dispatcher recommends the ${d.recommendedTier} tier for this delegation. ` +
      `Ask the user for approval before proceeding (or set allowPro: true). Task: "${task.slice(0, 120)}"`
  );
}
