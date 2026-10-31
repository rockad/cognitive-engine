/**
 * Zero-Network Local Complexity Heuristic Pre-Filter
 * Evaluated before remote decision calls to instantly resolve trivial mechanical tasks (<40 tokens)
 * or establish code density floors.
 */
export function evaluateLocalHeuristic(text: string): { route?: string; tier?: string; effort?: string } | null {
  const trimmed = text.trim();
  const tokenEst = Math.ceil(trimmed.length / 4);

  // Very short mechanical tasks (<40 tokens) matching routine transformation patterns
  if (tokenEst < 40) {
    const isFormatting = /\b(format|indent|organize imports|sort|prettier|clean whitespace|tabs to spaces)\b/i.test(trimmed);
    const isBoilerplate = /\b(scaffold boilerplate|empty template|stub function|skeleton)\b/i.test(trimmed);
    if (isFormatting || isBoilerplate) {
      return {
        route: 'openrouter_free',
        tier: 'openrouter_free',
        effort: 'low'
      };
    }
  }
  return null;
}

/**
 * Local Deterministic Quality Gate (Tier 3 Fallback for jev_judge).
 * Evaluates syntax, git conflict markers, compilation exit codes, and non-empty assertions
 * locally without calling remote LLM endpoints when OpenRouter is unreachable or returns 404.
 */
export function evaluateLocalQualityJudge(context: string, condition?: string): { passed: boolean; reason: string } {
  const ctx = context || '';

  // 1. Conflict marker validation
  if (ctx.includes('<<<<<<<') || ctx.includes('=======') || ctx.includes('>>>>>>>')) {
    return { passed: false, reason: 'Detected unresolved merge conflict markers in diff/output.' };
  }

  // 2. Non-empty check
  if (ctx.trim().length === 0) {
    return { passed: false, reason: 'Context is empty.' };
  }

  // 3. Test/Process exit code check
  if (ctx.includes('exit code: 1') || ctx.includes('exited with code 1') || ctx.includes('FAILED') || ctx.includes('FAIL:')) {
    if (!condition?.toLowerCase().includes('fail')) {
      return { passed: false, reason: 'Detected non-zero exit code or failure in test execution output.' };
    }
  }

  // 4. Broken JSON / syntax check if condition mentions JSON or valid format
  if (condition?.toLowerCase().includes('valid json')) {
    try {
      JSON.parse(ctx);
    } catch (e: any) {
      return { passed: false, reason: `Invalid JSON syntax: ${e.message}` };
    }
  }

  return { passed: true, reason: 'Local deterministic sanity checks passed.' };
}

/**
 * Scaffolding Detection Heuristic.
 * Detects whether a task involves new package/crate/component generation,
 * signaling that mechanical boilerplate (Cargo.toml, package.json, CLI clap structs)
 * should be offloaded to Tier 0 ($0.00 via jev_execute_free) before spawning paid workers.
 */
export function detectScaffoldingTask(taskText: string): { scaffoldFirst: boolean; boilerplateType: string | null } {
  const text = (taskText || '').toLowerCase();
  if (
    text.includes('cargo.toml') ||
    text.includes('package.json') ||
    text.includes('scaffold') ||
    text.includes('create crate') ||
    text.includes('new crate') ||
    text.includes('boilerplate') ||
    text.includes('clap struct')
  ) {
    let bpType = 'boilerplate';
    if (text.includes('cargo') || text.includes('rust') || text.includes('crate')) bpType = 'rust_crate';
    else if (text.includes('package.json') || text.includes('bun') || text.includes('node')) bpType = 'node_package';
    return { scaffoldFirst: true, boilerplateType: bpType };
  }
  return { scaffoldFirst: false, boilerplateType: null };
}
