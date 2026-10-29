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
| `harness2_probe` | Run each candidate in a local `sbx` microVM, or an ephemeral rootless Docker container when `sbx` is absent (below). With neither it **fails closed** and runs nothing; `allow_worktree_diff` opts in to static diffs, which execute no code. |
| `harness2_compose` | Contrast outcomes without labels and propose a patch. Output always has `requires_human_approval: true`. |
| `harness2_reflect` | Stage a lesson as **pending**. Nothing is committed. |
| `harness2_trigger` | Read-only pull check: evaluates a failure summary and recommends whether to probe, with readiness, cost class and `requires_human_approval` (see Triggers). Starts nothing. Not in the RFC 004 §2.7 list; it is the pull tool of §2.3. |
| `harness2_status` | Read-only, offline readiness report (sandbox, key configured, storage, protected set). Not in RFC 004; added for Track 4's Gate 3 and the conformance probe. |

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

## Sandbox: sbx, else rootless Docker, else nothing

`harness2_probe` runs candidate code only in a local sandbox, chosen in this order:

1. `sbx` microVM, when `sbx` is on `PATH`.
2. An **ephemeral rootless Docker container**, when it is not. It is used only if every one of these holds, checked on every run:
   - `docker` exists and the endpoint (`DOCKER_HOST` or the active context) is a **local unix socket**.
   - `docker info` lists `name=rootless` in its SecurityOptions. A rootful daemon is refused.
   - The pinned image (`alpine:3` plus digest, in `sandbox.DEFAULT_IMAGE`) is already local. It is never pulled at run time.
   - The verified endpoint is **pinned**: it is resolved once, and every docker call after the check (`info`, `image inspect`, `run`, `kill`, `rm`) names it with `-H unix://<socket>` and runs with `DOCKER_HOST` and `DOCKER_CONTEXT` removed from the environment. An ambient `DOCKER_HOST` or `DOCKER_CONTEXT` changed after the check cannot redirect the run to another daemon.
3. Neither: **fail closed**. A missing `docker`, an unreachable or stopped daemon, a rootful or remote daemon all raise `SandboxUnavailable` and nothing runs. There is no fallback to a rootful daemon or to the host. The only remaining option is the explicit `allow_worktree_diff` opt-in, which executes nothing.

Each container gets `--network none`, `--read-only`, `--cap-drop ALL`, `--security-opt no-new-privileges`, pid, memory and cpu limits, a `noexec` tmpfs for scratch, user `65534`, the candidate mounted read-only at `/candidate`, and `--rm`. The home directory and the Docker socket are never mounted and no host environment is passed in. Candidate file names must already be clean relative paths: `./payload.py`, `a//b`, `a/./b`, `a/../b`, absolute paths, backslashes, empty names, trailing slashes, NUL and other control characters are refused as written, before anything is staged, and files are staged under exactly the name given. On timeout the container is killed and removed; output is capped at 1 MiB per stream.

Host setup is rootless Docker (`dockerd-rootless-setuptool.sh install`), then `docker pull alpine:3` once. The integration tests (`-m requires_rootless_docker`) run real containers and **do not skip**: without a rootless daemon they fail.

## Upstream

Upstream [google-research/harness2](https://github.com/google-research/harness2) (Xu et al. 2026, [arXiv:2610.14224](https://arxiv.org/abs/2610.14224), Apache-2.0) is the primary source. It is pinned by commit SHA in `pyproject.toml` (`[tool.uv.sources]`, extra `upstream`) and locked in `uv.lock`; to adopt a new upstream version, move the `rev` and run `uv lock`. It is not vendored. *Promptbreeder* (Fernando et al. 2023) and *Large Language Models as Tool Makers* (Cai et al. 2023) are related work only. See `NOTICE`.

## Upstream seams (slice 3a)

Four safety seams sit between this engine and upstream's `Substrate` and `Proposer`. **Nothing here runs a model, starts a container or opens a connection: execution is not enabled.** Every seam is default-deny and fails closed, and each is importable without the `upstream` extra (upstream is read by duck typing or imported lazily).

| module | guards |
| --- | --- |
| `upstream_surface.py` | Maps upstream's `SurfaceSpec` components c1..c8 onto `invariants.MUTABLE`. Mutable: c1 system prompt (`prompt`), c3 memory (`context_assembly`), c4 tool guidance (`tool_description`), c5 subagents (`decomposition_strategy`, body only), c6 skills (`context_assembly`, body only). Denied: c2 guardrails (protected by slot denial and the `guardrails` path stem), c7 scripts (executable code), c8 plugins and hooks (protected), codex's single `AGENTS.md` (it folds in the guardrails), and every unknown slot. `check_surface(spec, request=, tree=)` raises `InvariantViolation` for any slot or path outside that set, or failing the path rules. `check_mutation(slot, path, old, new)` enforces frontmatter freezing, UTF-8 validity, size limits, and absence of control characters or body frontmatter blocks. |
| `upstream_egress.py` | `check_proposer(config, settings)` decides, before any session exists, whether an upstream Proposer may be created. Returns a `Decision` (allowed, reasons, warnings, redacted view). Refuses Vertex and any hosted cloud route, an endpoint that cannot be determined (upstream's Proposer has none: it must be declared), any endpoint that is not loopback or ZDR-listed, a hosted provider behind a loopback address, inherited credential-like env vars (by name only), non-approved `*_BASE_URL` and proxy vars, credential flags on the command line, unsafe working directories, and a `claude` CLI proposer unless `COGNITIVE_HARNESS2_ALLOW_CLAUDE_PROPOSER=1` (`settings.allow_claude_cli_proposer`), which is necessary and not sufficient. No secret value ever reaches a reason or the view. |
| `substrate.py` | `CognitiveSubstrate` implements upstream's `Substrate` Protocol. `surface`, `materialise` (read-only staging through `DockerRunner`'s key rules; no symlinks, UTF-8 only, capped) and `preflight` (`require_rootless_docker`, then a quota check only if the caller passes `quota_check=`) work and start nothing. `execute` and `read_trajectory` raise `NotImplementedError("slice 3b, not enabled")`. |
| `sweep_plan.py` | `plan_sweep(...)` returns an immutable `SweepPlan` (candidates, hard caps, upstream's ledger budget P = 3k and R = 4k, `advice.cost_class`, readiness, `requires_human_approval: true`) or raises `SweepRefused` listing every cause: a missing, zero, negative, infinite or oversized cap, a plan over its own cap, a candidate off the mutable surface, a known readiness blocker or quota refusal, or a Proposer decision that is not allowed. It starts no sweep. |

`tests/test_no_side_effects.py` is the proof: it traps subprocess, `os.system`, the exec and spawn family, asyncio subprocesses, sockets, DNS, urllib, http.client and httpx, runs every seam's happy and refusal paths, and asserts nothing was attempted. It also shows the Docker daemon is asked only by `preflight`, with pinned read-only `info` and `image inspect`.

Things worth knowing:

- Importing `harness2.substrate`, `harness2.config` or `harness2.core.proposer` runs upstream's `config.py`, which reads `$CWD/.env` into `os.environ`. The seams avoid those imports (a canary test pins this). `harness2.core.tree` and `harness2.core.ledger` are stdlib-only and safe.
- Even an allowed Proposer decision carries warnings: upstream's sessions run agent tools (shell, files) on the host, and `HOME` file credentials (gcloud, `~/.claude`, `~/.aws`) cannot be listed by an environment check. Isolating the proposer session is a slice 3b question.
- Upstream is inconsistent about c-numbers (its README makes guardrails c2, its prompt tables call `guardrails.md` c6). The seams bind to paths.

**The 3b gate.** Slice 3b (`DockerRunner` as `Substrate.execute`, one capped dry run, a hard budget cap, `requires_human_approval` kept) is not started and runs only on the user's separate go. Slice 3a is subject to independent review before that.

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
| `COGNITIVE_HARNESS2_ZDR_ENDPOINTS` | unset: comma-separated hosts you attest are zero-retention |
| `COGNITIVE_HARNESS2_ALLOW_CLAUDE_PROPOSER` | unset (off): `1`/`true` lets `upstream_egress` consider a `claude` CLI proposer, which spends Claude subscription quota |
| `COGNITIVE_HARNESS2_EXPERIENCE_ROOT` | `~/.local/share/cognitive-engine/experiences` |

The API key is read from `JEV_OPENROUTER_API_KEY` or `OPENROUTER_API_KEY` and is never returned or logged.

## What is not done

- **`sbx` is not installed on this machine**, so real microVM isolation is unverified. `SbxRunner._argv` is an assumption about the `sbx` command line; the fail-closed path is tested and does not depend on it. The rootless Docker fallback below is verified against a real daemon.
- **The engine does not yet call upstream.** Upstream is pinned and import-tested (see Upstream), but `engine.py`'s propose, probe and compose are still the slice 1 local baseline, and the composer is a heuristic (exit status, timeouts, output, stderr). The safety seams to upstream's `Substrate` exist (see Upstream seams); binding the stages themselves to upstream's execution is slice 3b.
- The OpenRouter quota field names follow RFC 004 and are unverified against the live API.
- The push hook counts tool failures only. It does not see `jev_judge` verdicts (the shape of that tool result in a PostToolUse payload has not been captured), and there is no `/probe` slash command or `engine probe`; the pull tool covers on-demand requests. The `PostToolUseFailure` payload is also uncaptured: the hook relies on the documented `hook_event_name`, `session_id` and `is_interrupt` fields.
- The path rule in `invariants.py` is used by `upstream_surface.py` and the staging key rules; the engine itself still mutates components, not files.
- `~/.docker/config.json` (container proxies, credential helpers) is still read by docker: only `DOCKER_*` variables are stripped from the child env. The endpoint resolver (`docker context inspect`, when `DOCKER_HOST` is unset) deliberately reads the ambient context.

## Tests

```bash
uv run pytest packages/harness2
```
