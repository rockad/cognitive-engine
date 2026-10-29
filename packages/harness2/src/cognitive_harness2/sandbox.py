"""Candidate execution in local microVMs, failing closed (RFC 004 §2.2).

Candidate code runs ONLY in one of two local runtimes, chosen in this order:

  1. a Docker Sandbox microVM driven by the standalone `sbx` CLI, when `sbx` is on PATH;
  2. otherwise an EPHEMERAL ROOTLESS DOCKER container (`DockerRunner`), and only when the
     daemon itself reports `name=rootless` in `docker info` SecurityOptions.

With neither, the prober refuses to run anything (fail closed): a missing `docker`, an
unreachable or stopped daemon and a rootful daemon all raise `SandboxUnavailable`. There is
no silent fallback to a rootful daemon or to host execution. The daemon endpoint the gate
verified is pinned (`-H unix://...`, ambient DOCKER_HOST and DOCKER_CONTEXT dropped) on every
docker call the runner makes afterwards, so it cannot change between the check and the run. The one remaining fallback is
an explicit opt-in that writes candidates to a temp directory and returns a diff for static
inspection; it never executes code.

⚠️ `SbxRunner._argv` is the single place that knows the `sbx` command line, and it is
UNVERIFIED against a real `sbx` (none is installed on this host). The fail-closed
behaviour does not depend on it and is fully tested; real microVM isolation is an open
acceptance item until `sbx` exists on the host. `DockerRunner` IS verified end to end
against a real rootless daemon (tests/test_docker_runner.py).
"""
from __future__ import annotations

import difflib
import json
import os
import secrets
import shutil
import subprocess
import tempfile
import threading
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Protocol

from . import invariants


class SandboxUnavailable(RuntimeError):
    """No usable local sandbox runtime: refuse to run candidate code."""


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


class SandboxRunner(Protocol):
    def run(self, spec: SandboxSpec) -> SandboxResult: ...


def require_sbx(which: Callable[[str], str | None] = shutil.which) -> str:
    path = which("sbx")
    if not path:
        raise SandboxUnavailable(
            "the `sbx` runtime is not installed, so candidate code will not be run (fail closed). "
            "Install sbx, or pass allow_worktree_diff to get static diffs only.")
    return path


class SbxRunner:
    def __init__(self, sbx_path: str):
        self.sbx_path = sbx_path

    def _argv(self, spec: SandboxSpec) -> list[str]:
        # UNVERIFIED against a real sbx: the subcommand and flags are assumptions.
        return [self.sbx_path, "run", "--", *spec.command]

    def run(self, spec: SandboxSpec) -> SandboxResult:
        try:
            p = subprocess.run(self._argv(spec), capture_output=True, text=True, timeout=spec.timeout_s)
        except subprocess.TimeoutExpired as e:
            return SandboxResult(None, (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or ""),
                                 "timed out", True)
        return SandboxResult(p.returncode, p.stdout, p.stderr)


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
        # Invariant harness files (guardrails.md) must be staged so the agent runs
        # with its guardrails, but protected execution paths (hooks, .git, guard) are refused.
        if name.rstrip(". ").lower() == "guardrails.md":
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
                 user: str = "65534:65534", max_output_bytes: int = MAX_OUTPUT_BYTES):
        """`docker_host` is the endpoint the gate verified (`VerifiedDocker.host`). With it, every
        docker call this runner makes goes to exactly that daemon and the ambient DOCKER_HOST and
        DOCKER_CONTEXT are never consulted. Without it, each `run()` resolves the endpoint once,
        verifies it, and pins the rest of that run to the endpoint it verified."""
        if docker_host is not None and not _is_local_unix(docker_host):
            raise SandboxUnavailable(
                f"the Docker endpoint {docker_host or 'is unknown'!r} is not a local unix socket; candidate "
                "code must never leave this machine (fail closed)")
        self.docker_path, self.docker_host, self.image, self.pids_limit = docker_path, docker_host, image, pids_limit
        self.memory, self.cpus, self.tmpfs_size, self.user = memory, cpus, tmpfs_size, user
        self.max_output_bytes = max_output_bytes

    def _argv(self, spec: SandboxSpec, stage_dir: Path, name: str, host: str) -> list[str]:
        return [
            self.docker_path, "-H", host, "run", "--rm", "--name", name,
            "--label", "cognitive-harness2.sandbox=1", "--pull", "never",
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
        if timed_out:
            return SandboxResult(None, stdout, "timed out", True)
        return SandboxResult(code, stdout, err.decode("utf-8", "replace"))


def default_runner(which: Callable[[str], str | None] = shutil.which) -> SandboxRunner:
    """`sbx` when installed, else a verified rootless Docker, else raise (fail closed)."""
    sbx = which("sbx")
    if sbx:
        return SbxRunner(sbx)
    try:
        verified = verify_rootless_docker(which)
        return DockerRunner(verified.path, docker_host=verified.host)
    except SandboxUnavailable as e:
        raise SandboxUnavailable(f"the `sbx` runtime is not installed and the Docker fallback is unusable: {e}") from e


def sandbox_status(which: Callable[[str], str | None] = shutil.which) -> str:
    """One line for harness2_status. Local only: no model, no network."""
    try:
        runner = default_runner(which)
    except SandboxUnavailable as e:
        return f"unavailable (fail closed): {e}"
    return "sbx" if isinstance(runner, SbxRunner) else "rootless-docker"


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
