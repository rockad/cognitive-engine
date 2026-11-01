"""Candidate execution in local microVMs, failing closed (RFC 004 §2.2).

Candidate code runs ONLY in one of two local runtimes. `COGNITIVE_HARNESS2_SANDBOX` is a tri-state:

  * unset or `auto` (the DEFAULT): a Docker Sandbox microVM driven by the standalone `sbx` CLI when ALL of
    hold: the installed version is in `VERIFIED_SBX_VERSIONS` ({0.50.1} today); a cheap, read-only,
    time-bounded probe passes (/dev/kvm accessible, daemon reachable, global network policy deny-all, and the
    two host-reach settings `ssh.agentForwardingEnabled` and `browser.enabled` both off, unless
    `COGNITIVE_HARNESS2_SBX_ALLOW_HOST_SETTINGS=1`). `sbx` being on PATH is NOT enough. Otherwise an
    EPHEMERAL ROOTLESS DOCKER container (`DockerRunner`), only when the daemon itself reports
    `name=rootless` in `docker info` SecurityOptions. When sbx is present but not selected the reason is
    recorded (`sandbox_report`) and logged once as a WARNING; when sbx is simply not installed Docker is
    chosen silently.
    The selection is a cheap probe, not a guarantee that a VM can be created: in `auto` only, a failure of the
    `SbxRunner` BEFORE the candidate command started (`SandboxNotStarted`) drops the cached selection, warns, and
    runs that spec on verified rootless Docker (the result says `runner` and `fallback_reason`). Never after exec.
  * `docker`: rootless Docker only; sbx is never consulted (no process, no probe).
  * `sbx`: sbx only, FAIL CLOSED: when it is not verified or not usable the runner raises
    `SandboxUnavailable`; it never falls back to Docker.
  * anything else fails closed too.

The probe never starts the daemon, creates a VM or pulls anything: every sbx process it runs is asked only
after the daemon socket already answers, under a short timeout, and the result is cached for a short while per
process. `SbxRunner` re-checks version, policy and the two settings on every run.

With neither, the prober refuses to run anything (fail closed): a missing `docker`, an
unreachable or stopped daemon and a rootful daemon all raise `SandboxUnavailable`. There is
no silent fallback to a rootful daemon or to host execution. The daemon endpoint the gate
verified is pinned (`-H unix://...`, ambient DOCKER_HOST and DOCKER_CONTEXT dropped) on every
docker call the runner makes afterwards, so it cannot change between the check and the run. The one remaining fallback is
an explicit opt-in that writes candidates to a temp directory and returns a diff for static
inspection; it never executes code.

`SbxRunner` (create -> cp in -> exec -> rm --force) IS verified end to end against a live sbx v0.50.1
microVM (tests/test_sbx_runner.py::test_live_*, COGNITIVE_HARNESS2_SBX_LIVE=1): exit codes, stdout/stderr,
staged files, the wall clock, a network that returns 403 or nothing, and an empty `sbx ls` afterwards. That is
why `VERIFIED_SBX_VERSIONS` holds exactly that version: adding another is the act of verifying the flow against
it (run the live test on it first), and nothing else should do it. `SbxRunner` itself also refuses to run for a
version outside the set it was given. The microVM is the isolation boundary, NOT the user inside it: the
candidate runs as `agent` with passwordless sudo (see the README, "Live findings").
`DockerRunner` IS verified end to end against a real rootless daemon
(tests/test_docker_runner.py).
"""
from __future__ import annotations

import difflib
import json
import logging
import os
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import tempfile
import threading
import time
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, NamedTuple, Protocol

from . import invariants, run_scope


class SandboxUnavailable(RuntimeError):
    """No usable local sandbox runtime: refuse to run candidate code."""


class SandboxNotStarted(SandboxUnavailable):
    """`SbxRunner` refused or failed BEFORE the candidate command was started (preflight, create, the network and
    staging checks): nothing of the candidate has run, so the same spec may safely run on another verified runtime.
    Never raised once `sbx exec` of the candidate has been started, nor when the sandbox could not be removed."""


@dataclass(frozen=True)
class SandboxSpec:
    command: Sequence[str]
    files: Mapping[str, str] = field(default_factory=dict)
    timeout_s: int = 120


@dataclass(frozen=True)
class SandboxResult:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    # Which runtime executed the command (RUNNER_SBX or RUNNER_DOCKER; None when a runner does not say) and, when it is
    # not sbx although sbx is present or was asked for, {code, detail, loud}. The two runtimes differ materially (image,
    # user, root filesystem, pids limit), so the same command can give different results on each: evidence must say which.
    runner: str | None = field(default=None, compare=False)
    fallback_reason: dict | None = field(default=None, compare=False)


class SandboxRunner(Protocol):
    def run(self, spec: SandboxSpec) -> SandboxResult: ...


def require_sbx(which: Callable[[str], str | None] = shutil.which) -> str:
    path = which("sbx")
    if not path:
        raise SandboxUnavailable(
            "the `sbx` runtime is not installed, so candidate code will not be run (fail closed). "
            "Install sbx, or pass allow_worktree_diff to get static diffs only.")
    return path


# The `sbx` versions the `SbxRunner` flow has been verified against, end to end, on a real install
# (the `requires_sbx_live` test). A version is added here only after that test passes on it; until
# then `default_runner` cannot return an `SbxRunner` for it, whatever is on PATH.
VERIFIED_SBX_VERSIONS: frozenset[str] = frozenset({"0.50.1"})

# The runtime switch, a tri-state: unset/empty/`auto` (default: sbx when verified AND usable, else rootless Docker),
# `docker` (rootless Docker only, sbx never consulted), `sbx` (sbx only, fail closed, never falls back).
ENV_SANDBOX = "COGNITIVE_HARNESS2_SANDBOX"
# `1` accepts a daemon with `ssh.agentForwardingEnabled` or `browser.enabled` on (both are on by default on a fresh
# install). Off by default: with either on, the sandbox can reach the host's SSH agent or browser.
ENV_ALLOW_HOST_SETTINGS = "COGNITIVE_HARNESS2_SBX_ALLOW_HOST_SETTINGS"
# The runner names as data (`SandboxResult.runner`, `harness2_status`).
RUNNER_SBX, RUNNER_DOCKER, RUNNER_NONE = "sbx", "rootless-docker", "unavailable"


# ── ephemeral rootless Docker (the fallback when sbx is absent) ───────────────────

# Pinned by tag AND digest: the image this host already holds, never pulled at run time.
DEFAULT_IMAGE = "alpine:3@sha256:be2656061b536c1883c4034790e21d0a09c5aaaf51968b89368a3c0371ea2c9a"
CANDIDATE_DIR = "/candidate"
MAX_OUTPUT_BYTES = 1 << 20
_PROBE_TIMEOUT_S = 15


def _probe(argv: list[str], run: Callable[..., subprocess.CompletedProcess],
           env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess:
    try:
        return run(argv, capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S, env=env)
    except subprocess.TimeoutExpired:
        verb = argv[3:5] if argv[1:2] == ["-H"] else argv[1:3]
        raise SandboxUnavailable(
            f"`{' '.join(verb)}` timed out after {_PROBE_TIMEOUT_S}s: the Docker daemon is "
            "unreachable (fail closed)") from None
    except OSError as e:
        raise SandboxUnavailable(f"cannot execute {argv[0]!r}: {e} (fail closed)") from e


def _first_line(text: str) -> str:
    return (text.strip().splitlines() or ["no output"])[0][:300]


@dataclass(frozen=True)
class VerifiedDocker:
    """What the gate verified: the `docker` binary AND the daemon endpoint it checked."""
    path: str
    host: str  # `unix://<socket>`; every later docker call is pinned to it with `-H`


def _is_local_unix(host: str) -> bool:
    return host.startswith("unix://") and len(host) > len("unix://")


def _pinned_env() -> dict[str, str]:
    """The process environment minus everything that can redirect the docker CLI.

    `-H` already outranks DOCKER_HOST and DOCKER_CONTEXT, but EVERY variable starting with DOCKER_ is
    dropped (DOCKER_CONFIG, whose config.json can set currentContext, a credential helper and container
    proxies; DOCKER_CERT_PATH, DOCKER_TLS_VERIFY, DOCKER_API_VERSION, DOCKER_CONTENT_TRUST, ...), so
    nothing ambient can be consulted by any docker invocation after the check. The endpoint is carried
    by `-H unix://...` and the image is never pulled.

    The one call that is not pinned is the endpoint resolver (`docker context inspect`, used only when
    DOCKER_HOST is unset): reading the ambient context is its job.
    """
    return {k: v for k, v in os.environ.items() if not k.upper().startswith("DOCKER_")}


def verify_rootless_docker(
    which: Callable[[str], str | None] = shutil.which,
    *,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    environ: Mapping[str, str] | None = None,
    image: str = DEFAULT_IMAGE,
    host: str | None = None,
) -> VerifiedDocker:
    """Return the verified `docker` path and endpoint, or raise SandboxUnavailable. Fails CLOSED.

    Allowed only when ALL hold: `docker` exists; the daemon endpoint is a local unix
    socket (so code never leaves this machine); the daemon answers `docker info`; its
    SecurityOptions contain `name=rootless`; and the pinned image is already present.
    Anything else, including an unreachable or stopped daemon, raises. There is no
    rootful or host fallback.

    The endpoint is resolved ONCE (from `host` if given, else DOCKER_HOST, else the active
    docker context), and every probe after that is pinned to it with `-H`. A caller that
    keeps the returned `host` and passes it on every later docker call cannot be redirected
    by an ambient DOCKER_HOST or DOCKER_CONTEXT that changes after this check.

    ⚠️ `docker info` can exit 0 with `null` when a TCP daemon is unreachable, so the exit
    status is never trusted: only a SecurityOptions list that names `rootless` passes.
    """
    env = os.environ if environ is None else environ
    path = which("docker")
    if not path:
        raise SandboxUnavailable("`docker` is not installed, so there is no rootless Docker fallback (fail closed)")

    if host is None:
        host = env.get("DOCKER_HOST")
        if not host:
            p = _probe([path, "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"], run)
            host = p.stdout.strip() if p.returncode == 0 else ""
    if not _is_local_unix(host):
        raise SandboxUnavailable(
            f"the Docker endpoint {host or 'is unknown'!r} is not a local unix socket; candidate code "
            "must never leave this machine (fail closed)")
    pinned = _pinned_env()

    p = _probe([path, "-H", host, "info", "--format", "{{json .SecurityOptions}}"], run, pinned)
    if p.returncode != 0:
        raise SandboxUnavailable(f"the Docker daemon is unreachable: {_first_line(p.stderr)} (fail closed)")
    try:
        options = json.loads(p.stdout)
    except ValueError:
        options = None
    if not isinstance(options, list) or "name=rootless" not in options:
        raise SandboxUnavailable(
            "the Docker daemon does not report name=rootless in its SecurityOptions; refusing to run "
            "candidate code on a rootful daemon (fail closed)")

    if _probe([path, "-H", host, "image", "inspect", "--format", "{{.Id}}", image], run, pinned).returncode != 0:
        raise SandboxUnavailable(
            f"the pinned sandbox image {image!r} is not present locally and is never pulled at run "
            f"time; run `docker pull {image.split('@')[0]}` once (fail closed)")
    return VerifiedDocker(path, host)


def require_rootless_docker(
    which: Callable[[str], str | None] = shutil.which,
    *,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    environ: Mapping[str, str] | None = None,
    image: str = DEFAULT_IMAGE,
) -> str:
    """`verify_rootless_docker`, returning only the `docker` path (the gate itself is unchanged)."""
    return verify_rootless_docker(which, run=run, environ=environ, image=image).path


def _stage_name(name: str) -> str:
    """A staged file name must ALREADY be a clean relative path, and one the protected set allows.

    Nothing is normalised for the caller: a key that needs `normpath` to become safe
    (`./payload.py`, `a//b`, `a/./b`, `a/../b`) is refused as written, so what is staged is
    exactly what was asked for. Refused: non-strings, empty, absolute, a leading or trailing
    `/`, backslashes, empty / `.` / `..` / space-padded segments, and control or invisible
    characters. Returns the key unchanged.
    """
    if not isinstance(name, str) or not name:
        raise ValueError(f"invalid staged file name {name!r}: must be a non-empty string")
    if any(unicodedata.category(c) in ("Cc", "Cf", "Cs", "Co", "Cn") for c in name):
        raise ValueError(f"invalid staged file name {name!r}: control or invisible characters")
    if "\\" in name:
        raise ValueError(f"invalid staged file name {name!r}: backslashes are not allowed")
    if name.startswith("/") or name.endswith("/"):
        raise ValueError(f"invalid staged file name {name!r}: must be a relative file path, not absolute or a directory")
    for seg in name.split("/"):
        if seg in ("", ".", "..") or seg != seg.strip():
            raise ValueError(f"invalid staged file name {name!r}: not a clean relative path (segment {seg!r})")
    try:
        invariants.assert_path_unprotected(name)
    except invariants.InvariantViolation:
        # The surface's own guardrails.md must be stageable so the agent runs with its guardrails,
        # but ONLY as the exact root-level name: no case variant, no trailing dot or space, no
        # directory prefix. Everything else protected (hooks, .git, guard, ...) stays refused.
        if name == "guardrails.md":
            return name
        raise
    return name


def _validate_stage_names(files: Mapping[str, str]) -> None:
    """Check every key before anything is created, so one bad key stages nothing."""
    for name in files:
        _stage_name(name)


def _drain(stream: IO[bytes], limit: int, sink: bytearray, truncated: list[bool]) -> None:
    while chunk := stream.read1(65536):  # type: ignore[attr-defined]
        room = limit - len(sink)
        if room > 0:
            sink += chunk[:room]
        if len(chunk) > room:
            truncated[0] = True


class DockerRunner:
    """One ephemeral, rootless, network-less, read-only container per run.

    Hardening, all on the command line (see `_argv`): no network, read-only root, every
    capability dropped, no-new-privileges, pid / memory / cpu limits, tmpfs scratch, an
    unprivileged user, the candidate staged read-only, `--rm`. The home directory and the
    Docker socket are never mounted, and no host environment variable is passed through.
    """

    def __init__(self, docker_path: str, *, docker_host: str | None = None, image: str = DEFAULT_IMAGE,
                 pids_limit: int = 128, memory: str = "256m", cpus: str = "1", tmpfs_size: str = "64m",
                 user: str = "65534:65534", max_output_bytes: int = MAX_OUTPUT_BYTES,
                 fallback_reason: dict | None = None):
        """`docker_host` is the endpoint the gate verified (`VerifiedDocker.host`). With it, every
        docker call this runner makes goes to exactly that daemon and the ambient DOCKER_HOST and
        DOCKER_CONTEXT are never consulted. Without it, each `run()` resolves the endpoint once,
        verifies it, and pins the rest of that run to the endpoint it verified. `fallback_reason` is why this
        runner and not sbx was built ({code, detail, loud}); it is only reported, in every `SandboxResult`."""
        if docker_host is not None and not _is_local_unix(docker_host):
            raise SandboxUnavailable(
                f"the Docker endpoint {docker_host or 'is unknown'!r} is not a local unix socket; candidate "
                "code must never leave this machine (fail closed)")
        self.docker_path, self.docker_host, self.image, self.pids_limit = docker_path, docker_host, image, pids_limit
        self.memory, self.cpus, self.tmpfs_size, self.user = memory, cpus, tmpfs_size, user
        self.max_output_bytes = max_output_bytes
        self.fallback_reason = fallback_reason

    def _argv(self, spec: SandboxSpec, stage_dir: Path, name: str, host: str) -> list[str]:
        return [
            self.docker_path, "-H", host, "run", "--rm", "--name", name,
            "--label", "cognitive-harness2.sandbox=1", *run_scope.labels(), "--pull", "never",
            "--network", "none", "--read-only",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--pids-limit", str(self.pids_limit),
            "--memory", self.memory, "--memory-swap", self.memory, "--cpus", self.cpus,
            "--user", self.user,
            "--tmpfs", f"/tmp:rw,noexec,nosuid,size={self.tmpfs_size}",
            "--mount", f"type=bind,src={stage_dir},dst={CANDIDATE_DIR},readonly",
            "--workdir", CANDIDATE_DIR, "-e", "HOME=/tmp",
            self.image, *spec.command,
        ]

    def _stage(self, spec: SandboxSpec) -> Path:
        _validate_stage_names(spec.files)  # before mkdtemp: a bad key leaves nothing behind
        stage =Path(tempfile.mkdtemp(prefix="harness2-stage-")).resolve()
        try:
            home = Path.home().resolve()
            if stage == home or home in stage.parents:
                raise SandboxUnavailable(f"refusing to stage candidates under the home directory ({stage})")
            if any(c in str(stage) for c in ',"\n'):
                raise SandboxUnavailable(f"staging path {stage} is not safe to mount")
            stage.chmod(0o755)  # the container user is not the owner
            for name, content in spec.files.items():
                target = stage / _stage_name(name)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                target.chmod(0o444)
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)
            raise
        return stage

    def _kill(self, name: str, host: str) -> None:
        env = _pinned_env()
        for verb in (["kill", name], ["rm", "-f", name]):
            try:
                subprocess.run([self.docker_path, "-H", host, *verb], capture_output=True,
                               timeout=_PROBE_TIMEOUT_S, env=env)
            except (subprocess.TimeoutExpired, OSError):
                pass

    def run(self, spec: SandboxSpec) -> SandboxResult:
        if not spec.command:
            raise ValueError("command cannot be empty")
        _validate_stage_names(spec.files)  # cheapest refusal first: no docker call for a bad key
        # Re-checked on every run: the daemon can be stopped or swapped between probes. The
        # endpoint it verifies is the endpoint every later call in this run is pinned to, so
        # an ambient DOCKER_HOST or DOCKER_CONTEXT that changes after the check is never used.
        host = verify_rootless_docker(lambda _: self.docker_path, run=subprocess.run, image=self.image,
                                      host=self.docker_host).host
        stage = self._stage(spec)
        name = f"harness2-{secrets.token_hex(6)}"
        out, err, cut = bytearray(), bytearray(), [False]
        proc: subprocess.Popen | None = None
        try:
            proc = subprocess.Popen(self._argv(spec, stage, name, host), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_pinned_env())
            readers = [threading.Thread(target=_drain, args=(s, self.max_output_bytes, b, cut), daemon=True)
                       for s, b in ((proc.stdout, out), (proc.stderr, err))]
            for t in readers:
                t.start()
            timed_out = False
            try:
                code: int | None = proc.wait(timeout=spec.timeout_s)
            except subprocess.TimeoutExpired:
                timed_out, code = True, None
                self._kill(name, host)
                try:
                    proc.wait(timeout=_PROBE_TIMEOUT_S)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            for t in readers:
                t.join(timeout=_PROBE_TIMEOUT_S)
        finally:
            if proc is not None and proc.poll() is None:
                proc.kill()
            self._kill(name, host)  # a no-op when --rm already removed it; never leaks a container
            shutil.rmtree(stage, ignore_errors=True)
        note = f"\n[output truncated at {self.max_output_bytes} bytes]\n" if cut[0] else ""
        stdout = out.decode("utf-8", "replace") + note
        tag = {"runner": RUNNER_DOCKER, "fallback_reason": self.fallback_reason}
        if timed_out:
            return SandboxResult(None, stdout, "timed out", True, **tag)
        return SandboxResult(code, stdout, err.decode("utf-8", "replace"), **tag)


# ── Docker Sandboxes (sbx) microVMs ───────────────────────────────────────────────

SBX_PARENT_DIR = "/tmp"
SBX_CANDIDATE_DIR = "/tmp/candidate"  # `sbx cp <stage>/candidate NAME:/tmp` puts the directory inside /tmp
# Asked of the policy engine in the context of the new sandbox, before any file or command reaches it.
SBX_DENIED_TARGETS = ("example.com:443", "example.com:80", "127.0.0.1:80", "localhost:80")
# What an `sbx` process may inherit: an ALLOW-list (a deny-list let DOCKER_HOST, DOCKER_CONFIG, DOCKER_AUTH_CONFIG, credentials and
# anything new through). Names are matched exactly and case-sensitively. `_SBX_ENV_ALLOW_EXTRA` is empty in production; tests that
# run a fake `sbx` configured through its own variables extend it.
_SBX_ENV_ALLOW = frozenset({"PATH", "HOME", "LANG", "TERM", "TMPDIR"})
_SBX_ENV_ALLOW_PREFIXES = ("XDG_", "LC_")
_SBX_ENV_ALLOW_EXTRA: tuple[str, ...] = ()
_SBX_POLICY_ACTIONS = frozenset({"net:connect:tcp", "net:connect:udp"})
_SBX_CREATE_TIMEOUT_S = 600  # the daemon may pull the agent image on first use
_SBX_EXEC_FAILURE = re.compile(r"\s*(failed to start|error: |inspect exec: )")


def sbx_network_policy_problem(policy_json: str) -> str | None:
    """None only when `sbx policy ls --json` says: an active deny-all (`**`, tcp and udp) network rule exists
    and NO network rule allows anything (whatever its status). Anything unparseable or unexpected is a problem."""
    try:
        doc = json.loads(policy_json)
    except ValueError:
        return "`sbx policy ls --json` did not return JSON"
    rules = doc.get("rules") if isinstance(doc, dict) else None
    if not isinstance(rules, list):
        return "`sbx policy ls --json` has no `rules` list"
    deny_all = False
    for rule in rules:
        if not isinstance(rule, dict):
            return "`sbx policy ls --json` has a malformed rule"
        kind = rule.get("resource_type")
        if not isinstance(kind, str):
            return "`sbx policy ls --json` has a rule without a resource_type"
        if not kind.startswith("network"):
            continue
        decision = rule.get("decision")
        if decision == "allow":
            return f"a network allow rule exists ({rule.get('name') or rule.get('id')!r}): the policy is not deny-all"
        if decision != "deny":
            return f"a network rule has the unknown decision {decision!r}"
        resources, actions = rule.get("resources"), rule.get("actions")
        if (rule.get("status") == "active" and isinstance(resources, list) and "**" in resources
                and isinstance(actions, list) and _SBX_POLICY_ACTIONS <= set(actions)):
            deny_all = True
    if not deny_all:
        return "no active network deny-all rule (`**`, tcp and udp): the policy is not deny-all"
    return None


def _sbx_env(environ: Mapping[str, str]) -> dict[str, str]:
    """The environment for every `sbx` call: an ALLOW-list (PATH, HOME, XDG_*, LANG, LC_*, TERM, TMPDIR), so nothing that
    can re-point the CLI, its settings or the Docker client (`DOCKER_HOST`, `DOCKER_CONFIG`, `DOCKER_AUTH_CONFIG`,
    `SBX_*`), no credential and not the host's SSH agent (`SSH_AUTH_SOCK`) ever reaches a process this module starts."""
    allowed = _SBX_ENV_ALLOW_PREFIXES + _SBX_ENV_ALLOW_EXTRA
    return {k: v for k, v in environ.items() if k in _SBX_ENV_ALLOW or k.startswith(allowed)}


# The two host-reach settings of the sbx daemon (`sbx settings ls`, both default true on v0.50.1): the first lets
# sandboxes use the host's SSH agent keys, the second lets them open URLs in the host browser. Read-only for us;
# the user turns them off with `sbx settings set <key> false` (SSH needs `sbx daemon restart` as well).
SBX_RISKY_SETTINGS = ("ssh.agentForwardingEnabled", "browser.enabled")
_SBX_SETTINGS_ARGV = ("settings", "ls", "--format", "json")


def sbx_settings_observed(settings_json: str) -> dict[str, bool]:
    """The values of `SBX_RISKY_SETTINGS` from `sbx settings ls --format json`. Raises ValueError when the output
    is not the expected list or either setting is missing or not a boolean: an unreadable setting is not "off"."""
    doc = json.loads(settings_json)
    if not isinstance(doc, list):
        raise ValueError("`sbx settings ls --format json` did not return a list")
    by_key = {row.get("key"): row.get("value") for row in doc if isinstance(row, dict)}
    out: dict[str, bool] = {}
    for key in SBX_RISKY_SETTINGS:
        if not isinstance(by_key.get(key), bool):
            raise ValueError(f"`sbx settings ls` has no boolean {key}")
        out[key] = by_key[key]
    return out


def host_settings_allowed(environ: Mapping[str, str]) -> bool:
    return (environ.get(ENV_ALLOW_HOST_SETTINGS) or "").strip() == "1"


def sbx_unsafe_settings(observed: Mapping[str, bool], environ: Mapping[str, str]) -> list[str]:
    """Which of the risky settings are on (empty when the documented override is set)."""
    if host_settings_allowed(environ):
        return []
    return [k for k in SBX_RISKY_SETTINGS if observed.get(k) is not False]


def _unsafe_settings_message(on: Sequence[str]) -> str:
    return (f"sbx host-reach setting(s) on: {', '.join(f'{k}=true' for k in on)} (a sandbox could reach the host's "
            f"SSH agent or browser); turn them off with `sbx settings set <key> false` (ssh also needs `sbx daemon "
            f"restart`), or accept the risk with {ENV_ALLOW_HOST_SETTINGS}=1")


class SbxRestartPending(RuntimeError):
    """A host-reach setting reads as off but is flagged `requires_restart` and was set after the daemon started."""

    def __init__(self, keys: Sequence[str]):
        self.keys = list(keys)
        super().__init__(
            f"sbx setting(s) {', '.join(self.keys)} read as off but are flagged `requires_restart` and were set after "
            f"the daemon started, so the running daemon may still apply the old value (existing agent connections "
            f"stay active); run `sbx daemon restart` (from a shell with the kvm group), or accept the risk with "
            f"{ENV_ALLOW_HOST_SETTINGS}=1")


def _settings_override_file(environ: Mapping[str, str]) -> Path:
    # Beside `settings.json.lock`, which sbx v0.50.1 creates there; the file itself exists once an override was set.
    home = environ.get("HOME") or str(Path.home())
    base = environ.get("XDG_CONFIG_HOME") or str(Path(home) / ".config")
    return Path(base) / "sandboxes" / "sandboxes" / "settings.json"


def _override_predates_daemon(environ: Mapping[str, str]) -> bool:
    """True only when the settings override file is provably no newer than the daemon socket (created when the daemon
    started, mtime never touched by connections). Anything that cannot be dated is False: not proven is pending."""
    try:
        return _settings_override_file(environ).stat().st_mtime <= _sbx_socket(environ).stat().st_mtime
    except OSError:
        return False


def sbx_restart_pending(settings_json: str, environ: Mapping[str, str]) -> list[str]:
    """The host-reach settings that read as OFF but may not be off in the RUNNING daemon.

    `sbx settings ls --format json` marks `ssh.agentForwardingEnabled` `requires_restart: true` (v0.50.1): the CLI
    prints the configured value, the daemon applies it only after `sbx daemon restart`. A key is pending when it is
    flagged, reads off, has an override (`source` other than `default`), and the override cannot be shown to be older
    than the daemon (file mtime <= daemon socket mtime). With no override the daemon read the default when it started,
    and the documented override accepts the risk. Unreadable documents have nothing pending: the parser refuses them.

    `source` values other than `default` were not observed (this host has no override): any other value counts as an
    override. The evaluated value comes from the daemon, so an env var the daemon itself was started with is already
    part of what `settings ls` prints; the CLI-side scrub of `DOCKER_SANDBOXES_*` does not hide it.
    """
    if host_settings_allowed(environ):
        return []
    try:
        doc = json.loads(settings_json)
    except ValueError:
        return []
    if not isinstance(doc, list):
        return []
    flagged = [row.get("key") for row in doc if isinstance(row, dict) and row.get("key") in SBX_RISKY_SETTINGS
               and row.get("requires_restart") is True and row.get("value") is False
               and row.get("source") != "default"]
    if flagged and not _override_predates_daemon(environ):
        return flagged
    return []


def _stage_for_sbx(spec: SandboxSpec) -> Path | None:
    """Write the files under `<tmp>/candidate/`; returns `<tmp>` (the caller removes it) or None when there are none."""
    _validate_stage_names(spec.files)  # before mkdtemp: a bad key leaves nothing behind
    if not spec.files:
        return None
    parent = Path(tempfile.mkdtemp(prefix="harness2-sbx-")).resolve()
    try:
        root = parent / "candidate"
        root.mkdir()
        for name, content in spec.files.items():
            target = root / _stage_name(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            target.chmod(0o444)
    except BaseException:
        shutil.rmtree(parent, ignore_errors=True)
        raise
    return parent


class SbxRunner:
    """One throwaway Docker Sandbox microVM per run, driven only by the `sbx` CLI, no shell, never the host.

    Flow (`run`): preflight (the daemon socket is the user's own, `sbx version` is in the verified set the
    runner was given, the GLOBAL network policy is deny-all) -> `sbx create shell` with NO workspace (no host
    bind mount) -> `sbx policy check network --sandbox` must deny every target in `SBX_DENIED_TARGETS` ->
    `sbx cp` the staged files in and `sbx exec find` to confirm exactly those files arrived -> `sbx exec` the
    command with a host-side wall clock -> ALWAYS (`finally`) `sbx rm --force`, and confirm with `sbx ls -q`
    that it is gone. Any surprise (unknown version, unexpected output, exit 125 from `sbx`, a policy that is
    not deny-all, a sandbox that will not go away) raises `SandboxUnavailable`: nothing runs on the host.

    Verified live on sbx v0.50.1 (see the module docstring and the README, "Live findings").
    """

    def __init__(self, sbx_path: str, *, environ: Mapping[str, str] | None = None,
                 verified: frozenset[str] | None = None, cpus: int = 1, memory: str = "512m",
                 create_timeout_s: int = _SBX_CREATE_TIMEOUT_S, control_timeout_s: int = 60,
                 max_output_bytes: int = MAX_OUTPUT_BYTES,
                 fallback: Callable[[dict], SandboxRunner] | None = None,
                 policy_problem: Callable[[str], str | None] | None = None):
        """`fallback` is for `auto` only: given the reason ({code, detail, loud}), it returns the verified Docker runner
        (or raises `SandboxUnavailable`). It is used only for a failure BEFORE the candidate started (`SandboxNotStarted`);
        None (the `sbx` setting, and the default) keeps the runner strictly fail-closed.

        `policy_problem` judges `sbx policy ls --json` in the preflight (default `sbx_network_policy_problem`: deny-all
        and no allow rule at all). The model-session route (`sbx_session`) passes its own, which also tolerates the
        per-sandbox rules of its own sessions."""
        self.policy_problem = policy_problem or sbx_network_policy_problem
        self.fallback = fallback
        self._fell_back: SandboxRunner | None = None
        self.sbx_path = sbx_path
        self.environ = dict(os.environ if environ is None else environ)
        self.verified = VERIFIED_SBX_VERSIONS if verified is None else verified
        self.cpus, self.memory = cpus, memory
        self.create_timeout_s, self.control_timeout_s = create_timeout_s, control_timeout_s
        self.max_output_bytes = max_output_bytes

    # ── the command lines: the only place that knows them ─────────────────────────

    def _argv(self, *args: str) -> list[str]:
        return [self.sbx_path, *args]

    def _create_argv(self, name: str) -> list[str]:
        return self._argv("create", "shell", "--name", name, "--no-progress", "--cpus", str(self.cpus),
                          "--memory", self.memory, "--pull", "missing", "--skills", "off")

    def _check_argv(self, name: str, target: str) -> list[str]:
        return self._argv("policy", "check", "network", "--sandbox", name, "--json", target)

    def _cp_in_argv(self, stage: Path, name: str) -> list[str]:
        return self._argv("cp", str(stage / "candidate"), f"{name}:{SBX_PARENT_DIR}")

    def _list_staged_argv(self, name: str) -> list[str]:
        return self._argv("exec", "--", name, "find", SBX_CANDIDATE_DIR, "-type", "f")

    def _exec_argv(self, spec: SandboxSpec, name: str, staged: bool) -> list[str]:
        # `--` before NAME: nothing in the candidate command can be read as a flag of `sbx` itself.
        return self._argv("exec", "-w", SBX_CANDIDATE_DIR if staged else SBX_PARENT_DIR, "--", name, *spec.command)

    def _rm_argv(self, name: str) -> list[str]:
        return self._argv("rm", "--force", name)

    def _ls_argv(self) -> list[str]:
        return self._argv("ls", "-q")

    # ── helpers ───────────────────────────────────────────────────────────────────

    def _ctl(self, argv: list[str], timeout: int, what: str) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                                  env=_sbx_env(self.environ))
        except subprocess.TimeoutExpired:
            raise SandboxUnavailable(f"`sbx {what}` timed out after {timeout}s (fail closed)") from None
        except OSError as e:
            raise SandboxUnavailable(f"cannot execute {argv[0]!r}: {e} (fail closed)") from e

    def _must(self, argv: list[str], what: str, timeout: int | None = None) -> subprocess.CompletedProcess:
        p = self._ctl(argv, self.control_timeout_s if timeout is None else timeout, what)
        if p.returncode != 0:
            raise SandboxUnavailable(f"`sbx {what}` failed (exit {p.returncode}): "
                                     f"{_first_line(p.stderr or p.stdout)} (fail closed)")
        return p

    def _preflight(self) -> None:
        sock = _sbx_socket(self.environ)
        try:
            st = sock.stat()
        except OSError:
            raise SandboxUnavailable(f"the sbx daemon socket {sock} does not exist: it is not running (fail closed; "
                                     "this runner never starts it)") from None
        if not stat.S_ISSOCK(st.st_mode) or st.st_uid != os.getuid():
            raise SandboxUnavailable(f"{sock} is not a socket owned by this user (fail closed)")
        if not _sbx_daemon_reachable(self.environ):
            raise SandboxUnavailable(f"the sbx daemon socket {sock} exists but nothing accepts a connection on it: the daemon "
                                     "is not reachable (a stale socket file; fail closed; this runner never starts it)")
        p = self._must(self._argv("version"), "version")
        m = _SBX_VERSION.search(p.stdout)
        version = m.group(1) if m else None
        if version is None:
            raise SandboxUnavailable("could not read the sbx version (fail closed)")
        if version not in self.verified:
            raise SandboxUnavailable(f"sbx version {version} is not in the verified set "
                                     f"({', '.join(sorted(self.verified)) or 'empty'}) (fail closed)")
        problem = self.policy_problem(self._must(self._argv("policy", "ls", "--json"), "policy ls").stdout)
        if problem:
            raise SandboxUnavailable(f"{problem} (fail closed)")
        # Re-read on every run, not trusted from the selection probe's cache: the user can flip them in between.
        raw = self._must(self._argv(*_SBX_SETTINGS_ARGV), "settings ls").stdout
        try:
            on = sbx_unsafe_settings(sbx_settings_observed(raw), self.environ)
        except ValueError as e:
            raise SandboxUnavailable(f"cannot read the sbx host-reach settings: {e} (fail closed)") from None
        if on:
            raise SandboxUnavailable(_unsafe_settings_message(on) + " (fail closed)")
        pending = sbx_restart_pending(raw, self.environ)
        if pending:
            raise SandboxUnavailable(f"{SbxRestartPending(pending)} (fail closed)")

    def _check_sandbox_denies_network(self, name: str) -> None:
        for target in SBX_DENIED_TARGETS:
            p = self._ctl(self._check_argv(name, target), self.control_timeout_s, "policy check network")
            try:
                verdict = json.loads(p.stdout)
            except ValueError:
                verdict = None
            # A denied check exits 1 (observed on v0.50.1). Anything but exit 1 AND allowed == false is a surprise.
            if not (isinstance(verdict, dict) and verdict.get("allowed") is False and p.returncode == 1):
                raise SandboxUnavailable(f"the sandbox's network policy does not deny {target} "
                                         f"(exit {p.returncode}: {_first_line(p.stderr or p.stdout)}) (fail closed)")

    def _verify_staged(self, spec: SandboxSpec, name: str) -> None:
        p = self._must(self._list_staged_argv(name), "exec find")
        got = {line for line in p.stdout.splitlines() if line}
        want = {f"{SBX_CANDIDATE_DIR}/{key}" for key in spec.files}
        if got != want:
            raise SandboxUnavailable(f"the staged files in the sandbox are not exactly the requested ones "
                                     f"(missing {sorted(want - got)}, extra {sorted(got - want)}) (fail closed)")

    def _exec(self, argv: list[str], timeout_s: int) -> SandboxResult:
        out, err, cut = bytearray(), bytearray(), [False]
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=_sbx_env(self.environ), start_new_session=True)
        try:
            readers = [threading.Thread(target=_drain, args=(s, self.max_output_bytes, b, cut), daemon=True)
                       for s, b in ((proc.stdout, out), (proc.stderr, err))]
            for t in readers:
                t.start()
            timed_out = False
            try:
                code: int | None = proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                timed_out, code = True, None
                self._kill_client(proc)
            for t in readers:
                t.join(timeout=_PROBE_TIMEOUT_S)
        finally:
            if proc.poll() is None:
                self._kill_client(proc)
        note = f"\n[output truncated at {self.max_output_bytes} bytes]\n" if cut[0] else ""
        stdout = out.decode("utf-8", "replace") + note
        if timed_out:
            # The in-VM process may outlive the killed client: the caller's `rm --force` destroys the VM.
            return SandboxResult(None, stdout, "timed out", True, runner=RUNNER_SBX)
        stderr = err.decode("utf-8", "replace")
        # 125 is sbx's "could not run it". On v0.50.1 a wedged VM (a fork flood) made `sbx exec` print
        # `failed to start stdin stream: ...` and exit 255, which a candidate could also exit with: only that
        # sbx-shaped stderr, never the bare number, is treated as sbx's own failure.
        if code == 125 or (code == 255 and _SBX_EXEC_FAILURE.match(stderr)):
            raise SandboxUnavailable(f"`sbx exec` could not run the command (exit {code}): "
                                     f"{_first_line(stderr)} (fail closed)")
        return SandboxResult(code, stdout, stderr, runner=RUNNER_SBX)

    @staticmethod
    def _kill_client(proc: subprocess.Popen) -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        try:
            proc.wait(timeout=_PROBE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            pass

    def _destroy(self, name: str) -> None:
        """`rm --force`, then prove with `ls -q` that the sandbox is gone. A sandbox that stays is an error."""
        rm = self._ctl(self._rm_argv(name), self.control_timeout_s, "rm --force")
        ls = self._ctl(self._ls_argv(), self.control_timeout_s, "ls")
        if ls.returncode != 0:
            raise SandboxUnavailable(f"could not confirm that sandbox {name} was removed (`sbx ls` exit "
                                     f"{ls.returncode}); remove it with `sbx rm --force {name}`")
        if name in ls.stdout.split():
            raise SandboxUnavailable(f"sandbox {name} was NOT removed (`sbx rm --force` exit {rm.returncode}: "
                                     f"{_first_line(rm.stderr or rm.stdout)}); remove it with `sbx rm --force {name}`")

    # ── the run ───────────────────────────────────────────────────────────────────

    def run(self, spec: SandboxSpec) -> SandboxResult:
        if not spec.command or not all(isinstance(a, str) and "\0" not in a for a in spec.command):
            raise ValueError("command must be a non-empty list of strings without NUL")
        _validate_stage_names(spec.files)  # cheapest refusal first: no sbx process for a bad key
        if self._fell_back is not None:  # sticky: a broken sbx would cost every remaining candidate a preflight
            return self._fell_back.run(spec)
        try:
            return self._run_in_sbx(spec)
        except SandboxNotStarted as e:
            if self.fallback is None:
                raise
            return self._run_on_fallback(spec, e)

    def _run_on_fallback(self, spec: SandboxSpec, why: SandboxNotStarted) -> SandboxResult:
        """`auto` only. sbx failed before the candidate started; the selection that chose it (cached for up to 60 s) is
        stale, so it is dropped, the event is logged loudly, and this spec runs on the Docker runner the factory
        verifies NOW. A Docker that is not usable fails closed, naming both."""
        clear_sbx_cache()
        reason = {"code": WHY_RUN_FAILED, "detail": str(why), "loud": True}
        try:
            self._fell_back = self.fallback(reason)
        except SandboxUnavailable as e:
            raise SandboxUnavailable(f"{why}; and the Docker fallback is unusable: {e}") from e
        _log.warning("sandbox runner is rootless Docker, not sbx: sbx failed before the candidate started: %s "
                     "[%s]. Fix sbx, or set %s=docker to choose Docker on purpose.", why, WHY_RUN_FAILED, ENV_SANDBOX)
        return self._fell_back.run(spec)

    def _run_in_sbx(self, spec: SandboxSpec) -> SandboxResult:
        try:
            self._preflight()
        except SandboxUnavailable as e:
            raise SandboxNotStarted(str(e)) from e
        stage = _stage_for_sbx(spec)
        name = f"harness2-{secrets.token_hex(6)}"
        try:
            try:
                self._must(self._create_argv(name), "create", self.create_timeout_s)
                if name not in self._must(self._ls_argv(), "ls").stdout.split():
                    raise SandboxUnavailable(f"sandbox {name} is not listed by `sbx ls -q` after create (fail closed)")
                self._check_sandbox_denies_network(name)
                if stage is not None:
                    self._must(self._cp_in_argv(stage, name), "cp")
                    self._verify_staged(spec, name)
            except SandboxNotStarted:
                raise
            except SandboxUnavailable as e:
                raise SandboxNotStarted(str(e)) from e
            # From here on the candidate has been started: nothing below may fall back to another runtime.
            return self._exec(self._exec_argv(spec, name, stage is not None), spec.timeout_s)
        finally:
            try:
                self._destroy(name)  # also when create failed half way: `rm --force` of a missing name is harmless
            finally:
                if stage is not None:
                    shutil.rmtree(stage, ignore_errors=True)


# ── choosing between sbx and rootless Docker ───────────────────────────────────────

SBX_ABSENT = "absent"
SBX_UNUSABLE = "present-but-unusable"
SBX_SELECTED = "usable-and-selected"
SBX_NOT_CONSULTED = "not-consulted"  # COGNITIVE_HARNESS2_SANDBOX=docker: sbx is never looked at

MODE_AUTO, MODE_DOCKER, MODE_SBX = "auto", "docker", "sbx"

# Structured reasons (`SbxProblem.code`) why a present sbx is not the selected runner.
WHY_NOT_INSTALLED = "not-installed"
WHY_FORCED_DOCKER = "forced-docker"
WHY_NO_VERIFIED_VERSION = "no-verified-version"
WHY_UNVERIFIED_VERSION = "unverified-version"
WHY_UNREADABLE_VERSION = "unreadable-version"
WHY_KVM = "kvm-unusable"
WHY_DAEMON_DOWN = "daemon-down"
WHY_POLICY = "policy-not-deny-all"
WHY_UNSAFE_SETTING = "unsafe-setting"
WHY_UNREADABLE_SETTING = "unreadable-setting"
WHY_PROBE_TIMEOUT = "probe-timeout"
WHY_PROBE_ERROR = "probe-error"
WHY_BAD_SETTING = "bad-setting-value"
WHY_RESTART_PENDING = "restart-pending"  # a host-reach setting reads off but needs `sbx daemon restart` to apply
WHY_RUN_FAILED ="sbx-run-failed"  # selected and usable at probe time, then failed before the candidate started

_SBX_PROBE_TIMEOUT_S = 5       # one read-only `sbx` command; the whole probe asks at most three
_SBX_PROBE_CACHE_TTL_S = 60.0  # per process; a daemon started later is noticed within this long


class SbxProbes(NamedTuple):
    """The read-only questions asked about a present `sbx`. Injectable, so selection is testable without one.

    Each returns None (or a value) when fine. `version`, `policy_problem` and `risky_settings` run an `sbx` process
    and are called ONLY once `daemon_reachable` is true: asking a CLI while its daemon is down can start the daemon
    (`sbx settings` says so), and nothing in this module may be what starts it. `risky_settings` returns the
    observed values of `SBX_RISKY_SETTINGS` and raises when they cannot be read. A probe that raises
    `subprocess.TimeoutExpired` is a timeout, any other exception an error; neither is ever a crash.
    """
    kvm_problem: Callable[[], str | None]
    daemon_reachable: Callable[[], bool]
    version: Callable[[str], str | None]
    policy_problem: Callable[[str], str | None]
    risky_settings: Callable[[str], Mapping[str, bool]]


_SBX_VERSION = re.compile(r"\bv?(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.]+)?)")


def _sbx_socket(environ: Mapping[str, str]) -> Path:
    # The location `sbx daemon status` reports on Linux (v0.50.1). Anywhere else the daemon reads as unreachable.
    base = environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "sandboxes" / "sandboxes" / "sandboxd" / "sandboxd.sock"


def _sbx_daemon_reachable(environ: Mapping[str, str]) -> bool:
    """True only if something ACCEPTS a connection on the sbx daemon socket. One check for selection and for the runner's
    preflight: a socket file whose daemon died (a stale file) passes `stat` and fails here."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(2)
        s.connect(str(_sbx_socket(environ)))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _run_readonly(argv: list[str], env: Mapping[str, str] | None = None,
                  timeout: float | None = None) -> subprocess.CompletedProcess:
    """One bounded probe command. Own process group, killed whole on timeout, so a hung `sbx` (or a child of it that
    keeps the pipes open) cannot hang the caller: raises `subprocess.TimeoutExpired` within about `timeout` + 2 s."""
    timeout = _SBX_PROBE_TIMEOUT_S if timeout is None else timeout
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            env=None if env is None else dict(env), start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        try:
            proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        raise
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def real_sbx_probes(environ: Mapping[str, str]) -> SbxProbes:
    env = _sbx_env(environ)  # the same scrubbed environment the runner uses, so settings read the same way

    def kvm() -> str | None:
        if not os.path.exists("/dev/kvm"):
            return "/dev/kvm does not exist (no virtualization interface)"
        if not os.access("/dev/kvm", os.R_OK | os.W_OK):
            return "/dev/kvm: permission denied (add the user to the kvm group and log in again)"
        return None

    def daemon() -> bool:
        return _sbx_daemon_reachable(environ)

    def version(path: str) -> str | None:
        p = _run_readonly([path, "version"], env)
        m = _SBX_VERSION.search(p.stdout) if p.returncode == 0 else None
        return m.group(1) if m else None

    def policy(path: str) -> str | None:
        p = _run_readonly([path, "policy", "ls", "--json"], env)
        if p.returncode == 0:
            return sbx_network_policy_problem(p.stdout)
        text = _first_line(p.stderr or p.stdout)
        return ("global network policy is not initialised (`sbx policy init <profile>`)"
                if "not been initialized" in text else f"`sbx policy ls` failed: {text}")

    def risky_settings(path: str) -> Mapping[str, bool]:
        p = _run_readonly([path, *_SBX_SETTINGS_ARGV], env)
        if p.returncode != 0:
            raise RuntimeError(f"`sbx settings ls` failed (exit {p.returncode}): {_first_line(p.stderr or p.stdout)}")
        observed = sbx_settings_observed(p.stdout)
        if not sbx_unsafe_settings(observed, environ):  # an "on" setting is reported as that, not as a pending restart
            pending = sbx_restart_pending(p.stdout, environ)
            if pending:
                raise SbxRestartPending(pending)
        return observed

    return SbxProbes(kvm, daemon, version, policy, risky_settings)


class SbxProblem(NamedTuple):
    code: str    # one of the WHY_* constants
    detail: str  # a sentence for people


@dataclass(frozen=True)
class SbxReport:
    state: str                      # SBX_ABSENT | SBX_UNUSABLE | SBX_SELECTED | SBX_NOT_CONSULTED
    path: str | None
    version: str | None
    mode: str                       # MODE_AUTO | MODE_DOCKER | MODE_SBX
    verified_versions: tuple[str, ...]
    problems: tuple[SbxProblem, ...]  # empty only when usable and selected
    settings: tuple[tuple[str, bool], ...] = ()  # SBX_RISKY_SETTINGS as observed, when they were read

    @property
    def selected(self) -> bool:
        return self.state == SBX_SELECTED

    @property
    def reasons(self) -> tuple[str, ...]:
        return tuple(p.detail for p in self.problems)

    def as_dict(self) -> dict:
        return {"state": self.state, "path": self.path, "version": self.version, "mode": self.mode,
                "verified_versions": list(self.verified_versions), "reasons": list(self.reasons),
                "codes": [p.code for p in self.problems], "settings": dict(self.settings)}


def sandbox_mode(environ: Mapping[str, str]) -> tuple[str, str | None]:
    """(mode, a problem with the setting). Unset, empty and `auto` are the default; an unrecognised value is a
    refusal, not a default."""
    raw = environ.get(ENV_SANDBOX) or ""
    value = raw.strip().lower()
    if value in ("", "auto"):
        return MODE_AUTO, None
    if value in ("docker", "rootless-docker"):
        return MODE_DOCKER, None
    if value == "sbx":
        return MODE_SBX, None
    return MODE_AUTO, f"{ENV_SANDBOX}={raw!r} is not recognised (use auto, docker or sbx)"


def inspect_sbx(which: Callable[[str], str | None] = shutil.which, *, environ: Mapping[str, str] | None = None,
                probes: SbxProbes | None = None, verified: frozenset[str] | None = None) -> SbxReport:
    """Everything that stands between a present `sbx` and being selected. Read-only; never starts `sbx` itself.

    Order, cheapest first, each step skipping the dearer ones when it already decides: mode `docker` (nothing is
    consulted) -> `sbx` on PATH -> a non-empty verified set -> /dev/kvm -> the daemon socket answers -> only then
    the `sbx` processes: version (an unverified version stops here), policy, settings. A timeout stops the
    sbx commands.

    The real probes are cached per process for a short time (injected `probes` never are).
    """
    environ = os.environ if environ is None else environ
    verified = VERIFIED_SBX_VERSIONS if verified is None else verified
    mode, bad = sandbox_mode(environ)
    listed = tuple(sorted(verified))
    if mode == MODE_DOCKER and not bad:
        return SbxReport(SBX_NOT_CONSULTED, None, None, mode, listed, (SbxProblem(
            WHY_FORCED_DOCKER, f"{ENV_SANDBOX}=docker: sbx is never consulted"),))
    path = which("sbx")
    if not path:
        return SbxReport(SBX_ABSENT, None, None, mode, listed, (SbxProblem(WHY_NOT_INSTALLED, "`sbx` is not on PATH"),))
    problems: list[SbxProblem] = []
    if bad:
        problems.append(SbxProblem(WHY_BAD_SETTING, bad))
    if not verified:
        problems.append(SbxProblem(WHY_NO_VERIFIED_VERSION, "sbx version is not in the verified set (the verified set "
                                   "is empty: SbxRunner has not been verified against any real version)"))
    if problems:  # nothing to ask an sbx process about: no filesystem access, no process
        return SbxReport(SBX_UNUSABLE, path, None, mode, listed, tuple(problems))
    if probes is not None:
        return _probe_sbx(path, environ, probes, verified, mode)
    return _cached(path, environ, verified, mode)


_FAILED = object()
_CACHE: dict[tuple, tuple[float, SbxReport]] = {}
_CACHE_LOCK = threading.Lock()


def clear_sbx_cache() -> None:
    """Forget the cached probe result (tests; or after changing sbx settings in a long-lived process)."""
    with _CACHE_LOCK:
        _CACHE.clear()


def _cached(path: str, environ: Mapping[str, str], verified: frozenset[str], mode: str) -> SbxReport:
    key = (path, mode, tuple(sorted(verified)), environ.get("XDG_STATE_HOME"), environ.get("HOME"),
           host_settings_allowed(environ))
    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < _SBX_PROBE_CACHE_TTL_S:
            return hit[1]
    rep = _probe_sbx(path, environ, real_sbx_probes(environ), verified, mode)
    with _CACHE_LOCK:
        _CACHE[key] = (time.monotonic(), rep)
    return rep


def _probe_sbx(path: str, environ: Mapping[str, str], pr: SbxProbes, verified: frozenset[str], mode: str) -> SbxReport:
    listed = tuple(sorted(verified))
    problems: list[SbxProblem] = []
    version: str | None = None
    observed: dict[str, bool] = {}
    timed_out = [False]

    def ask(label: str, fn: Callable, *args):
        try:
            return fn(*args)
        except subprocess.TimeoutExpired:
            timed_out[0] = True
            problems.append(SbxProblem(WHY_PROBE_TIMEOUT, f"{label} probe timed out after {_SBX_PROBE_TIMEOUT_S}s "
                                       "(a hung sbx counts as unusable)"))
        except SbxRestartPending as e:
            problems.append(SbxProblem(WHY_RESTART_PENDING, str(e)))
        except Exception as e:  # noqa: BLE001 - a probe that cannot answer means unusable, never a crash
            problems.append(SbxProblem(WHY_PROBE_ERROR, f"{label} probe failed: {type(e).__name__}: {e}"))
        return _FAILED

    kvm = ask("/dev/kvm", pr.kvm_problem)
    if isinstance(kvm, str):
        problems.append(SbxProblem(WHY_KVM, kvm))
    up = ask("daemon", pr.daemon_reachable)
    if up is False:
        problems.append(SbxProblem(WHY_DAEMON_DOWN, "sbx daemon is not reachable (`sbx daemon start`); its version, "
                                   "policy and settings were not checked, so that no sbx command can start it"))
    elif up is True and not timed_out[0]:
        version = ask("version", pr.version, path)
        if version is _FAILED:
            version = None
        elif version is None:
            problems.append(SbxProblem(WHY_UNREADABLE_VERSION, "could not read the sbx version"))
        elif version not in verified:
            problems.append(SbxProblem(WHY_UNVERIFIED_VERSION, f"sbx version {version} is not in the verified set "
                                       f"({', '.join(listed)})"))
        # An unverified or unreadable version: its other commands are not trusted either, so none is run.
        if version is not None and version in verified and not timed_out[0]:
            policy = ask("policy", pr.policy_problem, path)
            if isinstance(policy, str):
                problems.append(SbxProblem(WHY_POLICY, policy))
            if not timed_out[0]:
                got = ask("settings", pr.risky_settings, path)
                if got is not _FAILED:
                    try:
                        observed = {k: got[k] for k in SBX_RISKY_SETTINGS}
                    except (KeyError, TypeError):
                        problems.append(SbxProblem(WHY_UNREADABLE_SETTING, "the sbx host-reach settings "
                                                   f"({', '.join(SBX_RISKY_SETTINGS)}) could not be read"))
                    else:
                        on = sbx_unsafe_settings(observed, environ)
                        if on:
                            problems.append(SbxProblem(WHY_UNSAFE_SETTING, _unsafe_settings_message(on)))
    return SbxReport(SBX_UNUSABLE if problems else SBX_SELECTED, path, version, mode, listed, tuple(problems),
                     tuple(observed.items()))


def _docker_or_refuse(which: Callable[[str], str | None], sbx: SbxReport) -> VerifiedDocker:
    try:
        return verify_rootless_docker(which)
    except SandboxUnavailable as e:
        if sbx.state == SBX_ABSENT:
            raise SandboxUnavailable(f"the `sbx` runtime is not installed and the Docker fallback is unusable: {e}") from e
        if sbx.state == SBX_NOT_CONSULTED:
            raise SandboxUnavailable(f"{ENV_SANDBOX}=docker and the Docker runtime is unusable: {e}") from e
        raise SandboxUnavailable(f"the `sbx` runtime is not selected ({'; '.join(sbx.reasons)}) "
                                 f"and the Docker fallback is unusable: {e}") from e


@dataclass(frozen=True)
class SandboxChoice:
    """The outcome of the selection: which runner, why, and what verified it. Nothing here has run anything."""
    mode: str
    runner: str                        # RUNNER_SBX | RUNNER_DOCKER | RUNNER_NONE
    sbx: SbxReport
    docker: VerifiedDocker | None = None
    refusal: str | None = None         # set exactly when runner is RUNNER_NONE

    @property
    def fallback_reason(self) -> dict | None:
        """Why sbx is not the runner, as {code, detail, loud}; None when sbx is. `loud` is true when sbx is present
        and was not selected for a reason other than the user's choice: that is the case that is warned about."""
        if self.runner == RUNNER_SBX or not self.sbx.problems:
            return None
        p = self.sbx.problems[0]
        return {"code": p.code, "detail": p.detail, "loud": self.mode == MODE_AUTO and self.sbx.state == SBX_UNUSABLE}


def choose_sandbox(which: Callable[[str], str | None] = shutil.which, *, environ: Mapping[str, str] | None = None,
                   probes: SbxProbes | None = None, verified: frozenset[str] | None = None) -> SandboxChoice:
    """The whole selection (see the module docstring). Never raises for an unusable runtime: `refusal` says why."""
    environ = os.environ if environ is None else environ
    mode, bad = sandbox_mode(environ)
    rep = inspect_sbx(which, environ=environ, probes=probes, verified=verified)
    if bad:
        return SandboxChoice(mode, RUNNER_NONE, rep, refusal=f"{bad} (fail closed)")
    if mode == MODE_SBX:
        if rep.selected:
            return SandboxChoice(mode, RUNNER_SBX, rep)
        return SandboxChoice(mode, RUNNER_NONE, rep, refusal=(
            f"{ENV_SANDBOX}=sbx but the `sbx` runtime is not usable: {'; '.join(rep.reasons)} "
            "(fail closed; this setting never falls back to Docker)"))
    if mode == MODE_AUTO and rep.selected:
        return SandboxChoice(mode, RUNNER_SBX, rep)
    try:
        return SandboxChoice(mode, RUNNER_DOCKER, rep, docker=_docker_or_refuse(which, rep))
    except SandboxUnavailable as e:
        return SandboxChoice(mode, RUNNER_NONE, rep, refusal=str(e))


_WARNED: set[str] = set()
_WARN_LOCK = threading.Lock()
_log = logging.getLogger(__name__)


def _reset_warnings() -> None:
    with _WARN_LOCK:
        _WARNED.clear()


def _warn_fallback_once(choice: SandboxChoice) -> None:
    """One WARNING per process per distinct reason set: sbx is present, `auto` did not select it, Docker runs instead."""
    reason = choice.fallback_reason
    if not reason or not reason["loud"]:
        return
    msg = (f"sandbox runner is rootless Docker, not sbx: {'; '.join(choice.sbx.reasons)} "
           f"[{', '.join(p.code for p in choice.sbx.problems)}]. Fix sbx, or set {ENV_SANDBOX}=docker to choose Docker "
           "on purpose (this warning is then not shown).")
    with _WARN_LOCK:
        if msg in _WARNED:
            return
        _WARNED.add(msg)
    _log.warning("%s", msg)


def default_runner(which: Callable[[str], str | None] = shutil.which, *, environ: Mapping[str, str] | None = None,
                   probes: SbxProbes | None = None, verified: frozenset[str] | None = None) -> SandboxRunner:
    """The runner for model-free, no-network execution of untrusted code. Fails closed. Never rootful Docker, never the host.

    `COGNITIVE_HARNESS2_SANDBOX` unset or `auto`: `SbxRunner` when sbx is a verified version AND the read-only probe
    passes, else rootless Docker (a present-but-unselected sbx is warned about once). `docker`: rootless Docker,
    sbx never consulted. `sbx`: `SbxRunner` or `SandboxUnavailable`, never Docker. Any other value: refuse. In `auto` the `SbxRunner` also
    carries a lazy Docker fallback for a failure before the candidate started (see `SbxRunner.run`).
    `sbx` merely being on PATH changes nothing.
    """
    environ = os.environ if environ is None else environ
    verified = VERIFIED_SBX_VERSIONS if verified is None else verified
    choice = choose_sandbox(which, environ=environ, probes=probes, verified=verified)
    if choice.refusal:
        raise SandboxUnavailable(choice.refusal)
    if choice.runner == RUNNER_SBX:
        fallback = None
        if choice.mode == MODE_AUTO:  # `sbx` mode never falls back; `auto` falls back to Docker verified at that moment
            def fallback(reason: dict) -> SandboxRunner:
                vd = verify_rootless_docker(which)
                return DockerRunner(vd.path, docker_host=vd.host, fallback_reason=reason)
        return SbxRunner(choice.sbx.path or "sbx", environ=environ, verified=verified, fallback=fallback)
    assert choice.docker is not None
    _warn_fallback_once(choice)
    return DockerRunner(choice.docker.path, docker_host=choice.docker.host, fallback_reason=choice.fallback_reason)


def sandbox_report(which: Callable[[str], str | None] = shutil.which, *, environ: Mapping[str, str] | None = None,
                   probes: SbxProbes | None = None, verified: frozenset[str] | None = None) -> dict:
    """For harness2_status and the advice: `sandbox` is the selected runner (or the refusal), `runner` and `mode` say
    it as data, `fallback_reason` is {code, detail, loud} when sbx is not the runner, and `sbx` says why in full.

    Local only: no model, no network. Runs read-only `sbx` commands only when its daemon is already up.
    """
    c = choose_sandbox(which, environ=environ, probes=probes, verified=verified)
    sandbox = c.runner if c.runner != RUNNER_NONE else f"unavailable (fail closed): {c.refusal}"
    return {"sandbox": sandbox, "runner": c.runner, "mode": c.mode, "fallback_reason": c.fallback_reason,
            "sbx": c.sbx.as_dict()}


def sandbox_status(which: Callable[[str], str | None] = shutil.which, **kw) -> str:
    """One line for harness2_status. Local only: no model, no network."""
    return sandbox_report(which, **kw)["sandbox"]


def static_diff(name: str, original: str, candidate: str, *, allow_worktree_diff: bool) -> str:
    """A unified diff computed in a temp directory. Never executes anything."""
    if not allow_worktree_diff:
        raise SandboxUnavailable("static diff fallback needs explicit opt-in (allow_worktree_diff)")
    with tempfile.TemporaryDirectory(prefix="harness2-diff-") as d:
        base = Path(d)
        (base / "a").write_text(original, encoding="utf-8")
        (base / "b").write_text(candidate, encoding="utf-8")
        a = (base / "a").read_text(encoding="utf-8").splitlines(keepends=True)
        b = (base / "b").read_text(encoding="utf-8").splitlines(keepends=True)
    return "".join(difflib.unified_diff(a, b, f"a/{name}", f"b/{name}"))
