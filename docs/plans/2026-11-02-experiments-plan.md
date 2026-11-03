# Experiments Plan: Testing the Improvement Loop with a Smarter Proposer, and the Real sbx Run

- **Document Type**: Experiments plan, written in pre-registration style (hypotheses, metrics, sample sizes, stop rules and outcome tables are fixed BEFORE any result exists)
- **Date**: 2026-11-02
- **Authors**: Aleksandr Korolev ([@rockad](https://github.com/rockad)), Claude Code (Sonnet 5.5)
- **Target Reviewers**: [@Doc-Cr](https://github.com/Doc-Cr), Aleksandr Korolev
- **Style model**: `tools/bench/PREREGISTRATION.md` on PR #6 (`feat/model-bench`, head `888a4d4`): committed before results, weights and rules not changed afterwards, amendments listed separately
- **Companions**: [RFC 006](../rfc/006-subscription-channels-and-sbx-model-sessions.md), [Checks plan](2026-11-02-checks-plan.md)
- **Specifications referenced**: [RFC 004](../rfc/004-system2-harness2.md) sections 6.2, 7, 8, 9; [Multi-Track Implementation Plan](2026-10-26-multi-track-implementation-plan.md) section 3.3.3
- **Facts verified**: 2026-11-02 against `origin/feat/stage1-integration` at `9f12381`, `origin/docs/stage1-findings` at `19de968`, and the run records under `~/.local/state/cognitive-engine/harness2-stage1` (counts and sizes only; no prompt or model text was read). Anything not checked is marked UNVERIFIED.

---

## 1. Scope

An experiment here tests a hypothesis that can come out either way. Pass or fail engineering checks (merges, tickets, acceptance tests) are in the [Checks plan](2026-11-02-checks-plan.md).

**Background.** Stage 1 proved the plumbing with local models (RFC 004 section 9). The improvement loop is UNPROVEN: one legal edit was accepted by the gates (run `a81f`, aborted by the user, one rollout sample, unmeasured: a slot-table-legal `systemprompt.md` edit whose candidate rollout failed, with no final or selection), but no edit was ADOPTED by selection in the four completed hard-set runs (`1493`, `82cd`, `5ead`, `d08d`; `base-kept` every time; gemma4-e4b-16k and qwen3-4b-16k; a second aborted run, `72a1`, made no edit: RFC 004 section 9.11); the 4B proposers write the answer into a script (slot `c7`) and the slot table refuses the whole proposal; the measured baseline on `household/hard-1` is 0 of 8. **User decision (2026-11-01): stop loop experiments on local models; test later with a smarter proposer.** This plan is the "later", and it does not start anything.

**Experiments in this plan.**

| Id | Question | Real model call | Cost |
| --- | --- | --- | --- |
| EXP-1 | Does a hosted proposer (Stage 2: OpenRouter, `zdr` strict) produce a legal accepted edit that improves the local solver? | Yes (hosted proposer, local solver) | Dollars, capped by the user's number |
| EXP-2 | Does the same hold for a subscription proposer (Stage 3: `claude_cli`, `agy_cli`, `zdr` off, open content)? | Yes (subscription) | $0 marginal; shared subscription quota |
| EXP-3 | Does the sbx model route complete a real stage-1 run cleanly and fast enough to become the default? | Yes (local Ollama) | $0; the user's time and GPU |
| EXP-4 | Would keeping the legal parts of a refused proposal, or retrying with the refusal text, change the acceptance rate? | Maybe (see EXP-4) | $0 to low |
| EXP-5 | Is there a task difficulty at which an edit can show, given a 0 of 8 floor on `hard-1` and an easy set whose single base rollout already passed? | Yes (local Ollama, solver only) | $0; GPU time |

---

## 2. Rules for every experiment

- **No experiment runs without the user's typed approval wherever a real model is called.** That includes a local Ollama model, a hosted model and a subscription CLI. The approval is `stage1 run --approve <channel>` plus the approval phrase and the fresh four-word nonce typed by the user on their own terminal (RFC 004 section 9.6). A "go" in chat authorises preparation; it is not the approval, and an agent cannot give it. The gate refuses pipes, scripts without a terminal, and any process inside a known agent-harness environment. It does not stop a determined same-user process (stated limit), so the human stays the control.
- **Agents prepare, plan and analyse; the user runs.** Allowed without a run: `stage1 plan` (read-only), `stage1 selftest` with the fakes, reading `report.json` and `audit.jsonl` with `jq`, and arithmetic on counts.
- **Open synthetic content only.** Classification `open`; the bundled task sets only; nothing from memory, transcripts or the real corpus.
- **A hosted-model run additionally needs the user's own dollar number** in the channel profile: `caps.cost_usd` is required and must be above 0 for every channel whose retention is not `local` (profile validator, README "Channel profile"). Section 4.3 only PROPOSES a number.
- **One model call at a time** (the model gate) and never two experiments on the single GPU at once.
- **Fixed in advance and not changed after a result:** hypotheses, definitions (section 3), metrics, sample sizes, stop rules, outcome tables. A change after a result goes in section 10 as an amendment with the date and the reason, and the analysis reports both versions.
- **Record, do not read.** Analyses use counts, hashes, pass rates and sizes from `report.json` and `audit.jsonl`. Prompt and model text stays in the run directory (mode 0700).

---

## 3. Common definitions

- **Pass**: a rollout meets all three criteria of `grade_answer` (shared by `sum-signed` and `sum-signed-hard`; the output is `Total: X.XX` in `output/answer.txt`).
- **Base arm**: the rollouts `base1`..`baseN` with the unedited starting harness. **Final arm**: the repeats of the harness delivered by the sweep (`final_label`, here `agg1`) when it is not the base. Both on the same instance, the same solver model and the same sweep.
- **Valid sweep**: `status ok`, 3 proposer sessions exit 0, no teardown leftover, no denied host other than the pre-registered kinds, no credential finding. An invalid sweep does not count against a sweep limit; its cost does count against the dollar cap.
- **Legal accepted edit**: all of the following, read from `report.json` and `audit.jsonl`:
  - a `mutation_check` event with `ok: true` and a non-empty `changed` list (an `ok: true` event with `changed: []` is NO edit; run `d08d` has one such event for the AGG proposer);
  - every changed file is in slot `c1`, `c3`, `c4`, or the body of `c5`/`c6` (RFC 004 section 6.2 item 5), no frontmatter change, no `c2`, `c7` or `c8` path;
  - the stage has `kept_base` false and the report has `selection.chosen_tree_is_base` false (the delivered harness is not the base);
  - an answer-literal scan finds no instance total or per-instance constant in the changed files (a leaked answer makes the edit invalid, even if the slot table passed it).
- **Improvement**: the final arm's pass rate is higher than the base arm's, with a two-sided Fisher exact p below the experiment's alpha, base and final each at N=8 (`--rollouts 8`), same sweep. The test is the one the runner prints (`stage1.fisher_two_sided`); the table below was computed independently with a throwaway awk script and must be re-checked against the runner's function before use.
- **Selection caveat (verified in the README, "How selection decides at k=1")**: at k=1 upstream does not choose between base and candidate; it delivers `agg1` whenever it exists, so **an edit that makes things worse is adopted just the same**. "Adopted" is therefore not "better". Only the arm comparison counts.

**What N=8 against N=8 can show** (two-sided Fisher, alpha 0.05, computed 2026-11-02):

| Base arm | Final arm needed for p < 0.05 | p at that point |
| --- | --- | --- |
| 0 of 8 | 5 of 8 | 0.026 |
| 1 of 8 | 6 of 8 | 0.041 |
| 2 of 8 | 7 of 8 | 0.041 |

With alpha corrected for up to 3 adopted sweeps (0.05 / 3 = 0.0167): from a 0 of 8 base, 6 of 8 is needed (p = 0.007). 4 of 8 against 0 of 8 gives p = 0.077: not significant. Sample sizes larger than 8 per arm are in section 5 (EXP-1 stop rules).

**Reference data** (the Docker route, local `gemma4-e4b-16k`, hard set, from the run records; counts only):

| Run | Sessions | Wall | Solver median | Proposer median | Model requests per session | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| `58c3` (run #4, easy set) | 7 | 127.7 s | 11.2 s | 20.5 s | not summarised here | `base1` met 3 of 3 criteria (one rollout); `inv1` 2 of 3; all stages kept the base |
| `82cd` (1b, hard) | 7 | 246.5 s | 23.0 s | 36.4 s | not summarised here | no edit adopted (`base-kept`) |
| `d08d` (1c, hard, 8 rollouts) | 14 | 440.9 s | 27.6 s (max 46.2) | 38.4 s (max 49.0) | solver 6 to 10 (mean 7.4); proposer 9 to 16 (mean 13) | base 0 of 8; 2 `mutation_check` refused, 1 `ok` with no change |

The solver median varies 2.5 times between runs on the same route (11.2 s versus 27.6 s). Any latency comparison therefore needs a same-day control (EXP-3).

**Measured token sizes** (from `step_finish` events of the 11 solver sessions of `d08d`, gemma, 45 steps): input per step 3,432 to 8,582 tokens (mean 4,805), output per step up to 2,296 (mean 712). The proposer mission files are 11.1 to 13.7 KB (about 3.4k tokens at 4 characters per token, an estimate).

---

## 4. EXP-1 — Smarter proposer through Stage 2 (OpenRouter, `zdr` strict)

**Owner**: User (approval, model, cap, run); Claude Code (prepare, plan, analyse). **Cost**: dollars, section 4.1. **Blocked by**: see 4.5.

### 4.1 Hypothesis, design, metric

- **H1**: a hosted frontier proposer produces, in at least one of up to 3 valid sweeps, a legal accepted edit whose final arm beats the base arm of the same sweep (improvement, section 3) on `household/hard-1` with the local `gemma4-e4b-16k` solver.
- **H0**: it does not. Either no legal edit appears, or the edit does not move the pass rate.
- **Design.** One proposer channel (hosted, `retention = "zdr-attested"` as declared by the user, `max_classification = "open"`), local solver channel (`local-ollama`, unchanged). Flags as in stage 1c so the only change from `d08d` is the proposer: `--task-set hard --rollouts 8 --proposer-hint on`, `attempts` unchanged at 1. Per sweep: 3 proposer sessions (`inv1`, `dom1`, `agg1`), 4 solver rollouts, 7 extra base rollouts, and 7 extra final rollouts only if an edited harness is adopted. The solver stays local, so only the proposer sessions cost dollars.
- **Primary metric**: for each adopted sweep, `pass_rates.final.passed / n` against `pass_rates.base.passed / n` (N=8 each), Fisher two-sided, with alpha 0.05 for one adopted sweep, 0.025 for two, 0.0167 for three.
- **Secondary metrics** (descriptive, no test): legal-edit rate = legal accepted edits over proposer sessions; refusal reasons by slot; changed-file count per proposal; steps and model requests per proposer session; wall time; dollars per sweep; the old baseline (0 of 8) as a prior against the new base arm.
- **Old baseline.** The 0 of 8 from `d08d` is a prior only. EXP-1 compares against its own base arm. If the new base arm is 3 of 8 or more, the instance differs from the earlier measurement; report it and do not pool.

### 4.2 Sample size and stop rules

- **Maximum 3 valid sweeps** with the same hosted model and profile. The model is written into the profile before sweep 1 and does not change mid-experiment; a different model is a new experiment with its own go.
- **Stop at the first adopted sweep whose final arm completed (N=8)**, then analyse. Do not run further sweeps to "confirm" without a new go.
- **Stop with H0** after 3 valid sweeps with no legal accepted edit, or with an edit but no improvement at the corrected alpha.
- **Larger N**: if exactly one sweep shows a gap of 3 of 8 or more with p between the corrected alpha and 0.20, one extension is allowed: rerun the same harness for 8 more rollouts per arm (N=16), once, with the user's number. At N=16, a 0 of 16 base needs 5 of 16 for p < 0.05 (p = 0.043). The extension needs the user's go.
- **Hard stops (any one ends the sweep and the experiment until fixed):** dollars spent reach the cap; the wall cap (`wall_s`, 5400 s in the profile used for `d08d`); any teardown leftover; a denied host or path outside the pre-registered kinds; any sign of a credential in `audit.jsonl`, the harvested workspace or the proposer cache; the proposer's model name differing from the pinned one.

### 4.3 Dollar cap PROPOSAL (needs the user's number)

The estimate comes from run `d08d` and is a range, not a quote. Prices are NOT verified: the three rows are hypothetical price points to show scale; replace them with the chosen model's price.

- **Billable work**: 3 proposer sessions per sweep (the solver is local and free). In `d08d` the 3 proposer sessions made 9 to 16 model requests each (mean 13). Requests include a `GET /api/tags` and `POST /api/show` calls as well as the chat steps: the 11 solver sessions made 45 chat steps (4.1 per session) in 7.4 requests per session, a ratio of about 1.8, so a proposer session is about 7 steps. A stronger hosted model may take more; the range below assumes 6 to 30 steps per session.
- **Tokens per sweep** (3 proposers): input about 162k (6 steps, 9k each) to 1.35M (30 steps, 15k each), central about 400k; output about 13k to 135k, central about 36k. Input per step starts near 8k (4.8k measured for the solver plus the 3.4k mission) and grows with tool results. Reasoning tokens bill as output and are not separable here.
- **Worst case per request**: `opencode` sends `max_completion_tokens: 32000` (README), so one request can bill up to 32k output tokens. The cap must therefore be enforced on spend, not on the estimate.

| Hypothetical price (in / out per million tokens) | Low | Central | High |
| --- | --- | --- | --- |
| $1 / $5 | $0.23 | $0.58 | $2.03 |
| $3 / $15 | $0.68 | $1.74 | $6.08 |
| $15 / $75 | $3.40 | $8.70 | $30.40 |

- **Proposal** (the user replaces both numbers): per-sweep cap **$5.00**, series cap **$15.00** for the 3 sweeps. At the $3 / $15 point this covers the high case of one sweep; at the $15 / $75 point it stops sweep 1 early, which is the intended behaviour.
- **Enforcement is not proven.** RFC 004 section 7 leaves "Dollar budget cap enforced fail-closed by ledger pre-flight" unticked, and the live OpenRouter `GET /api/v1/key` field names are unverified (RFC 004 section 8 item 7). Whether the runner stops a session mid-way on spend is UNVERIFIED. A provider-side limit on a dedicated OpenRouter key is a second backstop (whether OpenRouter offers a per-key credit limit is UNVERIFIED).

### 4.4 Model choice criteria (fixed before the run)

The model id is chosen by the user from the OpenRouter catalogue at the time of the run (the catalogue is not checked here). It must satisfy:

- **Not free tier**: no `openrouter/free`, no `:free` suffix (refused by `egress.py`).
- **Zero-data-retention route available** that the user can attest in the profile; the attestation is declared, not verified (RFC 005 section 2.5).
- **Tool calling works** through the OpenAI-compatible `/v1/chat/completions` that `opencode` v2.0.25 uses via `@ai-sdk/openai-compatible`. This is verified first with the existing stub probe pattern (`tests/real_cli_probe.py`, no hosted call) and then by the first sweep itself.
- **Context of at least 32k tokens** and a documented price, with the central cost of one sweep at or below half the per-sweep cap.
- **Reasoning behaviour that can be bounded**: a model that bills unbounded reasoning is excluded unless the profile can limit it.
- **Exact id pinned in the proxy.** The route pins the model name; the name sent by `opencode` must equal the approved name (see the R-1 finding, RESOLVED 2026-11-02, in the Checks plan: on the local route every session had model-denied requests because `opencode` calls `POST /api/show` for every model that `GET /api/tags` lists and the pin allows only the approved one, which is the pin working as designed; the sessions are unaffected. A hosted run must still send exactly the approved id).
- Not the same family as the solver, to avoid self-preference. Not required, but recorded.
- Fable is not available on the user's subscription; this does not restrict OpenRouter models.

### 4.5 Blocked by, and what must be true before it starts

| Blocker | Kind | Detail |
| --- | --- | --- |
| Stage-1 runner accepts a hosted proposer channel | Code, not yet written | At `9f12381` `stage1.py` pins `ZDR = "strict"`, accepts only `kind == "opencode"` with `retention == "local"` on a loopback endpoint, and the solver runner refuses a non-local channel. A split proposer channel and solver channel is needed (RFC 006 phase A4) |
| Key delivery for OpenRouter | Decision D9 and possibly code | The key cannot go in env (`OPENROUTER_` is a refused secret prefix). Options: the proxy credential route (RFC 006 section 2.5) or a dedicated capped key mounted as a file |
| Dollar enforcement | Code, to verify | See 4.3 |
| Live quota field check | Check | `GET /api/v1/key` against the live API (RFC 004 section 8 item 7) |
| The user's number and model | User gate | Section 4.3, 4.4 |
| Typed approval | User gate | Section 2 |
| PRs | Merge | Can be prepared on a branch stacked on #5; the run itself should use `main` after #8, #9, #5 are merged (agreed order) so the code under test is the merged code |
| Other experiments | Order | EXP-5 first (section 8) |

### 4.6 Outcomes and what we do next

| Result | Conclusion | Next step |
| --- | --- | --- |
| O1: at least one legal accepted edit and improvement at the corrected alpha | On this instance and this proposer, the loop improved a weak solver harness. It does not show the method generalises (one instance, one proposer draw) | Replicate on other instances (needs the instance selector of EXP-5), then EXP-2; record in RFC 004 section 9 |
| O2: legal accepted edit, final arm not significantly better, rate not lower | Inconclusive | At most the one N=16 extension (section 4.2); otherwise stop |
| O3: legal accepted edit, final arm lower (point estimate below base, or significantly lower) | The loop adopted a harmful harness: the k=1 rule adopts any edit | Do not run more hosted sweeps; investigate selection at k>1 (a different experiment); record that adoption is not improvement |
| O4: proposals made but all refused (illegal `c7`/`c8`/frontmatter edits) | A bigger proposer has the same failure; model size is not the cause | EXP-4 (retry with refusal text first; keep-legal-parts only on the user's decision) |
| O5: proposals made no edit or empty edits | The proposer did not engage with the mission | Inspect the mission prompt and tool-call failures (counts only); do not raise the cap |
| O6: edit adopted but the answer-literal scan fires | The proposer leaked the answer into a legal file | Treat as an illegal edit; tighten the mission text; note the scan as a standing check |
| O7: invalid sweep (credential finding, leftover, unexpected denial, cap hit) | Infrastructure or safety fault, not a result | Stop; fix; do not count against the sweep limit; the cost still counts |

### 4.7 Threats to validity

- **One instance, one solver, one proposer draw.** `household/hard-1` only; the other eight instances exist but there is no selector yet. A clear gap measures this final harness on this instance, not the method.
- **Floor effect.** The base arm is at 0 of 8. If a legal edit cannot lift a 4B solver (answers in `d08d` ranged from -302 to 10572 against a true -1291.27, so the failure is large, not a rounding error), H0 holds for a reason unrelated to the proposer. EXP-5 addresses this.
- **Selection rule at k=1** (section 3): adoption does not mean improvement.
- **Rollouts are not independent draws of tasks.** They are repeats on one instance; Fisher treats them as independent Bernoulli trials.
- **Hint effect.** The `ENVIRONMENT RULES` hint is on, as in stage 1c; a hint-off arm is not run.
- **Model denials on the local route (R-1, RESOLVED 2026-11-02).** Four or eight denied requests per session on the local route are `POST /api/show` calls for the other installed models, refused by the model pin as designed; they do not fail a session. The run summary does not count model denials (Checks plan R-18), so the denial check in the table below must read the per-session `egress_denied` in `audit.jsonl` and the model names in the `egress` log lines, not `denied_requests`. A hosted route could still fail for a naming reason, so the approved id is checked first.
- **Multiple looks.** Up to 3 sweeps; handled by the corrected alpha. The N=16 extension is a second look and is flagged as such if used.
- **Non-stationarity.** Hosted models change; the model id, date and provider route are recorded per sweep (names and hashes, not content).
- **Cost uncertainty.** The estimate may be wrong by a factor of 5 in either direction.

---

## 5. EXP-2 — Stage 3 through subscription channels

**Owner**: User (token, approval, run); Claude Code (prepare, analyse). **Cost**: $0 marginal; consumes the shared Claude (and Antigravity) quota. **Blocked by**: RFC 006 decisions and checks (below), and EXP-1 for ordering.

- **H2**: a subscription proposer (`claude_cli` on a Claude model the subscription offers, or `agy_cli`) produces a legal accepted edit that improves the local solver, under the same definitions, flags and analysis as EXP-1.
- **Not a head-to-head with EXP-1.** The models differ, so the comparison is descriptive.
- **Design.** As EXP-1 with `zdr = off`, `retention = unknown`, `classification = open`, the typed phrase `approve <channel> <sweep_id> zdr-off open-only` (RFC 006 section 2.6) and the credential route of RFC 006 for `claude_cli`. At most 3 valid sweeps per channel. `agy_cli` is a separate arm and runs only after decision D4.
- **Quota rule (proposed, decision D5):** no sweep when the 5h or 7d bucket is under 20%; stop a series if a sweep consumed more than the user's per-sweep share (the user's number). The per-session quota cost is UNVERIFIED and is recorded for the first sweep.
- **Models.** The subscription has no Fable. The channel model is whatever `--model` the Claude CLI accepts on the subscription, written into the profile before sweep 1 (RFC 004 shows an example `claude-sonnet-5-5`, which is a template value, not a verified entitlement).
- **Hard stops:** those of EXP-1 plus: the sentinel scan finds the dedicated token anywhere outside the proxy container; a vendor terms or rate-limit refusal; the CLI refuses a custom base URL (RFC 006 spike A-11 should settle this before any real sweep).
- **Outcomes:** the table of EXP-1 applies. Two extra rows:

| Result | Conclusion | Next step |
| --- | --- | --- |
| O8: the CLI cannot run contained with a proxy-injected credential | Option B is not available; option 2b or option A decision | Return to RFC 006 decisions; no sweep |
| O9: quota exhausted before 3 valid sweeps | The channel is too expensive in quota to use for sweeps | Report quota per sweep; do not retry the same day |

- **Threats:** those of EXP-1, plus: quota shared with the user's interactive work (a heavy day changes available capacity); the vendor CLI's own system prompt, tools and defaults differ from `opencode` and change behaviour independent of the model; retention is unknown by declaration.
- **Before it starts (all must be true):** RFC 006 accepted; D1 to D8 answered; checks A-1 to A-10 pass with a stub; A-11 recorded; a dedicated token created by the user; EXP-1 finished or explicitly skipped by the user; typed approval.

---

## 6. EXP-3 — The real stage-1 run on the sbx model route

**Owner**: User (runs it at a terminal); Claude Code (plan, analyse). **Cost**: $0; about 1 hour of the user's time, GPU free. **Blocked by**: nothing that needs a merge (the code is in #5); the user's availability; sbx host settings.

**Purpose**: this is the flip-the-default experiment of RFC 006 Part B. It measures the sbx route on a real local model against a same-day Docker control.

### 6.1 Hypotheses (operational, can fail)

- **H3a**: the sbx route completes the same hard-set sweep as the Docker route with `status ok`, all sessions exit 0, no timeout.
- **H3b**: the median session wall time on sbx is at most 2.0 times the median of a same-day Docker control run with the same flags. The factor 2.0 is a proposal; the user confirms or changes it BEFORE the run.
- **H3c**: no session ends with a stream-truncation, EOF or proxy-timeout error, including the longest streamed reply of `gemma4-e4b-16k` with thinking on.
- **H3d**: teardown is clean and the sbx policy is restored exactly.

### 6.2 Design

- Control: one Docker sweep, `COGNITIVE_HARNESS2_MODEL_SANDBOX` unset, flags `--task-set hard --rollouts 2 --proposer-hint on` (3 proposers, 4 solver rollouts and 1 extra base: 8 sessions).
- Treatment: two sbx sweeps, `COGNITIVE_HARNESS2_MODEL_SANDBOX=sbx`, same flags, same day, same Ollama state (model loaded, nothing else on the GPU).
- Order: control first, then sbx, then sbx again. If the control fails, stop and fix before any sbx sweep.
- Preconditions to verify with `stage1 plan` for each: `model sessions run in: ...` shows the expected runtime; both host-reach settings off; no restart pending; the sbx global policy is deny-all with no global allow.
- Snapshots before and after each sbx sweep: `sbx policy ls --json` (saved), `sbx ls -q`, `sbx settings ls`, the list of `h2s-*` and `h2bridge-*` directories under `XDG_RUNTIME_DIR`.

### 6.3 Metrics (all from `report.json`, `audit.jsonl`, and the snapshots)

| Metric | Source | Pass condition |
| --- | --- | --- |
| Status and exit codes | `report.json` `status`, `sessions_summary.exit_codes` | `ok`; only `0` |
| Session wall by role | `audit.jsonl` `session_end.duration_s` (median, max) | sbx median at most 2.0 x control median; max under 300 s |
| Long-stream behaviour | per-rollout `metrics.json` `errors`; `report.json` `errors`; `session_end.truncated` | no stream error; `truncated` false |
| Model requests per session | `session_end.model_requests`, `egress_allowed`, `egress_denied` | recorded; compared with the control (not a pass condition) |
| Denied requests | `sessions_summary.denied_reasons` | only kinds seen in the control run; none new |
| Teardown | `report.json` `teardown`; `sbx ls -q`; `sbx policy ls --json` diff | empty; no `h2m-` sandbox; policy identical |
| Per-request latency | UNVERIFIED whether recorded anywhere; session wall is the measure | n/a |

### 6.4 Outcomes and what we do next

| Result | Conclusion | Next step |
| --- | --- | --- |
| All of H3a to H3d hold on both sbx sweeps | The route is viable on a real model | RFC 006 criteria B-3 to B-9 for the user's decision to flip the default; if accepted, one small commit |
| H3a holds, H3b fails (slower than 2.0 x) | Correct but slow | Find whether the cost is the create and copy-in (fixed per session) or the proxy path; keep opt-in; do not flip |
| H3c fails (truncation or EOF) | The sbx proxy path cannot carry long replies | Do not flip; investigate the sbx proxy timeouts; open an upstream issue only with evidence |
| H3d fails (leftover or policy changed) | Safety fault | Stop; roll back (RFC 006 section 3.5); fix; no flip |
| sbx unusable on the day (daemon, settings, restart pending) | No result | Fix the host and rerun; not a failure of the route |

### 6.5 Threats to validity

- Two sbx sweeps and one control are small; run-to-run variation on Docker alone was 2.5x (section 3). The same-day control and the 2.0 factor are the guard.
- First-use effects (VM image pull, cold cache) inflate the first sbx session; report the first session separately and exclude it from the median only if the exclusion is stated in advance (it is: exclude sbx session 1 of sweep 1 from the median, report it).
- GPU contention from the desktop (8 GB shared) can change latency independent of the route.
- Only sbx v0.50.1 is covered. A different version voids the result.
- The proposer side stays a 4B model, so this run says nothing about the improvement loop.

---

## 7. EXP-4 — Keeping the legal parts, and the retry alternative

**Owner**: User (decision to deviate); Claude Code (prepare). **Cost**: $0 to low. **Blocked by**: the user's stop decision (2026-11-01) for local-model loop runs; EXP-1 outcome O4 for the hosted arm.

**What it tests.** Run `d08d` shows the shape of the problem. The DOM proposer changed `scripts/ledger_sum.py` (`c7`), `skills/ledger_calculation/SKILL.md` and `systemprompt.md`; the INV proposer changed `scripts/calculate_total.py` (`c7`) and `tool_descriptions/tool_guidance.md`. The slot table refused both whole proposals, so the legal parts (for DOM, at most `systemprompt.md`, because a new skill file is also refused; for INV, `tool_guidance.md`) were never tested. The AGG proposer made no change. Two variants:

- **EXP-4a, keep the legal parts.** Drop only the forbidden files (`c2`, `c7`, `c8`, frozen frontmatter, new skills) and apply the rest. **This deviates from upstream's whole-proposal refusal** and from the consumption invariant (RFC 004 section 2.1: use upstream directly; adapt only where direct use fails). It needs a user decision before any code.
- **EXP-4b, retry with the refusal text (upstream-native).** `SumSignedAdapter.validate_tree` already feeds the refusal reason back as retry text (README, "The slot table is applied inside the pipeline on every set"), but stage 1 forces `attempts` to 1. Allowing `attempts = 2` is a configuration change inside upstream's own mechanism, not a deviation. It doubles the proposer sessions.

**Why neither was chosen.** 4a deviates from upstream and a partial proposal may be semantically broken (a prompt that tells the solver to call a script that was dropped). Its legal parts can also carry the same cheat in prose (an answer or a per-instance constant in `systemprompt.md`), so it needs the answer-literal scan. 4b was not tried because the 2026-11-01 decision stopped local experiments before it came up.

**Hypotheses.**

- **H4a**: applying only the legal parts of refused proposals lifts the acceptance rate above 0 and produces a harness that passes more often than the base arm.
- **H4b**: with one retry that shows the refusal text, a proposer revises into a legal edit more often than not.

**Design (offline first).** Replay needs no new proposer call: build harnesses from the legal parts of the stored `d08d` proposals and run 8 solver rollouts each against the local model (16 sessions, about 8 minutes, $0). The stored proposals sit in `proposer-audit/*.workspace` inside the run directory; do not delete `d08d` before this is decided (Checks plan L-d1). Content has not been read for this plan.

**Condition to revisit.** Only if EXP-1 ends in O4 (a stronger proposer still violates the slot table) AND the user decides EXP-4b is not enough AND the user accepts the deviation for 4a in writing. Until then it stays a recorded, unchosen option (RFC 004 section 9.11).

| Result | Conclusion | Next step |
| --- | --- | --- |
| 4a replay improves over base at the table of section 3 | Partial application would have helped on these proposals | Decision on the deviation, with the leak scan as a standing gate |
| 4a replay shows no gain | Legal parts alone are not enough | Close the option |
| 4b yields a legal edit in a majority of retried proposals | Upstream's own retry fixes most refusals | Make `attempts = 2` an option of the runner; no deviation |
| 4b yields none | Retry does not help | Return to task difficulty (EXP-5) |

**Threats.** The two stored proposals are two draws from a 4B proposer; their legal parts may be empty or trivial. Replay does not exercise the proposer's reaction to feedback (4b needs a live proposer). The leak scan must run before the rollouts.

---

## 8. EXP-5 — Task difficulty and the baseline band

**Owner**: User (run); Claude Code (prepare, analyse). **Cost**: $0; about 35 minutes of GPU for 72 sessions at about 28 s each (estimate from `d08d`). **Blocked by**: the user's stop decision, which covered improvement-loop runs (this is solver calibration without a proposer, so it needs the user to confirm it is in scope); code that does not exist yet.

**Problem.** The measurements sit at both ends. On the easy set the base rollout already passed (run #4: `base1` met all 3 criteria, so every stage kept the base; a ceiling seen in one rollout only). On `household/hard-1` it scored 0 of 8 (a floor: with 0 of 8 the true rate could still be up to about 31% at 95% one-sided confidence, from 1 - 0.05^(1/8)). An edit can only show between the two. The earlier "about half at k=1" estimate was wrong for this instance, and the instances differ: six of nine hard instances use CRLF line endings.

- **Question.** Is there an instance, or a variant, where the base arm's pass rate with `gemma4-e4b-16k` falls in the band **0.15 to 0.60**?
- **Why this band.** At N=8 per arm, a base near 0.15 to 0.40 leaves room both to rise (5 to 7 of 8 is within reach of the table in section 3) and to fall; below that, a ceiling-on-zero problem; above it, too little headroom.
- **Design.** Base harness only, no proposer. For each of the 9 hard instances, N=8 rollouts: 72 sessions. Pre-registered classification per instance: floor (0 or 1 of 8), band (2 to 5 of 8), ceiling (6 or more of 8). If no instance is in the band, create intermediate variants (fewer amounts, fewer notations) and measure those the same way; this needs code and a new amendment.
- **Metric.** Pass count per instance out of 8; the 95% interval is wide at N=8 (for 3 of 8 about 9% to 76%, Clopper-Pearson, computed by hand), so the band is a screen, not a precise estimate; a candidate instance is then re-measured at N=16 before use in EXP-1.
- **Code that does not exist at `9f12381`:** an instance selector (the runner fixes `household/hard-1`; the README says the other eight are "behind a future flag") and a base-only mode (`--rollouts N` always includes the proposer sessions). UNVERIFIED that either can be done without touching the runner's hot file `stage1.py`.
- **Option, not planned:** a hosted solver calibration to see whether the instance is solvable at all; costs dollars and needs the same prerequisites as EXP-1.

| Result | Conclusion | Next step |
| --- | --- | --- |
| At least one instance in the band | A usable difficulty exists | Use it for EXP-1 (replacing `hard-1` only by pre-registered amendment before EXP-1 starts) |
| All hard instances at the floor | A 4B solver cannot be moved by legal edits on this family, or edits cannot reach it | Build variants; or accept a stronger local solver; do not spend EXP-1 dollars on a floor |
| Some instances at the ceiling | The family is too easy in places | Exclude them |
| Wide spread inside one instance across repeats | Arithmetic noise dominates | Report; consider `temperature` and sampling settings (UNVERIFIED which the runner controls) |

**Threats.** N=8 is small. The per-instance difference may come from line endings or from notation mix rather than difficulty in a useful sense. Running 72 sessions in a row on one GPU may drift with thermal state; record time of day and order.

---

## 9. Order and gates

**Order (proposed).** EXP-5 and EXP-3 first, EXP-1 next, EXP-2 after, EXP-4 only on its condition.

| Step | Experiment | Gate (all must be true) | Why here |
| --- | --- | --- | --- |
| 1 | EXP-3 (sbx real run) | User at a terminal; typed approval; sbx host settings off; GPU free; factor 2.0 confirmed or changed in advance | Independent of the loop; code is in #5; unblocks the sbx default decision |
| 2 | EXP-5 (difficulty) | Instance selector and base-only mode written and reviewed (Claude Code); user confirms it is in scope of the 2026-11-01 stop decision; typed approval | Tells EXP-1 which instance has room to move; cheap |
| 3 | EXP-1 (Stage 2) | The blockers of section 4.5 are closed; model and dollar numbers written by the user; EXP-5 result read; typed approval | The first test of the loop with a smarter proposer |
| 4 | EXP-2 (Stage 3) | RFC 006 accepted; D1 to D8 answered; checks A-1 to A-11 done; dedicated token; EXP-1 finished or skipped; typed approval | Depends on the credential route and on EXP-1 for comparison |
| 5 | EXP-4 (keep-legal or retry) | EXP-1 ended in O4; user decision recorded | Only matters if a stronger proposer is still refused |

EXP-3 and EXP-5 can swap places; they must never overlap on the GPU. Merges do not gate the experiments except where noted (EXP-1 and EXP-2 should run on merged code; the plan is to prepare them on a branch stacked on `feat/stage1-integration`). Dependencies on PRs #3 to #10 are in RFC 006 section 4.2 and the Checks plan section 3.

**Statement.** No experiment in this plan runs, and no real model is called, without the user's typed approval at their own terminal for that run. The user's number for any dollar cap and the user's choice of model are inputs to the run, not defaults. An agent may prepare a plan, run the read-only `stage1 plan`, rehearse with the fakes (`stage1 selftest`) and analyse the records afterwards. It may not approve, start or extend a sweep.

---

## 10. Amendments

None. This file is committed before any EXP result exists. A change after a result goes here with the date, the reason, what changed, and the analysis under both versions.
