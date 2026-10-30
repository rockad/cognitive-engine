# cognitive-harness2

System 2 for Cognitive Engine (RFC 004): a failure-driven Propose-Probe-Compose prompt optimizer behind a strict, loopback-only MCP server. Python 3.12, `uv`, Apache-2.0.

```bash
uv run cognitive-harness2 serve                 # http://127.0.0.1:8120/mcp
uv run cognitive-harness2 serve --stdio
uv run cognitive-harness2 experience pending    # lessons an agent staged
uv run cognitive-harness2 experience approve ID --by NAME
```

## Tools

| tool | what it does |
| --- | --- |
| `harness2_propose` | Generate mutation candidates. Refuses protected or unknown components and any endpoint the ZDR/classification policy forbids **before** anything is sent. `dry_run` reports the verdict without calling a model. |
| `harness2_probe` | Run each candidate in an ephemeral rootless Docker container (by default), or in a local `sbx` microVM only when opted in, verified and usable (below). With neither it **fails closed** and runs nothing; `allow_worktree_diff` opts in to static diffs, which execute no code. |
| `harness2_compose` | Contrast outcomes without labels and propose a patch. Output always has `requires_human_approval: true`. |
| `harness2_reflect` | Stage a lesson as **pending**. Nothing is committed. |
| `harness2_trigger` | Read-only pull check: evaluates a failure summary and recommends whether to probe, with readiness, cost class and `requires_human_approval` (see Triggers). Starts nothing. Not in the RFC 004 §2.7 list; it is the pull tool of §2.3. |
| `harness2_status` | Read-only, offline readiness report (sandbox and the `sbx` verdict, key configured, storage, protected set). Not in RFC 004; added for Track 4's Gate 3 and the conformance probe. |

## Rules enforced, and where

- **Protected set** (`invariants.py`): only `prompt`, `tool_description`, `context_assembly` and `decomposition_strategy` are mutable; everything else, and every unknown name, is refused. Protected paths (hooks, hook, guard, egress, ZDR, governance, settings, approval, and git control paths: `.git`, `.gitmodules`, `.gitattributes`, `.github`) are refused after normalisation (NFKC, case folding, separators, `.` and `..` segments): the protected files, the protected directories themselves, and everything under them, plus `../`, absolute and drive-letter escapes and control or invisible characters.
- **Triggers** (`triggers.py`): probing starts on a failed verification (`jev_judge` FAIL or score < 0.50), 3+ consecutive failures, or an explicit request. Routing confidence is accepted and ignored. See Triggers below.
- **Egress** (`egress.py`): under `zdr=strict` only a loopback or explicitly listed zero-retention host, never a free-tier model. Unknown classification is `restricted`.
- **Quota** (`quota.py`): `GET /api/v1/key` before an OpenRouter run; fails closed on no key, error, or an unreadable response.
- **Experience** (`experience.py`): stored under `~/.local/share/cognitive-engine/experiences/<domain>/`, never inside the checkout. An agent can stage; only the CLI, with a named human, can commit.

## Triggers: advisory push, explicit pull

System 2 never starts by itself. Two things can tell you it might be worth running; neither runs it.

**Pull: `harness2_trigger`.** Send a failure summary, get a recommendation. It is read-only: it starts no sandbox, calls no model, writes no file and creates no run. The closed argument schema takes `on_demand`, `judge_passed`, `judge_score`, `consecutive_failures`, `failure_signatures` (up to 20 short fingerprints, not stored or echoed), `zdr`, `classification` and `check_quota`. The answer carries:

- `advise`: a trigger fired. It says nothing about readiness.
- `trigger` and `reason`: which rule fired (`explicit`, `judge_failed`, `judge_score` or `retries_exhausted`), or `null` when none did.
- `ready`: tri-state over the four readiness checks.
  - `false`: at least one check is known-bad. `blockers` says which.
  - `true`: every check is positively ok.
  - `null`: none is known-bad, but at least one is unchecked or unknown (for example quota without `check_quota`). A known-bad check always wins over an unchecked one.
- `readiness` and `blockers`: sandbox (the runtime is constructed and checked, nothing is run), egress and ZDR verdict, quota, and whether a generator model is configured. `blockers` lists only the known-bad ones. Quota is pre-flighted only when `check_quota` is true and the endpoint is OpenRouter; otherwise it is reported as not checked, which makes `ready` `null` rather than `true`.
- `cost_class`: `none`, `local` (loopback endpoint), `free_quota` or `metered`.
- `requires_human_approval`: always `true`.

Example: three failures on an OpenRouter endpoint, quota not checked. A trigger fired, nothing is known-bad, but quota is unknown:

```json
{"advise": true, "trigger": "retries_exhausted", "reason": "retries exhausted (3 >= 3)",
 "ready": null, "blockers": [], "cost_class": "metered", "requires_human_approval": true, "read_only": true,
 "readiness": {"sandbox": {"ok": true}, "egress": {"ok": true}, "quota": {"ok": null, "status": "not_checked"}, "model": {"ok": true}}}
```

The rules are `triggers.should_probe` (RFC 004 §2.3): `jev_judge` FAIL or score below 0.50, 3 or more consecutive failures (a trailing run of identical `failure_signatures` counts), or an explicit request. `routing_confidence` is not an input. There is no stagnation trigger, and the closed schema rejects the old `attempts_without_progress` argument.

**Push: `hooks/harness2-advise.sh`.** A Claude Code hook (bash and `jq`) that counts consecutive tool failures per session and, at the limit, adds advisory context pointing at `harness2_trigger`:

- `PostToolUseFailure` counts one failure (a user interrupt is not counted) and advises at 3, then again at 6, 9 and so on.
- `PostToolUse` (a tool succeeded) and `UserPromptSubmit` (a new task) reset the count.
- `Stop` shows the user a one-line suggestion if the limit was reached. It does not reset.
- It never blocks, never exits non-zero (bad input, a missing `jq`, an unwritable state directory all exit 0), makes no network call, and writes only a count: one short numeric token line per failure in `<state>/<session_id>.count` (each failure takes its own line number as its position, so a burst of simultaneous failures still advises exactly once per multiple of the limit, with no lock), mode 700, no prompt text, tool input, error text or secrets. Session ids that are not `[A-Za-z0-9_-]{1,64}` are ignored. A state file or state directory that is a symlink is never read, appended to, reset or created through (the hook exits 0 and does nothing). Counts older than 7 days are pruned.
- `COGNITIVE_HARNESS2_HOOK_STATE_DIR` overrides the state directory (default `$XDG_STATE_HOME` or `~/.local/state`, then `cognitive-engine/harness2-hook`). The script's `RETRY_LIMIT` is pinned to `triggers.RETRY_LIMIT` by a test.

It is **documented, not installed**. To enable it, merge this into `~/.claude/settings.json` or a project `.claude/settings.json` (not the Jev adapter's `hooks.json`), replacing `<repo>`:

```json
{
  "hooks": {
    "PostToolUseFailure": [{ "hooks": [{ "type": "command", "command": "<repo>/packages/harness2/hooks/harness2-advise.sh" }] }],
    "PostToolUse":        [{ "hooks": [{ "type": "command", "command": "<repo>/packages/harness2/hooks/harness2-advise.sh" }] }],
    "UserPromptSubmit":   [{ "hooks": [{ "type": "command", "command": "<repo>/packages/harness2/hooks/harness2-advise.sh" }] }],
    "Stop":               [{ "hooks": [{ "type": "command", "command": "<repo>/packages/harness2/hooks/harness2-advise.sh" }] }]
  }
}
```

Leave out `onFailure: "block"`: without it a crashing hook is fail-open (verified in the captured payload notes), which is what an advisory wants. `PostToolUse` fires on every tool call, so the script exits early when there is no count to reset.

## Sandbox: rootless Docker, else nothing (sbx is opt-in)

`harness2_probe` runs candidate code only in a local sandbox, chosen in this order:

1. `sbx` microVM, **only** when opted in, verified and usable (see "sbx (Docker Sandboxes)" below). `sbx` being on `PATH` changes nothing.
2. An **ephemeral rootless Docker container**, otherwise. This is the default. It is used only if every one of these holds, checked on every run:
   - `docker` exists and the endpoint (`DOCKER_HOST` or the active context) is a **local unix socket**.
   - `docker info` lists `name=rootless` in its SecurityOptions. A rootful daemon is refused.
   - The pinned image (`alpine:3` plus digest, in `sandbox.DEFAULT_IMAGE`) is already local. It is never pulled at run time.
   - The verified endpoint is **pinned**: it is resolved once, and every docker call after the check (`info`, `image inspect`, `run`, `kill`, `rm`) names it with `-H unix://<socket>` and runs with `DOCKER_HOST` and `DOCKER_CONTEXT` removed from the environment. An ambient `DOCKER_HOST` or `DOCKER_CONTEXT` changed after the check cannot redirect the run to another daemon.
3. Neither: **fail closed**. A missing `docker`, an unreachable or stopped daemon, a rootful or remote daemon all raise `SandboxUnavailable` and nothing runs. There is no fallback to a rootful daemon or to the host. The only remaining option is the explicit `allow_worktree_diff` opt-in, which executes nothing.

Each container gets `--network none`, `--read-only`, `--cap-drop ALL`, `--security-opt no-new-privileges`, pid, memory and cpu limits, a `noexec` tmpfs for scratch, user `65534`, the candidate mounted read-only at `/candidate`, and `--rm`. The home directory and the Docker socket are never mounted and no host environment is passed in. Candidate file names must already be clean relative paths: `./payload.py`, `a//b`, `a/./b`, `a/../b`, absolute paths, backslashes, empty names, trailing slashes, NUL and other control characters are refused as written, before anything is staged, and files are staged under exactly the name given. On timeout the container is killed and removed; output is capped at 1 MiB per stream.

Host setup is rootless Docker (`dockerd-rootless-setuptool.sh install`), then `docker pull alpine:3` once. The integration tests (`-m requires_rootless_docker`) run real containers and **do not skip**: without a rootless daemon they fail.

### sbx (Docker Sandboxes)

`sbx` is Docker Sandboxes: a standalone CLI that runs AI agents in local microVMs (`sbx create`, `run`, `exec`, `policy`, `daemon`, `diagnose`). RFC 004 names it as the preferred isolation.

**It is not selected by default**, because `SbxRunner` has not been verified end to end against a live microVM (see "Live findings" below: on the host it was written on, the sbx daemon could not create a VM), and an unverified runner must not decide where candidate code runs. Until now having `sbx` on `PATH` was enough to select it; it no longer is. `default_runner` returns `SbxRunner` only if **all** hold:

- You opted in: `COGNITIVE_HARNESS2_SANDBOX=sbx`. Unset, empty, `auto` and `docker` mean rootless Docker. Any other value is refused (fail closed), not ignored.
- The installed version is in `sandbox.VERIFIED_SBX_VERSIONS`. **It is empty**, so `sbx` is unselectable today.
- A read-only usability probe passes: `/dev/kvm` is accessible to the user, the `sbx` daemon is reachable, and the global network policy is initialised **and deny-all** (an active `**` tcp+udp deny rule, and no network allow rule at all). The probe never starts the daemon: `sbx version` and `sbx policy ls --json` run only once the daemon socket already answers.

Anything else gets rootless Docker exactly as above, and with no usable Docker either, the usual fail-closed refusal. There is no fallback to rootful Docker or to the host.

`harness2_status` keeps its `sandbox` string (the selected runner, or the refusal) and adds an `sbx` object: `state` (`absent`, `present-but-unusable` or `usable-and-selected`), `path`, `version`, `opted_in`, `verified_versions` and `reasons` (for example: not opted in, version not in the verified set, daemon not reachable, `/dev/kvm` permission denied, policy not initialised).

To enable it, the user does (these change the host, so they are yours to run):

1. Give the user `/dev/kvm` access: `sudo usermod -aG kvm "$USER"`, then log out and in again.
2. Start the daemon: `sbx daemon start`.
3. Initialise the global network policy: `sbx policy init <allow-all|balanced|deny-all>` (pick the profile deliberately).
4. **The missing step:** run the live test from a shell and daemon that really have the `kvm` group: `COGNITIVE_HARNESS2_SBX_LIVE=1 uv run pytest -q packages/harness2/tests/test_sbx_runner.py -m requires_sbx_live`. It creates, uses and removes throwaway `harness2-*` sandboxes (no model, no workspace mount, `shell` agent only) and asserts exit codes, stdout/stderr, a failing network call, and an empty `sbx ls` afterwards. Only if it passes, add the version to `VERIFIED_SBX_VERSIONS`. Only then does step 5 do anything.
5. Export `COGNITIVE_HARNESS2_SANDBOX=sbx` for the daemon.

#### How `SbxRunner` runs a candidate

`preflight` (the daemon socket is a socket owned by this user at the path `sbx daemon status` reports; `sbx version` is in the verified set the runner was given; `sbx policy ls --json` is deny-all) -> `sbx create shell --name harness2-<hex> --no-progress --cpus N --memory M --pull missing --skills off` (no PATH: **no host bind mount**) -> `sbx ls -q` lists it -> `sbx policy check network --sandbox NAME --json` must deny `example.com:443`, `example.com:80`, `127.0.0.1:80` and `localhost:80` -> `sbx cp <stage>/candidate NAME:/tmp` and an `sbx exec find` that must list exactly the staged files -> `sbx exec -w /tmp/candidate -- NAME CMD...` under a host-side wall clock (the client process group is SIGKILLed on timeout) -> **always**, in a `finally`, `sbx rm --force NAME`, then `sbx ls -q` must not list it (a sandbox that stays is an error). The staging-key rules are `DockerRunner`'s (`_validate_stage_names`, checked before any process starts). Every `sbx` call is an argv list without a shell, with stdin closed, and with every `DOCKER_SANDBOXES_*` and `SBX_*` variable removed from its environment (the CLI reads its settings from them); nothing from the host environment is passed into the VM. Any surprise (unknown version, unparseable output, a policy that is not deny-all, `sbx exec` exiting 125, which `sbx` uses for "could not run it", a leaked sandbox) raises `SandboxUnavailable`: nothing runs on the host and there is no fallback.

#### Live findings (sbx v0.50.1, Linux, 2026-10-30)

"Observed" means run against the real CLI and daemon on the development host. **The daemon could not start a VM there**: `sbx create` reached the container start and failed with `Hypervisor error: KVM error: Permission denied (os error 13)` because the `sbx daemon start` process had the groups `4 24 27 30 46 100 985 986 1000` and **not `kvm` (991)** while `/dev/kvm` is `root:kvm 0660` (`sbx diagnose`: "Virtualization — not accessible"). Everything that needs a running VM is therefore **not observed**.

| Operation | Real command | Observed |
| --- | --- | --- |
| version | `sbx version` | exit 0, `v0.50.1 <commit>` |
| global policy | `sbx policy ls --json` | exit 0, `{"rules":[{id,name,resource_type,decision,resources,status,actions,...}]}`; here `default-deny-all` (`network`, `deny`, `**`, actions `net:connect:tcp` + `net:connect:udp`, `active`) plus two filesystem allow-all rules |
| policy check | `sbx policy check network --json [--sandbox N] TARGET` | a denied target prints `{"allowed":false,...}` and **exits 1**; a missing sandbox exits 1 (`404 ... not found`) |
| create | `sbx create shell --name N --no-progress --cpus 1 --memory 512m --pull missing --skills off` | every flag accepted; on this host exit 1, `request failed: 500 Internal Server Error: failed to run sandbox container` (daemon log: the KVM error above); the failed sandbox is not listed afterwards |
| create, bad name | `sbx create shell --name 'bad name'` | exit 1, `name must match regexp ^[a-zA-Z0-9][a-zA-Z0-9.-]+$` |
| list | `sbx ls -q` / `sbx ls --json` / `sbx ls` | names one per line, empty output and exit 0 when none / `[]` / `No sandboxes found` |
| exec, missing sandbox | `sbx exec NAME CMD` | **exit 125**, `error: sandbox 'NAME' not found` |
| cp, missing sandbox | `sbx cp SRC NAME:/tmp` or `sbx cp NAME:/tmp/x DST` | exit 1, same message |
| rm, missing sandbox | `sbx rm [--force] NAME` | exit 1, same message |
| exec in a running VM: exit code, stdout/stderr, 127 for a missing command, cwd `-w` | `sbx exec -w DIR -- NAME CMD...` | **not observed** (documented: exit status is the command's, 125 = sbx could not run it, 127 = missing command) |
| cp in and out, directory, symlink out | `sbx cp` | **not observed** (documented: a directory is placed inside an existing destination directory; links leading out of a workspace are refused) |
| wall clock | host-side kill of the `sbx exec` client, then `sbx rm --force` | **not observed**; whether the in-VM process survives the client dying is unknown, which is why `rm --force` always follows |
| network from inside under deny-all | `wget`/`curl` in the VM | **not observed**; only the policy engine's verdict (`policy check`: example.com, 127.0.0.1, localhost all denied, "no matching allow rule (default deny)") |
| filesystem (read-only root? where can a command write?) | | **not observed** |
| resource limits | `--cpus N`, `--memory M` (min 512m) on create | flags accepted; enforcement **not observed** |

Remaining caveats:

- **kvm.** The user running `sbx daemon start` (or the login shell it is started from) must really have the `kvm` group: check with `grep Groups /proc/$(pgrep -u "$USER" -f 'sbx daemon start')/status`. Starting it from `sg kvm` or a fresh login fixes it; this module never starts, stops or restarts the daemon.
- **The first `create` may pull the agent image.** That is done by the daemon, not by the candidate, and it needs network access outside the sandbox policy; `--pull missing` avoids repeated pulls.
- **Settings the policy check does not cover** (read from `sbx settings ls`, defaults on this host): `ssh.agentForwardingEnabled=true` (sandboxes may list and use host SSH agent keys when one is forwarded) and `browser.enabled=true` (sandboxed processes may open URLs in the host browser). Set both to `false` (`sbx settings set ...`) before relying on sbx for hostile code; neither is changed or checked by `SbxRunner`.
- The deny-all check is the policy engine's answer, not a measurement of the VM's network; the live test measures one real connection attempt.
- `SandboxSpec` has no artefact-collection field, so nothing is copied out of the VM; only stdout, stderr and the exit code come back.

## Upstream

Upstream [google-research/harness2](https://github.com/google-research/harness2) (Xu et al. 2026, [arXiv:2610.14224](https://arxiv.org/abs/2610.14224), Apache-2.0) is the primary source. It is pinned by commit SHA in `pyproject.toml` (`[tool.uv.sources]`, extra `upstream`) and locked in `uv.lock`; to adopt a new upstream version, move the `rev` and run `uv lock`. It is not vendored. *Promptbreeder* (Fernando et al. 2023) and *Large Language Models as Tool Makers* (Cai et al. 2023) are related work only. See `NOTICE`.

## Upstream seams (slice 3a)

Four safety seams sit between this engine and upstream's `Substrate` and `Proposer`. **Nothing here runs a model, starts a container or opens a connection unless an `ExecutionGrant` says so (see the solver section below); with no grant, execution is refused.** Every seam is default-deny and fails closed, and each is importable without the `upstream` extra (upstream is read by duck typing or imported lazily).

| module | guards |
| --- | --- |
| `upstream_surface.py` | Maps upstream's `SurfaceSpec` components c1..c8 onto `invariants.MUTABLE`. Mutable: c1 system prompt (`prompt`), c3 memory (`context_assembly`), c4 tool guidance (`tool_description`), c5 subagents (`decomposition_strategy`, body only), c6 skills (`context_assembly`, body only). Denied: c2 guardrails (protected by slot denial and the `guardrails` path stem), c7 scripts (executable code), c8 plugins and hooks (protected), codex's single `AGENTS.md` (it folds in the guardrails), and every unknown slot. `check_surface(spec, request=, tree=)` raises `InvariantViolation` for any slot or path outside that set, or failing the path rules. `check_mutation(slot, path, old, new)` enforces frontmatter freezing, UTF-8 validity, size limits, and absence of control characters or body frontmatter blocks. |
| `upstream_egress.py` | `check_proposer(config, profile=, channel=, zdr=, classification=)` decides, before any session exists, whether an upstream Proposer may be created. Returns a `Decision` (allowed, reasons, warnings, redacted view). The Proposer configuration must match a channel declared in the profile exactly (kind, provider, endpoint host and port, model), and that channel must be allowed by the zdr mode and the sweep's classification (see **Channel profile**). Also refuses Vertex and any hosted cloud route, an endpoint that cannot be determined (upstream's Proposer has none: it must be declared), a hosted provider behind a `local` channel, inherited credential-like env vars (by name only), any proxy variable (names containing PROXY, such as NO_PROXY: refused outright, since the runner sets proxies itself), `*_BASE_URL` vars that are not a declared endpoint, a command line that is not exactly the expected argv for the kind (a per-kind flag allow-list, `COMMAND_SHAPES`: any other flag, `--`, `@file` or endpoint-looking value is refused), and any working directory that is, or resolves through symlinks to, a protected path or that is not under a `workspace_roots` entry of the profile. Every reason names the channel and the rule that fired. No secret value ever reaches a reason or the view. |
| `substrate.py` | `CognitiveSubstrate` implements upstream's `Substrate` Protocol. `surface`, `materialise` (read-only staging through `DockerRunner`'s key rules; no symlinks, UTF-8 only, capped) and `preflight` (`require_rootless_docker`, then a quota check only if the caller passes `quota_check=`) work and start nothing. `read_trajectory` is pure code. `execute` runs a contained solver session only with an `ExecutionGrant` (see "Solver substrate and sum-signed (slice 3b)"); without one it raises `ExecutionRefused`. |
| `sweep_plan.py` | `plan_sweep(...)` returns an immutable `SweepPlan` (candidates, hard caps, upstream's ledger budget P = 3k and R = 4k, `advice.cost_class`, readiness, `requires_human_approval: true`) or raises `SweepRefused` listing every cause: a missing, zero, negative, infinite or oversized cap, a plan over its own cap, a candidate off the mutable surface, a known readiness blocker or quota refusal, or a Proposer decision that is not allowed or was made for another channel, classification or zdr mode. It takes the channel profile, the channel name, an explicit classification (missing is restricted) and the zdr mode, re-checks the channel policy, and caps the plan at the minimum of the requested and the channel caps. It starts no sweep. |

`tests/test_no_side_effects.py` is the proof: it traps subprocess, `os.system`, the exec and spawn family, asyncio subprocesses, sockets, DNS, urllib, http.client and httpx, runs every seam's happy and refusal paths, and asserts nothing was attempted. It also shows the Docker daemon is asked only by `preflight`, with pinned read-only `info` and `image inspect`.

Things worth knowing:

- Importing `harness2.substrate`, `harness2.config` or `harness2.core.proposer` runs upstream's `config.py`, which reads `.env` from the clone directory (or the working directory for a wheel install) into `os.environ`. The seams avoid those imports (a canary test pins this). `harness2.core.tree` and `harness2.core.ledger` are stdlib-only and safe.
- Even an allowed Proposer decision carries warnings: upstream's sessions run agent tools (shell, files) on the host, and `HOME` file credentials (gcloud, `~/.claude`, `~/.aws`) cannot be listed by an environment check. Isolating the proposer session is a slice 3b question.
- Upstream is inconsistent about c-numbers (its README makes guardrails c2, its prompt tables call `guardrails.md` c6). The seams bind to paths.

**The 3b gate.** Slice 3b (see "Contained proposer sessions (slice 3b)" below for the session runner; `DockerRunner` as `Substrate.execute`, one capped dry run, a hard budget cap, `requires_human_approval` kept) is not started and runs only on the user's separate go. Slice 3a is subject to independent review before that.

## Contained proposer sessions (slice 3b)

The proposer's agent CLI (shell and file tools) runs in a **rootless Docker container per session**, never on the host. **No model call and no real CLI is run by this code's tests**: they drive a fake CLI (`tests/fixtures/fake-cli`, a shell script). The real stage-1 run waits for the user's go.

```
 host (rootless dockerd, every call: docker -H unix://<verified socket>, no DOCKER_* in the env)
 ┌─────────────────────────────────────────────────────────────────────────────────────────────────────┐
 │  ProposerSessionRunner ── gate: rootless? channel allowed? proposer decision? image pinned? argv ok? │
 │                                                                                                     │
 │  HostBridge (local channel only)                    private dir 0700                                │
 │    127.0.0.1:11434 ◄──── bytes ◄──── ollama.sock 0600 ──────────────┐ (mounted read-only,           │
 │    (ollama, host loopback)                                          │  PROXY container only)        │
 │                                                                     ▼                               │
 │   ┌─ session container ───────────┐      ┌─ proxy container (egress_proxy.py) ─────┐   egress net   │
 │   │ agent CLI (ro bind mount)     │ int. │ allow-list: exact host:port, deny rest  │ ─────────────► │
 │   │ ro root, tmpfs HOME + /tmp    ├─────►│ resolve once, pin IP, refuse private /  │  (hosted only) │
 │   │ cap-drop ALL, user 65534      │ net  │ loopback / link-local / metadata        │  the profile's │
 │   │ workspace rw at the SAME path │ (no  │ local channel: relay to the unix socket │  endpoint(s)   │
 │   │ credential file (ro, optional)│route)│ log: host:port + decision only          │  only          │
 │   └───────────────────────────────┘      └─────────────────────────────────────────┘                │
 └─────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

| piece | module | what it enforces |
| --- | --- | --- |
| image | `image.py`, `containers/proposer/{Dockerfile,build.sh}` | `ubuntu:26.04` pinned **by digest**; `ca-certificates`, `python3-minimal`; no apt lists; user 65534. The tag is `cognitive-harness2-proposer:<12 hex of sha256(Dockerfile, proxy source)>`, so a tag names its source. A session runs the image **by id**. An unpinned, `latest`, untagged or label-mismatched image is refused; it is never pulled or built at run time. |
| host bridge | `host_bridge.py` | One unix socket (private 0700 directory, socket 0600) forwarded to one fixed **loopback IP literal** and port (names are refused). Connection, byte, idle and connect caps; logs nothing; always cleans up. |
| proxy | `egress_proxy.py` | Default deny on exact `(host, port)`; host normalisation (case, trailing dot, IDNA, ideographic full stops); ambiguous numerics (`127.1`, `2130706433`, `0x7f.1`, `0177.0.0.1`) refused; an IP literal passes only if listed; **resolve once and connect to the IP**; refuses private, loopback, link-local (incl. `169.254.169.254`), multicast, reserved, CGNAT and IPv4-in-IPv6 addresses even for an allow-listed name, and refuses a mixed answer whole; CONNECT and absolute-URI HTTP only (origin form, userinfo, fragments refused; Host pinned, hop-by-hop headers dropped, `Connection: close`); request-line 8 KiB, header block 32 KiB, 100 headers, 10 s for the whole head (slowloris), connection, rate, idle, lifetime and byte caps; the access log is `decision host port rule` and nothing else. |
| runner | `session_runner.py` | The lifecycle and the gate (below). |
| harvest | `workspace_harvest.py` | Reading the workspace back: refuses sockets, FIFOs, devices, symlinks that escape, setuid/setgid/sticky, execute bits, hardlinks, files over 512 KiB, count/depth/total caps, hostile names (control and ANSI characters), protected paths, and any file containing the credential value. |

**What `run` checks before anything is created (fail closed).** The rootless gate against the pinned image id; `channels.decide` recomputed from `zdr` and `classification`; for `opencode` and `claude_cli`, an allowed `upstream_egress.Decision`; a wall clock within the channel's `wall_s`; `argv[0]` equal to the mounted CLI path (`/opt/cli/<name>`), with the rest of argv data and never a shell string; a workspace that is a normalised, symlink-free, non-system directory (never the home directory or an ancestor); an environment of `UPPER_SNAKE` names that cannot override the proxy variables and cannot look like a credential; no credential value in argv or env.

**Session container.** `--read-only`, tmpfs `HOME` (`/home/agent`) and `/tmp` (size-capped, `noexec,nosuid`), `--cap-drop ALL`, `no-new-privileges`, user 65534, pids/memory/cpu limits (defaults 256, 2g, 2 cpus: stage-1 proposals), the CLI bind-mounted read-only, the workspace read-write at the same absolute path, one `--internal` network, `--dns 127.0.0.1`, `HTTP(S)_PROXY` at the proxy container and `NO_PROXY=127.0.0.1,localhost,::1` (the session's OWN loopback only: opencode serves itself on a random loopback port and asks it through a proxy-aware client, which the proxy would otherwise deny; set by the runner, never by the caller; the proxy, the host and every external name are never bypassed). Nothing else is mounted: never the home directory, never the docker socket. The **proxy container** is read-only, cap-dropped and limited too; it is attached to the internal network and, for a hosted channel only, an egress network. The local channel has no egress network at all.

**Credentials** go only into the session container, as a read-only file mounted at a path the caller names (`Credential`), never argv, `-e VALUE`, `docker inspect` or `ps`. The value is redacted from stdout and stderr, and harvest refuses workspace files that contain it. Deviation from a "0400 file": under rootless Docker the container user 65534 is not the file's owner, so the file is `0444` with a random 24-hex name in a `0711` directory inside a `0700` parent, removed in `finally`; the secrecy comes from the private parent and from only the session container mounting it.

**Workspaces.** The container user is not the host user, so the workspace directory must be writable by others (mode `0777`), and files the CLI creates are owned by a sub-uid the host user cannot delete. After the session a one-shot container (no network, only the workspace, `CHOWN`/`DAC_OVERRIDE`/`FOWNER`) gives them to the host user (`reclaim_workspace`, default on). That `chown` also clears any setuid bit the model set.

**Using it.**

```python
from cognitive_harness2 import image, sandbox
from cognitive_harness2.session_runner import ProposerSessionRunner, SessionSpec, Credential

verified = image.gate_for_build()                      # rootless gate; pins the socket
ref = image.resolve_image(verified)                    # checked, by id; raises if absent
runner = ProposerSessionRunner(verified, ref, channel, zdr="strict", classification="open", proposer_decision=decision)
result = runner.run(SessionSpec(argv=("/opt/cli/opencode", "run", ...), workspace="/abs/ws", cli="/abs/path/opencode", timeout_s=600))
# result.stdout / .stderr (capped, escape-stripped, redacted), .egress_log, .harvest, .timed_out
```

For a local channel the CLI must be configured with `runner.local_endpoint` (`http://harness2-proxy.internal:<port>`), because the session's own `127.0.0.1` is empty. That name is NOT in `NO_PROXY`: a proxy-aware client sends the request THROUGH the proxy, whose config routes exactly (`harness2-proxy.internal`, port) and (`127.0.0.1`, port) to the bridge socket (log line `reason=unix-route`); a client that connects to the listener directly lands on the same socket. Measured with the real opencode: it goes through the proxy.

**Rebuild the image.** `packages/harness2/containers/proposer/build.sh` (or `uv run python -m cognitive_harness2.image build`; `status` only checks). It builds from a context of two files with the pinned socket. Any change to the Dockerfile or `egress_proxy.py` changes the tag, so rebuild after editing either; old tags are removed by hand (`docker rmi cognitive-harness2-proposer:<old>`). The base digest is in the Dockerfile; to move it, pull the new base once, edit the `FROM`, rebuild. Packages are installed from the Ubuntu archive at build time, so the digest pins the base, not the package set.

**Measured on this host** (rootless dockerd 29.9.2, rootlesskit `gvisor-tap-vsock --disable-host-loopback`): an `--internal` network has no default route and Docker's resolver does not answer external names on it; the session reaches the proxy by its network alias; a proxy container that is root with every capability dropped can connect to the bridge socket mounted read-only (a non-root container user cannot open a socket owned by the host user, which is container root); host loopback is unreachable from containers by `127.0.0.1`, `172.17.0.1`, `10.0.2.2`, `192.168.127.1` and `host.docker.internal`.

**Not covered, honestly.**

- The CLI sends **whatever it likes to its allow-listed endpoint**, including anything in the workspace or the credential it was given. The proxy limits where, not what. A hostile model with a credential can spend it at that endpoint.
- The host bridge exposes ollama (the whole API, not only generation) to the **proxy container**, and the proxy relays any session traffic to it. Other host loopback services stay unreachable.
- DNS to the allow-listed name is resolved by the proxy container through Docker's resolver, so the allowed hostname (and nothing else) is visible to the host's resolver.
- The egress side trusts the container runtime's networking; a kernel or runc escape defeats all of this (rootless limits the blast radius to the host user).
- Output is stripped of escape sequences and control characters and redacted by exact credential value; a transformed (encoded) secret is not recognised.
- `SbxRunner` (microVM) is unverified and never selected; this path is rootless Docker only.
- Verified here: opencode v2.0.25 only, against a stub model (see **First-contact findings**); it starts and works with a read-only root and noexec tmpfs `HOME` and `/tmp`. Not verified: `claude` and `agy` inside this container. A CLI that needs to execute from `HOME` or `/tmp` (native code unpacking) needs `SessionSpec(tmp_exec=True)`; one that writes next to its token needs `tmpfs_dirs`.
- The wall cap compared against is the channel's `wall_s`, which the profile defines per sweep; per-session budgeting is the caller's.
- **The workspace is a rw bind mount to host disk** with no total-size quota (see the coverage notes below).
- If the Python process is killed with SIGKILL, containers and networks labelled `cognitive-harness2.session` can remain; `runner.reap_all()` removes exactly those.

**Threat-model coverage** (checklist from the Antigravity review, by test; `T` = `tests/test_session_integration.py`, `U` = `tests/test_session_runner.py`, `H` = `tests/test_workspace_harvest.py`, `P` = `tests/test_egress_proxy.py`):

| item | where |
| --- | --- |
| read-only root, caps dropped, no-new-privileges, user | `U` every hardening flag; `T` privileges_are_dropped |
| rootless gate, rootless mapping | `T` not_rootless / stopped_daemon / missing_image fail closed; `T` reclaim (container root = host user) |
| `/proc`, `/sys`, mount, unshare, cgroup | `T` kernel_and_privileged_operations_fail |
| same-path workspace, no `../` escape, nothing else mounted | `T` nothing_else_is_mounted; `U` mounts_are_exactly |
| tmpfs HOME and /tmp, noexec, size cap | `T` only_the_workspace_and_tmpfs; `T` tmpfs_is_size_capped |
| symlink and hardlink attacks | `T` hardlinks_across_the_mount_boundary; `T` symlink_pre_created; `H` symlink rules |
| CLI binary read-only | `T` privileges_are_dropped (mount `ro`, append denied) |
| environment scrubbing, `DOCKER_*` | `T` session_environment_is_closed (hostile ambient env); `U` pinned_and_carries_no_docker_variable |
| token hygiene (ro, not in argv/env/inspect/ps/workspace) | `T` credential tests; `U` no_secret_is_anywhere_in_any_argv |
| harvest scrubbing | `T` credential_written_into_the_workspace; `H` credential rule |
| no egress, loopback unreachable, DNS, proxy enforcement | `T` the network tests (direct targets, DNS, unset proxy, proxy deny table) |
| unix-socket bridge, proxy-only mount | `T` local_channel...bridge; `T` bridge_directory_is_private; `U` proxy_container_command_line |
| proxy allow-list, redirects, rebinding, oversize, slowloris | `P` |
| fork bomb, OOM, CPU quota, wall clock, guaranteed kill | `T` fork_bomb / memory_hogs / resource_limits / timeout_kills / crash cleanup |
| output cap, ANSI filter | `T` output_is_capped_stripped; `U` terminal escapes |
| harvest: special files, escaping symlinks, suid/exec, size | `H`; `T` reclaimed...harvest_refuses_the_mess |

Not covered by a test: modification of staged protected files (`guardrails.md`) is the caller's check, because this runner stages nothing and `inspect_workspace` can only refuse protected path *names*; total bytes written to the workspace are not capped (only a single file, `SessionLimits.file_bytes`), so a hostile session can fill the host disk through the rw mount unless the workspace sits on a quota'd filesystem; UDP is not probed separately (there is no route at all); a kernel or runc escape is out of scope.

## Solver substrate and sum-signed (slice 3b)

The SOLVER half of a k=1 run: the rollouts. Upstream at k=1 (`modes=('agg',)`) runs 4 solver rollouts (base, inv1, dom1, agg1) and 3 proposer sessions (inv1, dom1, agg1; the reflection session is skipped), so a sweep is 7 contained sessions in all. **No model is called and no real CLI is run by any of this code's tests**: they drive a fake CLI (`tests/fixtures/fake-cli solve`) in the real rootless daemon. The real call waits for the user's go.

```
 ExecutionGrant ──► SweepBudget ──► ProposerSessionRunner(role="solver") ──► CognitiveSubstrate.execute
 (channel, sweep id,   (wall, per-session,   (same container as a proposer:         (workspace, AGENTS.md, inputs,
  approver, expiry,     max sessions, per-    same hardening, same channel           harvest, run dir)
  hard caps)            role caps)            decision; no upstream Decision)              │
                                                                                           ▼
                                SumSignedAdapter.execute ──► metrics.json ──► .adapt_complete ──► score (pure) / verifier container
```

**The task, `sum-signed`** (`sum_signed.py`). `input.txt` holds five signed amounts; the solver writes `Total: X.XX` (the signed sum, two decimals) to `output/answer.txt`. Three domains (household, retail, payroll) of two tasks each, amounts derived from sha256 of the task id, so they never drift; a positives-only sum is wrong on every instance.

**The verifier**, two implementations of one verdict, kept equal by running 46 shared cases through both (malformed, missing, extra text, whitespace, negative zero, rounding, huge numbers, unicode digits, NUL, oversize):

- `VERIFY_SH`: busybox `sh` and `awk`, run in `DockerRunner` (`--network none`, `--read-only`, default alpine, no python). It does its arithmetic in whole cents and prints `{"n_passed","n_criteria","all_pass"}`.
- `grade_answer`: the same rules in Python. `adapter.score(run_dir, task)` uses it, so scoring needs no container, and returns `None` only if the judge cannot judge (an unknown task), never a zero.
- Three criteria: the file exists as a regular, non-link, NUL-free file of at most 4096 bytes; its trimmed content is exactly `Total: ` and a two-decimal ASCII number; that line is the expected total in its one canonical spelling. `-0.00`, `007.50`, `+1.00`, `1,234.56`, `9.76e1` and fullwidth digits are all wrong. The answer is judged as UTF-8 with undecodable bytes replaced; a file anywhere but `output/answer.txt` is no answer.

**The budget** (`sweep_budget.py`). `SweepBudget(total_wall_s, per_session_s, max_sessions, role_caps=)` is one thread-safe deadline and session counter shared by proposer and solver sessions. `reserve_session(role)` returns a `Reservation` (`.timeout_s` = `min(per_session, time left)`, also `float()`) or raises `BudgetExhausted` (`.reason`: `deadline`, `sessions`, `role-sessions`). Nothing starts after the deadline or in its last second. The clock is monotonic and injectable. The runner reserves before its docker gate, clips the session's timeout to the reservation, and releases it with how the session ended; a session that never started (refused, gate failed) gives its slot back. `snapshot()` is plain numbers and outcome words for an audit log. `SweepBudget.from_channel(channel, proposer_sessions=3, solver_sessions=4)` takes the total wall from the profile's `caps.wall_s`.

**The grant** (`substrate.ExecutionGrant`). Plain data from the stage-1 approval gate: `channel`, `sweep_id`, `approved_by`, `expires_at` (epoch s), `max_proposer_sessions`, `max_solver_sessions`, `max_wall_s`, `per_session_s`, `classification`. `ExecutionGrant.from_plan(plan, sweep_id=, approved_by=, ttl_s=)` derives one from an approved `SweepPlan` (P sessions, R rollouts, its wall cap); `grant.make_budget()` builds the matching `SweepBudget`. There is no boolean switch: `CognitiveSubstrate(surface, execution=SolverExecution(...))` is the only wiring, and `execute` raises `ExecutionRefused` before it makes a directory or calls docker when any of these hold: no wiring, no grant, an expired grant (checked again before every run id), another channel or classification than the runner's, a runner that is not `role="solver"`, a runner without a `SweepBudget`, or a budget larger than the grant's caps.

**What `execute` does** per run id: make a workspace (mode 1777, under the profile's workspace roots) holding `AGENTS.md` (the install's always-on files, in surface order, joined by a rule) and the task inputs, and an `output/` directory; run one solver session; write `opencode_session.jsonl` (the session's stdout), copy only the `output/` files that harvest accepted (nothing that is a link, special file, executable, hard-linked, oversized, oddly named, protected or holds the credential), write `metrics.json`; remove the workspace. A run that could not be made (budget spent, session refused, a stream with no parseable JSON events, so a dead chain) leaves `error.json` and no `metrics.json`, and the adapter then raises and does not cache it. A teardown that leaves anything behind raises `SessionCleanupError`. `read_trajectory` is pure code, a copy of upstream's `render_events` (pinned to it by a test that renders the same streams through both).

Honest limits:

- **Fake CLI only.** (The stage-1 runner replaces the argv and environment below: see "Running stage 1".) The opencode `--format json` event shape, its provider configuration (`opencode.json` or environment) and the argv in `substrate.opencode_argv` are assumptions copied from upstream's reference launcher and have never met a real binary. Settled since, against the real v2.0.25 and a stub model (**First-contact findings**): opencode needs neither `tmp_exec` nor `tmpfs_dirs`, and the provider block is right; what a real gemma does with it is not.
- `OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX` cannot be set: the runner refuses environment names that look like credentials (`TOKEN`). Upstream sets it to 64000.
- Only the always-on files are rendered into `AGENTS.md`. Skills, subagents, scripts and plugins of the harness tree are not installed in the workspace (upstream's `workspace_agents_md` mode installs them under `.opencode/`).
- The solver can delete nothing it was given (sticky bit), but it can still write anything it likes under `output/` and the workspace; only `output/` is read back.
- `max_solver_sessions` counts sessions, not tokens or money; the cost cap is the channel's, enforced by the caller.
- The solver half does not import upstream itself (the proposer binding in the next section does, through `upstream_import`); the adapter is duck typed, and a local copy of `runmark` (pinned to upstream by a test) writes `.adapt_complete`.

## Proposer binding (slice 3b)

Upstream's `Proposer` is the object `run_task` and `run_sweep` call to start every proposer and selector session. Out of the box a session runs on the host, inherits the whole process environment and forces Vertex AI. This slice binds **upstream's own orchestration** (its prompts, evidence, retries, cache, audit trail and harvest) to **contained sessions** by subclassing `Proposer`, so nothing upstream decides about the method is reimplemented. **No model is called and no real CLI is run by its tests**: they drive a fake CLI (`tests/fixtures/fake-proposer-cli`, installed as `opencode`, `claude` and `agy`) through the real `ProposerSessionRunner` on the real rootless daemon.

| piece | module | what it does |
| --- | --- | --- |
| verified import | `upstream_import.py` | `import_upstream(runs_dir=...)` imports `config`, `core.{spec,proposer,recursion,schedule,tree}` with the current directory switched to a private **empty** directory and `os.environ` scrubbed of `HARNESS2_*`, `GOOGLE_CLOUD_*` and Vertex variables (plus `HARNESS2_RUNS_DIR` under your directory and a placeholder `HARNESS2_IMPROVER_MODEL`), so `config.py` cannot read a stray `.env` or freeze your Vertex project. Then it **verifies**: the installed distribution is `PINNED_COMMIT` (a git install via `direct_url.json`'s `commit_id`; an editable install must be a git checkout at that HEAD with unmodified tracked files and no `.env` beside the package); the package Python found is the one that distribution installed (no shadowing on `sys.path`) and `config.__file__` is inside it; `os.environ` gained, lost or changed nothing but the controlled variables and upstream's one `setdefault` (`GOOGLE_CLOUD_LOCATION`); `harness2.benchmarks` and `harness2.cli` are not loaded. The environment and directory are restored in a `finally`; any surprise raises `UpstreamImportError` and removes the half-imported modules. Here upstream is installed from git into `site-packages` (`direct_url.json` carries the commit); a module imported by someone else first is refused, because it cannot be shown to have read nothing. |
| command shapes | `upstream_egress.py` | `COMMAND_SHAPES` is the allow-list of exactly the argv this package runs: `opencode run --standalone --auto --format json --model M PROMPT` (opencode v2.0.25; the v1 `bun ... --dir --variant` shape is gone), `claude -p --output-format json --permission-mode ... --no-session-persistence --model M --effort E --append-system-prompt S [--max-budget-usd N]` (prompt on stdin), and `agy --print=PROMPT --dangerously-skip-permissions --model=M [--effort=low]` (joined flags). `agy_cli` is a checked kind (`CHECKED_KINDS`), with `googleapis.com` as its own endpoint rather than a Vertex route. |
| model gate | `model_gate.py` | `GATE`: one slot, `hold(timeout=, deadline=)` and `async with ahold(...)`. A single GPU serves one model call at a time; the solver sessions (WS-E) take the same gate. Deadline-aware: a wait past it raises `GateTimeout` naming the holder. Not re-entrant. |
| contained proposer | `contained_proposer.py` | `contained_proposer_class(up)` builds `ContainedProposer(Proposer)`; `make_proposer(up, SessionConfig(...))` builds one; `make_selector_prop(prop)` builds the selector's, to pass as `selector_prop=` (otherwise upstream's `_selector_for` builds a plain host `Proposer`). |
| drift pins | `tests/test_upstream_compat.py` | Pins every private or fragile upstream name the binding relies on. A failure says what drifted and the pinned commit. |

```python
from cognitive_harness2 import image, upstream_import
from cognitive_harness2.contained_proposer import SessionConfig, make_proposer, make_selector_prop

up = upstream_import.import_upstream(runs_dir=audit / "runs")         # BEFORE anything imports harness2
verified = image.gate_for_build()
session = SessionConfig(verified=verified, image=image.resolve_image(verified), profile=profile, channel="local",
                        zdr="strict", classification="open", scratch_root=str(scratch),   # inside a workspace_roots entry
                        cli_paths={"opencode": "/abs/opencode"})                          # named opencode | claude | agy
prop = make_proposer(up, session, audit_dir=audit / "proposer")
up.schedule.run_task(task_id, k, adapter=adapter, prop=prop, runs_root=runs, modes=("agg",), libraries=None,
                     selector_prop=make_selector_prop(prop))
```

**What a `ContainedProposer` session does.** Upstream's `call` copies the evidence to `<tmp>/context` and calls `_claude_cli` or `_opencode`; both run `_contained`, which routes on the channel's REAL kind (an `agy_cli` channel is carried by an `opencode`-kind `ChannelSpec` because upstream's `CHANNELS` is a closed tuple, and `real_kind` says what it is):

1. copies `<tmp>` to a private workspace under `scratch_root` (world-writable for the container user, which is not the host user) and writes `MISSION.md` (opencode, agy) or builds the stdin (claude);
2. builds the exact argv and **checks it with `check_proposer`** against the channel profile (kind, model, endpoint, retention, classification, command shape, environment). A refusal raises `ProposerRefused` and **starts nothing**;
3. takes the **model gate**, then runs the session ONLY through `ProposerSessionRunner` (two containers, one `--internal` network, the proxy allow-list). The container is the fence: opencode v2 has no permission variable, and none of the three CLIs' own flags is relied on for isolation;
4. a timeout kills the session (`subprocess.TimeoutExpired`, which upstream reports as `timeout after Ns`) and the runner's teardown leaves no container, network or directory;
5. inspects the workspace (`workspace_harvest`): **any finding or any symlink fails the session** and nothing is copied back, because upstream's `_harvest` uses `copytree`, which follows links;
6. parses stdout with upstream's own parsers (`_opencode_stdout`, `_opencode_cost`, `_parse_json`), preferring `output/answer.md` for opencode and agy, and returns `(text, cost, err)`; cost is 0.0 for a local channel;
7. copies only the inspected regular files back into upstream's directory, from where upstream harvests `context/harness` into `Reply.workspace`, then deletes the scratch copy.

`env()` is replaced: it never reads `os.environ`, sets no Vertex variable (`CLAUDE_CODE_USE_VERTEX`, `GOOGLE_VERTEX_*`, `VERTEXAI_*`, `CLOUD_ML_REGION`) and returns only the container's extras (opencode: `OPENCODE_CONFIG_CONTENT` with an OpenAI-compatible provider whose base URL is the proxy alias for a local channel or the channel's endpoint plus `api_path`, `OPENCODE_DISABLE_MODELS_FETCH=1`, XDG directories under the tmpfs `HOME`; claude: updater and traffic switches; agy: none). Credentials are `SessionConfig.credentials[kind]`: the runner's read-only credential file, never an env value.

**Deviations from upstream's defaults (all deliberate):**

- **`attempts=1`.** `ContainedProposer.attempts` is 1 (upstream: 2), and `apply_overrides` sets `recursion.STAGE_ATTEMPTS`, `recursion.TRANSPORT_ATTEMPTS` and `config.TRANSPORT_ATTEMPTS` to 1, once, when the class is first built. A failed local session is not retried blind. A test pins the constant names, so an upstream rename is a loud failure and not a silent return to retries.
- **No reflection.** Call `run_task` with `libraries=None` and `modes=("agg",)`, so each task makes 3 proposer sessions and no experience-reflection session. This is the caller's choice (the stage-1 runner, a later step); nothing here enforces it.
- **Serialised calls.** One model call at a time (the gate), where upstream allows 2 through its own semaphore (which still wraps the gate). A session waits for the gate up to `gate_wait_s` (default four times its wall clock), then fails with `GateTimeout`.
- **Per-session timeout 600 s** (`SessionConfig.timeout_s`), also capped by the channel's `wall_s`.
- `Reply.channel` and the audit `meta.json` carry the REAL kind (`agy_cli`), not the carrier's.

**Honest limits.**

- **The opencode provider schema is confirmed** against the real v2.0.25 and a stub model (**First-contact findings**): `npm: "@ai-sdk/openai-compatible"` is required, `options.baseURL` works. `opencode.json` (`agent.build.steps`) is still written as upstream did; v2 ignores it harmlessly. `--variant` (effort) does not exist in v2, so effort has no effect on opencode.
- **Unverified in the container:** that claude or agy start with a read-only root and a tmpfs `HOME` at all; `TMP_EXEC` is `False` for all three (measured for opencode, a guess for the others).
- **claude's token.** The runner exposes a credential as a file and, with `env_var`, sets the variable to the file's PATH, not its value. `CLAUDE_CODE_OAUTH_TOKEN` expects the value, so a claude subscription session needs either runner support for that or a credentials file mounted where claude reads it (`~/.claude/.credentials.json`, unverified). Not solved here. agy's token is a file (`~/.gemini/antigravity-cli/antigravity-oauth-token`) and fits the credential mechanism as is. The caller reads the token; this package never touches the host's credential files.
- **agy** prints its final answer on stdout (taken as plain text) or in `output/answer.md`; its retention is unknown, so the profile caps it at `open` content, and the container is its only boundary.
- `--max-budget-usd` is passed to claude only when `SessionConfig.max_budget_usd` is set; a subscription's cost is notional.
- The scratch workspace is a rw bind mount to host disk with no quota (see the contained-session limits above), and a hostile model can still send anything in its workspace to the allow-listed endpoint.
- `import_upstream` proves that importing read no `.env`. It cannot prove upstream never reads one LATER (config's `load_dotenv` runs only at import).
- A harvest finding fails the whole session, including a harmless one (an executable bit on a script the harness tree contains). Widen `SessionConfig.harvest_limits` deliberately if a real tree needs it.
- **Staged protected files.** Upstream stages the WHOLE harness under `context/harness/`, `guardrails.md` included, and the harvest refuses every protected-named path. `ContainedProposer` therefore takes a manifest (`workspace_harvest.protected_manifest`) of the protected-named files it staged and another after the session: files that come back byte-identical are accepted; a staged one that was edited, removed, replaced by a link or made executable fails the session, and so does a NEW protected-named path. (Found by the stage-1 selftest: without it every real proposer stage failed with `protected-path`.)
- `SessionConfig.budget` and `SessionConfig.observer` put the proposer sessions on the same `SweepBudget` as the rollouts and into the same audit trail.

## Running stage 1

Stage 1 is the first run of the method against a real model: a local smoke run, `k = 1`, on the synthetic `sum-signed` task, with `opencode` in a rootless container talking to **your Ollama on loopback**. 3 proposer sessions (inv1, dom1, agg1) and 4 solver rollouts (base1, inv1, dom1, agg1), one model call at a time, `modes=('agg',)`, upstream's reflection session skipped (`run_task(..., libraries=None)`), attempts forced to 1. $0, nothing leaves the machine, a human approves the sweep. Code: `stage1.py`; profile template: `examples/harness2-channels.stage1.toml`.

**Nothing in this repository has made a real model call.** The only place one can happen is the `run` command below, which you type. (The first `run`, 2026-10-30, made none either: all seven sessions died in about two seconds on a proxy denial of opencode's own loopback traffic, fixed in `session_runner.session_env`; see **First-contact findings**.)

### What to check first

- **Ollama is up and has the model**: `ollama list` shows `gemma4:12b` (else `ollama pull gemma4:12b`). `plan` checks this with one `GET /api/tags` and loads nothing.
- **GPU memory is free**: `nvidia-smi` (or your equivalent). A 12B model needs several GB of VRAM on top of whatever else holds the GPU; `ollama ps` shows what is loaded now. Nothing here measures this for you, and the first call may spend minutes loading the model before its first token (the bridge allows up to 15 minutes idle, but the per-session timeout is still 600 s).
- **The image exists**: `uv run python -m cognitive_harness2.image status` (build once with `... image build`; a run never builds or pulls).
- **The profile**: copy the template somewhere you own and point `workspace_roots` at a directory that exists (the runner makes its scratch directories there and removes them):

```bash
mkdir -p ~/h2-stage1 ~/h2-workspaces
cp packages/harness2/examples/harness2-channels.stage1.toml ~/h2-stage1/channels.toml   # edit workspace_roots if yours differs
export COGNITIVE_HARNESS2_CHANNELS_FILE=~/h2-stage1/channels.toml                         # or pass --profile on every command
```

The runner reads the profile from `--profile` or that variable, never from the default `~/.config` file, and never writes under `~/.config`.

### The commands

```bash
# 1. The plan. Read-only: it changes nothing and prints the command that approves the sweep.
uv run python -m cognitive_harness2.stage1 plan --opencode ~/.opencode/bin/opencode

# 2. The run. Needs --approve <channel> AND a phrase you type on this terminal; a pipe or a script cannot approve it.
uv run python -m cognitive_harness2.stage1 run --approve local-ollama --opencode ~/.opencode/bin/opencode

# Rehearsal with a FAKE opencode and a FAKE Ollama on the real daemon (no model, safe to run any time):
uv run python -m cognitive_harness2.stage1 selftest            # --keep-in DIR keeps the audit and report
```

Options: `--wall-s N` (total wall clock; it can only LOWER the profile's `wall_s`, raise that in the profile), `--per-session-s N` (default 600), `--runs-dir DIR` (the PARENT of the per-sweep directory, default `~/.local/state/cognitive-engine/harness2-stage1`), `--classification` (default `open`: the task, the harness and every prompt are synthetic), `--channel` (default `local-ollama`).

`plan` prints the channel, endpoint, model, caps, expected P and R, timeouts, classification, that nothing leaves the machine, each preflight result and the exact `run` command. What `plan` does: read the profile, ask the channel policy, run the pinned read-only `docker info` and `docker image inspect`, send one `GET /api/tags` to the loopback Ollama. It creates no file, directory, container, network or socket, and `plan` is the default command.

`run` refuses (exit 2, nothing started, nothing created) unless: `--approve` names the channel, stdin and stdout are a terminal, every preflight check passes (rootless Docker, the pinned image, `opencode` found, Ollama answering with the model, the workspace root), `plan_sweep` accepts, and you type `approve local-ollama <sweep id>` exactly. Only then does it create the run directory and the `ExecutionGrant` (channel, sweep id, who approved, expiry, caps: 3 proposer + 4 solver sessions, the wall clock, the per-session timeout), and start the first session; the host bridge opens per session, so nothing listens before the grant exists.

### Expected duration against the caps

- Sessions run **one at a time** (the model gate). The caps are 600 s per session and **2700 s (45 min) for the whole sweep**.
- The worst case, 7 sessions all running to their timeout, is 4200 s, **more than the 45 minutes**. The budget then refuses to start sessions in the last second before the deadline and ends every running one by it, so a slow model produces a **partial** result (some rollouts or proposals missing), not an overrun. Raise `wall_s` in the profile if you want to give it longer.
- How long a 12B model really takes per session is **unknown** until the first call. The fake run takes about 14 seconds in all.

### How to abort

- **Ctrl-C** in the run's terminal. The teardown runs in a `finally`: the current container and its proxy are killed and removed, the networks and the host bridge are closed, the scratch workspaces are deleted, the report is written with status `aborted` (exit code 130).
- From another terminal: `kill -INT <pid>` does the same. `kill -9` skips the teardown; then remove what carries the label and the scratch by hand:

```bash
D="docker -H unix:///run/user/1000/docker.sock"
$D ps -a -q --filter label=cognitive-harness2.session | xargs -r $D rm -f
$D network ls -q --filter label=cognitive-harness2.session | xargs -r $D network rm
rm -rf ~/h2-workspaces/stage1-*          # the sweep's scratch under your workspace root
ls "$XDG_RUNTIME_DIR" | grep -E '^h2(s|bridge)-'     # stray bridge/session directories, if any
```

### Where the audit and the report go

`<runs-dir>/<sweep id>/` (mode 0700), created only after you approve:

| file | what |
| --- | --- |
| `audit.jsonl` | one JSON line per event, flushed as it happens: `plan`, `approval`, `grant`, `sweep_start`, `session_reserved`, `session_start` (timeout), `session_end` (exit code, timed out, duration, leftovers), `session_refused` (budget, gate or authorisation, with the reason), `rollout_start`, `rollout_verdict`, `error`, `teardown`, `sweep_end`. Numbers and short words only: never a prompt, an argv, an environment, a credential or model output beyond a 160-character excerpt of a rollout's answer. |
| `report.json` | the final result: status, total wall, the ledger (`P`, `R`, expected 3 and 4), pass or fail per rollout (all three criteria, the answer excerpt, exit code, timed out, the CLI's `error` events), the proposer stages, session counts and outcomes, the teardown result. Written even when the sweep failed. It says what reached the model, in these fields: `approved_for_real_model` (you approved a real run; false in the selftest), `cli_sessions_started`, `model_sessions_reached` (sessions during which the egress proxy opened a connection to the host bridge, i.e. to Ollama's door; it does not say the model answered), `real_model_call` (true only if approved AND at least one session reached the model; it used to be the approval flag and was misleading), and `sessions_summary` (exit codes, timeouts, denied `host:port` with how many sessions hit each, the first CLI error). |
| `results/`, `upstream-runs/`, `proposer-audit/` | upstream's and the solver's own artifacts: each rollout's `opencode_session.jsonl` stream and `output/`, upstream's summary, and **upstream's own mission and response files** for each proposer session. These are the method's working records and DO hold prompts and model text; keep the directory private (it is 0700) and delete it when done. |

The run ends with an `outcome` line, for example `outcome   7 sessions started, 0 reached the model: 7 exited rc=1; first error: UnexpectedStatus: 403; proxy denied: 127.0.0.1:34175 (7 sessions, not-allow-listed)`, and, when nothing reached the model, a line saying nothing was asked of it. Denied hosts are `host:port` only.

Exit codes: 0 clean and the ledger is 3 and 4; 1 the sweep failed, the identity did not hold, or sessions started and none reached the model; 2 refused before anything started; 3 the teardown left something behind (named in the report); 130 interrupted.

### What only a real gemma call can settle

The real opencode binary has now been run in the stage-1 session against a stub model (next section). What is still an assumption, and where the first real run is likeliest to disappoint:

- **Whether gemma4:12b emits well-formed tool calls** through Ollama's OpenAI-compatible `/v1/chat/completions`, and whether it uses `write` on `output/answer.txt` (solver) or `output/answer.md` (proposer) at all, or just talks. The stub always does exactly what the script says.
- **Context truncation.** Each step sends about 15 KB (about 4,000 tokens: 6 tool definitions, the system prompt, `AGENTS.md`, the task) plus the tool results so far. Ollama's default context window may be smaller than that and silently drops the START of the prompt, which is where the instructions are. After the first call, look at the `CONTEXT` column of `ollama ps`; if it is small, set the model's `num_ctx` (a Modelfile `PARAMETER num_ctx`, or `OLLAMA_CONTEXT_LENGTH`) before trusting any result.
- **Speed and memory**: tokens per second, model load time (the first call may spend minutes before its first token), VRAM, and so how many of the 600 s per session and 2,700 s per sweep are really needed.
- **Whether Ollama accepts the request body** opencode sends (`store`, `stream_options.include_usage`, `max_completion_tokens: 32000`, tools). The stub accepted everything; Ollama may ignore or reject a key.
- **A failing or absent Ollama behaves like a 5xx**: opencode retries about 85 s and then exits 1 with the server's message. A model Ollama does not have is a fast exit 1 (404).
- **Whether a 12B model can do the work.** The method asks the proposer to edit harness files and answer in `output/answer.md`; a small model may well produce nothing usable, in which case the stages fall back to the base harness and the report says so. That is a result, not a fault of the runner.
- **The prompt reaches the solver as one flattened line** (the command-line gate refuses control characters, so newlines become spaces). The stub sees it intact enough to detect the task text; whether gemma copes is open.
- opencode v2 has no permission environment variable and `--variant` (effort) does not exist, so neither is set; the container is the only fence.

### First-contact findings (stub-model probe)

`tests/real_cli_probe.py` and `tests/test_real_cli.py` (pytest marker `real_cli`) run the REAL opencode v2.0.25 in the exact contained stage-1 solver session: `stage1.solver_argv`, `stage1.solver_env_builder`, `TMP_EXEC`, the stage-1 workspace layout (`AGENTS.md`, `input.txt`, `output/`), the same hardening, host bridge and egress proxy. The "model" is a stub OpenAI-compatible server on this host's loopback that answers from a script and logs what it is asked (paths, header NAMES, the model field, which tools were offered, whether `AGENTS.md` reached the prompt). No Ollama, no model.

```bash
COGNITIVE_HARNESS2_REAL_CLI=1 uv run pytest -q packages/harness2/tests/test_real_cli.py     # skipped without the variable or the binary
uv run python packages/harness2/tests/real_cli_probe.py --scenario write --config api       # one session, prints the events and the stub's log
```

Measured, 2026-10-30, opencode v2.0.25:

| question | finding |
| --- | --- |
| the first real run's failure | opencode `--standalone` starts a private HTTP server on a random port of its OWN `127.0.0.1` and calls it with a proxy-aware client. `NO_PROXY` was empty, so the call went to the egress proxy, which denied it (`host=127.0.0.1`, 403, `UnexpectedStatus: 403`) before the model was ever contacted. Fixed: `NO_PROXY=127.0.0.1,localhost,::1`, set by the runner. |
| the second real run's failure | all 7 sessions reached ollama (`reached_model 7`, nothing denied) and ollama itself answered `HTTP 403`. Its DNS-rebinding guard serves only a `Host` that is a loopback/private/unspecified IP, `localhost`, the machine's hostname or a name ending `.localhost` / `.local` / `.internal`; the provider URL named the proxy alias `harness2-proxy`, which the proxy forwards as `Host`. Fixed: the alias is `harness2-proxy.internal` (Docker resolves a dotted alias on the `--internal` network). The stub servers (`FakeOllama`, the probe's `StubModel`) now enforce the same rule through ONE helper, `ollama_guard` (measured table in its docstring and `tests/test_ollama_guard.py`), so a bad `Host` or a foreign `Origin` fails the selftest and the real-CLI probe instead of passing. The real opencode sends no `Origin`; `Authorization` is never checked on these routes. The run summary now says "the proxy denied nothing, the model endpoint itself refused (HTTP 403)" when that is what happened. |
| what the stubs still do not check of real Ollama | UNCERTAIN, not modelled: other `content` shapes in a chat message (only a numeric one is a measured 400), tool-schema validity, `tool_choice`, context truncation (silent) and generation limits; an address merely assigned to a local interface (Ollama accepts it as `Host`); a non-loopback listener (`OLLAMA_HOST=0.0.0.0` skips the Host check entirely and only applies to a differently bound Ollama than the user's); `OLLAMA_ORIGINS` set by the user (the stub models the default list; opencode sends no `Origin`, measured); the model not being pulled or being unloaded (404 / slow first token). Measured and modelled: `Host`, `Origin`, malformed JSON, missing `model`, empty `messages`, unknown model (404), numeric content. |
| model traffic | goes THROUGH the proxy (`http://harness2-proxy.internal:<port>/v1`, log `reason=unix-route`), as a proxy-aware client does. Per session: one `GET /api/tags`, then one `POST /v1/chat/completions` per model step (streaming, `stream_options.include_usage`, `store: false`, `max_completion_tokens: 32000`). No `Authorization` header: no key is needed. |
| provider block | `npm: "@ai-sdk/openai-compatible"` is **required** (without it: `provider.no-route: Unsupported package for ollama/gemma4:12b`, exit 1, the model never contacted). `options.baseURL` works, and so does `api` in its place; `name` is optional; `baseURL`-only and `api`-only (no `npm`) fail. The existing block was right. |
| `TMP_EXEC` | **False** works (both `/tmp` and `HOME` mounted `noexec`): text reply, `shell`, `read`, `write` all ran. Changed from True. |
| where it writes | `HOME` (tmpfs): about 0.5 MB, a sqlite `~/.local/share/opencode/opencode.db` (+ `-wal`, `-shm`), `log/`, `shell/`, `repos/`, `~/.cache/opencode/bin`, `~/.config/opencode`, `~/.local/state/opencode`. `/tmp`: about 19 MB, three `.bun-*.so/.node` files and `/tmp/opencode`. The workspace gets nothing but what the model writes (no stray files). Both tmpfs are capped at 256 MB. |
| start time | a whole contained session (network, proxy, bridge, opencode start, one model round trip, teardown) took 2.9 to 3.3 s. About 2 s of that is the container machinery (the failed first run spent 2.2 s per session). |
| `AGENTS.md` | picked up from the working directory: its text is in the system prompt of every step. |
| tools | opencode offers 12; **`grep` and `glob` fail** (`ripgrep execution failed`: opencode tries to download `rg` from github.com, the proxy rightly denies it, the image has none). `webfetch`, `websearch`, `question` and `subagent` cannot do useful work here. All six are now denied in the config (`permission`), leaving `edit`, `read`, `shell`, `skill`, `write`, `execute`: the request shrinks from 22 KB to 15 KB. The tool is `shell`, not `bash`. |
| `--title` | with `--title adapt` opencode skips a separate title-generation model call, so one chat request per step instead of two (`COMMAND_SHAPES` allows `--title <word>`). |
| events (`--format json`) | one JSON object per line, each with `type`, `timestamp`, `sessionID`, `part`. A tool round is `step_start`, `tool_use` (`part.tool`, `part.state.status` `completed` or `error`, `state.input`, `state.output` or `state.error`), `step_finish` (`reason: "tool-calls"`, `cost`, `tokens`); the final answer is one `text` event (`part.text`, the whole text, not deltas). Errors are `{"type":"error","error":{"type":..., "message":..., "status"?}}`. `trajectory.render_events` and upstream's `_opencode_stdout` and `_opencode_cost` read these streams correctly, except upstream's error text (below). |
| the answer file | a stubbed `write` tool call to `output/answer.txt` executes inside the workspace, the file is harvested, copied to the run's `output/` and passes `sum_signed.grade_answer` (all three criteria). The proposer shape (`MISSION.md`, `opencode.json`, `output/answer.md`, the upstream prompt) works the same; `opencode.json` is ignored. |
| exit codes | 0 after a normal end; 1 after an `error` event. A model the server does not have (404): exit 1 at once with the server's message. A 5xx or 429: about 85 s of retries (11 requests), then exit 1. |
| failure text | upstream's parser reads only the old `error.data.message`, so a v2 failure was reported as `rc=1: empty reply`. `trajectory.error_events` reads `error.message` and is used for the proposer's error text and in each solver run's `metrics.json` (`errors`). |

## Channel profile

Which proposer channels may carry content is declared in one TOML file, under one policy. The channels are local models (`opencode` to Ollama on loopback), the agent's own provider (`claude_cli`, `agy_cli`) and OpenRouter. The file lives at `COGNITIVE_HARNESS2_CHANNELS_FILE`, else `~/.config/cognitive-engine/harness2-channels.toml`.

```toml
# Directories a Proposer session may use as its working directory (absolute, normalised).
workspace_roots = ["/home/me/h2-workspaces"]

# A local model on loopback: content never leaves this machine.
[channels.local-ollama]
kind = "opencode"                      # opencode | claude_cli | agy_cli
provider = "ollama"                    # for opencode: the model id's prefix
endpoint = "http://127.0.0.1:11434/v1" # opencode takes one `endpoint`
model = "ollama/qwen3:30b"
retention = "local"                    # local | zdr-attested | unknown
max_classification = "restricted"      # open < gated < restricted
approval = "per-sweep"                 # the only value: a human approves each sweep
[channels.local-ollama.caps]           # hard per-sweep caps; all three required, > 0 (cost_usd = 0 only for retention = "local")
sessions = 30
wall_s = 3600
cost_usd = 1.0

# OpenRouter, with zero data retention that YOU have arranged and attest to.
[channels.openrouter]
kind = "opencode"
provider = "openrouter"
endpoint = "https://openrouter.ai/api/v1"
model = "openrouter/anthropic/claude-sonnet-4"
retention = "zdr-attested"
max_classification = "gated"
approval = "per-sweep"
[channels.openrouter.caps]
sessions = 20
wall_s = 1800
cost_usd = 5.0

# The agent's own subscription: retention unknown, so open content only.
[channels.claude-harness]
kind = "claude_cli"
provider = "anthropic"
endpoints = ["https://api.anthropic.com"]  # the vendor CLIs take a list `endpoints`
model = "claude-sonnet-5-5"
retention = "unknown"
max_classification = "open"
approval = "per-sweep"
[channels.claude-harness.caps]
sessions = 10
wall_s = 1200
cost_usd = 2.5

[channels.antigravity]
kind = "agy_cli"
provider = "google"
endpoints = ["https://generativelanguage.googleapis.com"]
model = "gemini-3-pro"
retention = "unknown"
max_classification = "open"
approval = "per-sweep"
[channels.antigravity.caps]
sessions = 10
wall_s = 1200
cost_usd = 2.5
```

**The rule.** A channel is allowed only if both hold:

- its `retention` satisfies the zdr mode: `strict` (the default) allows only `local` and `zdr-attested`; `off` also allows `unknown`;
- the sweep's classification is within the channel's `max_classification`, and a channel with `retention = "unknown"` carries `open` content only.

The sweep's classification is the strictest of its sources (`channels.sweep_classification`); unclassified is `restricted`. A channel that is not in the file is denied, and the Proposer configuration must match the declared one on kind, provider, endpoint host and port, and model.

**ZDR is declared, not verified.** `zdr-attested` records what you say about an agreement or account setting. Nothing in this package contacts a provider or checks it. Subscription channels default to `unknown` for that reason, and `unknown` with a `max_classification` above `open` is refused as malformed.

**Loud failure.** A profile grants egress, so a bad one is refused whole with `ChannelProfileError`, naming the file and every problem, never repaired and never partly used. The validator rejects:

- invalid TOML, a non-table top level, unknown keys (a near miss gets a hint: `retenion` -> did you mean `retention`; `channel` -> `channels`), wrong types, bad `kind`, `retention`, `max_classification` and `approval` values, missing required fields, duplicate channel names (case-insensitive);
- `endpoint` where the kind takes `endpoints` and the reverse, URLs that are not http(s), or carry userinfo, a query or a fragment;
- caps that are missing, zero, negative, NaN or infinite, boolean, or above the sanity ceilings;
- model ids with whitespace, control or shell characters, or a leading dash;
- `retention = "unknown"` with `max_classification` above `open`; `retention = "local"` with a non-loopback endpoint;
- a file over 1 MB, not UTF-8, a symlink pointing nowhere, a symlink loop, a directory or FIFO;
- the env var set but empty, relative, or naming a file that does not exist.

`retention = "zdr-attested"` on a loopback endpoint is accepted with a warning, since a loopback gateway can forward content off the machine. With the env var unset and no default file, no channel is declared: everything is denied, and the message says how to create the file. That is not an error.

## Slot table (3b)

Slice 3b extends the safety seams to govern proposed candidate file mutations across upstream surfaces (RFC 004 §6.5):

| slot | upstream path | component | status & constraint |
| --- | --- | --- | --- |
| `c1` | `systemprompt.md` | `prompt` | Mutable |
| `c2` | `guardrails.md` | `(guardrail)` | **Denied**: protected by slot table and path stem `guardrails` |
| `c3` | `LongTermMEMORY.md` | `context_assembly` | Mutable |
| `c4` | `tool_descriptions/tool_guidance.md` | `tool_description` | Mutable |
| `c5` | `sub_agents/<name>/AGENT.md` | `decomposition_strategy` | **Mutable (BODY ONLY)**: leading frontmatter is frozen |
| `c6` | `skills/<name>/SKILL.md` | `context_assembly` | **Mutable (BODY ONLY)**: leading frontmatter is frozen |
| `c7` | `scripts/<name>.py`, `<skill>/scripts/*` | `(scripts)` | **Denied**: executable code |
| `c8` | `plugin/<name>.ts`, `hooks_file` | `(lifecycle_hook)` | **Denied**: lifecycle hooks |

### Frontmatter Freezing & Mutation Policy

Subagent definitions (`c5`) and skills (`c6`) carry YAML frontmatter defining execution parameters (`model`, `tools`, `allowed-tools`, `permissions`, `effort`). Permitting candidate mutations to alter frontmatter introduces privilege and model-tier escalation hazards (e.g. escalating from Flash to Opus, or granting tool execution).

`upstream_surface.check_mutation(slot, path, old_text, new_text)` enforces:
1. **Slot & Invariant Verification**: `slot` must be mutable (`c1`, `c3`, `c4`, `c5`, `c6`). `path` must not touch any protected stem (`guardrails`, `hooks`, `guard`, `.git`) or path escape.
2. **Leading Frontmatter Freezing**:
   - If `old_text` has leading frontmatter, `new_text` must contain an **exact byte-identical** frontmatter block (byte-level match, identical fences, preserved whitespace, no CRLF/LF tampering, no key or value modifications).
   - If `old_text` has no frontmatter, `new_text` must not introduce one.
3. **Body Hygiene**: The body must not introduce a secondary frontmatter-looking block or stray frontmatter keys (`model:`, `tools:`, `permissions:`, etc.) following a `---` fence outside fenced code blocks.
4. **Encoding & Size Hygiene**: Files must be valid UTF-8, strictly capped at 512 KiB, free of NUL characters (`\x00`), and free of disallowed Unicode control or invisible characters (BOM `\ufeff`, zero-width spaces, bidi overrides, ANSI escapes).

## Configuration

| variable | default |
| --- | --- |
| `COGNITIVE_HARNESS2_PORT` | `8120` (not in RFC 004; chosen clear of 8100/8105/8110/8115) |
| `COGNITIVE_HARNESS2_BASE_URL` | `http://127.0.0.1:11434/v1` (local Ollama) |
| `COGNITIVE_HARNESS2_MODEL` | unset: a real `propose` refuses until a model is configured |
| `COGNITIVE_HARNESS2_ZDR_ENDPOINTS` | unset: comma-separated hosts you attest are zero-retention, for the slice 1 probe path only (the Proposer gate reads the channel profile instead) |
| `COGNITIVE_HARNESS2_CHANNELS_FILE` | unset: `~/.config/cognitive-engine/harness2-channels.toml`. The channel profile (see **Channel profile**); no file means no channel is declared and everything is denied. Set to a missing file: malformed, refused loudly |
| `COGNITIVE_HARNESS2_EXPERIENCE_ROOT` | `~/.local/share/cognitive-engine/experiences` |

The API key is read from `JEV_OPENROUTER_API_KEY` or `OPENROUTER_API_KEY` and is never returned or logged.

## What is not done

- **`sbx` microVM isolation is unverified.** `sbx` (v0.50.1) is installed here and `SbxRunner` is written against its real command line (preflight, create flags and refusals checked live), but the daemon could not start a VM (no `kvm` group), so `exec`, `cp`, the network and the filesystem inside a VM were never observed and `sbx` is never selected (see "sbx (Docker Sandboxes)", "Live findings"); the fail-closed path is tested and does not depend on it. Rootless Docker is verified against a real daemon.
- **The engine does not yet call upstream.** Upstream is pinned and import-tested (see Upstream), but `engine.py`'s propose, probe and compose are still the slice 1 local baseline, and the composer is a heuristic (exit status, timeouts, output, stderr). The safety seams to upstream's `Substrate` exist (see Upstream seams); binding the stages themselves to upstream's execution is slice 3b.
- The OpenRouter quota field names follow RFC 004 and are unverified against the live API.
- The push hook counts tool failures only. It does not see `jev_judge` verdicts (the shape of that tool result in a PostToolUse payload has not been captured), and there is no `/probe` slash command or `engine probe`; the pull tool covers on-demand requests. The `PostToolUseFailure` payload is also uncaptured: the hook relies on the documented `hook_event_name`, `session_id` and `is_interrupt` fields.
- The path rule in `invariants.py` is used by `upstream_surface.py` and the staging key rules; the engine itself still mutates components, not files.
- `~/.docker/config.json` (container proxies, credential helpers) is still read by docker: only `DOCKER_*` variables are stripped from the child env. The endpoint resolver (`docker context inspect`, when `DOCKER_HOST` is unset) deliberately reads the ambient context.

## Tests

```bash
uv run pytest packages/harness2
```

The `real_cli` tests (the real opencode binary against a stub model, no Ollama) are skipped unless `~/.opencode/bin/opencode` exists AND `COGNITIVE_HARNESS2_REAL_CLI=1`, so the normal suite is hermetic. Run them on their own:

```bash
COGNITIVE_HARNESS2_REAL_CLI=1 uv run pytest -q packages/harness2/tests/test_real_cli.py
```
