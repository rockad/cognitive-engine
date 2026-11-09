# Cognitive Engine (`cognitive-engine`)

Autonomous Cognitive Substrate uniting System 1 Coprocessor (`Jev`), System 2 Recursive Optimizer (`Harness²`), and Unified SQLite Memory.

## Tripartite Architecture

- **Core Platform (`cognitive-engine`)**: The foundational execution substrate providing low-latency System 1 decisions, recursive System 2 optimization, and unified durable SQLite memory.
- **Peripheral Marketplace (`agentic-hub`)**: Reusable plugins, MCP bridges, and shared execution capabilities.
- **Consumer Vault (`doppelganger`)**: Aleksandr Korolev's second brain, agent home, and knowledge repository.

## Component Layout

- `packages/jev/`: System 1 Coprocessor (Bun/TypeScript)
- `packages/harness2/`: System 2 Recursive Optimizer (Python/uv)
- `packages/memory/`: Unified SQLite Memory Substrate (Python/uv)
- `adapters/antigravity/`: Antigravity integration adapter
- `adapters/claude/`: Claude Code integration adapter
- `adapters/opencode/`: OpenCode integration adapter (deferred)

## CI

`.github/workflows/ci.yml` runs on every pull request and on pushes to `main`. It is **advisory**: each job sets `continue-on-error: true`, so failures are reported but never block a merge.

- `tsc`: `bunx tsc --noEmit` in every workspace that has a `tsconfig.json`
- `bun test packages/jev adapters`
- `pytest` per Python package (`harness2`, `memory`) via `uv`, skipped with a notice while a package has no tests

Tests that need a local Ollama daemon or the sbx sandbox must be marked `@pytest.mark.ollama` or `@pytest.mark.sbx`; CI deselects them with `-m "not ollama and not sbx"`. Register the markers in the package's `pyproject.toml` with the first such test.

## References

- RFC 001: Autonomous Cognitive Substrate Architecture
