# Next-session kickoff prompt (paste this into a fresh Claude Code session)

This file is a prompt, not a handoff channel. The only channel between agents is `~/.agents/antigravity-claude-bridge-cognitive-engine.md` (the repo handoff was removed on purpose, see RFC 004 section 9 and the PR list). The numbers below that change over time (PR state, heads, test counts) are deliberately not repeated here: read them from the sources listed.

---

You are continuing work on `rockad/cognitive-engine` (hub checkout `~/projects/cognitive-engine`, stay out of it: work in your own git worktree, never touch its uncommitted files). Stage 1 of Harness2 is built and proven; what is left is merging, checks, and the experiments that need the user's go.

## Read first (in this order)

1. `~/.agents/cognitive-engine-tasklist.md` (shared tracker; newest entries are at the bottom of section 4 and in section 5).
2. `gh pr list --state open` and the banner at the top of any PR body: the PR titles read `[merge N/9 · after #X]` and give the merge order. Do not reorder.
3. `docs/plans/2026-11-02-checks-plan.md` (things with a pass/fail answer, merge checklist, open tickets R-1..R-18) and `docs/plans/2026-11-02-experiments-plan.md` (pre-registered EXP-1..EXP-5). Until PR #11 merges they live on `origin/docs/future-plans`: `git show origin/docs/future-plans:<path>`.
4. `docs/rfc/006-subscription-channels-and-sbx-model-sessions.md` (Stage 3 and `sbx` model sessions; open decisions D1-D10) and `docs/rfc/004-system2-harness2.md` section 9 (what the real runs showed).
5. `~/.agents/agent-bridge-protocol.md` and the tail of `~/.agents/antigravity-claude-bridge-cognitive-engine.md` (Antigravity may have replied to message 27).

## Where things stand (verify, do not trust)

- Plumbing is accepted as proven (contained sessions, route filter and model pinning, approval gate, slot table, `sbx` route, teardown, audit). The improvement loop is UNPROVEN and local-model experiments are paused by the user's decision of 2026-11-01. A smarter proposer (Stage 2: OpenRouter, zdr strict, dollar cap) is the next test and needs the user's separate typed go and cap.
- Nothing is merged. The user merges the PRs; you retarget stacked children to `main` after each parent merges, resolve the known docs conflicts on the branch (merge fresh `main` in, merge commits only, never squash), and re-run the checks plan rows.
- Antigravity (the other agent, on Google quota) was out of quota at the last check and may not have replied; its replies, if any, are in the channel file.

## Standing rules (each cost a correction once)

- Never push to `main`, never force-push, never push to branches you did not create. Opening PRs and merging need the user's go; the user has authorised the existing nine only.
- The user types every real-model approval (`stage1 run`); you never approve, never run it, never set `COGNITIVE_HARNESS2_*` approval variables. Stage 2 and Stage 3 each need their own go.
- Model attribution: there is NO Fable in the user's subscription. An agent launched with `model: fable` is silently served by Sonnet. Before writing "reviewed by model X", read the agent transcript's `message.model` field (`~/.claude/projects/<proj>/<session>/subagents/**/agent-*.jsonl`). Default subagents to Sonnet; Opus only for expensive judgements and only when the user agreed.
- Run reports undercount: `sessions_summary.denied_requests` counts path denials only. Model-pin denials are in `results/*/metrics.json` egress lines. Read `audit.jsonl` and per-session metrics before claiming "none" or "all".
- One real-daemon test suite at a time: wrap every Docker-backed or `sbx-live` pytest in `flock <lockfile>` (use one fixed lock file you create, and tell any parallel agent its path). Run pytest from the repo root. Jobs started with `&`/`nohup` inherit SIGINT as ignored: the test conftest handles it, do not remove that fixture.
- `sbx`: the user's permission to change `sbx` network policy was granted for the model-route work only. Ask again; back the policy up first; sandbox-scoped rules only; restore and verify (`sbx ls` empty).
- Secrets: never read or print credential files or token values (for example `~/.config/opencode/service.json`). Running `opencode` commands auto-starts a background `opencode serve --service`: stop what you start.

## Tooling pitfalls

- zsh does not word-split unquoted `$VAR`; use explicit commands or arrays.
- `sd` treats the pattern as a regex: use `sd -F` for literal text, and `--` before a pattern that starts with `-`.
- `gh pr edit` fails here on a classic-projects GraphQL error: use `gh api -X PATCH repos/rockad/cognitive-engine/pulls/N -F body=@file` (and `-F title=...`). `gh pr list/view/create` work.
- `rm -rf` on a variable path is blocked by a safety check: use `mktemp -d` and leave scratch directories; do not work around it.
- `pgrep -f "stage1 run"` matches your own shell command: filter with `rg -v rg` or check the process tree.
- Use `jq`, `rg`, `fd`, `yq`, `sd`, `mdq` instead of ad-hoc Python (the user's global rules); stop and say so if a tool is missing.

## First actions (propose, then wait for the user's pick)

1. Check the channel for Antigravity's reply and the PR list for anything merged or changed; report in five lines.
2. If the user has merged PRs: retarget the stacked children, regenerate the banners and titles (the marker comments `<!-- merge-order:start -->` ... `<!-- merge-order:end -->` in each body), re-run the matching checks-plan rows, push nothing to `main`.
3. Otherwise offer, in this order: (a) the open check tickets in the checks plan (R-18 reporting gap for model denials, the guard backlog items that need PR #4 merged), (b) RFC 006 phase A (stub-only spike for the credential route, stacked on the integration branch), (c) the Stage 2 profile and dollar cap for EXP-1 for the user to approve.
4. Keep the shared tracker and the checks plan in sync at each milestone; append to the channel in one write (temp file, then `cat >>`), with your own conversation id and message numbers starting at 1.
