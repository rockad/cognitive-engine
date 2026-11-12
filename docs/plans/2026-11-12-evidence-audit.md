# Evidence audit of the planning docs (2026-11-12)

- **Date**: 2026-11-12, against `origin/main` at `fbba1c8`
- **Purpose**: the owner wants a grounded, evidence-based plan before cognitive-engine work restarts (delivery plan, decision 19). This audit labels what the planning docs claim, says what the evidence shows, and lists what the restart plan has to settle first.
- **Status**: draft for the owner's review. Not committed.
- **Method**: four read-only audits split the docs by group, then one independent review re-checked the 15 most important findings and the audits themselves. Everything was read-only: `git`, `gh` GET calls, `rg`, trial merges (`git merge-tree`) and test runs in `git archive` extracts. No repository, branch or GitHub object was written.
- **Labels**: VERIFIED (the evidence was read in the run), CONTRADICTED, STALE (was true, no longer), UNVERIFIED (needs a live run, an external service or the owner), REPORTED (a statement with no artifact).

## 1. Coverage

| Audit | Documents | Claims | Verified | Contradicted | Stale | Unverified | Reported |
|---|---|---|---|---|---|---|---|
| A | multi-track plan, RFC 001 | 89 | 37 | 21 | 15 | 14 | 2 |
| B | RFC 002, 003, 004, 007, port map | 106 | 53 | 22 | 7 | 18 | 6 |
| C | delivery plan, PR plan, backlog, handoff, RFC 005 | 95 | 49 | 14 | 12 | 10 | 10 |
| D | adoption review (evidence grounding) | 111 | 54 | 11 | - | 20 | - |

Audit D uses its own labels (VERIFIED, WEAK 20, UNGROUNDED 5, UNVERIFIED, CONTRADICTED). The review re-checked 27 items: 21 confirmed, 2 partly, 1 wrong, 3 unverifiable. The per-claim tables are in the scratchpad reports and are not part of this document. Counts are as the agents reported them; the review found the counting slightly inconsistent (for example 51 vs 53 verified in B), which does not change a finding.

## 2. The findings that matter for a restart

Each was re-verified from source by the review unless marked.

1. **Jev has three owners on `main`.** Retire it (RFC 001 s7, RFC 005), "relocates here" (delivery plan line 72, backlog lines 33, 110, 192) and "the live line is agentic-hub, `packages/jev` frozen" (decisions 14, 15, 19; port-map line 26 still says 2.4.0 continues here). No RFC 005 amendment exists on any branch.
2. **The live Jev plugin lacks the hardened guard.** agentic-hub `plugins/jev` 2.3.2 has one `PreToolUse` matcher (`Agent|Task`), no `Workflow` matcher, no agent-definition resolver, no `onFailure` block and no `CLAUDE_CODE_SUBAGENT_MODEL` handling. Those exist only in the reference `packages/jev` here. Its clamp semantics also differ. "All Jev fixes go to agentic-hub" therefore needs a parity table first. 2.3.2 has no tag or release.
3. **Main holds scaffolds, not the system the RFCs describe.** `packages/memory` and `packages/harness2` are `pyproject.toml` only, `packages/lookup` is absent, and the `engine` CLI, `bin/`, `third_party/` and the Track 4 branch exist nowhere. The real code is on #5 and #8.
4. **Merge order and conflict notes are wrong.** #5 contains #8 and #9 (121 files, +39,142 over #9; 171 files against main), so the plan's separate stacks double-count. #6 is based on an old #5. All restack conflicts are docs-only (`HANDOFF.md`, the plan, RFC 002, plus `README.md` for #11); children merge cleanly into their parents.
5. **#12 does not conflict with main** (trial merge clean, GitHub MERGEABLE). It is Jev code (new `pre-invocation.ts`, governance and config edits), so it falls under decision 19. The "restack #12" steps should become "move to agentic-hub or close".
6. **The S1 entry gate is stale in the docs.** It closed on 2026-11-11 (agentic-hub commit `1213787`) and agentic-hub PR #37 (S1-PG) opened 2026-11-12. Nothing evidenced ties the cognitive-engine restack to coordination.
7. **CI proves nothing about Python or review.** `ci.yml` is advisory (`continue-on-error`), the pytest step skips ("no Python tests"), no open PR has a run, main has no branch protection, and #22 merged without a PR run. #5 and #8 have zero reviews. In clean extracts #5 passes 4,940 tests and #8 plus tools pass 249; nothing runs them in CI. "Doc-Cr approved" #20 and #22 means COMMENTED reviews saying "Good to merge"; the collaborator invite is still pending.
8. **The sandbox policy differs from the RFC.** RFC 004 says sbx-only, fail closed; #5 defaults to sbx-else-rootless-Docker. The code verifies sbx `0.50.1` only and the workstation has `0.51.0` (local, not part of the repo), so Docker would be picked silently today. The candidate has passwordless sudo inside the VM.
9. **Port 8120 collides in code.** Every Harness² head defaults to 8120 (coordination's port); 8125 is in no tree. The rule "sbx must never reach 8120/8121" has no code or test. Lookup staging (8101) is not implemented.
10. **Egress "classification inheritance" is not implemented** (TypeScript has no classification logic; Python takes a caller-declared value). `docs.visibility` exists only on #8.
11. **Jev "mandatory" behaviours are prompt text.** The guard is silent without a config file, mode `on` behaviours are advice, and there is no 20% low-quota clamp in code (the quota thresholds read the Antigravity file only).
12. **Memory evidence is for the wrong version.** The RAM and 9/10 recall figures were measured on Basic Memory 0.23.2; the pin is 0.24.1. Nobody re-measured. `list_notes` does not exist upstream, yet RFC 003 and the plan still list it.
13. **Jev hard-codes the legacy Recall URL** (`http://127.0.0.1:8110/mcp`, tool `read_note`) in both lines, so retiring that container silently disables session-memory advice. `JEV_RECALL_URL` is still only a plan.
14. **RFC 005 on main is out of date** and exists as four text variants (main; #5/#8; #7; #11): it retires jev, omits coordination and subagents, puts lookup on 8100, names a non-existent branch and wrong paths, and its tag `v2.4.0-split` clashes with the Jev line.
15. **The adoption review is a pre-decision document and must not seed the restart plan.** Examples: "Codex runs on your subscription: yes" has no supporting statement; the ntfy and `gh --blocked-by` adopt verdicts rest on reported or unreachable evidence; the S1b claim design uses SQLite `BEGIN IMMEDIATE` while S1 moved to Postgres; the agy plugin-bundle adoption is overtaken by later spikes. Also: `google-research/harness2` is a pinned upstream dependency in #5's `NOTICE`, not a clean-room implementation, which RFC 001 and the plan say.

Not verifiable from here: the OpenRouter quota field `free_model_daily_requests`, the Harness² paper id in `NOTICE`, and the live sbx run (it creates VMs).

## 3. Corrections to the audits themselves

- Audit B's "the port map exists twice" is wrong: agentic-hub has no `docs/` since `9417a84`, and the delivery plan's link to it is dead.
- Audit A's VERIFIED on the RAM and recall figures, on the sbx "fail closed" claim and on the protected-invariant set rested on a measurement from another version, a commit message and a file list. Treat them as REPORTED. Audit B read the code for the invariants and its labels are the better ones.
- Audit D's "8120 resolved" hides the code collision (finding 9).
- Audit C's "S1-PG uncommitted" is already superseded by PR #37.
- "CI green" is ambiguous: steps succeed, but the job is advisory (finding 7).
- Doc-Cr's review link in the plan (`issuecomment-3456382024`) is dead; the real comment id is `6464620383`.

## 4. What the audits did not cover

1. Security of the sandbox and egress design as built (nobody read the sandbox, session and proxy code end to end; whether a Docker-run candidate can reach host loopback services is untested).
2. The agentic-hub side as the live line: no feature-by-feature Jev parity, hub issues #24 to #28, or PR #37.
3. Quota and review-capacity arithmetic for the planned Opus pre-passes against the review sizes.
4. #5 and #8 have never been reviewed, and no review gate for about 52,000 changed lines is planned beyond "slice for Doc-Cr".
5. The other ~38 origin branches and ~39 worktrees; RFC 006, the checks plan and the experiments plan (only on #11).
6. Public-repo hygiene: this repository is public and three files on `main` still contain an absolute home path (the multi-track plan, RFC 001 and the 2026-11-09 case study).
7. The standalone `lookup` and `recall` repos (unique fixes to port, the AGPL boundary for `packages/memory`, the basic-memory 1.0.0 cutover).
8. The Doc-Cr review process (pending invite, hub issue #13) and whether the `ax` backlog items are already fixed (`ax` commit `65b97b0` appears to fix them).

## 5. What the restart plan must settle first

| # | Decision or evidence step | Owner | Closing evidence |
|---|---|---|---|
| 1 | Jev's single home and the scope sentence for cognitive-engine | owner | one signed paragraph replacing RFC 001 s7, RFC 002 status, RFC 005 Phase C, plan line 72, backlog lines 33/110/192, port-map line 26; list of #3, #4, #20, #22, #12 as reference-only |
| 2 | Jev parity table (`packages/jev` 2.4.0 vs `plugins/jev` 2.3.2), guard hardening first | agentic-hub session | feature-by-feature diff, plus a re-probe on the installed Claude Code (the existing probe was on 2.1.312) showing whether a Workflow-spawned subagent bypasses the hub's `Agent|Task` hook |
| 3 | Define "S1" and whether the restacks wait for it | owner, agentic-hub session | the gate line in the spikes doc, PR #37 status, the owner's go on S1 kickoff, and a statement of what in the restack depends on coordination (finding 4 says nothing does) |
| 4 | One merge order and the fate of each open PR | owner, cognitive-engine session | a table regenerated by `git merge-tree` after every change; decisions on #12, #13 (retarget to main, stale spec link) and #6 (rebase onto the current #5) |
| 5 | Doc baseline: merge the corrections on #7 and #11 first, or treat main's docs as historical | cognitive-engine session | `HANDOFF.md` keep-or-delete, four RFC 005 variants reduced to one, clean trial merges, a link check |
| 6 | Sandbox policy: sbx-only fail closed, or sbx-else-rootless-Docker | owner | amended RFC 004; the verified sbx version matched to the installed one by running the live test (the owner attends, it creates VMs); a test that a Docker-run candidate cannot reach host loopback 8120/8121 |
| 7 | Port plan in code and docs | cognitive-engine session with the hub session | 8125 default in settings, README and tests; lookup staging on 8101; one port-map location; a test or documented probe for the sbx-never-reaches rule |
| 8 | Egress classification inheritance, specified and tested before Track 4 gates use it | cognitive-engine session | a written derivation rule (strictest source wins) with failing-then-passing fixtures in TypeScript and Python; `docs.visibility` enforcement read and run once |
| 9 | External facts, one live call each | owner | a redacted quota-API response showing the field names; the arXiv id and upstream NOTICE wording; which Codex, Copilot and ChatGPT plans are actually held, recorded in the backlog, before any adoption-review verdict is reused |
| 10 | Re-measure memory on the shipped pin; take the basic-memory 1.0.0 decision once | owner, cognitive-engine session | RSS and 9/10 recall on native 0.24.1 stored in the repo; a decision on `bm_` tool names that also settles the Jev recall URL |
| 11 | Make CI and review meaningful | owner, cognitive-engine session | `ci.yml` blocking or explicitly advisory in the plan; CI on a restacked head; branch protection state; Doc-Cr's invite accepted and #5 and #8 reviewed formally in slices |
| 12 | Budget the restart | owner | quota figures for the Opus pre-passes against the review sizes; which of the 38 branches and 39 worktrees to retire; scrub of the three public files with a home path; refresh of the stale checkbox and count lines in the plan, PR plan and handoff |

## 6. Corrections applied with this audit (2026-11-12)

- "#12 conflicts with main" and the "restack #12" steps replaced in the delivery plan, PR plan, backlog and handoff (#12 merges cleanly and is Jev code: move or close).
- The stale S1 entry-gate lines updated in the delivery plan, PR plan and backlog (gate closed 2026-11-11; "after S1" still needs a definition, decision 3).
- Absolute home paths removed from the three public files (multi-track plan, RFC 001, the 2026-11-09 case study).
- Dead links fixed: the Doc-Cr review comment (now `issuecomment-6464620383`, checked with the API) and the agentic-hub port-map link.
- The "Jev relocates here" wording (backlog lines 33, 110, 192, delivery plan RFC 005 item, port-map line 26) replaced with the decision 14, 15 and 19 position, and a dated note added to RFC 001, RFC 002 and RFC 005.

Not applied, because they need the owner's decision: the single Jev ownership paragraph (decision 1 above), the RFC 005 amendment itself, and everything in section 5.
