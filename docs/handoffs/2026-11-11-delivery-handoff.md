# Session handoff: delivery state on 2026-11-11

> **Update 2026-11-12.** #20 and #22 are merged (`0f58554`, `c260bf5`). The owner decided that all Jev fixes and changes go to the agentic-hub plugin until cognitive-engine work resumes, and that cognitive-engine hosts fixes only for its own project (delivery plan, decision 19). The Jev follow-up code (next step 3 below) is therefore not built here, and the "wait for Doc-Cr" and "merge #20, #22" steps are done. The rest of this file is the 2026-11-11 state and is otherwise unchanged.

Written so a fresh Claude Code session (any subscription) can continue without the previous conversation. The standing plan is [`../plans/2026-11-10-delivery-plan.md`](../plans/2026-11-10-delivery-plan.md); this note adds the exact state, the next steps in order and the traps already hit.

## State in one table

| PR | Branch | Head | State |
|---|---|---|---|
| #20 `[jev 1/2]` guard hardening (includes advisory CI and the OpenCode V2 guard) | `feat/guard-claude-followup` | `b971956` | open, ready, clean. Review by @Doc-Cr (comment-style) answered and pushed |
| #22 `[jev 2/2]` advisor port, jev 2.4.0 | `feat/jev-advisor-port` | `4a79643` | open, ready, clean. Review by @Doc-Cr answered and pushed |
| (no PR yet) Jev follow-up | `feat/jev-followup`, stacked on #22 | `e431d02` | documentation done and pushed; **code not written** |
| #12 Argon hardening | `feat/argon-hardening` | | open, clean on `main`; restack once after #20 and #22 merge |
| #5, #7, #8 and children #6, #9, #10, #11, #13 | | | conflict with `main`; wait for the Stage-1 gate (coordination), then restack |

Closed and folded: #19 (CI) and #21 (OpenCode V2 guard) are inside #20 (merge commit `dd6ae2a`). Merged earlier: #3, #4, #18.

Docs: the planning docs and the adoption research moved here from `agentic-hub` and sit on `main` under `docs/` (scrubbed of machine-specific details). Treat this repository as public.

## Next steps, in order

1. Wait for @Doc-Cr's reply on #20 and #22. Doc-Cr has a pending collaborator invitation, so a formal review request (`POST /pulls/N/requested_reviewers`) fails until it is accepted; a mention in a comment works.
2. When #20 and #22 are approved: merge #20 first, then #22 (merge commits, no squash). Confirm with the owner before each merge. Then tell the coordination session (it works in `agentic-hub`) that #22 merged, so it swaps its copy of the Jev case study for a pointer.
3. Write the follow-up code on `feat/jev-followup`, then open a draft PR with base `feat/jev-advisor-port` and retarget it to `main` after #22 merges. Decided scope:
   - `JEV_RECALL_URL` env override in `packages/jev/src/core/telemetry.ts`: default `http://127.0.0.1:8110/mcp`, empty value disables, a test pins the `read_note` tool name (basic-memory 1.0.0 renames it `bm_read`).
   - Warn-only note: when any of `ANTHROPIC_DEFAULT_OPUS_MODEL`, `ANTHROPIC_DEFAULT_SONNET_MODEL`, `ANTHROPIC_DEFAULT_HAIKU_MODEL` or `CLAUDE_CODE_EFFORT_LEVEL` is set, the guard adds one line to its note naming the variable (never its value) and records the variable names in the decisions-log entry. It never blocks. The bypass itself is already documented in the Jev README ("Known limits (subagent guard)"), which says this warning is planned: update that sentence when it ships.
   - `JEV_HARNESS`: the OpenCode plugin sets it at init, the Antigravity adapter in its launcher or MCP env; one test each.
4. After the Stage-1 gate opens: restack #12, then #5, #7, #8 and their children, per the delivery plan (`git rebase --onto main`, a `range-diff` comment, `--force-with-lease`, always confirm force-pushes first).

## Decisions already taken (do not re-ask)

See the delivery plan, "Decisions" and "The 2026-11-10 /clarify pass". Highlights: one PR stacked on #22 for the follow-up; OpenCode tier map keeps its pay-per-token default and is overridden in the user's config; the native `Agent(model:opus)` deny rule is a documented opt-in snippet, no installer; env remaps and effort default are warn-only; log rotation deferred (about 20 KB a day measured); #12 restacks once, after #20 and #22.

## Rules the owner set

- Model policy: Sonnet by default for subagents; Opus only for a judgement that is expensive to get wrong (adversarial verification, one final gate); ask when unsure. Be mindful of the 5-hour quota.
- Outward-facing actions (force-push, merge, closing a PR, requesting review, deleting a remote branch) need the owner's go each time. Commit and push only when asked.
- Never put secrets, tokens, home paths, host names or private repository names in this repository's docs or commits.
- Stage explicit paths only. Use a separate worktree per branch; do not switch branches in the main checkout while worktrees are in use.

## Traps already hit

- `gh pr edit` fails with a Projects (classic) GraphQL error. Edit title and body with the REST API: `gh api -X PATCH repos/<owner>/<repo>/pulls/N -f title=... -F body=@file`. `gh pr ready N` works.
- A stacked PR (base is another PR's branch) is merged with `gh api -X PUT repos/<owner>/<repo>/pulls/N/merge-async -f merge_method=merge`.
- In zsh a variable holding several paths is not split into words: use an array (`files=(a b c)`, then `$files`).
- `sd` rejects a replacement that starts with `- `: write `sd -F -- 'find' '- replacement' file`.
- A fresh worktree has no `node_modules`: run `bun install --frozen-lockfile` before `bun test`. Editor diagnostics about `bun:test` and `node:fs` in a worktree are noise; `bun run tsc --noEmit` and `bun test` are the checks.
- `git merge-tree --write-tree <a> <b>` (exit code) is a cheap pairwise conflict test between branches.
- Verification commands: `cd packages/jev && bun run tsc --noEmit && bun test`; `cd adapters/opencode && bun test`; `cd adapters/claude && bun test`.

## Parked or owned elsewhere

- `agentic-hub` clean-up (docs links, removal of machine-specific details at its tip, its open PRs) is being done by the coordination session. The git history rewrite of `agentic-hub` is paused by the owner.
- A GitHub Project board for this repository was proposed (fields Stack and Gate, auto-add PRs) and needs the `project` token scope (`gh auth refresh -s project`); not created.
- Open owner decisions: cognitive-engine roles (symmetric peers instead of Orchestrator and Builder), the basic-memory 1.0.0 cutover scope (one decision at Track 4), Doc-Cr accepting the collaborator invitation.
