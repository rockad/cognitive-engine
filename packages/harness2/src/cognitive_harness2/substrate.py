"""Seam 3: `CognitiveSubstrate`, upstream's Substrate Protocol with execution switched off (slice 3a).

Upstream's `Substrate` (harness2.substrate.base) is five members: `surface`, `materialise`,
`execute`, `read_trajectory` and `preflight`, plus a `name`. This class implements the three that
cannot run a model or a candidate:

  * `surface`      the upstream `SurfaceSpec` it was built with, refused at construction unless
                   `upstream_surface.check_surface` recognises its shape
  * `materialise`  stages the editable tree for a later run: read-only, under the hardened staging
                   (`DockerRunner._stage`, so DockerRunner's key rules decide every file name), no
                   symlinks, UTF-8 text only, capped in count and size
  * `preflight`    `require_rootless_docker`, then the quota pre-flight only if the caller wired
                   one in; raises `PreflightError` (a RuntimeError, as upstream's contract says)
                   carrying the remediation; starts nothing

`execute` and `read_trajectory` raise `NotImplementedError("slice 3b, not enabled")`. There is no
argument, setting or environment variable that turns them on: slice 3b replaces this class's
methods in a separate, user-gated change.

This module imports nothing from upstream. `materialise` returns `StagedInstall`, which has the
same fields as upstream's `Install`, because importing `harness2.substrate` runs `harness2.config`,
which reads `$CWD/.env` into `os.environ`; a test pins the two field lists together.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import stat
import subprocess
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import quota as quota_mod
from .invariants import InvariantViolation
from .sandbox import (
    DEFAULT_IMAGE,
    DockerRunner,
    SandboxSpec,
    SandboxUnavailable,
    _validate_stage_names,
    require_rootless_docker,
)
from .upstream_surface import AllowedSurface, check_surface

NOT_ENABLED = "slice 3b, not enabled"

# harness2.core.spec.INSTALL_MODES at the pinned commit (a test pins the two together)
INSTALL_MODES = ("workspace_agents_md", "home_dir_jinja", "workspace_config", "codex_root")
_CODEX_MODE = "codex_root"

MAX_FILES = 512
MAX_FILE_BYTES = 1 << 20
MAX_TOTAL_BYTES = 8 << 20


class PreflightError(RuntimeError):
    """A readiness check failed. The message carries the remediation; nothing was started."""


@dataclass(frozen=True)
class StagedInstall:
    """The fields of upstream's `Install`: where the tree was staged, a digest of its source, extras."""
    staging_dir: Path
    fingerprint: str
    extras: Mapping[str, str] = field(default_factory=dict)


def openrouter_quota_check(api_key: str | None, *, client: Any = None) -> Callable[[], quota_mod.QuotaVerdict]:
    """A synchronous quota check for `CognitiveSubstrate(quota_check=...)`: one `GET /api/v1/key`, no model call.

    Works from plain code and from inside a running event loop. Without a key it returns a refusal
    and makes no request.
    """
    def check() -> quota_mod.QuotaVerdict:
        coro = quota_mod.preflight(api_key, client)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()
    return check


def _fingerprint(files: Mapping[str, str]) -> str:
    h = hashlib.sha256()
    for rel in sorted(files):
        h.update(rel.encode())
        h.update(b"\0")
        h.update(files[rel].encode())
        h.update(b"\0")
    return h.hexdigest()[:16]


def _read_tree(root: Path) -> dict[str, str]:
    """Every regular UTF-8 file under `root` as rel -> text. Symlinks and special files are refused."""
    if root.is_symlink():
        raise ValueError(f"{root.name!r} is a symlink; refusing to stage through it")
    if not root.is_dir():
        raise ValueError(f"{str(root)!r} is not a directory")
    files: dict[str, str] = {}
    total = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in [*dirnames, *filenames]:
            p = Path(dirpath, name)
            if p.is_symlink():
                raise ValueError(f"{p.relative_to(root).as_posix()!r} is a symlink; refusing to stage it")
        for name in sorted(filenames):
            p = Path(dirpath, name)
            rel = p.relative_to(root).as_posix()
            st = p.lstat()
            if not stat.S_ISREG(st.st_mode):
                raise ValueError(f"{rel!r} is not a regular file; refusing to stage it")
            if len(files) >= MAX_FILES:
                raise ValueError(f"the tree has more than {MAX_FILES} files; refusing to stage it")
            if st.st_size > MAX_FILE_BYTES:
                raise ValueError(f"{rel!r} is larger than {MAX_FILE_BYTES} bytes; refusing to stage it")
            total += st.st_size
            if total > MAX_TOTAL_BYTES:
                raise ValueError(f"the tree is larger than {MAX_TOTAL_BYTES} bytes in total; refusing to stage it")
            try:
                files[rel] = p.read_bytes().decode("utf-8")
            except UnicodeDecodeError:
                raise ValueError(f"{rel!r} is not valid UTF-8 text; refusing to stage it") from None
    return files


class CognitiveSubstrate:
    """Upstream's Substrate Protocol: staging and readiness only. Execution is slice 3b."""

    name = "cognitive"

    def __init__(self, surface_spec: Any, *,
                 which: Callable[[str], str | None] = shutil.which,
                 run: Callable[..., subprocess.CompletedProcess] | None = None,
                 environ: Mapping[str, str] | None = None,
                 image: str = DEFAULT_IMAGE,
                 quota_check: Callable[[], quota_mod.QuotaVerdict] | None = None):
        """`surface_spec` is an upstream SurfaceSpec (e.g. `OPENCODE_SURFACE`). `quota_check`, when given,
        is called by `preflight` after the Docker gate; leave it out and no quota request is ever made."""
        self._allowed = check_surface(surface_spec)  # raises InvariantViolation for a shape it does not know
        self._spec = surface_spec
        self._which, self._environ, self._image, self._quota_check = which, environ, image, quota_check
        # resolved at call time, so a patched subprocess.run is what runs
        self._run = run or (lambda *a, **k: subprocess.run(*a, **k))
        self._staged: set[Path] = set()

    # ── Substrate ────────────────────────────────────────────────────────────────
    def surface(self) -> Any:
        return self._spec

    def allowed_surface(self) -> AllowedSurface:
        """What the optimizer may mutate on this surface (see `upstream_surface`)."""
        return self._allowed

    def materialise(self, tree_dir: Path, spec: Any, ctx: Mapping[str, str], *,
                    changed: Iterable[str] | None = None) -> StagedInstall:
        """Stage `tree_dir` read-only and return where.

        `changed` names the paths the optimizer edited; every one must be on the mutable surface or
        `InvariantViolation` is raised before anything is read. Every staged name (the tree's and the
        `file:<rel>` entries of `ctx`) must satisfy DockerRunner's key rules. Nothing is staged on refusal.
        The staged directory is removed with `release`.
        """
        mode = getattr(spec, "install_mode", None)
        if mode not in INSTALL_MODES:
            raise ValueError(f"install_mode {mode!r} is not one of {INSTALL_MODES}")
        if (mode == _CODEX_MODE) != (getattr(self._spec, "name", None) == "codex"):
            raise ValueError(f"install_mode {mode!r} does not belong to the {self._spec.name} surface")
        if changed is not None:
            check_surface(self._spec, tree=list(changed))

        root = Path(tree_dir)
        files = _read_tree(root)
        if not any(a in files for a in self._spec.always_on):
            raise ValueError(f"{str(root)!r} is not a {self._spec.name} harness tree (no always-on file present)")

        extra: dict[str, str] = {}
        for key, body in ctx.items():
            if not isinstance(key, str):
                raise ValueError("ctx keys must be strings")
            if key.startswith("file:"):
                if not isinstance(body, str):
                    raise ValueError("ctx file contents must be strings")
                rel = key[len("file:"):]
                if rel in files or rel in extra:
                    raise ValueError(f"ctx file {rel!r} collides with a file already in the tree")
                extra[rel] = body
        staged_files = {**files, **extra}
        _validate_stage_names(staged_files)  # DockerRunner's key rules, before anything is created

        stage = DockerRunner("docker")._stage(SandboxSpec(command=("true",), files=staged_files))
        try:
            for dirpath, _dirs, _files in os.walk(stage, topdown=False):
                os.chmod(dirpath, 0o555)
        except BaseException:
            self._remove(stage)
            raise
        self._staged.add(stage)
        return StagedInstall(stage, _fingerprint(files), {"install_mode": mode, "read_only": "true"})

    def execute(self, task_ref: str, install: Any, run_ids: list[str], timeout: int) -> list[Path]:
        raise NotImplementedError(NOT_ENABLED)

    def read_trajectory(self, stream_path: Path, *, obs_trunc: int, max_chars: int, args_trunc: int = 400) -> str:
        raise NotImplementedError(NOT_ENABLED)

    def preflight(self) -> None:
        """Rootless-Docker gate, then the caller's quota check if one was given. Starts nothing."""
        try:
            require_rootless_docker(self._which, run=self._run, environ=self._environ, image=self._image)
        except SandboxUnavailable as e:
            raise PreflightError(
                f"rootless Docker is not usable: {e}. Remediation: install rootless Docker "
                f"(`dockerd-rootless-setuptool.sh install`), start the daemon, point DOCKER_HOST at its unix "
                f"socket, then run `docker pull {self._image.split('@')[0]}` once for the pinned sandbox image. "
                f"Nothing was started.") from e
        if self._quota_check is None:
            return
        try:
            verdict = self._quota_check()
        except Exception as e:  # noqa: BLE001 - fail closed, and never repeat the message (it may hold a key)
            raise PreflightError(f"the quota pre-flight could not run ({type(e).__name__}). Remediation: check the "
                                 "OpenRouter key and network, or use a local endpoint. Nothing was started.") from None
        if not isinstance(verdict, quota_mod.QuotaVerdict):
            raise PreflightError("the quota pre-flight returned an unreadable verdict. Nothing was started.")
        if not verdict.ok:
            raise PreflightError(f"the quota pre-flight refused: {verdict.reason}. Remediation: wait for the quota "
                                 "to reset or use a local endpoint. Nothing was started.")

    # ── cleanup ──────────────────────────────────────────────────────────────────
    def release(self, install: StagedInstall) -> None:
        """Remove a directory this substrate staged. Anything else is refused."""
        stage = Path(install.staging_dir)
        if stage not in self._staged:
            raise ValueError(f"{str(stage)!r} was not staged by this substrate; refusing to remove it")
        self._remove(stage)
        self._staged.discard(stage)

    @staticmethod
    def _remove(stage: Path) -> None:
        for dirpath, dirnames, _files in os.walk(stage, topdown=True):
            os.chmod(dirpath, 0o755)
        shutil.rmtree(stage, ignore_errors=True)
