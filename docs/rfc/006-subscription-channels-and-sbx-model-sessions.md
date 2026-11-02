# RFC 006: Track 3 — Subscription Proposer Channels (Stage 3) and sbx for Model Sessions

- **RFC Number**: 006
- **Track**: Track 3 (follow-up to RFC 004 section 6.2 stage 3, section 8 item 9, sections 9.7, 9.9, 9.11)
- **Status**: Proposed. Design only: nothing in this RFC is built, and no credential or real vendor call was used to write it.
- **Runtime**: Python 3.12 / uv
- **License**: Apache-2.0
- **Target Subsystem**: `packages/harness2/` (`session_runner.py`, `egress_proxy.py`, `stage1.py`, `channels.py`, `sbx_session.py`)
- **Target Branches (proposed)**: `feat/stage3-credential-route` (Part A) and `feat/sbx-model-default` (Part B), both cut from `feat/stage1-integration` (PR #5)
- **Parent Blueprint**: [RFC 001](001-harness2-jev-unified-cognitive-substrate.md)
- **Related RFCs**: [RFC 004: System 2](004-system2-harness2.md), [RFC 005: Egress & Retirement](005-egress-retirement.md)
- **Implementation Plan**: [Multi-Track Implementation Plan](../plans/2026-10-26-multi-track-implementation-plan.md)
- **Companion plans**: [Checks plan](../plans/2026-11-02-checks-plan.md), [Experiments plan](../plans/2026-11-02-experiments-plan.md)
- **Design note (outside the repo)**: `~/.agents/cognitive-engine-stage3-credential-delivery-design.md` (2026-11-01, DESIGN ONLY). It is not in the repository, so this RFC restates what a reviewer needs; the note stays the long form.
- **Facts verified**: 2026-11-02 against `origin/feat/stage1-integration` at `9f12381` (PR #5 head) and `origin/docs/stage1-findings` at `19de968` (PR #7 head). Anything not checked against git, a PR or a file is marked UNVERIFIED.

---

## 1. Summary & Motivation

Stage 1 proved the plumbing with a local model: contained sessions, the allow-list egress proxy with route filter and model pinning, the host unix-socket bridge, the approval gate, the slot table, harvest, teardown and audit (RFC 004 section 9). It did not prove the improvement loop: no proposer edit has been accepted in any of 5 real hard-set runs, and the baseline is 0 of 8 (RFC 004 section 9.11). On 2026-11-01 the user stopped loop experiments on local models and asked for a smarter proposer later.

This RFC covers the two pieces of "item 4" that sit on that path:

- **Part A — Stage 3**: proposer sessions through the user's Claude Code subscription (`claude_cli`) and Antigravity subscription (`agy_cli`). The blocking unknown is RFC 004 section 8 item 9: how a subscription credential reaches a contained session without being a plain value that model-written code can read.
- **Part B — sbx for model sessions**: the opt-in `sbx` route for the stage-1 model sessions exists and is live-verified against a stub model. It is not proven by a real stage-1 run and it is not the default.
- **Part C — Dependency and blocker table**: what blocks each work item, exactly which PRs, which user gates, which external factors, and whether work can start now on a branch stacked on PR #5.

One finding from reading the code changes the scope of Part A (section 3.3): the stage-1 runner at PR #5's head only accepts a local channel. Stage 2 (OpenRouter) and Stage 3 both need that generalised, and both need a way to hand a key to a contained session. The credential route proposed here is vendor-neutral, so it also serves Stage 2.

---

## 2. Part A — Stage 3: Subscription Channels

### 2.1 Goal

- Run a proposer session with `claude_cli` or `agy_cli` inside the existing contained-session topology (rootless Docker, `--internal` network, allow-list proxy), on the user's subscription, with open synthetic content only and `zdr` explicitly off.
- The long-lived vendor credential is never present in the session container where model-written code could read it.
- A human approves each sweep with a typed phrase that cannot be reused from a `zdr=strict` run.
- Every claim of the design is testable with a stub endpoint and a random sentinel instead of a real credential.

### 2.2 Non-goals

- Any content above `open`. Subscription channels are `retention: unknown`; the profile validator already refuses `unknown` with a higher `max_classification` (README, "Channel profile").
- Verifying the vendor's retention. ZDR stays "declared, not verified" (RFC 004 section 6.2 item 2, RFC 005 section 2.5).
- A cloud sandbox, or running the vendor CLI on the host.
- Minting short-lived vendor tokens. None is documented for a subscription; revocation of the long-lived token is manual.
- `sbx secret` as the delivery mechanism (option D below): unproven, and model sessions stay on Docker for Part A.
- Making the interactive Claude Code or Antigravity sessions of the user part of this flow. The dedicated token and a dedicated profile are separate from them.

### 2.3 What exists today (checked at PR #5 head `9f12381`)

| Fact | Where | Consequence |
| --- | --- | --- |
| The channel profile already accepts `claude_cli` and `agy_cli` with `retention = "unknown"` and `max_classification = "open"` | `packages/harness2/README.md`, "Channel profile" | Profile shape needs no change |
| `channels.decide` refuses `retention=unknown` under `zdr=strict` and refuses anything above `open` on an unknown-retention channel | `channels.py`; design note section 6 | The refusal is the safe default; `zdr=off` needs its own flow (section 2.6) |
| `stage1.py` pins `ZDR = "strict"` (line 73) and its `preflight` accepts only `ch.kind == "opencode"`, `retention == "local"` and loopback endpoints (line 314); the solver runner raises "stage 1 runs on a local channel only" (line 766) | `stage1.py` | No subscription or OpenRouter channel can be approved or run by the stage-1 runner today |
| `Credential(container_path, value, env_var=None)` mounts a file read-only into the SESSION container; the file is `0444` in a `0711` directory under a `0700` parent in `XDG_RUNTIME_DIR` | `session_runner.py`; README "Contained proposer sessions" | Inside the container the file is world-readable by design: model code can `cat` it. No code path builds a `Credential` yet |
| The runner refuses credential-like environment names; `upstream_egress.py` lists `ANTHROPIC_`, `OPENAI_`, `OPENROUTER_`, `JEV_` among secret prefixes | `upstream_egress.py:66` | A key cannot be passed as an environment value |
| For a hosted channel the proxy handles `CONNECT host:port` and tunnels bytes; TLS is end to end | design note section 2 | The proxy cannot see headers, so it cannot inject an `Authorization` header today |
| The local-model route is a plain-HTTP, parsed, method-and-path-filtered, model-pinned relay | RFC 004 section 9.5 | It is the template for a credential route (section 2.5) |
| The image tag is `cognitive-harness2-proposer:<12 hex of sha256(Dockerfile, proxy source)>` | README | Any change to `egress_proxy.py` needs an image rebuild and produces a new tag |
| `agy` headless in a container: one endpoint `daily-cloudcode-pa.googleapis.com:443`, HTTP/2 SSE, token file mode 0400 | RFC 004 section 6.2 item 4 (spike only, not run in stage 1) | The stdlib proxy has no HTTP/2, so a credential route does not work for `agy` yet |
| Claude CLI behaviour with a custom base URL and an OAuth token | design note section 1 | UNVERIFIED. Names such as `ANTHROPIC_BASE_URL` appear in the binary; semantics are untested |

### 2.4 Threat model

**Attacker.** Code the model writes and runs through the CLI's own shell tool, as uid 65534 in the same container and as the same user as the CLI. It has a read-only root, noexec `/tmp` and `HOME`, no capabilities, and only the proxy is reachable.

**The fact that decides everything.** Anything the CLI can read (an environment value, an open descriptor, a file, a refresh token) the model's shell tool can read too (`/proc/<pid>/environ`, `/proc/<pid>/fd/N`, the file). Read-only mounts, mode `0400`, `--cap-drop ALL` and a noexec tmpfs stop other users and other containers, not the model. A credential hidden from the model must therefore not be in the session container at all.

**Assets.**

| Asset | Why it matters |
| --- | --- |
| The subscription identity and quota | Shared with the user's interactive sessions; abuse exhausts the 5h and 7d buckets |
| A refresh token, if one is present | A leak outlives the session; the Google token for `agy` is likely to carry one (UNVERIFIED) |
| The content sent to the vendor | Open synthetic text only; retention unknown by declaration |
| The harvested workspace, `audit`, and the proposer cache | A smuggled credential would land there |

**Per-option exposure** (condensed from design note section 5; "attack" is what model code can do):

| Attack | A: file (0444) | A2/A3: env or fd | B: proxy-injected |
| --- | --- | --- | --- |
| Read the credential | Yes | Yes | No: only a placeholder |
| Use it at the allowed vendor host with any path, model or count | Yes, through a CONNECT tunnel the proxy cannot inspect | Yes | Only what the route allows: listed paths, pinned model, request and byte caps |
| Smuggle it into the reply, the workspace or the cache | Yes for base64 or split copies; only the exact value is redacted | Yes | Nothing to smuggle |
| Abuse the proxy itself | Allow-list only | Same | New HTTP surface: header smuggling, request splitting, SSRF, response scrubbing; it must be fuzzed like the ollama route |
| Exhaust the quota | Bounded by session count and wall clock | Same | Also bounded by proxy request and byte caps |

**Residual on every option.** The open synthetic content goes to the vendor with retention unknown (accepted by the explicit `zdr=off` approval). The Claude quota is shared with interactive use. Whether automated `claude -p` and `agy --print` sweeps are acceptable under the vendors' terms is the user's call. `agy` under option A has the worst residual: a Google account token readable by model code.

### 2.5 Credential-delivery options and recommendation

| Id | What the session holds | Where the real credential lives | Works for | Verdict |
| --- | --- | --- | --- | --- |
| A. File (today's `Credential`) | The real token, a `0444` file | Host tmpfs, bind-mounted read-only into the SESSION | `agy` yes; `claude` only as `~/.claude/.credentials.json`, which carries a refresh token and risks logging the host out | Fallback for `agy` only |
| A2. Environment value | The real token in env and `/proc/self/environ` | Runner passes `-e`; visible in `docker inspect` | `claude` | Refused by the runner by design |
| A3. Inherited descriptor or read-once wrapper | The real token in memory and `/proc/<pid>/fd` | File read by a wrapper, then removed | `claude` (`CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR` exists as a name) | Small gain over A2 |
| **B. Credential route in the proxy** | A placeholder; `ANTHROPIC_BASE_URL` points at the proxy | A file mounted only into the PROXY container, read once, unlinked after the READY line | `claude` (to be proven); not `agy` | **Recommended for `claude_cli`** |
| 2b. TLS intercept | A placeholder plus a per-session CA certificate | Proxy container only; CA key never leaves it | `claude` fallback | If the CLI refuses a custom base URL with OAuth |
| D. `sbx secret` in a microVM | `*_API_KEY=proxy-managed` placeholder | sbx host proxy | Unproven (`sbx secret set` lists `anthropic` and `google`; header shape unknown) | Not pursued; RFC 004 keeps model sessions on Docker |

**Recommendation (carried from the design note, not yet decided).**

- Build option B for `claude_cli` first: the same proxy machinery as the ollama route with a TLS upstream. The session gets `ANTHROPIC_BASE_URL=http://harness2-proxy.internal:<port>` and a fixed non-secret placeholder token, both set by the runner and never by the caller (like `NO_PROXY` today). The proxy strips inbound `Authorization`, `x-api-key`, `cookie` and `x-*-token` headers, injects `Authorization: Bearer <real>`, opens TLS to the allow-listed host (resolve once, reject private addresses, verify the certificate, pin SNI), and relays the stream. It inherits the path allow-list, model pin, request cap, body and response caps, and access log.
- Keep `agy_cli` on option A for open synthetic content, gated by an explicit decision (D4), until HTTP/2 and a base-URL setting exist.
- Use a DEDICATED `claude setup-token` token for this flow, stored under `~/.config/cognitive-engine/credentials/` (dir `0700`, file `0600`), read only by a `stage1 run` process the human starts at a terminal; rotate it after each sweep series.
- Fail closed when `XDG_RUNTIME_DIR` is not a tmpfs: today the runner falls back to `tempfile.gettempdir()`, which may be a real disk (design note section 4).

**The credential route also serves Stage 2.** OpenRouter via `opencode` needs an API key, and the key cannot go in env. The same route (placeholder in the session, bearer injected by the proxy, TLS to `openrouter.ai`) delivers it. The alternative for Stage 2 is a dedicated key with a provider-side spending limit, mounted as a file with option A (whether OpenRouter supports a per-key credit limit and how `opencode` reads a key by file are UNVERIFIED). This is decision D9.

### 2.6 `zdr=off` approval flow and audit fields

**Flow (proposed).**

- Never a default and never a persisted setting. A separate flag `--zdr off` on `stage1 run`, valid ONLY with a channel whose retention is `unknown` and with classification fixed to `open`.
- The typed phrase differs from a strict run so it cannot be pasted: `approve <channel> <sweep_id> zdr-off open-only` plus the fresh four-word nonce, read from `/dev/tty`. The existing refusal inside a known agent-harness environment stays (RFC 004 section 9.6).
- Before the prompt the terminal prints: provider, endpoints, model, "retention UNKNOWN: content is sent to the vendor and may be retained or used by it", the credential mode, the caps, and the input files with hashes.
- Open synthetic content is enforced, not asserted: with `zdr=off` only the bundled task sets (`task_sets.py`) and the candidate harness files are allowed as inputs; a custom task directory or any file outside a manifest is refused.
- `ExecutionGrant` gains `zdr` and `retention` (today it carries channel, sweep id, approver, expiry, caps and classification only). `CognitiveSubstrate.execute` re-checks them on every call and refuses a grant whose zdr is `off` for a channel that is not `unknown`, or whose classification is not `open`.

**Audit fields** (`audit.jsonl`: numbers and short words only; no prompt text, no credential):

| Group | Fields |
| --- | --- |
| `approval` | approver, time, sweep id, phrase kind (`zdr-off open-only`), hash of the typed text (not the nonce), terminal name |
| Channel facts | name, kind, provider, endpoints, model, retention, zdr, classification, caps, profile file hash |
| Credential facts, never the value | mode (`proxy-injected` or `file`), a token label (for example `harness2-claude-2026-11`), an 8-hex fingerprint, and the removal proof (host file unlinked at, proxy container removed at, no leftovers) |
| Input manifest | file names and sha256 of everything sent (mission, evidence, harness files); the vendor request ids from response headers, so a deletion request can name them |
| Traffic | proxy counters per session (requests, bytes, denied hosts and paths, model pins refused); quota state before and after (the Claude 5h and 7d buckets, or `jev_quota`) |
| Refusals | every `retention-vs-zdr` or `unknown-retention-open-only` that fired when the sweep was planned under `zdr=strict`, so the log shows the policy was reached and overridden by a person |

### 2.7 Acceptance criteria (testable with a STUB endpoint; no real credential in any test)

The "secret" in every test is a random sentinel `H2TEST-<24 hex>` generated by the test, and TLS uses a self-signed CA reachable only through a `_test_ca_file` option that the runner never sets (the same pattern as the existing `test_allow_cidrs` option, which is marked "TESTS ONLY" in `contained_proposer.py`).

- [ ] **A-1 Delivery.** The stub HTTPS upstream receives `Authorization: Bearer <sentinel>` and never the placeholder. An inbound `Authorization`, `x-api-key` or `cookie` from the session is overwritten or dropped; the stub records the header set it saw.
- [ ] **A-2 Invisibility.** A hostile fake CLI (an extension of `tests/fixtures/fake-proposer-cli`) dumps its env, scans every `/proc/*/environ` and `/proc/*/fd`, walks every readable file, tries the credential directory, DNS, and a direct connect to the stub. The sentinel appears nowhere: not in env, any container file, stdout, stderr, the harvested workspace, `docker inspect` of both containers, or host `ps`.
- [ ] **A-3 Route limits.** `POST /v1/messages` on the allowed model passes; another path gets 403 with a logged denial; a body naming another model is refused; request, body and response caps trip; a request-splitting attempt and a duplicate-header attempt are refused.
- [ ] **A-4 Streaming.** An SSE response from the stub reaches the fake CLI byte for byte.
- [ ] **A-5 Teardown.** The host credential file is gone right after READY. After the session no `h2s-*` directory, container or network remains (per-run id checks, as the existing leak tests do).
- [ ] **A-6 Policy.** `zdr=strict` with the unknown-retention channel is refused. `--zdr off` with a classification other than `open` is refused. `--zdr off` with the `zdr-off open-only` phrase and a pty (the existing pty harness) is approved. The audit lines of section 2.6 exist and contain no sentinel.
- [ ] **A-7 Negative control.** The same hostile script under option A (file mount) DOES find the sentinel. This proves the test can fail and records why A is not the answer for `claude`.
- [ ] **A-8 Tmpfs base.** With `XDG_RUNTIME_DIR` unset or not a tmpfs, a credential-bearing session is refused instead of falling back to a disk path.
- [ ] **A-9 Image identity.** Changing the proxy source changes the image tag; a session refuses an image whose label does not match.
- [ ] **A-10 No real secret in tests.** A repository-wide search for token-shaped strings in the new tests finds only the sentinel pattern.
- [ ] **A-11 Spike record (needs the user's go to run the real `claude` binary against the stub, no credential).** Recorded: which hosts and paths the CLI contacts, the `Authorization` form, the beta headers, whether it starts with `TMP_EXEC=False`, whether it accepts OAuth with a plain-HTTP base URL, and whether it loads any user settings or hooks from the container (none expected: `HOME` is a tmpfs). This result decides B versus 2b before proxy code is written.

Real-credential acceptance is not a test. It is the first approved sweep (Experiments plan, EXP-2) and is checked by the audit fields above.

### 2.8 Phases

| Phase | Deliverable | Gate to enter |
| --- | --- | --- |
| A0 | Decisions D1 to D10 recorded; the `claude gateway` documentation read (it may already offer vendor-supported header injection) | User answers |
| A1 | Spike A-11 against a stub, no credential | User go to run the real binary |
| A2 | Credential route in `egress_proxy.py` with a fake TLS upstream; tests A-1, A-3, A-4 first; rebuilt image and new tag | A1 result (B or 2b) |
| A3 | Runner plumbing in `session_runner.py`: `Credential(mode="proxy")` mounted into the proxy only, placeholder and base URL set by the runner, unlink after READY, tmpfs-only base; tests A-2, A-5, A-7, A-8, A-9 | A2 |
| A4 | Stage-1 generalisation: non-local channels, `--zdr off`, `ExecutionGrant.zdr/retention`, input manifest, audit fields; tests A-6, A-10 | A3; also the prerequisite for Stage 2 (section 2.3) |
| A5 | `agy_cli` lane under option A, or wait for a proxy design | D4, Antigravity quota |
| A6 | First real, user-approved sweep with the dedicated token (EXP-2) | A4, D2, D3, D5, D6, D7 and the user's typed approval |

### 2.9 Open decisions

Items D1 to D8 are the "Decision needed from the user" list of the design note, unchanged in substance. D9 and D10 are added by this RFC.

| Id | Decision | Owner | Recommendation | Blocks |
| --- | --- | --- | --- | --- |
| D1 | Approve option B for `claude`, with 2b as fallback if the spike shows the CLI refuses OAuth with a custom base URL | User | Yes | A2 |
| D2 | Allow step A1: run the real `claude` binary in the container against a STUB endpoint with a placeholder token (no credential, no vendor traffic) | User | Yes | A1 |
| D3 | Create a DEDICATED token with `claude setup-token`, yourself at a terminal; say where its scope and lifetime are shown and how it is revoked | User | Yes; both are undocumented in `--help` | A6 |
| D4 | `agy`: accept option A (token readable by model code, probably with a refresh token) for open synthetic content, or use a dedicated Google account for that lane, or wait for a proxy-injected design (needs HTTP/2 and a known base-URL setting) | User | Dedicated account, or wait | A5 |
| D5 | Confirm automated `claude -p` and `agy --print` sweeps are acceptable on the user's subscriptions, and set caps: sessions per sweep, and no sweep when the 5h or 7d bucket is under 20% | User | Cap and gate on quota | A6 |
| D6 | `zdr=off` approval shape: separate flag, changed phrase `zdr-off open-only`, manifest-only inputs, or lighter or stricter | User | As proposed in section 2.6 | A4 |
| D7 | Rotate the dedicated token after every sweep, or per series of sweeps | User | Per series, and on any anomaly | A6 |
| D8 | Read the `claude gateway` documentation before building, in case it gives vendor-supported header injection | Claude Code (read), User (accepts) | Yes, before A2 | A2 design |
| D9 | Stage 2 key delivery: use the credential route of Part A, or a dedicated OpenRouter key with a provider-side limit mounted as a file, or accept the key readable by model code for the experiment | User | Credential route if A3 lands first; otherwise a capped dedicated key | EXP-1 (Experiments plan) |
| D10 | Where the design note lives: keep it under `~/.agents` only, or copy it into `docs/` once this RFC is accepted so reviewers (including `@Doc-Cr`) can read the long form | User | Copy after acceptance | Review |

---

## 3. Part B — sbx for Model Sessions

### 3.1 What exists (PR #5, documented in the harness2 README section "sbx model route")

- `sbx_session.py` provides `SbxSessionRunner`, a `ProposerSessionRunner` subclass with the same `authorize`, budget, observer, `SessionResult` and `SessionCleanupError`. The selection function is `choose_model_runtime`; `make_runner` returns Docker or sbx and never falls back from sbx to Docker.
- Opt-in only: `COGNITIVE_HARNESS2_MODEL_SANDBOX=sbx`. Unset, empty or `docker` is rootless Docker. Any other value is refused, and `auto` does not exist for model sessions.
- Selection requires: an sbx version in `VERIFIED_SBX_MODEL_VERSIONS` (v0.50.1), `/dev/kvm` and the daemon working, a global policy with an active `**` deny for tcp and udp and no global allow rule, and both host-reach settings off (`ssh.agentForwardingEnabled`, `browser.enabled`; the user set both off on 2026-10-31).
- Route: the SAME `egress_proxy.py` runs as a host process on a random `127.0.0.1` port with the same unix route to the host bridge, the same five-pair allow list and the same model pin. The VM reaches it as `host.docker.internal:<P>` through the sbx proxy. Per sandbox exactly five HTTP allow rules are added, scoped with `--sandbox NAME`, and are removed with the sandbox. The global policy is never changed.
- Live-verified on sbx v0.50.1 on 2026-11-01 against a STUB model and the real `opencode` v2.0.25: allowed requests 200, management paths and a foreign model denied, no other host or port reachable, IPv6 loopback not reachable, teardown clean, global policy identical before and after.
- Review: two read-only passes on Opus 5.5 (verified from transcripts), a third round of fixes applied. Fixes made after the Opus passes have not been reviewed by a stronger model (RFC 004 section 9.10). The first "Fable 5.1" pass was actually served by Sonnet 5.5 (corrected in RFC 004 section 9.10).

### 3.2 The gap to making it the default

| Gap | What closes it | Who |
| --- | --- | --- |
| Never run on a real model. Latency over the sbx proxy, a long streamed reply from `gemma4-e4b-16k` with thinking on, and context size are unmeasured | A real stage-1 run on sbx (Experiments plan, EXP-3) | User (the approval gate needs a human terminal) |
| `stage1 plan` still requires rootless Docker and the proposer image to pass, although the sbx route uses neither | Make those checks conditional on the selected runtime, or accept dual requirements | Claude Code |
| Isolation is a different shape from Docker (section 3.3) | A written decision that the shape is acceptable, with the flip criteria below | User |
| `VERIFIED_SBX_MODEL_VERSIONS` holds one version | Any sbx upgrade re-runs the IPv6 and policy experiments before the version is added | Claude Code |
| A hard kill leaves the sandbox and its five rules until the next run's stale sweep | Accepted; the sweep exists and is tested with fakes | none |
| No stronger-model review of fixes after round 3 | One more read-only pass (Checks plan, item-6 leftovers) | User decides; Claude Code runs |

### 3.3 Isolation-shape difference

| Property | Docker route (default) | sbx route (opt-in) |
| --- | --- | --- |
| Boundary | Container: read-only root, `--cap-drop ALL`, `no-new-privileges`, user 65534, pids, memory and cpu cgroups | microVM: a stronger boundary |
| User inside | 65534, no sudo | `agent` (uid 1000) with passwordless `sudo` and group `docker` |
| Root filesystem | Read-only; tmpfs `HOME` and `/tmp`, `noexec` | Writable overlay |
| Process limit | pids cgroup, enforced | `ulimit -u` only: advisory, and `sudo` can lift it |
| CPU and memory | cgroups per container | VM-level `--cpus` and `--memory` |
| Network | `--internal` network, no default route; only the proxy container is reachable | Global deny-all plus five HTTP allow rules for `localhost:<P>`; the sbx proxy applies a coarse path match as a second layer under the exact filter of `egress_proxy.py` |
| Workspace | Bind-mounted read-write at the same absolute path; files owned by a sub-uid, `reclaim_workspace` fixes ownership | virtiofs read-write at the same absolute path; files appear owned by the host user, no reclaim step |
| Timeout | Container killed | Host kills the `sbx exec` client AND runs `sbx rm --force` (killing the client alone does not stop the in-VM process) |
| Cost per session | A whole contained session against a stub model took 2.9 to 3.3 s | About 20 s against a stub, including create and a 200 MB copy-in of `opencode` |
| Leftovers | Labelled containers and networks; `reap_all` | Sandbox named `h2m-...` with an owner proof; stale sweep at the start of every `stage1 run` and every session |

Reading: sbx trades a hardened, minimal container for a stronger outer boundary and a permissive inside. The inside permissiveness matters only for what the session can do to itself and its workspace, which is the same exposure as Docker; it does not widen what is reachable from outside.

### 3.4 Flip-the-default acceptance criteria

The default changes only if all of these hold, measured on the user's host and recorded in the Experiments plan (EXP-3):

- [ ] **B-1** A real stage-1 sweep on the hard task set completes on the sbx route with `status ok`, all sessions exit 0, and no session hits the 600 s timeout.
- [ ] **B-2** Teardown is clean: `sbx ls -q` lists no `h2m-` sandbox, `sbx policy ls --json` is identical to its pre-run snapshot, no host proxy process and no `h2s-*` or `h2bridge-*` directory remain, and the report's `teardown` block is empty.
- [ ] **B-3** The median session wall time is at most 2.0 times the Docker median from the reference runs (proposal; the user confirms the factor). Reference values from run `d08d`: about 29.5 s per solver session and 38 s per proposer session.
- [ ] **B-4** No session ends with an error event caused by stream truncation, EOF or a proxy timeout; the longest streamed reply completes.
- [ ] **B-5** Every denied request is of a pre-registered kind (Experiments plan EXP-3 table); none comes from the sbx layer for an allowed path.
- [ ] **B-6** The `stage1 plan` preflight no longer requires Docker or the proposer image when sbx is selected, or the dual requirement is accepted in writing.
- [ ] **B-7** One read-only review pass on the code changed since the last Opus pass, by the strongest model the subscription offers, with no open HIGH finding.
- [ ] **B-8** The user accepts the isolation shape of section 3.3 in writing.
- [ ] **B-9** Both host-reach settings are off at the time of the run and the restart-pending heuristic passes.

If B-1 to B-9 hold, the change is a small commit that makes sbx the default for model sessions, keeps `COGNITIVE_HARNESS2_MODEL_SANDBOX=docker` as a forced override, and keeps the loud failure when sbx is selected but unusable (no silent fallback).

### 3.5 Rollback

- **Immediate, no code change**: `COGNITIVE_HARNESS2_MODEL_SANDBOX=docker` (or unset, while the default is still Docker). Docker route code is untouched by the sbx route (the image tag did not change).
- **After a default flip**: revert the single flip commit. A merge commit workflow (never squash) keeps that revert clean.
- **Leftovers**: the next `stage1 run` runs the stale sweep; a manual check is `sbx ls -q` for names starting `h2m-` and `sbx policy ls --json` against the saved pre-run snapshot. Remove a sandbox only with `sbx rm --force <exact name>`.
- **Triggers**: any leftover that the sweep does not clear, any policy that differs from the snapshot, any session that reaches a host other than the one proxy port, or a latency factor above the B-3 threshold on two consecutive runs.

---

## 4. Part C — Dependency and Blocker Table

### 4.1 Where things stand (checked 2026-11-02)

- PRs #3 to #10 are all open and unmerged. The agreed merge order (user) is #3, #4, #8, #9, #5, #6, #10, #7, with merge commits only and children retargeted to `main` after their parents merge.
- PR #5 (`feat/stage1-integration`, head `9f12381`) contains the heads of #8 and #9 (verified with `git merge-base --is-ancestor`), so it carries Track 2 and the conformance runner until those land. It does not contain #10.
- PR #6 (`feat/model-bench`) is 69 commits behind PR #5's head and adds only `tools/bench` (23 files); it is disjoint from everything in this RFC.
- PRs #3 and #4 touch `packages/jev`, `adapters/` and `tools/probes`, not `packages/harness2`. Their only overlap with #5 and #7 is documentation: a dry-run merge (`git merge-tree`) of the plan and RFC 002 conflicts after #3 and #4 are merged and then any docs-carrying branch is merged.

### 4.2 Work items

"Start now" means: the work can begin today on a branch cut from `feat/stage1-integration` and be reviewed against it. It does not mean it can merge today.

| Item | Blocked by PRs | User gates | External factors | Start now on a branch stacked on #5? | What would conflict |
| --- | --- | --- | --- | --- | --- |
| D8: read `claude gateway` docs | none | accepts findings | none | Yes (read-only, no branch) | nothing |
| A1: spike with stub | none | D2 (run the real binary) | Claude CLI behaviour with a custom base URL: UNVERIFIED | Yes: new test scripts only | nothing |
| A2: credential route in `egress_proxy.py` | merge: #5 (and #8, #9 before it) | D1; A1 result | none | Yes | Proxy source and image tag; conflicts only if #5's head changes the same file (merge #5 into the branch). New tag, image rebuild |
| A3: runner plumbing in `session_runner.py` | merge: #5 | none beyond A2 | none | Yes | `session_runner.py` is touched by #5's review fixes; keep commits small and merge #5's head often |
| A4: stage-1 generalisation (`stage1.py`, `channels.py`, `ExecutionGrant`) | merge: #5 | D6 | none | Yes, but the highest conflict risk: `stage1.py` is 2049 lines and the hot file of #5 | Any further #5 fix touching `stage1.py`; plan A4 as the last commits and rebase onto the merged #5 |
| A5: `agy_cli` lane | merge: #5 | D4 | Antigravity quota is out (no date): a real `agy` run is blocked; stub-based tests are not | Partly: stub tests yes, real run no | same files as A4 |
| A6 / EXP-2: first real subscription sweep | merge: #5 (so the sweep runs from `main`) | D3, D5, D7, typed approval | Claude subscription capabilities; shared quota; vendor terms; no Fable on the subscription | No | n/a |
| B1 / EXP-3: real stage-1 run on sbx | none to run; the code is in #5 | The user runs it at a terminal | sbx v0.50.1 only; both host-reach settings off | Yes: from a checkout of `feat/stage1-integration` | nothing |
| B2: make the Docker preflight conditional under sbx | merge: #5 | none | none | Yes | `stage1.py` (same risk as A4) |
| B3: flip the default | merge: #5; B1 success | B-8 acceptance | none | No: decided after B1 | one small commit in `sbx_session.py` and the stage-1 wiring |
| Docs: update RFC 004 sections 6.2, 7, 8, 9 after A or B land | merge: #7 | none | none | Stack on `docs/future-plans` | RFC 004 and the plan are also edited by #3, #5, #7 (the two-file docs conflict) |

### 4.3 Statements

- **Part B can start today.** The code is in PR #5; the only thing missing is the user's real run. It needs no merge.
- **Part A can start today up to and including the stub spike and the proxy credential route.** A4 (stage-1 generalisation) can be written on a branch stacked on #5, but it will conflict with any further change to `stage1.py` on #5, so it should be the last piece and be rebased onto merged #5.
- **Nothing in this RFC can merge before #5.** And #5 cannot merge before #8 and #9 under the agreed order, because it carries their commits (merge commits keep stacked branches valid, so a branch stacked on #5 survives its parents merging).
- **#3 and #4 do not block any code in this RFC.** They block only the documentation merge order (the shared two-file conflict) and Track 1 consumers.
- **#10 and #6 do not block anything here.** #10 is Lookup/SMB; #6 is `tools/bench`.
- **#7 (docs, last in the order) blocks the repository text of RFC 004 section 9.** This RFC and its companions are stacked on #7, so their cross-references to RFC 004 section 9 resolve only after #7 merges.
- **External blockers that no merge removes**: Antigravity quota (blocks real `agy` runs and Antigravity's own review of `374b790..9f12381`); the Claude subscription's capabilities (no Fable; `setup-token` scope and lifetime undocumented in `--help`); and whether the Claude CLI accepts OAuth with a custom base URL (UNVERIFIED until A1).
- **Track 4 (RFC 005) is not blocked by this RFC and does not block it.** RFC 005 gate 3 ("Harness2 completes candidate probing in local sandboxes and confirms fail-closed behaviour") concerns model-free `sbx` and Docker execution, which this RFC does not change. RFC 005 gate 3 is still unticked in the repository text; whether it can be ticked is a Track 4 question and is not decided here.

---

## 5. Cross-References

- RFC 004 section 6.2 (stage 3 definition), section 7 (stage 3 acceptance: the current text says "execute without leaking credentials outside container"; the design in this RFC strengthens it to "never in the session container"), section 8 item 9 (token delivery, the unsolved assumption), sections 9.5 to 9.7, 9.9, 9.10, 9.11.
- RFC 005 section 2.5 (channel retention levels, "declared, not verified").
- Plan sections 3.3.2, 3.3.3 and the Stage 2 / 3 matrix row.
- [Checks plan](../plans/2026-11-02-checks-plan.md): merge checklist, item-6 leftovers, and the acceptance checks A-1 to A-11 and B-1 to B-9 as rows.
- [Experiments plan](../plans/2026-11-02-experiments-plan.md): EXP-2 (Stage 3) and EXP-3 (sbx real run).
