# Cognitive Engine (`cognitive-engine`)

Autonomous Cognitive Substrate uniting System 1 Coprocessor (`Jev`), System 2 Recursive Optimizer (`Harness²`), and native Memory (upstream `basic-memory`) with Lookup search.

## Tripartite Architecture

- **Core Platform (`cognitive-engine`)**: The foundational execution substrate providing low-latency System 1 decisions, recursive System 2 optimization, and durable native memory and archive search.
- **Peripheral Marketplace (`agentic-hub`)**: Reusable plugins, MCP bridges, and shared execution capabilities.
- **Consumer Vault (`doppelganger`)**: Aleksandr Korolev's second brain, agent home, and knowledge repository.

## Component Layout

- `packages/jev/`: System 1 Coprocessor (Bun/TypeScript)
- `packages/harness2/`: System 2 Recursive Optimizer (Python/uv)
- `packages/memory/`: launcher for the pinned upstream `basic-memory` (Python/uv, AGPL-3.0; PR #8)
- `packages/lookup/`: archive and transcript search (Python/uv, MIT; PR #8, SMB reads in PR #10)
- `adapters/antigravity/`: Antigravity integration adapter
- `adapters/claude/`: Claude Code integration adapter
- `adapters/opencode/`: OpenCode integration adapter (committed, deferred, unverified)
- `tools/`: MCP conformance runner and probe specs (PR #9); `tools/bench` (PR #6)

Directories land on `main` as their PRs merge; see the plan for the PR stack and merge order.

## References

- RFCs (`docs/rfc/`):
  - [RFC 001: Autonomous Cognitive Substrate Architecture](docs/rfc/001-harness2-jev-unified-cognitive-substrate.md) (umbrella and Track Status Matrix)
  - [RFC 002: Track 1, System 1 Jev and Adapters](docs/rfc/002-system1-jev.md)
  - [RFC 003: Track 2, Memory and Lookup](docs/rfc/003-memory-lookup.md)
  - [RFC 004: Track 3, System 2 Harness²](docs/rfc/004-system2-harness2.md)
  - [RFC 005: Track 4, Egress Gates and agentic-hub Retirement](docs/rfc/005-egress-retirement.md)
  - [RFC 006: Track 3 follow-up, Subscription Proposer Channels and sbx Model Sessions](docs/rfc/006-subscription-channels-and-sbx-model-sessions.md)
- Plans (`docs/plans/`):
  - [Multi-Track Implementation Plan](docs/plans/2026-10-26-multi-track-implementation-plan.md)
  - [Claude Workflows and Jev Integration Plan](docs/plans/2026-10-28-claude-workflows-jev-integration-plan.md)
  - [Checks Plan](docs/plans/2026-11-02-checks-plan.md)
  - [Experiments Plan](docs/plans/2026-11-02-experiments-plan.md)
