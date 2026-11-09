# RFC 007: Future Design Note — Bounding Subagent Effort

- **RFC Number**: 007
- **Track**: Track 1 (follow-up to RFC 002)
- **Status**: Note (no design yet)
- **Runtime**: TypeScript / Bun
- **Target Subsystem**: `packages/jev/` (`workflow-guard.ts`, `guard.ts`), `adapters/claude/`
- **Parent Specification**: [RFC 002: Track 1 — System 1: TypeSafe Jev Coprocessor & Multi-Harness Adapters](002-system1-jev.md)

---

## 1. Summary

The tier-ceiling design in RFC 002 bounds which model a subagent runs on. It never bounded how much that model thinks. That was an oversight in the original design, not a decision: a subagent on the session model, or on a cheaper one, can still be given a high `effort` and spend far more than the ceiling intends.

## 2. Current interim behaviour

Both guards apply one function, `effortReason` (`packages/jev/src/agent-definitions.ts`), to every effort they can read:

- The Workflow guard (`workflow-guard.ts`) reads a literal `effort` in `agent()` options and the `effort:` of an `agentType`'s definition.
- The Agent guard (`guard.ts`) reads the call's `effort` and the `effort:` of the subagent's definition (the strict frontmatter reader in `agent-frontmatter.ts`, cross-checked against Claude Code's own parser, as for `model`).

The threshold:

- `xhigh` and `max` ask.
- `low`, `medium` and `high` pass.
- On the Agent tool only, `lower` passes (one step below an effort the agent would otherwise run at, so it cannot raise it) and `higher` asks (one step above the session effort, which the guard does not read).
- Any other literal, a value that is not a string (including an integer in a definition), or a non-literal value in a workflow (variable, expression, spread), is unverifiable and asks, as `model` is treated.

This threshold is an interim choice made by the user. There is no specification behind it. `CLAUDE_CODE_SUBAGENT_EFFORT` (the environment default for subagent effort) is not read, and the precedence between the call's effort, the definition's and the environment's is not modelled: the guard asks when either the call's or the definition's effort is above the threshold.

## 3. What a proper plan must decide

- **What effort levels rank as.** Whether levels form an ordered scale like model tiers, and where `xhigh` and `max` sit on it, rather than a fixed two-level cut-off.
- **How the session effort is read.** The guard has a source for the session model (state file, then transcript) but none for the session effort. A ceiling needs one, and needs a defined behaviour when it is unknown.
- **How it composes with the model ceiling.** Whether effort is bounded independently of model, or whether a cheaper model earns a higher effort allowance, and what a project ceiling in `.jev.json` can say about it.
- **How it composes with the low-quota clamp.** Whether the clamp to the cheapest tier also clamps effort, and whether that applies to the Agent tool and Workflow `agent()` alike.

## 4. Until then

Treat the `xhigh`/`max` threshold as a stopgap. Do not build further behaviour on it, and do not read it as a statement about how effort should be ranked.
