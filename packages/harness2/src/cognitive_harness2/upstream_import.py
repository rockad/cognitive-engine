"""The one place upstream (google-research/harness2) is imported, and imported on purpose.

Importing `harness2.config` has side effects this package does not want:

  * `load_dotenv(ROOT/.env)` reads a `.env` into `os.environ` (ROOT is the upstream checkout when a
    `pyproject.toml` sits beside the package, else the CURRENT DIRECTORY, so a stray `.env` in whatever
    directory the process happens to run in is loaded);
  * `os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "global")`;
  * it freezes environment-derived constants (the runs directory, model names, the Vertex project).

`import_upstream` therefore imports the modules this package binds to (`config`, `core.spec`,
`core.proposer`, `core.recursion`, `core.schedule`, `core.tree`) under controlled conditions and then
VERIFIES that nothing leaked. It is default-deny: any surprise raises `UpstreamImportError` and leaves
the process as it found it.

Conditions during the import:

  * the current directory is a private EMPTY temporary directory (restored afterwards);
  * `os.environ` is a snapshot minus every variable that would feed a frozen constant (`HARNESS2_*`,
    `GOOGLE_CLOUD_*`, Vertex variables) plus `HARNESS2_RUNS_DIR` (under the caller's directory) and a
    non-empty `HARNESS2_IMPROVER_MODEL`; the real environment is restored in a `finally`.

Checks (each one raises, naming what drifted):

  1. the installed distribution is the pinned commit. A git install (`direct_url.json` with
     `vcs_info.commit_id`) must name `PINNED_COMMIT`; an editable install (`dir_info.editable`) must be a
     git checkout whose HEAD is `PINNED_COMMIT`, whose tracked files are unmodified and which holds no `.env`
     beside the package (it would be read); anything else is refused;
  2. the package Python would import is the directory that distribution installed (no shadowing earlier on
     `sys.path`), and `config.__file__` is inside it;
  3. no `.env` was read: `os.environ` gained, lost or changed nothing except the controlled variables and
     the one `setdefault` upstream is known to make (`KNOWN_SETDEFAULTS`);
  4. `<package>.benchmarks` and `<package>.cli` (heavy, side-effecting) are not in `sys.modules`;
  5. the restored `os.environ` equals the snapshot and the current directory is the original one.

A module imported by someone else before this call cannot be proven clean, so it is refused. A second
call returns the first result.
"""
from __future__ import annotations

import contextlib
import importlib
import importlib.machinery
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from types import ModuleType

PINNED_COMMIT = "1cfe254e8d25b15f37048c3730134686abe60335"   # == [tool.uv.sources].harness2.rev (pinned by a test)
PACKAGE = "harness2"
MODULES = ("config", "core.spec", "core.proposer", "core.recursion", "core.schedule", "core.tree")
FORBIDDEN_MODULES = ("benchmarks", "cli")
KNOWN_SETDEFAULTS = frozenset({"GOOGLE_CLOUD_LOCATION"})      # upstream config.py's own os.environ.setdefault
PLACEHOLDER_MODEL = "contained/placeholder"
_SCRUB_PREFIXES = ("HARNESS2_", "GOOGLE_CLOUD_", "GOOGLE_VERTEX_", "ANTHROPIC_VERTEX_", "VERTEXAI_")
_SCRUB_NAMES = frozenset({"CLOUD_ML_REGION"})


class UpstreamImportError(RuntimeError):
    """The upstream import cannot be trusted. Nothing is left changed in the process environment."""


@dataclass(frozen=True)
class Upstream:
    package: str
    commit: str
    install: str                    # "git site-packages" | "editable git checkout"
    root: Path                      # the package directory that was imported
    config: ModuleType
    spec: ModuleType
    proposer: ModuleType
    recursion: ModuleType
    schedule: ModuleType
    tree: ModuleType


_CACHE: dict[str, Upstream] = {}


# ── the installed distribution ────────────────────────────────────────────────────

def _git(directory: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        p = subprocess.run(["git", "-C", str(directory), *args], capture_output=True, text=True, timeout=30,
                           env=env, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise UpstreamImportError(f"cannot run git in the editable upstream checkout {directory} ({type(e).__name__})") from None
    if p.returncode != 0:
        raise UpstreamImportError(f"git {args[0]} failed in the editable upstream checkout {directory}: "
                                  f"{p.stderr.strip()[:200]}")
    return p.stdout.strip()


def _editable_dir(url: str) -> Path:
    from urllib.parse import unquote, urlparse
    u = urlparse(url)
    if u.scheme != "file":
        raise UpstreamImportError("the editable upstream install does not point at a local directory")
    return Path(unquote(u.path))


def _installed(dist_name: str, package: str, expected_commit: str) -> tuple[str, str, Path]:
    """(commit, how it is installed, the package directory it installed). Raises unless it is `expected_commit`."""
    try:
        dist = metadata.distribution(dist_name)
    except metadata.PackageNotFoundError:
        raise UpstreamImportError(
            f"upstream distribution {dist_name!r} is not installed; install the pinned build with "
            f"`uv sync --extra upstream` (commit {expected_commit[:12]})") from None
    raw = dist.read_text("direct_url.json")
    if not raw:
        raise UpstreamImportError(f"distribution {dist_name!r} has no direct_url.json, so its commit cannot be "
                                  f"verified (expected the pinned git commit {expected_commit[:12]})")
    try:
        info = json.loads(raw)
    except json.JSONDecodeError:
        raise UpstreamImportError(f"distribution {dist_name!r} has an unreadable direct_url.json") from None
    vcs = info.get("vcs_info") or {}
    if vcs.get("vcs") == "git" and vcs.get("commit_id"):
        commit, how = str(vcs["commit_id"]), "git site-packages"
        pkg_dir = Path(str(dist.locate_file(package)))
    elif (info.get("dir_info") or {}).get("editable"):
        clone = _editable_dir(str(info.get("url", "")))
        commit, how = _git(clone, "rev-parse", "HEAD"), "editable git checkout"
        pkg_dir = clone / package
        if _git(clone, "status", "--porcelain", "--untracked-files=no"):
            raise UpstreamImportError(f"the editable upstream checkout {clone} has modified tracked files; it is not "
                                      f"the pinned commit {expected_commit[:12]}")
        if (clone / "pyproject.toml").is_file() and (clone / ".env").exists():
            raise UpstreamImportError(f"{clone / '.env'} exists and upstream's config.py would load it into the "
                                      "environment; move it away or install the pinned build instead")
    else:
        raise UpstreamImportError(f"distribution {dist_name!r} is neither a git install nor an editable git checkout, "
                                  "so its commit cannot be verified")
    if commit.lower() != expected_commit.lower():
        raise UpstreamImportError(f"upstream {dist_name!r} is at commit {commit[:12]}, but this package is pinned to "
                                  f"{expected_commit[:12]} ({how}); reinstall with `uv sync --extra upstream`")
    return commit, how, pkg_dir


def _found_directory(package: str) -> Path:
    """Where Python would import `package` from, found WITHOUT importing it."""
    spec = importlib.machinery.PathFinder.find_spec(package)
    if spec is None or not spec.submodule_search_locations:
        raise UpstreamImportError(f"package {package!r} cannot be found on sys.path")
    return Path(next(iter(spec.submodule_search_locations)))


def _same(a: Path, b: Path) -> bool:
    return os.path.realpath(a) == os.path.realpath(b)


def _within(path: str | Path, directory: Path) -> bool:
    real, base = os.path.realpath(path), os.path.realpath(directory)
    return real == base or real.startswith(base + os.sep)


# ── the import ────────────────────────────────────────────────────────────────────

def _scrubbed(env: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in env.items() if not (k.startswith(_SCRUB_PREFIXES) or k in _SCRUB_NAMES)}


def _names(keys) -> str:
    return ", ".join(sorted(ascii(k) for k in keys)[:12])


def import_upstream(*, runs_dir: str | Path, package: str = PACKAGE, dist_name: str | None = None,
                    expected_commit: str = PINNED_COMMIT) -> Upstream:
    """Import the pinned upstream modules safely and verify it (see the module docstring). Raises `UpstreamImportError`."""
    dist_name = dist_name or package
    if package in _CACHE:
        cached = _CACHE[package]
        if sys.modules.get(f"{package}.config") is cached.config:
            return cached
        del _CACHE[package]
    if f"{package}.config" in sys.modules or package in sys.modules:
        raise UpstreamImportError(
            f"{package} was already imported by something else, so it cannot be shown to have read no .env or "
            "changed no environment; call import_upstream before anything imports it")

    commit, how, expected_dir = _installed(dist_name, package, expected_commit)
    found = _found_directory(package)
    if not _same(found, expected_dir):
        raise UpstreamImportError(f"{package} would be imported from {found}, but the installed distribution is at "
                                  f"{expected_dir}: something earlier on sys.path shadows it")

    runs = Path(runs_dir).expanduser()
    if not runs.is_absolute():
        raise UpstreamImportError("runs_dir must be an absolute path")
    controlled = {"HARNESS2_RUNS_DIR": str(runs), "HARNESS2_IMPROVER_MODEL": PLACEHOLDER_MODEL}

    snapshot = dict(os.environ)
    cwd0 = os.getcwd()
    before_modules = set(sys.modules)
    private = tempfile.mkdtemp(prefix="h2-import-")
    mods: dict[str, ModuleType] = {}
    failure: BaseException | None = None
    try:
        os.environ.clear()
        os.environ.update(_scrubbed(snapshot))
        os.environ.update(controlled)
        baseline = dict(os.environ)
        os.chdir(private)
        for name in MODULES:
            mods[name] = importlib.import_module(f"{package}.{name}")

        # 3. nothing but the controlled variables and the known setdefault may differ
        after = dict(os.environ)
        added = set(after) - set(baseline) - KNOWN_SETDEFAULTS
        removed = set(baseline) - set(after)
        changed = {k for k in baseline if k in after and after[k] != baseline[k]}
        if added or removed or changed:
            parts = []
            if added:
                parts.append(f"added {_names(added)}")
            if removed:
                parts.append(f"removed {_names(removed)}")
            if changed:
                parts.append(f"changed {_names(changed)}")
            raise UpstreamImportError("importing upstream changed os.environ beyond the controlled variables ("
                                      + "; ".join(parts) + "): a .env was read or import code mutated the environment")
        # 2. config.__file__
        cfg = mods["config"]
        if not _within(getattr(cfg, "__file__", "") or "", expected_dir):
            raise UpstreamImportError(f"{package}.config was loaded from {getattr(cfg, '__file__', None)}, "
                                      f"not from {expected_dir}")
        root = getattr(cfg, "ROOT", None)
        if root is not None and not (_same(Path(root), private) or _same(Path(root), expected_dir.parent)):
            raise UpstreamImportError(f"{package}.config.ROOT is {root}: neither the empty import directory nor the "
                                      "checkout that holds the package, so a .env there may have been read")
        # 4. heavy modules
        heavy = sorted(m for m in sys.modules if any(m == f"{package}.{h}" or m.startswith(f"{package}.{h}.")
                                                     for h in FORBIDDEN_MODULES))
        if heavy:
            raise UpstreamImportError(f"importing upstream pulled in {', '.join(heavy)}, which this package never loads")
    except BaseException as e:   # noqa: BLE001 - recorded, state is restored below, then re-raised
        failure = e
    finally:
        with contextlib.suppress(OSError):
            os.chdir(cwd0)
        os.environ.clear()
        os.environ.update(snapshot)
        shutil.rmtree(private, ignore_errors=True)
        if failure is not None:
            for m in set(sys.modules) - before_modules:       # never leave a half-trusted upstream importable
                if m == package or m.startswith(package + "."):
                    del sys.modules[m]
    if failure is not None:
        if isinstance(failure, UpstreamImportError):
            raise failure
        raise UpstreamImportError(f"importing upstream failed ({type(failure).__name__}: {str(failure)[:200]})") from failure
    # 5. restored
    if dict(os.environ) != snapshot or os.getcwd() != cwd0:
        raise UpstreamImportError("the process environment or working directory could not be restored")
    up = Upstream(package=package, commit=commit, install=how, root=expected_dir, config=mods["config"],
                  spec=mods["core.spec"], proposer=mods["core.proposer"], recursion=mods["core.recursion"],
                  schedule=mods["core.schedule"], tree=mods["core.tree"])
    _CACHE[package] = up
    return up
