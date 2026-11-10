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

`.github/workflows/ci.yml` runs on every pull request and on pushes to `main`. It is **advisory**: each job sets `continue-on-error: true`, so a failure appears as a failed step inside the run, but the job and the PR check stay green and never block a merge. Read the step results to see a failure. Runs on pull requests cancel when superseded; runs on `main` are never cancelled.

- `tsc`: `bunx tsc --noEmit` in every workspace that has a `tsconfig.json`
- `bun test packages/jev adapters`
- `pytest` per Python package (`harness2`, `memory`) via `uv run --no-project --with pytest pytest`, skipped with a notice while a package has no tests (or while only `ollama`/`sbx`-marked tests exist: pytest exits 5 when everything is deselected, which CI treats as success). `--no-project` keeps pytest from building the package, whose hatchling config has no package source yet; tests that import the package need it installed (for example `--with-editable .`) once the package directory exists.

Tests that need a local Ollama daemon or the sbx sandbox must be marked `@pytest.mark.ollama` or `@pytest.mark.sbx`; CI deselects them with `-m "not ollama and not sbx"`. Register the markers in the package's `pyproject.toml` with the first such test.

## References

- RFC 001: Autonomous Cognitive Substrate Architecture
