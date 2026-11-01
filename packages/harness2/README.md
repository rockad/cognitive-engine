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
| `harness2_probe` | Run each candidate in a local `sbx` microVM when sbx is a verified version and passes a read-only usability probe (the default, `auto`), else in an ephemeral rootless Docker container (below). With neither it **fails closed** and runs nothing; `allow_worktree_diff` opts in to static diffs, which execute no code. |
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

## Sandbox: sbx when verified and usable, else rootless Docker, else nothing

`harness2_probe` runs candidate code only in a local sandbox, and so does anything else that builds its runner with `sandbox.default_runner` (the server's `Engine`, `harness2_status`, `harness2_trigger`'s readiness). The sum-signed verifier (`container_score`) and `Substrate` take their `SandboxRunner` as a parameter: pass them `default_runner()` to get this selection (the tests pass a `DockerRunner` directly). `COGNITIVE_HARNESS2_SANDBOX` is a tri-state:

| Value | Runner | When it cannot be had |
| --- | --- | --- |
| unset, empty or `auto` (**default**) | `sbx` microVM if the installed version is in `VERIFIED_SBX_VERSIONS` **and** the read-only usability probe passes; otherwise rootless Docker | Docker is used, with the reason recorded and (when sbx is present) warned about once; with no usable Docker either: **fail closed** |
| `docker` | rootless Docker; sbx is never consulted (no `which`, no process, no probe) | fail closed |
| `sbx` | `sbx` microVM only | **fail closed** (`SandboxUnavailable`): never falls back to Docker |
| anything else | none | fail closed |

Case, and surrounding blanks, do not matter (`SBX`, ` auto `). `rootless-docker` is accepted as an alias of `docker`. Never rootful Docker, never the host.

**What stays on Docker whatever this says.** The stage-1 proposer and solver sessions (`ProposerSessionRunner`, the egress proxy, the host bridge) call the model, so they do not go through `default_runner`: the global deny-all gives a sandbox no route to a model. `sbx` here is for the model-free, no-network execution of untrusted code (`harness2_probe`, the sum-signed verifier's `container_score`, `Substrate.execute`'s `runner.run`). Nothing in this section changes the session runners. A model session can run on sbx only through its own, separate opt-in, `COGNITIVE_HARNESS2_MODEL_SANDBOX=sbx`, which has a route of its own (a sandbox-scoped rule to our egress proxy): see "sbx model route". `COGNITIVE_HARNESS2_SANDBOX` neither selects nor blocks it.

**Cost.** An sbx-backed run creates a microVM: the create alone is about **4 s** more than a Docker container (`sbx create` about 4 s with the image present, `exec` about 0.6 s, `rm` about 0.7 s; the first run on a host also pulls the shell image), and the whole `run()` (preflight, create, four policy checks, copy in, listing, exec, rm, ls) measured about **10 s** end to end on this host (the eight verifier runs of the live verifier test took 86 s). A caller that executes many candidates pays that per candidate: `harness2_probe` over `n` = 8 candidates takes about 80 s on sbx (a cold image pull can add up to the 600 s create timeout), against a few seconds on Docker, so a client with a typical 60 s tool timeout may give up on it; the work still finishes server-side. `harness2_probe` runs in a worker thread, so `harness2_status`, `harness2_trigger` and the transport keepalive are served while it runs. Set `COGNITIVE_HARNESS2_SANDBOX=docker` to choose the faster, container-grade runner on purpose.

**Fallback table (`auto`).** `harness2_status` and `sandbox_report()` give `runner`, `mode`, and `fallback_reason` (`{code, detail, loud}`); `sbx.codes` and `sbx.reasons` list every problem found.

| Situation | Runner | `fallback_reason.code` | Warned |
| --- | --- | --- | --- |
| sbx not installed (the normal case) | Docker | `not-installed` | no |
| `COGNITIVE_HARNESS2_SANDBOX=docker` | Docker | `forced-docker` | no |
| verified set empty | Docker | `no-verified-version` | yes |
| version not in `VERIFIED_SBX_VERSIONS` (or unreadable) | Docker | `unverified-version` / `unreadable-version` | yes |
| `/dev/kvm` missing or not accessible to this process | Docker | `kvm-unusable` | yes |
| daemon socket does not answer | Docker | `daemon-down` | yes |
| global network policy not initialised or not deny-all | Docker | `policy-not-deny-all` | yes |
| `ssh.agentForwardingEnabled` or `browser.enabled` is on | Docker | `unsafe-setting` | yes |
| `ssh.agentForwardingEnabled` reads off but is flagged `requires_restart` and its override is newer than the daemon (or cannot be dated) | Docker | `restart-pending` | yes |
| a setting is missing from `sbx settings ls` | Docker | `unreadable-setting` | yes |
| a probe command timed out (5 s each) | Docker | `probe-timeout` | yes |
| a probe command failed or printed something unexpected | Docker | `probe-error` | yes |
| all of the above pass | **sbx** | none | no |

**Which runner ran a candidate is part of the result.** The two runtimes are not interchangeable: Docker is alpine (busybox, no `python3`), user `nobody`, read-only root, `--network none`, pids limit 128; sbx is Ubuntu, user `agent` with sudo, a writable `/tmp/candidate`, no pids limit. The same `command` can exit 127 on one and 0 on the other. So every `SandboxResult` carries `runner` (`sbx` or `rootless-docker`) and, when it is not sbx although sbx is present or was the first choice, `fallback_reason` (`{code, detail, loud}`). `harness2_probe` returns both per result and at the top level (`runner`, which is `mixed` if a run used both, with `runners` listing them, and `fallback_reason`); `harness2_compose` puts the runner and the fallback code in its `evidence` line, and `harness2_reflect` puts the runner in the staged lesson (`2 of 3 candidate(s) succeeded (sandbox, runner: sbx)`), so lessons from the two populations are told apart. A runner that does not say (a test double) is `unknown`; static diffs are `none`.

The warning is one `WARNING` log line from `cognitive_harness2.sandbox` (stderr, with no logging configured), once per process per distinct reason set, saying which runner is used and why. `harness2_trigger` names the runner and the reason in `readiness.sandbox`.

**Selection is not a guarantee a VM can be created (`auto` only).** The probe is cheap and cannot tell whether `sbx create` will work (image not cached and host offline, VM boot failure, daemon stopped inside the 60 s cache window, a setting flipped since). So in `auto` the `SbxRunner` that `default_runner` returns carries a Docker fallback: when preflight, create, the network check or file staging fails, that is, before `sbx exec` of the candidate has been started (`SandboxNotStarted`, a `SandboxUnavailable` subclass), it drops the cached selection, logs a `WARNING` (`sbx-run-failed`, with the sbx error), verifies rootless Docker at that moment and runs that spec on it. The result says so: `runner: rootless-docker` and `fallback_reason.code: sbx-run-failed`. It then stays on Docker for the rest of that runner's life (one `harness2_probe`), so a broken sbx does not cost every remaining candidate a preflight and up to 600 s of create. It never falls back once the candidate command has started (the candidate may have run, so running it again elsewhere is not safe), nor when the sandbox could not be removed, and not at all in `sbx` mode, which stays strictly fail-closed. If Docker is not usable either, it fails closed naming both reasons.

**The probe is safe to run by default.** It is cheap and read-only, bounded by a 5 s timeout per command (a hung `sbx` is killed with its process group and counts as unusable, never as a crash or a hang), and cached for 60 s per process. It never starts the daemon, creates a VM or pulls an image: the `sbx` commands it runs (`version`, `policy ls --json`, `settings ls --format json`) are only asked once a connect to the daemon socket already succeeds, because `sbx settings` says it starts the daemon if necessary. A timeout stops the remaining commands, and an unverified version stops them too. `SbxRunner` re-reads version, policy and the two settings on every run, so a change inside the cache window is still caught.

The candidate is run, in order of preference, in:

1. `sbx` microVM, selected as above (see "sbx (Docker Sandboxes)" below). `sbx` being on `PATH` changes nothing by itself.
2. An **ephemeral rootless Docker container**, otherwise. It is used only if every one of these holds, checked on every run:
   - `docker` exists and the endpoint (`DOCKER_HOST` or the active context) is a **local unix socket**.
   - `docker info` lists `name=rootless` in its SecurityOptions. A rootful daemon is refused.
   - The pinned image (`alpine:3` plus digest, in `sandbox.DEFAULT_IMAGE`) is already local. It is never pulled at run time.
   - The verified endpoint is **pinned**: it is resolved once, and every docker call after the check (`info`, `image inspect`, `run`, `kill`, `rm`) names it with `-H unix://<socket>` and runs with `DOCKER_HOST` and `DOCKER_CONTEXT` removed from the environment. An ambient `DOCKER_HOST` or `DOCKER_CONTEXT` changed after the check cannot redirect the run to another daemon.
3. Neither: **fail closed**. A missing `docker`, an unreachable or stopped daemon, a rootful or remote daemon all raise `SandboxUnavailable` and nothing runs. There is no fallback to a rootful daemon or to the host. The only remaining option is the explicit `allow_worktree_diff` opt-in, which executes nothing.

Each container gets `--network none`, `--read-only`, `--cap-drop ALL`, `--security-opt no-new-privileges`, pid, memory and cpu limits, a `noexec` tmpfs for scratch, user `65534`, the candidate mounted read-only at `/candidate`, and `--rm`. The home directory and the Docker socket are never mounted and no host environment is passed in. Candidate file names must already be clean relative paths: `./payload.py`, `a//b`, `a/./b`, `a/../b`, absolute paths, backslashes, empty names, trailing slashes, NUL and other control characters are refused as written, before anything is staged, and files are staged under exactly the name given. On timeout the container is killed and removed; output is capped at 1 MiB per stream.

Host setup is rootless Docker (`dockerd-rootless-setuptool.sh install`), then `docker pull alpine:3` once. The integration tests (`-m requires_rootless_docker`) run real containers and **do not skip**: without a rootless daemon they fail.

### sbx (Docker Sandboxes)

`sbx` is Docker Sandboxes: a standalone CLI that runs AI agents in local microVMs (`sbx create`, `run`, `exec`, `policy`, `daemon`, `diagnose`). RFC 004 names it as the preferred isolation.

**It is the default when usable.** `SbxRunner` is verified end to end against a live sbx v0.50.1 microVM (see "Live findings" below), but that is a per-version fact, so only versions in `VERIFIED_SBX_VERSIONS` qualify. Having `sbx` on `PATH` is not enough to select it (it once was). With `COGNITIVE_HARNESS2_SANDBOX` unset or `auto`, `default_runner` returns `SbxRunner` only if **all** hold; with `sbx` set it demands the same and refuses (never Docker) when any fails:

- The installed version is in `sandbox.VERIFIED_SBX_VERSIONS`. It holds exactly `0.50.1`, the version the live test passed on; any other version is refused.
- A read-only usability probe passes: `/dev/kvm` is accessible to this process, the `sbx` daemon is reachable, the global network policy is initialised **and deny-all** (an active `**` tcp+udp deny rule, and no network-reaching allow rule at all: neither `network*` nor `http` (a global HTTP allow reaches the gateway proxy, which `sbx policy check network` does not see), and no rule of a resource type outside the observed allow-list `network`, `http`, `filesystem:read`, `filesystem:write`: any other type is a foreign rule that makes sbx unusable, never skipped), and both `ssh.agentForwardingEnabled` and `browser.enabled` are **off** (see "The two host-reach settings" below). The probe never starts the daemon: `sbx version`, `sbx policy ls --json` and `sbx settings ls --format json` run only once the daemon socket already answers.

In `auto`, anything else gets rootless Docker as above, with the reason recorded and warned about; with no usable Docker either, the usual fail-closed refusal. There is no fallback to rootful Docker or to the host.

`harness2_status` keeps its `sandbox` string (the selected runner, or the refusal) and adds `runner`, `mode`, `fallback_reason` and an `sbx` object: `state` (`absent`, `present-but-unusable`, `usable-and-selected` or `not-consulted`), `path`, `version`, `mode`, `verified_versions`, `reasons` and `codes` (see the fallback table above) and `settings` (the two host-reach settings as read).

#### The two host-reach settings

`ssh.agentForwardingEnabled` (lets sandboxes list the host's SSH agent keys and sign with them) and `browser.enabled` (lets sandboxed processes open URLs in the host browser) both **default to true** on a fresh sbx v0.50.1 install, including this host. Finding (read-only, `sbx settings --help`, `get`, `ls --format json`, 2026-10-31):

- They **can be read**: `sbx settings get --json <key>` and `sbx settings ls --format json` print the evaluated value (default, environment variable or override, whichever wins). They are **global** (daemon-wide): `sbx create` has no per-sandbox flag for either.
- They **can be disabled**: `sbx settings set ssh.agentForwardingEnabled false` and `sbx settings set browser.enabled false` (a user override; `sbx settings unset <key>` reverts it). `browser.enabled` takes effect within about five seconds; `ssh.agentForwardingEnabled` is marked as needing `sbx daemon restart` (and, per its description, disabling blocks new agent connections while existing ones stay active). Disabling `browser.enabled` also breaks in-sandbox OAuth logins that redirect to localhost, which a model-free `shell` sandbox never does.
- So the usability probe **refuses to select sbx** while either is on (`unsafe-setting`, with the exact commands in the reason), and `SbxRunner` re-reads them on every run.
- **"Off" in `settings ls` is the configured value, not proof that the running daemon applies it.** The JSON marks `ssh.agentForwardingEnabled` `requires_restart: true`: after `sbx settings set ssh.agentForwardingEnabled false` and no `sbx daemon restart`, `settings ls` shows `false` while a daemon that has not restarted may still forward the agent. The code cannot see the daemon's applied state, so it reads `requires_restart` and `source` and **refuses (`restart-pending`)** when a flagged key reads off, has an override (`source` other than `default`), and that override is not provably older than the daemon: the override file (`$XDG_CONFIG_HOME/sandboxes/sandboxes/settings.json`, beside the `settings.json.lock` sbx creates there) must have an mtime no newer than the daemon socket (created when the daemon starts). A file that is missing or cannot be dated counts as pending, and the fix is `sbx daemon restart`. This is a heuristic on file times, not a read of the daemon: it is conservative (any later change to the settings file also trips it until the next restart), and the `source` strings of an override were not observed on this host (it has none), so any value other than `default` is treated as one. `settings ls` is evaluated by the daemon, so an environment variable the daemon itself was started with is already part of the value it prints (the scrub of `DOCKER_SANDBOXES_*` from the CLI's own environment does not hide it).
- The one override is `COGNITIVE_HARNESS2_SBX_ALLOW_HOST_SETTINGS=1` (exactly `1`), for a host with no SSH agent and no browser; it accepts only these two settings (including the pending-restart case), every other check still applies. Do not read the probe as proof that the host agent is not forwarded: it checks configuration.
- `SSH_AUTH_SOCK` was not scrubbed from the environment of the `sbx` processes this module starts; it is now, and the environment of every `sbx` process is an ALLOW-list (`PATH`, `HOME`, `XDG_*`, `LANG`, `LC_*`, `TERM`, `TMPDIR`) instead of a deny-list, so `DOCKER_HOST`, `DOCKER_CONFIG`, `DOCKER_AUTH_CONFIG`, `SBX_*` and any credential never reach it, with a test. That is defence in depth only: the setting concerns the daemon's own environment, which this module neither reads nor controls, so the refusal above is the real control. The CLI does not show which agent socket the daemon would forward.

To use sbx (these change the host, so they are the user's to run):

1. Give the user `/dev/kvm` access: `sudo usermod -aG kvm "$USER"`, then log out and in again. The process that calls `default_runner` needs it too (the probe checks it), and so does the shell that starts the daemon.
2. Start the daemon: `sbx daemon start`.
3. Initialise the global network policy: `sbx policy init <allow-all|balanced|deny-all>` (pick the profile deliberately).
4. Turn the two settings off: `sbx settings set browser.enabled false`, `sbx settings set ssh.agentForwardingEnabled false`, then `sbx daemon restart` (from a shell with the `kvm` group). Or, knowingly, export `COGNITIVE_HARNESS2_SBX_ALLOW_HOST_SETTINGS=1`.
5. Nothing to export: `auto` is the default. `COGNITIVE_HARNESS2_SANDBOX=sbx` makes a missing or unsafe sbx an error instead of a Docker run.
6. To verify another version, run the live test with the daemon started from a shell that really has the `kvm` group: `COGNITIVE_HARNESS2_SBX_LIVE=1 uv run pytest -q packages/harness2/tests/test_sbx_runner.py -m requires_sbx_live`. It sets the override above for its own trivial candidates, creates, uses and removes throwaway `harness2-*` sandboxes (no model, no workspace mount, `shell` agent only) and asserts exit codes, stdout/stderr, the staged files, a network that gives 403 or no data, a wall-clock kill, and an empty `sbx ls` afterwards. Only if it passes, add that version to `VERIFIED_SBX_VERSIONS`. No other test starts a VM: the live test is skipped unless `COGNITIVE_HARNESS2_SBX_LIVE=1`.

#### How `SbxRunner` runs a candidate

`preflight` (the daemon socket is a socket owned by this user at the path `sbx daemon status` reports; `sbx version` is in the verified set the runner was given; `sbx policy ls --json` is deny-all) -> `sbx create shell --name harness2-<hex> --no-progress --cpus N --memory M --pull missing --skills off` (no PATH: **no host bind mount**) -> `sbx ls -q` lists it -> `sbx policy check network --sandbox NAME --json` must deny `example.com:443`, `example.com:80`, `127.0.0.1:80` and `localhost:80` -> `sbx cp <stage>/candidate NAME:/tmp` and an `sbx exec find` that must list exactly the staged files -> `sbx exec -w /tmp/candidate -- NAME CMD...` under a host-side wall clock (the client process group is SIGKILLed on timeout) -> **always**, in a `finally`, `sbx rm --force NAME`, then `sbx ls -q` must not list it (a sandbox that stays is an error). The staging-key rules are `DockerRunner`'s (`_validate_stage_names`, checked before any process starts). Every `sbx` call is an argv list without a shell, with stdin closed, and with every `DOCKER_SANDBOXES_*` and `SBX_*` variable removed from its environment (the CLI reads its settings from them); nothing from the host environment is passed into the VM. Any surprise (unknown version, unparseable output, a policy that is not deny-all, `sbx exec` exiting 125, which `sbx` uses for "could not run it", or 255 with sbx-shaped stderr, a leaked sandbox) raises `SandboxUnavailable`: nothing runs on the host. In `sbx` mode that is the end; in `auto` the runner built by `default_runner` falls back to Docker for a failure **before** the candidate started (see "Selection is not a guarantee a VM can be created").

#### Live findings (sbx v0.50.1, Linux, 2026-10-31)

Run against the real CLI and a real microVM with throwaway `h2-verify-*` `shell` sandboxes (no agent, no model, no workspace mount, every one removed, `sbx ls` empty afterwards). The first attempt on 2026-10-30 failed at `create` with `Hypervisor error: KVM error: Permission denied` because the `sbx daemon start` process lacked the `kvm` group; after it was restarted with `sg kvm -c 'sbx daemon restart'` everything below worked.

| Operation | Real command | Observed |
| --- | --- | --- |
| version | `sbx version` | exit 0, `v0.50.1 <commit>` |
| global policy | `sbx policy ls --json` | exit 0, `{"rules":[{id,name,resource_type,decision,resources,status,actions,...}]}`; `default-deny-all` is `network`, `deny`, `**`, `net:connect:tcp`+`udp`, `active` |
| policy check | `sbx policy check network --json [--sandbox N] TARGET` | a denied target prints `{"allowed":false,...}` and **exits 1**; a missing sandbox exits 1 |
| create | `sbx create shell --name N --no-progress --cpus 1 --memory 512m --pull missing --skills off` | ~4 s with the image present, exit 0, no login prompt; `sbx ls --json` shows `cpus`, `memory_mib`, status `running` |
| list | `sbx ls -q` | names one per line, nothing when empty |
| exec | `sbx exec [-w DIR] -- N CMD...` | stdout and stderr stay separate, exit status is the command's, ~0.6 s overhead; stdin is closed when the client's is; flag-looking arguments after the name (`-x -y`) reach the command |
| exec, missing command | `sbx exec N nope` | exit **127**, `OCI runtime exec failed: executable file ... not found` (a missing `-w` directory also exits 127) |
| exec, missing sandbox | `sbx exec nope CMD` | exit **125**, `sandbox 'nope' not found` |
| exec, wedged VM | after a 4000-process fork flood | `failed to start stdin stream: ... i/o timeout`, exit **255**, or `error: inspect exec: context deadline exceeded`; the sandbox still showed `running`, and `rm --force` still removed it |
| cp in | `sbx cp DIR N:/tmp` | exit 0; the directory lands inside an existing destination, a repeat merges (no nesting); file modes (0444) kept; owner `agent` |
| cp in, symlinks | `sbx cp DIR N:/tmp/c` (with and without `-L`) | links are copied **as links**, even ones pointing outside the stage and with `-L` (the refusal in `--help` applies to a *workspace*, which these sandboxes do not have) |
| cp out | `sbx cp N:/tmp/out.txt DST/` | works, also directories; a candidate-made symlink to `/etc/passwd` arrives on the host **as a symlink**, which is why the runner copies nothing out |
| cp, bad path | `sbx cp F N:/nope/` | exit 1, `destination directory ... does not exist`; `cp` writes as root, `exec` runs as `agent` |
| rm | `sbx rm --force N` | ~0.7 s, also while an exec is running (the client then exits); removes the VM |
| wall clock, client kill | SIGKILL or SIGTERM of the `sbx exec` client | **the process inside the VM keeps running** (`ps` still showed `sleep 300` 3 s later) and the sandbox stays `running`; only `rm --force` stops it. The runner therefore always kills the client AND removes the sandbox |
| wall clock, inside | `sbx exec N timeout -s KILL 3 sleep 300` | `timeout` exists in the image; exits 124 after 3 s |
| user | `id` | `uid=1000(agent)` in groups `sudo`, `docker`; **`sudo -n` works (root)**; default cwd `/home/agent/workspace`; Ubuntu 26.04, 1 vCPU and 488 MB RAM for `--cpus 1 --memory 512m` |
| filesystem | `touch` | `/` is not writable by `agent` but is a **writable overlay for root** (`rw`, not read-only); `/tmp` and `$HOME` are writable; no `/candidate`, no workspace mount; 20 GB disk |
| resources | `--cpus`, `--memory` | enforced by the VM size (`nproc` 1, `MemTotal` 488 MB); there is no cgroup or pid limit inside (`ulimit -u` 1896); an unbounded memory or process hog wedges **that VM**, not the host |
| network, proxy path | `curl http://example.com/` (the VM's `HTTP_PROXY`) | **HTTP 403 from the sandbox gateway**, curl exit 0; the same for `host.docker.internal:11434` |
| network, direct | `curl --noproxy '*'`, a raw socket | `connect()` **succeeds** (a transparent proxy accepts) but no data flows: `Empty reply from server`, `recv` returns nothing, including for the host's loopback ollama through `host.docker.internal`, `gateway.docker.internal`, `172.17.0.0` and the host's LAN IP; DNS does not resolve (`gethostbyname` fails); `sbx policy log` lists every attempt as BLOCKED, `no matching allow rule (default deny)` |
| env inside | `env` | `HTTP(S)_PROXY`, `*_API_KEY=proxy-managed`, `GH_TOKEN=gho_sbxproxymanaged...` (placeholders, `SBX_CRED_*_MODE=none`; `sbx secret ls` is empty), `SSH_AUTH_SOCK=/run/ssh-agent.sock` (`ssh-add -l`: `communication with agent failed`, no host agent here) |

What this means:

- **The boundary is the microVM, not the user inside it.** The candidate is `agent` with passwordless `sudo` and a writable root overlay; that is contained by the VM, which is thrown away.
- **"No network" is "nothing gets through", not "connect() fails".** The runner's check is the policy engine's verdict; the live test measures 403 or an empty reply, never content.
- A timeout kills the client, but the in-VM process lives until `rm --force`; the runner's `finally` guarantees that. Exit 255 with sbx-shaped stderr is treated as sbx's failure, a bare 255 as the candidate's.
- Remaining caveats: `ssh.agentForwardingEnabled=true` and `browser.enabled=true` were the defaults on this host (`sbx settings ls`) when these runs were made, and were not checked then; they are now checked (see "The two host-reach settings"). The first `create` pulls the shell image over the network (done by the daemon, not the candidate); `--pull missing` avoids repeated pulls. `SandboxSpec` has no artefact field, so nothing is copied out. The stage-1 proposer and solver sessions still use rootless Docker (they need the model route): nothing in this runner changes them.

## sbx model route

The proposer and solver sessions call a model, so for them `sbx` needs a route to our egress proxy and to nothing else. The section above says "`sbx` has no route to one"; that was true of the GLOBAL deny-all policy and is not true of a sandbox-scoped rule. This section records the spike (2026-11-01, sbx v0.50.1, Linux/WSL2, throwaway `h2-sbxm-*` sandboxes), the design that keeps the Docker route's security properties, and what was built. **The route is opt-in and never the default**: it is selected only by `COGNITIVE_HARNESS2_MODEL_SANDBOX=sbx` (see "Selecting it"), and it must not become a default before a real stage-1 run has succeeded on it, which only the user can run.

### Spike findings (sbx v0.50.1)

**The policy language** (`sbx policy allow network --help`, read before use):

- A rule's resources are host names, domains, IPs or CIDRs, wildcards (`*.x.com`, `**.x.com`, `?`, `[12]`) and an optional `:port`; TCP by default, `--protocol tcp,udp`.
- It can also be an HTTP rule: `--method GET,POST|ANY` and `--path GLOB` (default `/**`), repeatable `--query name=value`. **The sbx proxy enforces them**: with one rule `GET localhost:18766 /a`, `GET /a` gave 200 and `GET /b`, `POST /a` and `GET /a/x` gave 403 (a raw `net:connect` check of that host:port reports "Authorization required by policy"). Its path match is coarse (`/a/`, `//a` and `/%61` also match `/a`; `/A` does not), so it is a SECOND layer under our exact filter, not a replacement.
- A deny beats an allow for the same name. The global `default-deny-all` (`**`, tcp+udp) is a rule like any other and stays untouched.

**What "the host" is.** Inside the VM `HTTP(S)_PROXY=http://gateway.docker.internal:3128` (the sbx policy proxy) and `NO_PROXY=localhost,127.0.0.1,::1,gateway.docker.internal`. The VM's own `127.0.0.1` and `localhost` are the VM's loopback (nothing there: connection refused). The way out is the proxy, and it maps **`host.docker.internal` to the HOST's loopback**: `curl http://host.docker.internal:18765/` reached a server bound to `127.0.0.1:18765` on the host (no `0.0.0.0` bind needed), and the policy engine names that target **`localhost:18765`**, not `host.docker.internal:18765` (a rule written with the latter did nothing: "Approval required for localhost:18765"; `policy check` of `host.docker.internal:P` reads "denied" while the real request is judged as `localhost:P`, so the connectivity test, not `policy check`, is the truth for that name). The host side sees `Host: localhost:<port>` (the proxy rewrites it) and an origin-form request.

**Exactly one host:port is possible, and it can be scoped to one sandbox.**

| Question | Finding |
| --- | --- |
| Scope | `--sandbox NAME` adds the rule to policy `local` scoped to that sandbox (`scope: sandbox:NAME`, `origin: scoped`). Without it the rule is **global** (daemon-wide). `sbx policy ls --json` (no argument) lists every rule of every scope; `sbx policy ls --json NAME` lists only the rules that APPLY to NAME (global ones plus its own: a rule scoped to another sandbox is absent) |
| Isolation between sandboxes | A second sandbox got 403 for the first one's allowed port; the first one's rule never appeared in the second's listing |
| Can the rule exist before the sandbox? | No: `allow network --sandbox nosuch` is `sandbox "nosuch" not found`. The sandbox is created first (it has no route yet: global deny-all), then the rules are added |
| Removal | `sbx policy rm network --sandbox NAME --id RULE_ID --force` (`--force` is required without a terminal; without it: `stdin is not a terminal; pass --force`; removal by `--resource` hits the same prompt). Effective at once (the next request was 403). **`sbx rm --force NAME` removes the sandbox's scoped rules with it** (listing before: 1 scoped rule; after: only `default-deny-all`) |
| Rule ids | `allow network` prints `Added rule <uuid> to policy local (scope: sandbox:NAME): ...`; rules have no names, only a uuid, so the owner of a rule is its sandbox name |
| Wrong guesses are safe | `host.docker.internal:22`, `localhost:11434` (the real Ollama), another port of the same host, `127.0.0.1:P`, `gateway.docker.internal:P`, direct sockets (`curl --noproxy`) and a CONNECT tunnel to the port all failed (403, empty reply or timeout) with only the HTTP rules present |

**opencode in the VM.** `~/.opencode/bin/opencode` (v2.0.25, 200 MB) is copied with `sbx exec -u root N mkdir -p /opt/cli` then `sbx cp opencode N:/opt/cli/` (about 1.4 s) and runs: `HOME=/tmp/h XDG_*=/tmp/h/... /opt/cli/opencode --version` prints `opencode v2.0.25`. Ubuntu image, `python3` present, user `agent` (uid 1000, `sudo`, group `docker`).

**exec, env, stdin.** `sbx exec -i -e K=V N CMD` passes env and stdin (a piped line came back through `cat`), exit status is the command's, stdout and stderr stay separate. The container env also carries the proxy variables, `*_API_KEY=proxy-managed` placeholders and `GH_TOKEN=gho_sbxproxymanaged...`: the runner therefore starts every session through `env -i` with a COMPLETE environment of its own.

**Workspace mount.** `sbx create shell NAME /abs/dir` bind-mounts the directory (virtiofs, read-write) **at the same absolute path** in the VM. A file the VM user creates is owned by the HOST user on the host (also modes 0600 and a symlink to `/etc/passwd`, which arrives as a symlink: harvest is host-side and does not follow links, as for Docker). So no uid juggling and no `reclaim` container are needed; a host-side `chmod u+rwX` walk covers a directory the session left unreadable.

**Limits.** `--cpus` and `--memory` are VM-level (verified: `free` showed 476 MB for `--memory 512m`). There is no pids limit at VM level (`ulimit -u` says 1896 as the default, and the in-VM user has `sudo`, so a `ulimit` is advisory); the wall clock is the host-side kill of the `sbx exec` client PLUS `sbx rm --force` (killing the client does not stop the in-VM process: README, "Live findings"). A request-size and request-count cap is the egress proxy's, as in Docker.

**Daemon-global side effects of this spike:** none left. The global policy was never changed (every rule was sandbox-scoped and went away with its sandbox). It was saved first (`sbx policy ls`, `--json`, `settings ls`) and compared after: identical.

### Design: the same properties as the Docker route

| Property | Docker route | sbx route |
| --- | --- | --- |
| The model endpoint is OUR filtering proxy | proxy container, unix route to the host bridge, `OLLAMA_ROUTE_ALLOW` (5 `(method, path)` pairs) and model pinning | the SAME `egress_proxy.py`, run as a host process on `127.0.0.1:<P>` (a random free port, never Ollama's) with the same unix route to the same `HostBridge` socket, the same allow list and `allowed_models`; the VM reaches it as `http://host.docker.internal:<P>` through the sbx proxy |
| The session reaches ONLY that proxy | `--internal` network, no default route; `NO_PROXY` is the session's own loopback | global deny-all stays; per sandbox exactly five HTTP allow rules `localhost:<P>` (one per `OLLAMA_ROUTE_ALLOW` pair); nothing else applies to the sandbox, which the runner reads back with `sbx policy ls --json NAME` before it starts anything |
| Second filter | none | the sbx proxy's own method and path match on those five rules (coarse), under our exact filter |
| Teardown cannot be interrupted | `_shielded_from_interrupts` + `_Cleanup` | the same, reused |
| Crash safety | labelled containers/networks, `reap_all` | the sandbox is named `h2m-<run scope>-<pid>-<hex>`; its rules die with it; a startup sweep removes every `h2m-` sandbox whose pid is dead (and with a run scope set, only that scope's) |
| Leftovers reported | containers, networks, directory | sandbox, scoped rules, host proxy process, directory |

**What "deny-all plus our own rule" means, precisely.** For SELECTION (read-only, global listing): an active global `**` deny for tcp and udp must exist; **no global network or http allow rule may exist** (any scope other than `global` or `sandbox:<name>` is refused as unknown); a rule scoped to an `h2m-` sandbox must be one of ours (an HTTP allow of `localhost:<port>` with a method and a path) or it is refused; a rule scoped to some other sandbox does not apply to a sandbox this runner creates and is ignored (but a rule of a resource type outside the observed allow-list `network*`, `http`, `filesystem:read`, `filesystem:write` is refused whatever its scope or decision: fail closed). For a SESSION (authoritative, per sandbox, after the rules were added): `sbx policy ls --json NAME` must show exactly the active global deny-all, plus exactly the five HTTP allow rules (methods and paths equal to `OLLAMA_ROUTE_ALLOW`, host `localhost:<P>`, scoped to NAME), and nothing else that allows anything; `sbx policy check network --sandbox NAME` must deny `example.com:443`, `example.com:80`, `127.0.0.1:80`, `localhost:80`, `localhost:<ollama port>` (the real Ollama) and `localhost:<P+1>`. A foreign allow rule that applies to the sandbox (a global one added after selection, say) makes the session refuse and tear down before the CLI starts.

**Why the endpoint is rewritten at run time.** The CLI's provider URL is built before a session exists (`runner.local_endpoint`, `http://harness2-proxy.internal:<ollama port>`), but the proxy's port is chosen per session. The sbx runner keeps `local_endpoint` as the logical URL (so every upstream-egress check and the config stay byte-identical to the Docker route) and replaces exactly that origin, in the values of the session's environment only (never argv, never a file), with `http://host.docker.internal:<P>`.

**Lifecycle (`SbxSessionRunner.run`).** sweep stale `h2m-` sandboxes of dead owners -> preflight (daemon, verified version, global policy as defined above, host-reach settings, the same checks `SbxRunner` makes) -> bridge, proxy config, host proxy started and ready -> `sbx create shell --name h2m-... --cpus --memory --skills off WORKSPACE` -> five `policy allow network --sandbox` rules -> read back and check the per-sandbox policy -> read the VM's `HTTP_PROXY` (must be the gateway) -> copy the CLI in -> `sbx exec ... env -i ... CLI ARGS` under the host wall clock -> **teardown, shielded from SIGINT/SIGTERM/SIGHUP and each step run whatever the one before did** (for the whole session, in the main thread, SIGTERM and SIGHUP are turned into `KeyboardInterrupt`, so a plain `kill`, a systemd stop or a closed terminal takes the same path as Ctrl-C; the previous handlers are restored afterwards; in another thread nothing can be installed, and a kill there is a hard kill): remove the rules by id, `sbx rm --force`, prove with `sbx ls -q` and `sbx policy ls --json` that neither the sandbox nor a rule of it remains, stop the host proxy (it prints the final `ACCESS-SUMMARY`), close the bridge, remove the private directory, then the host-side workspace permission fix. A hard kill of the Python process cannot run teardown: the proxy gets `PR_SET_PDEATHSIG` (it dies with its parent), and the sandbox, with its rules, is removed by the next run's startup sweep; until then the rules name a port nothing listens on, and the sweep exists so that a later unrelated process that happens to bind that port is not reachable from a stale VM.

### What was built and what it was measured on

`sbx_session.py`: the pure policy reading and names (`model_policy_problem`, `sandbox_policy_problem`, `stale_rule_sandboxes`, `sandbox_name`), the selection (`choose_model_runtime`, `ModelRuntime`), `SbxSessionRunner` (a `ProposerSessionRunner` subclass: same `authorize`, budget, observer, `SessionResult`, `SessionCleanupError`), `make_runner` (Docker or sbx, never a fallback) and the sweep's leftover check. The runner and the stage-1 wiring change no Docker behaviour (the image tag is unchanged: `egress_proxy.py` and the Dockerfile are untouched, the proxy simply also runs as a host process here).

**Live, on sbx v0.50.1 (2026-11-01), `tests/test_sbx_session.py -k live`, under the shared lock:**

| from inside the VM | result |
| --- | --- |
| `GET /v1/models`, `GET /api/tags`, `POST /v1/chat/completions` (pinned model), through the sbx proxy to `host.docker.internal:<P>` | 200, 200, 200 (the stub saw exactly these three, with a `Host` a real Ollama accepts) |
| `POST /api/delete`, `POST /api/pull`, `GET /api/ps` | 403 from the **sbx** layer: our proxy's own log counts none of them, so the second filter is real |
| `POST /v1/chat/completions` naming another model | 403 from OUR proxy (`unix-route-model-denied`, the only denial in its summary) |
| `example.com`, `host.docker.internal:22`, `host.docker.internal:11434` (the real Ollama's port), `host.docker.internal:<P+1>` | 403 |
| raw socket to `host.docker.internal:<P>` and to `gateway.docker.internal:<P>`, `CONNECT host:<P>` through the proxy | no data / timeout |
| `127.0.0.1:<P>` inside the VM | connection refused (the VM's own loopback) |
| the REAL opencode v2.0.25, as a stage-1 solver session (`solver_argv`, `solver_env_builder`) against the stub | exit 0, the stub's streamed `STUB-DONE`, 2 model requests allowed (`GET /api/tags`, `POST /v1/chat/completions`), 0 denied, about 20 s including the create and the 200 MB copy-in |
| afterwards | `sbx ls` empty, `sbx policy ls --json` identical to before, nothing of ours left |

Findings that changed the code: the VM's `sh` is dash and has no `ulimit -u` (the session wrapper is `bash`); the policy engine names the host `localhost`, not `host.docker.internal`; HTTP-only rules block the CONNECT tunnel that a plain `host:port` rule would have allowed, so the route is five HTTP rules and no raw tcp rule; the VM's `HTTP_PROXY` is read and must be the gateway (`http://gateway.docker.internal:<port>`) or the session refuses.

**Caveats, stated plainly.**

- **Not the default, and not proven on a real model.** The opt-in route passed against a stub model and the real opencode binary; nothing says how it behaves with a real gemma 12B (latency over the sbx proxy, a long streamed reply, context size). Only the user can run that: `COGNITIVE_HARNESS2_MODEL_SANDBOX=sbx uv run python -m cognitive_harness2.stage1 plan`, then `run --approve`. Docker stays the default until it has.
- **The Docker preflight still runs.** `stage1 plan` still requires rootless Docker and the proposer image to pass (the sbx route does not use either; a later change can make them optional under sbx).
- **Isolation is a different shape.** Docker: a read-only root, no capabilities, user 65534, pids and memory cgroups. sbx: a microVM (a stronger boundary) in which the session user `agent` has passwordless sudo and a writable root; `--cpus` and `--memory` are VM-level, `ulimit -u` is advisory, and a session can change anything inside its own VM and in its workspace (as in Docker) but nothing else. `host.docker.internal` reaches the host's loopback ONLY on the rule's one port.
- **Another process that binds the allowed port can be reached by that sandbox while it runs.** The port is random, the rule exists only for that sandbox and is removed with it, and the proxy that owns the port is started first; a stale rule after a hard kill is removed by the next run's sweep (a leaked sandbox keeps its five rules until then).
- **A foreign rule scoped to another sandbox is ignored at selection** (it cannot apply to a sandbox this runner creates); the per-sandbox listing is the authoritative check and refuses anything else that applies.
- **The sbx proxy's HTTP match is coarse** (`/a/`, `//a`, `/%61` all match `/a`): it is a second layer, the exact filter is ours.
- **Daemon-global effects: none.** Every rule is sandbox-scoped. The global policy was never changed by the spike, the tests or the runner (compared before and after each live run).

### Selecting it

`COGNITIVE_HARNESS2_MODEL_SANDBOX` unset, empty or `docker`: rootless Docker, exactly as before. `sbx`: the sbx route, **only if** the sbx version is in `VERIFIED_SBX_MODEL_VERSIONS`, `/dev/kvm` and the daemon work, the global policy passes the definition above and both host-reach settings are off. Otherwise `stage1 plan` shows a failing `model_runtime` check with every reason and the run refuses: **there is no fallback to Docker** when sbx was asked for. Any other value is refused too (`auto` does not exist for model sessions). `stage1 plan` prints `model sessions run in: ...` in the preflight list so the runtime is visible before approving.

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

For a local channel the CLI must be configured with `runner.local_endpoint` (`http://harness2-proxy.internal:<port>`), because the session's own `127.0.0.1` is empty. That name is NOT in `NO_PROXY`: a proxy-aware client sends the request THROUGH the proxy, whose config routes exactly (`harness2-proxy.internal`, port) and (`127.0.0.1`, port) to the bridge socket (log line `reason=unix-route`, one per request); a client that connects to the listener directly lands on the same socket and is served the same way. Both are filtered to ollama's chat and model-list paths (next paragraphs). Measured with the real opencode: it goes through the proxy.

**The host-bridge route is filtered, not tunnelled.** The bridge socket reaches the WHOLE HTTP API of the host's ollama, and a session is untrusted: raw bytes would let it `DELETE /api/delete`, `POST /api/pull`, `/api/create`, `/api/push`, `/api/copy`. So the proxy speaks HTTP on a unix route and on its listener (`egress_proxy.py`, config `unix_routes[].allow`, built in `session_runner.proxy_config` from `egress_proxy.OLLAMA_ROUTE_ALLOW`):

| allowed (method, exact path) | why |
| --- | --- |
| `POST /v1/chat/completions` | the model call: opencode sends one per model step (streaming SSE) |
| `GET /api/tags` | opencode sends it once per session before the first step (measured below); read-only, lists the installed model names |
| `GET /v1/models` | the OpenAI-compatible model list; opencode does not send it today |
| `POST /v1/completions` | the completion twin of chat; opencode does not send it today |
| `POST /api/show` | added 2026-10-31 after real stage-1b runs: opencode v2.0.25 sends `{"model": ...}` here at the start of each model step to learn the model's capabilities and context length (55 denials in those runs, the only request denied; the denial was harmless but left opencode without that metadata). Read-only: it returns the Modelfile, template, parameters and details of a model that is already installed, which is low-sensitivity text, and it changes nothing on the host. The sandbox cannot use it to change anything: `/api/pull`, `/api/create`, `/api/copy`, `/api/delete`, `/api/push`, `/api/generate` and the rest stay denied. It gets the same strict framing and the same request body cap as the other POSTs (the route table has no per-entry caps; the shared cap is far above a `{"model": ...}` body) |

Nothing else: not `/api/delete|pull|push|create|copy|generate|chat|ps|embed|embeddings|blobs` or any other `/api/*` (only `/api/tags` and `/api/show` are on the list), nor another method, spelling or suffix of `/api/show` (`GET`, a trailing slash, a query, other case, `%2f`, `/api/shows`), not `/v1/embeddings`, not `/v1/models/{id}` (opencode does not need it), not another method on an allowed path. The match is exact on the path as written: a query string, a trailing slash, other case, percent-encoding, `//`, a dot segment or a fragment is a different path and is denied; the forwarded path is the allow-listed literal, never a normalised one. What a refusal looks like: HTTP 403 `unix-route-path-denied` to the session, and one log line `ACCESS decision=deny host=<route host> port=<port> reason=unix-route-path-denied method=<M> path=<P>` (never a header or body; see the next paragraph for `method` and `path`), after which the connection is closed politely (FIN, bounded drain, as every refusal). Other codes: `unix-route-connect-denied` (a CONNECT tunnel to a unix route is refused: it would carry any request), `unix-route-host-mismatch` (a `Host` that disagrees with the absolute-URI, or on the listener is not a name routed to its socket), `unix-route-bad-framing`, `unix-route-unsupported-header`, `unix-route-bad-connection`, `bad-chunking`, `ambiguous-body-framing`, `unix-route-body-too-large` (413), `unix-route-request-limit` and `unix-route-pipeline-limit` (429), and after a head is sent `unix-route-response-too-large` and `unix-route-response-aborted` (the connection just ends).

**What was denied, by method and path.** Only a `unix-route-path-denied` line carries `method=<M> path=<P>`: `M` is the request method if it is 1 to 16 capital letters (else `?`); `P` is the request target cut to 80 characters, with every byte outside printable ASCII 0x21-0x7e percent-escaped (so a path cannot add a space, a field or a line to the log) and the query string replaced by `?` and its length (`/v1/models?<12>`, so a credential in a query never reaches the log). `session_runner.egress_summary` turns these into `denied_requests`, a map `"METHOD path"` to a count (at most 20 distinct keys, the rest under `other`; absent when no line has the fields, as in older logs), which the `session_end` audit event, `report.json` `sessions_summary.denied_requests` (summed over sessions) and the end-of-run `denied requests:` line show. Use it to find out what the CLI sends that is not on `OLLAMA_ROUTE_ALLOW` (for example `GET /api/version`, a `HEAD`, or `GET /v1/models?<n>`). If a path opencode legitimately needs shows up, widen `OLLAMA_ROUTE_ALLOW` deliberately (one exact method-and-path pair, with a test and a README row); never loosen the matching. The proxy runs inside the session image, so a changed `egress_proxy.py` takes effect only in a rebuilt image (the image tag changes).

**The model a request names is checked too.** A path filter alone left the model free: ollama obeys the `model` in the body, a signed-in host ollama relays `name:cloud` off the machine, and any large local model could be picked. Every unix route therefore carries `allowed_models`, a required list (1 to 32 exact names, no wildcard, none containing `cloud`; a route without it fails to load, as one without `allow` does, and all routes to one socket must agree). The session runner fills it from the approved channel's model id without its provider prefix (`ollama/gemma4-e4b-16k` gives `gemma4-e4b-16k`, which is what the opencode config sends) and, when the name has no tag, also the `:latest` spelling ollama accepts. For `POST /v1/chat/completions`, `POST /v1/completions` and `POST /api/show` the proxy, which has the whole body buffered (at most 1 MiB), parses it strictly before anything goes upstream: UTF-8 JSON, one object and nothing after it, no duplicate key anywhere, no `NaN`, no top-level key that is a case or Unicode fold of `model` (Go matches field names case-insensitively, so `{"model":"ok","Model":"x:cloud"}` would otherwise be read by ollama as `x:cloud`), `model` a string equal character for character to one allowed name (on `/api/show`, `name` is accepted in place of `model`, and when both are present both must match). Anything else is a 403 `unix-route-model-denied` and one log line `ACCESS decision=deny host=<h> port=<p> reason=unix-route-model-denied model=<M>`, where `<M>` is the model string escaped and cut like a path (printable ASCII only, 64 characters) or one of the fixed words `(missing)`, `(non-string)`, `(unparsed)`, `(not-an-object)`, `(key-lookalike)`. The bytes forwarded are the original body.

**Counters that a flood cannot cut.** The runner reads the proxy container's log and keeps at most 256 KiB of `ACCESS` lines, so after a flood of denied requests the later lines were dropped and the counts under-reported. The proxy now keeps running counters itself (allowed, denied, model requests, denied hosts and reasons, a bounded `denied_requests` map) and prints them as ONE `ACCESS-SUMMARY {json}` line every 100 decisions and once more, with `"final":true`, at graceful exit. The runner stops the proxy (`docker stop -t 3`) after the session so that final line is in the log, keeps every valid summary line past the cap, and `egress_summary` uses the latest one when it is final or when the lines were cut; with no summary it counts the lines and reports `truncated: true` when the cap was hit. A summary line is accepted only if it is exactly what the proxy prints (known keys, plain non-negative integers, bounded lists of well-formed tokens); a forged one inside a path cannot exist, because every request-derived value in a log line is escaped to one printable token and so cannot start a line.

Every request on a keep-alive connection is parsed on its own: strict request line and headers (no obs-fold, no bare CR/LF, no control characters, no duplicate `Content-Length`, no `Transfer-Encoding` with `Content-Length`), `Content-Length` digits only, chunked decoded strictly (hex size, no extension, no trailer), no `Expect`, `Upgrade`, `TE`, `Trailer` or a `Connection` token other than `keep-alive`/`close`, a body on a GET refused, and the request is rebuilt: allow-listed path, `Host` pinned to the route's own name, hop-by-hop headers dropped, `Content-Length` framing only, the body read and bounded BEFORE anything is sent upstream (so a request that fails halfway never reaches ollama). HTTP/1.0 is served once and closed. Responses stream through unchanged (SSE keeps working, byte for byte; tested with a stub that streams over chunked encoding) while their framing is followed (Content-Length, chunked, or until close) so the next request on the connection is told apart; a malformed upstream answer is a clean 502. Caps (config `limits`, defaults): 1 MiB per request body (`route_max_request_bytes`; the real opencode sends about 15 KB and grows with the history), 64 MiB per response (`route_max_response_bytes`), 2000 forwarded requests per proxy, i.e. per session (`route_max_requests`), 2 pipelined requests queued behind the one in flight (`route_max_pipelined`), 60 s for a request body (`body_timeout_s`), plus the existing head, idle, lifetime and byte caps. The log line of an allowed request is now one per REQUEST (`reason=unix-route`, or `unix-listener`), so `model_requests` counts requests, several per keep-alive connection.

**The image tag changes.** The proxy ships in the image by content hash; this change edits `egress_proxy.py`, so the tag differs and the image must be rebuilt (`uv run python -m cognitive_harness2.image build`) before any Docker-backed test or run. A session started against an old image has the unfiltered proxy.

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

**The task, `sum-signed`** (`sum_signed.py`). `input.txt` holds five signed amounts; the solver writes `Total: X.XX` (the signed sum, two decimals) to `output/answer.txt`. Three domains (household, retail, payroll) of two tasks each, amounts derived from sha256 of the task id, so they never drift; a positives-only sum is wrong on every instance. A harder family, `sum-signed-hard` (twelve amounts in mixed notations, a base harness that does not explain them), is described under "Stage 1b (hard task set)".

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

# 2. The run. Needs --approve <channel> AND a phrase plus a fresh 4-word nonce that you type on this terminal (run it from a plain shell yourself).
uv run python -m cognitive_harness2.stage1 run --approve local-ollama --opencode ~/.opencode/bin/opencode

# Rehearsal with a FAKE opencode and a FAKE Ollama on the real daemon (no model, safe to run any time):
uv run python -m cognitive_harness2.stage1 selftest            # --keep-in DIR keeps the audit and report
```

Options: `--wall-s N` (total wall clock; it can only LOWER the profile's `wall_s`, raise that in the profile), `--per-session-s N` (default 600), `--runs-dir DIR` (the PARENT of the per-sweep directory, default `~/.local/state/cognitive-engine/harness2-stage1`), `--classification` (default `open`: the task, the harness and every prompt are synthetic), `--channel` (default `local-ollama`), `--task-set easy|hard` (default `easy`; `hard` is stage 1b, below).

`plan` prints the channel, endpoint, model, caps, expected P and R, timeouts, classification, that nothing leaves the machine, each preflight result and the exact `run` command. What `plan` does: read the profile, ask the channel policy, run the pinned read-only `docker info` and `docker image inspect`, send one `GET /api/tags` to the loopback Ollama. It creates no file, directory, container, network or socket, and `plan` is the default command.

`run` refuses (exit 2, nothing started, nothing created) unless: `--approve` names the channel, stdin and stdout are a terminal, every preflight check passes (rootless Docker, the pinned image, `opencode` found, Ollama answering with the model, the workspace root), `plan_sweep` accepts, and you type `approve local-ollama <sweep id>` followed by the four-word nonce shown on the terminal, exactly. Only then does it create the run directory and the `ExecutionGrant` (channel, sweep id, who approved, expiry, caps: 3 proposer + 4 solver sessions, the wall clock, the per-session timeout), and start the first session; the host bridge opens per session, so nothing listens before the grant exists.

**What the approval gate does and does not stop.** It stops pipes, redirected stdin or stdout, scripts that have no terminal (the confirmation is read from the controlling terminal, `/dev/tty`, never from stdin, and the typed text must include a fresh four-word nonce shown there), and anything running inside a known agent-harness environment (the run is refused when `CLAUDECODE`, `CLAUDE_CODE_*`, `ANTIGRAVITY*`, `GEMINI_CLI*`, `AGY_*`, `OPENCODE*` or `CODEX*` is set; no flag overrides that). It does **not** stop a determined process of the same user that drives a pseudo-terminal (python `pty.fork`, `script`, `expect`, `tmux send-keys`): such a process can read the phrase and the nonce and type them back, and it can clear the environment markers first. `tests/test_stage1_approval_pty.py` asserts both sides of this on purpose. The human at the terminal remains the control: do not start `run` from a session an agent can drive, and do not leave a terminal with a pending prompt open to one.

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
| model traffic | goes THROUGH the proxy (`http://harness2-proxy.internal:<port>/v1`, log `reason=unix-route`), as a proxy-aware client does. Per session: one `GET /api/tags`, then one `POST /v1/chat/completions` per model step (and, against a real ollama, one `POST /api/show` before each step, see the next row) (streaming, `stream_options.include_usage`, `store: false`, `max_completion_tokens: 32000`). No `Authorization` header: no key is needed. |
| the exact requests opencode sends (route filter, measured 2026-10-31 through an instrumented proxy, scenarios text, write, shell) | in absolute-URI form to the proxy, HTTP/1.1, `Connection: keep-alive` (plus `Proxy-Connection`, which is dropped), `Host` equal to the URI's authority, `Accept-Encoding: gzip, deflate, br, zstd`, `User-Agent: opencode/latest/2.0.25/cli`, tracing headers (`b3`, `traceparent`, `x-opencode-*`, `x-session-*`), no `Authorization`, no `Origin`, no query string. The complete list: **1. `GET /api/tags`** (`Accept: application/json`, once, first), then **2..n. `POST /v1/chat/completions`** (`Content-Type: application/json`, `Content-Length` 14.6 KB for the first step, 15 to 18 KB later; the head and the start of the body arrive in one packet). Never `/v1/models`, never `/v1/models/{id}`, never `/v1/completions`, never any `/api/*` but `tags` (against the stub model). **Amended 2026-10-31 after real stage-1b runs against ollama:** the real opencode v2.0.25 also sent **`POST /api/show`** (body `{"model": ...}`) at the start of each model step, 55 times, and it was the only request the proxy denied; the stub model never provokes it, which is why the probe above did not list it. It is now allowed (read-only model info, see the route-filter table). So the allow list is the five pairs above: three used (`GET /api/tags`, `POST /api/show`, `POST /v1/chat/completions`), two kept for the OpenAI-compatible surface. `test_every_request_the_real_opencode_sends_is_on_the_proxys_allow_list_and_none_is_denied` pins the stub-visible requests exactly and tolerates `POST /api/show` anywhere. The proxy ships inside the session image, so the change takes effect only in a rebuilt image (the tag changes with `egress_proxy.py`). |
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

## Stage 1b (hard task set)

**Why.** Stage 1 run #4 (2026-10-31) went end to end, but on `sum-signed` the base harness already scores 3 of 3 on all four rollouts, so every proposer stage kept the base and the improvement loop (propose, harvest, accepted edit, candidate rollout, selection) was never exercised with an ACCEPTED edit. `--task-set hard` runs the same sweep (P=3 proposer sessions, R=4 rollouts, k=1, `modes=('agg',)`, the profile's caps, $0, nothing leaves the machine) on a task family the base harness is built to get wrong.

**The task family, `sum-signed-hard`** (`sum_signed_hard.py`). Nine fixed instances (household, retail, payroll, three each; the default is `household/hard-1`), each a ledger of **twelve** amounts in the notations of an export from several systems, one of each in every instance, in a hash-shuffled order, six instances with CRLF line endings:

| notation | example | reads as |
| --- | --- | --- |
| plain | `-12.50`, `130.05` | itself |
| thousands separator | `1,234.56`, `-1,234.56` | `1234.56`, `-1234.56` |
| parentheses | `(45.00)`, `($1,234.50)` | negative |
| currency | `$45.10`, `-$12.30` | the number, sign kept |
| short forms | `12.3`, `7`, `+0.5` | `12.30`, `7.00`, `0.50` |
| trailing spaces | `45.10␠␠` | ignore them |

The output contract is `sum-signed`'s, unchanged: `Total: X.XX` as the one line of `output/answer.txt`, two decimals, a minus sign for a negative total, no sign on zero, so the grader is shared. The same two implementations of one verdict as before: a busybox `sh` + `awk` verifier that parses the messy input itself, and the Python reference parser behind `grade_answer`; the tests run a shared table through both in a real container (41 answer cases, 14 edge inputs the awk must total to the hand-written value, 27 unusable inputs that give no verdict from either).

**The weak base harness.** `systemprompt.md` is "You are a bookkeeper... add up the amounts and write the total to the answer file." It says nothing about parentheses, currency symbols, thousands separators, short forms, carriage returns or rounding, and neither does the task prompt (it says only that the lines "are not all written the same way"). `guardrails.md` holds one real protected line. `LongTermMEMORY.md` and `tool_guidance.md` are empty. The files are valid slot-table material: c1, c3 and c4 are mutable (no frontmatter), c2 is protected. The rules a good proposer would write down are `sum_signed_hard.FORMAT_RULES`; a reader that ignores the notations writes `sum_signed_hard.naive_total` (for `household/hard-1`: `Total: -638.11` against the right `Total: -1291.27`), which fails on the total alone (2 of 3 criteria).

**The slot table is applied inside the pipeline on every set.** `SumSignedAdapter.validate_tree`, which upstream calls on every proposed harness, also runs `upstream_surface.check_mutation` on each file changed against the base (so a `guardrails.md` edit, a frontmatter block, a script or any off-surface file is refused with the reason fed back as retry text, and a new skill is refused because c6 frontmatter is frozen). Upstream's own trigger lines appended to `guardrails.md` for a new skill are not blamed on the proposer. The easy set enforces it too since the F6 fix (it used to run without it, as in run #4, so a proposer could add a non-executable `scripts/x.py` (c7) or `plugin/p.ts` (c8) that still shipped in the solver's harness tree; both are refused now, and `TaskSet.enforce_slot_table` defaults to true, kept as a parameter only so a test can turn it off). Every proposal's verdict is audited as a `mutation_check` event (files changed, slots, accepted or refused; never a line the model wrote).

### How to run it

```bash
# Rehearsal with the fakes on the real daemon (no model; 5 sweeps of about 20 s each, one per scenario):
uv run python -m cognitive_harness2.stage1 selftest --task-set hard                   # --scenario NAME runs one; --keep-in DIR keeps the audit and report

# The plan: read-only, shows "task set hard" and prints the approve command with --task-set hard in it.
uv run python -m cognitive_harness2.stage1 plan --task-set hard --opencode ~/.opencode/bin/opencode

# The run: the same approval gate as stage 1 (--approve and the phrase and nonce typed on this terminal).
uv run python -m cognitive_harness2.stage1 run --approve local-ollama --task-set hard --opencode ~/.opencode/bin/opencode
```

The default is `--task-set easy`, so every command above this section means what it did; `plan` and `run` print the set, the audit's `sweep_start` and `report.json` record it, and the approve command spells `--task-set` out only when it is not the default. Read the result:

```bash
R=~/.local/state/cognitive-engine/harness2-stage1/<sweep id>          # or <runs-dir>/<sweep id>
jq '{task_set, task, selection, stages: [.stages[] | {label, kept_base, fell_back, error}]}' $R/report.json
jq '.rollouts[] | {label, passed, n_passed, answer_excerpt}' $R/report.json
jq -c 'select(.event == "mutation_check")' $R/audit.jsonl              # the slot-table verdict on each proposal
```

The run ends with a `selection` line, for example `selection improved: final agg1 (an edited harness was adopted); base1 failed, agg1 passed`.

### How selection decides at k=1 (read this before believing a verdict)

At k=1 upstream does **not** choose between the base and a candidate. `run_task` calls `selection.trivial_selection`, whose pool is exactly the k composed finals of the mode, here only `agg1` (the base, `inv1` and `dom1` are not in it by construction). It drops `agg1` only if its rollout delivered nothing or the artifact renders empty; otherwise it delivers it, with `chosen_is_base` always false, no selector session and no score read. So `adopted` in `report.json` (`selection.adopted`) means only that the delivered `agg1` harness is not the base (`chosen_tree_is_base` false, which needs the `agg1` stage to have an accepted edit: `kept_base` false, not `fell_back`). **An edit that makes things worse is adopted just the same.** Whether an adopted edit helped is a separate fact, from this runner's own verdicts on the base and chosen rollouts, which selection never sees: `selection.verdict` is `improved` (base1 failed, final passed), `regressed`, `unchanged`, `base-kept` (no accepted edit reached the final) or `unknown`. A test pins this upstream behaviour so a change of the pinned upstream cannot silently change the claim.

### What the selftest proves, and what it does not

`selftest --task-set hard` runs five sweeps on the real rootless daemon with a fake opencode that plays a SCRIPTED proposer and a solver that reads a ledger correctly only when its `AGENTS.md` says "parentheses" (otherwise it reads each line's leading number, like `naive_total`):

| scenario | the scripted proposer | what is asserted |
| --- | --- | --- |
| `format-rules` | appends `FORMAT_RULES` to `systemprompt.md` (a legal c1 edit, body only) in all three stages | the edit is HARVESTED (the on-disk `inv1`, `dom1`, `agg1` harnesses differ from the base in `systemprompt.md` only and carry the rules); it passed the slot table (an independent `check_mutation` after the fact, plus three accepted `mutation_check` events); it RAN as a candidate and changed the outcome (`base1` fails with the weak reader's exact answer, 2 of 3 criteria; `inv1`, `dom1`, `agg1` pass with the exact line); upstream's selection ADOPTED it (`agg1` delivered, `kept_base` false, `adopted` true, verdict `improved`) |
| `forbid-guardrails` | edits `guardrails.md` (c2) | every stage `fell_back` with the harvest's "staged protected file was changed"; no harness differs from the base; no slot-table event (the harvest refused first); base kept |
| `forbid-executable` | adds `scripts/helper.py` with the executable bit | harvest refusal ("executable"); base kept |
| `forbid-frontmatter` | prepends a frontmatter block to `systemprompt.md` | slot-table refusal (three refused `mutation_check` events, "frontmatter"); base kept |
| `forbid-script` | adds a plain `scripts/helper.py` (c7) | slot-table refusal ("slot c7 ... not mutable"); base kept |

In a refusal scenario every rollout fails (a base harness fails this task), the delivered `agg1` is base-equal, and the verdict is `base-kept`. Each sweep also asserts what stage 1's does: P=3, R=4, seven sessions all reaching the fake endpoint, clean teardown, no real model claimed. The easy selftest is unchanged (`selftest` with no flag; its report now also carries the `selection` block).

It proves the pipeline CAN run the loop: harvest, validation, candidate rollout and adoption all work when a proposer produces a legal edit, and the refusal layers hold. It does **not** show that any real model can produce that edit, or that the edit transfers: the "proposer" is a script and the "solver" is a switch on a keyword.

### What to expect with a 4B model (honest version)

- **The results will be noisy, and `k=1` with 4 rollouts is a smoke signal, not a measurement.** One instance, one rollout per harness, one sample each: a base that fails once may pass the next time, and a pass or fail of `agg1` says little about the edit. Nothing here is a statistical comparison.
- **The base may already pass.** A 4B model may read `(45.00)` and `$` correctly with no instruction (the task prompt only says the lines differ). Then this run reads like run #4: all stages keep the base, `verdict` is `base-kept` or `unchanged`, and the set is not hard enough for that model. That is a result.
- **The base may fail and the loop still not close.** The proposer sees the failing trajectories and the deliverable but never the expected total or the verdict, so it must infer the notation rules from an answer that does not match the input. A small model may edit nothing, edit something irrelevant, write a `<notes>` reply without touching `context/harness/`, or break a rule (every refusal is in `stages[].error`, and `mutation_check` shows slot-table refusals). Proposers cannot create skills on this set (c6 frontmatter is frozen), only edit `systemprompt.md`, `LongTermMEMORY.md` and `tool_guidance.md`.
- **An adopted edit is not a better harness.** See the selection section: at k=1 it is adopted because it exists. Judge `improved` against the `answer_excerpt` of `base1` and `agg1` before believing it, and expect a candidate that passes by luck.
- **The cost is the same as stage 1** (seven sessions, one model call at a time, the same caps, about the same duration; twelve lines of input instead of five change little).
- What would be a real signal: repeating the run over several instances and sweeps. This runner runs one task per sweep (`household/hard-1`); the other eight instances are there for that, behind a future flag.

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
| `COGNITIVE_HARNESS2_MODEL_SANDBOX` | unset (rootless Docker). `sbx` opts the stage-1 model sessions into the sbx route (see "sbx model route"); any other value is refused |

The API key is read from `JEV_OPENROUTER_API_KEY` or `OPENROUTER_API_KEY` and is never returned or logged.

## What is not done

- **`sbx` is verified on v0.50.1 only, and is the default when usable.** The live test (`COGNITIVE_HARNESS2_SBX_LIVE=1`, `-m requires_sbx_live`) passed on that version; other versions are refused (in `auto`: Docker, warned). Inside the VM the candidate is `agent` with passwordless sudo and a writable root overlay (the VM is the boundary), `connect()` to the outside succeeds but no data flows, and `ssh.agentForwardingEnabled` / `browser.enabled` must be off (both default on; the selection refuses until they are, or `COGNITIVE_HARNESS2_SBX_ALLOW_HOST_SETTINGS=1` is set). Each sbx run costs about 4 s more than a Docker run. The sum-signed verifier script is written for busybox `sh` and `awk`; the sbx shell image is Ubuntu, so the live test `test_live_the_sum_signed_verifier_gives_the_same_verdict_in_sbx_as_in_python` runs the verifier on both task sets inside a real sbx and compares it with the Python grader (it agreed on 2026-10-31). Nothing is copied out of the VM. Rootless Docker is verified against a real daemon and is the fallback and the only runner for the model-calling sessions.
- **The engine does not yet call upstream.** Upstream is pinned and import-tested (see Upstream), but `engine.py`'s propose, probe and compose are still the slice 1 local baseline, and the composer is a heuristic (exit status, timeouts, output, stderr). The safety seams to upstream's `Substrate` exist (see Upstream seams); binding the stages themselves to upstream's execution is slice 3b.
- The OpenRouter quota field names follow RFC 004 and are unverified against the live API.
- The push hook counts tool failures only. It does not see `jev_judge` verdicts (the shape of that tool result in a PostToolUse payload has not been captured), and there is no `/probe` slash command or `engine probe`; the pull tool covers on-demand requests. The `PostToolUseFailure` payload is also uncaptured: the hook relies on the documented `hook_event_name`, `session_id` and `is_interrupt` fields.
- The path rule in `invariants.py` is used by `upstream_surface.py` and the staging key rules; the engine itself still mutates components, not files.
- `~/.docker/config.json` (container proxies, credential helpers) is still read by docker: only `DOCKER_*` variables are stripped from the child env. The endpoint resolver (`docker context inspect`, when `DOCKER_HOST` is unset) deliberately reads the ambient context.

## Tests

```bash
uv run pytest packages/harness2
```

The sbx model route has fake-sbx tests in the normal suite (`test_sbx_model_policy.py`, `test_sbx_model_selection.py`, `test_sbx_session.py`, stage-1 runtime tests in `test_stage1.py`; no VM, no Docker) and two opt-in live tests that start throwaway `h2m-*` sandboxes and a stub model on the host. Run them one at a time, under whatever lock your host uses for sbx and Docker:

```bash
COGNITIVE_HARNESS2_SBX_LIVE=1 uv run pytest -q packages/harness2/tests/test_sbx_session.py -k test_live -s
```

The `real_cli` tests (the real opencode binary against a stub model, no Ollama) are skipped unless `~/.opencode/bin/opencode` exists AND `COGNITIVE_HARNESS2_REAL_CLI=1`, so the normal suite is hermetic. Run them on their own:

```bash
COGNITIVE_HARNESS2_REAL_CLI=1 uv run pytest -q packages/harness2/tests/test_real_cli.py
```
