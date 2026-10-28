"""Candidate execution in local microVMs, failing closed (RFC 004 §2.2).

Candidate code runs ONLY in one of two local runtimes, chosen in this order:

  1. a Docker Sandbox microVM driven by the standalone `sbx` CLI, when `sbx` is on PATH;
  2. otherwise an EPHEMERAL ROOTLESS DOCKER container (`DockerRunner`), and only when the
     daemon itself reports `name=rootless` in `docker info` SecurityOptions.

With neither, the prober refuses to run anything (fail closed): a missing `docker`, an
unreachable or stopped daemon and a rootful daemon all raise `SandboxUnavailable`. There is
no silent fallback to a rootful daemon or to host execution. The one remaining fallback is
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
import posixpath
import secrets
import shutil
import subprocess
import tempfile
import threading
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


def _probe(argv: list[str], run: Callable[..., subprocess.CompletedProcess]) -> subprocess.CompletedProcess:
    try:
        return run(argv, capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise SandboxUnavailable(
            f"`{' '.join(argv[1:3])}` timed out after {_PROBE_TIMEOUT_S}s: the Docker daemon is "
            "unreachable (fail closed)") from None
    except OSError as e:
        raise SandboxUnavailable(f"cannot execute {argv[0]!r}: {e} (fail closed)") from e


def _first_line(text: str) -> str:
    return (text.strip().splitlines() or ["no output"])[0][:300]


def require_rootless_docker(
    which: Callable[[str], str | None] = shutil.which,
    *,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    environ: Mapping[str, str] | None = None,
    image: str = DEFAULT_IMAGE,
) -> str:
    """Return the `docker` path, or raise SandboxUnavailable. Fails CLOSED.

    Allowed only when ALL hold: `docker` exists; the daemon endpoint is a local unix
    socket (so code never leaves this machine); the daemon answers `docker info`; its
    SecurityOptions contain `name=rootless`; and the pinned image is already present.
    Anything else, including an unreachable or stopped daemon, raises. There is no
    rootful or host fallback.

    ⚠️ `docker info` can exit 0 with `null` when a TCP daemon is unreachable, so the exit
    status is never trusted: only a SecurityOptions list that names `rootless` passes.
    """
    env = os.environ if environ is None else environ
    path = which("docker")
    if not path:
        raise SandboxUnavailable("`docker` is not installed, so there is no rootless Docker fallback (fail closed)")

    host = env.get("DOCKER_HOST")
    if not host:
        p = _probe([path, "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"], run)
        host = p.stdout.strip() if p.returncode == 0 else ""
    if not host.startswith("unix://"):
        raise SandboxUnavailable(
            f"the Docker endpoint {host or 'is unknown'!r} is not a local unix socket; candidate code "
            "must never leave this machine (fail closed)")

    p = _probe([path, "info", "--format", "{{json .SecurityOptions}}"], run)
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

    if _probe([path, "image", "inspect", "--format", "{{.Id}}", image], run).returncode != 0:
        raise SandboxUnavailable(
            f"the pinned sandbox image {image!r} is not present locally and is never pulled at run "
            f"time; run `docker pull {image.split('@')[0]}` once (fail closed)")
    return path


def _stage_name(name: str) -> str:
    """A staged file name must be a clean relative path that the protected set allows."""
    if not name or "\0" in name or posixpath.isabs(name):
        raise ValueError(f"invalid staged file name {name!r}")
    return invariants.assert_path_unprotected(name)


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

    def __init__(self, docker_path: str, *, image: str = DEFAULT_IMAGE, pids_limit: int = 128,
                 memory: str = "256m", cpus: str = "1", tmpfs_size: str = "64m",
                 user: str = "65534:65534", max_output_bytes: int = MAX_OUTPUT_BYTES):
        self.docker_path, self.image, self.pids_limit = docker_path, image, pids_limit
        self.memory, self.cpus, self.tmpfs_size, self.user = memory, cpus, tmpfs_size, user
        self.max_output_bytes = max_output_bytes

    def _argv(self, spec: SandboxSpec, stage_dir: Path, name: str) -> list[str]:
        return [
            self.docker_path, "run", "--rm", "--name", name,
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
        stage = Path(tempfile.mkdtemp(prefix="harness2-stage-")).resolve()
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

    def _kill(self, name: str) -> None:
        for verb in (["kill", name], ["rm", "-f", name]):
            try:
                subprocess.run([self.docker_path, *verb], capture_output=True, timeout=_PROBE_TIMEOUT_S)
            except (subprocess.TimeoutExpired, OSError):
                pass

    def run(self, spec: SandboxSpec) -> SandboxResult:
        if not spec.command:
            raise ValueError("command cannot be empty")
        # Re-checked on every run: the daemon can be stopped or swapped between probes.
        require_rootless_docker(lambda _: self.docker_path, image=self.image)
        stage = self._stage(spec)
        name = f"harness2-{secrets.token_hex(6)}"
        out, err, cut = bytearray(), bytearray(), [False]
        proc: subprocess.Popen | None = None
        try:
            proc = subprocess.Popen(self._argv(spec, stage, name), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            readers = [threading.Thread(target=_drain, args=(s, self.max_output_bytes, b, cut), daemon=True)
                       for s, b in ((proc.stdout, out), (proc.stderr, err))]
            for t in readers:
                t.start()
            timed_out = False
            try:
                code: int | None = proc.wait(timeout=spec.timeout_s)
            except subprocess.TimeoutExpired:
                timed_out, code = True, None
                self._kill(name)
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
            self._kill(name)  # a no-op when --rm already removed it; never leaks a container
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
        return DockerRunner(require_rootless_docker(which))
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
