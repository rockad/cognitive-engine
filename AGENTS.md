# AGENTS.md

Instructions for AI coding assistants working in `cognitive-engine`.

## Worktree Discipline
- **Never work in the primary hub clone**: The primary hub clone (`~/projects/cognitive-engine`) must remain clean on `main` and is reserved solely for orchestration and cross-harness bridge communications.
- **One agent, one worktree**: Every agent session or concurrent feature workstream must operate within its own dedicated git worktree (`~/projects/cognitive-engine-<name>`).
- Use `tools/wt` to create, inspect, and remove worktrees.
- Pre-commit hook enforces this policy by rejecting commits attempted in the primary worktree unless `CE_ALLOW_HUB=1` is explicitly set.
