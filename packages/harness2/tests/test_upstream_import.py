"""`import_upstream`: the deliberate, verified import of the pinned upstream (no real model, no network).

Most tests build a FAKE upstream package on a temporary `sys.path`, with a dist-info that says which commit it is,
so every refusal can be provoked: a `.env` read, a mutated environment, a wrong commit, a shadowed package, a
heavy module pulled in. One test then runs the same function against the really installed upstream.
"""
from __future__ import annotations

import importlib
import itertools
import json
import os
import subprocess
import sys
import textwrap
import tomllib
from importlib import metadata
from pathlib import Path

import pytest

from cognitive_harness2 import upstream_import as ui
from cognitive_harness2.upstream_import import PINNED_COMMIT, UpstreamImportError, import_upstream

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"
_counter = itertools.count()

CONFIG = textwrap.dedent('''
    import os
    from pathlib import Path

    PKG = Path(__file__).resolve().parent
    ROOT = PKG.parent if (PKG.parent / "pyproject.toml").is_file() else Path.cwd()
    LOADED = {}

    def load_dotenv(path):
        try:
            text = Path(path).read_text()
        except OSError:
            return
        for line in text.splitlines():
            k, _, v = line.partition("=")
            if k.strip():
                LOADED[k.strip()] = v
                os.environ.setdefault(k.strip(), v)

    load_dotenv(ROOT / ".env")
    os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "global")
    RUNS = os.environ["HARNESS2_RUNS_DIR"]
    IMPROVER_MODEL = os.environ["HARNESS2_IMPROVER_MODEL"]
    VERTEX_PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
''')


def _modules_of(name: str) -> list[str]:
    return [m for m in sys.modules if m == name or m.startswith(name + ".")]


@pytest.fixture
def fresh():
    """Stash every real `harness2` module (other tests import it) and the cache; restore them afterwards."""
    stashed = {m: sys.modules.pop(m) for m in _modules_of("harness2")}
    saved_cache = dict(ui._CACHE)
    ui._CACHE.clear()
    added: list[str] = []
    try:
        yield added
    finally:
        for name in added:
            for m in _modules_of(name):
                del sys.modules[m]
        for m in _modules_of("harness2"):
            del sys.modules[m]
        sys.modules.update(stashed)
        ui._CACHE.clear()
        ui._CACHE.update(saved_cache)


class Fake:
    def __init__(self, tmp_path, monkeypatch, fresh, *, commit="c" * 40, config_extra="", recursion_extra="",
                 editable=False, with_dist=True, name=None):
        self.name = name or f"fakeh2{next(_counter)}"
        self.site = tmp_path / f"site-{self.name}"
        self.site.mkdir()
        self.pkg = self.site / self.name
        (self.pkg / "core").mkdir(parents=True)
        (self.pkg / "__init__.py").write_text("")
        (self.pkg / "core" / "__init__.py").write_text("")
        (self.pkg / "config.py").write_text(CONFIG + config_extra)
        for mod in ("spec", "tree", "proposer", "schedule"):
            (self.pkg / "core" / f"{mod}.py").write_text("from .. import config\n")
        (self.pkg / "core" / "recursion.py").write_text("from .. import config\n" + recursion_extra)
        if with_dist:
            info = self.site / f"{self.name}-0.0.dist-info"
            info.mkdir()
            (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {self.name}\nVersion: 0.0\n")
            if editable:
                du = {"url": self.site.as_uri(), "dir_info": {"editable": True}}
            else:
                du = {"url": "https://example.invalid/x.git", "vcs_info": {"vcs": "git", "commit_id": commit}}
            (info / "direct_url.json").write_text(json.dumps(du))
        monkeypatch.syspath_prepend(str(self.site))
        importlib.invalidate_caches()
        fresh.append(self.name)
        self.commit = commit

    def run(self, tmp_path, **kw):
        return checked(runs_dir=tmp_path / "runs", package=self.name, expected_commit=self.commit, **kw)


def checked(**kw):
    """`import_upstream`, then (even when it raised) prove the environment and working directory are as before."""
    snap, cwd = dict(os.environ), os.getcwd()
    try:
        return import_upstream(**kw)
    finally:
        assert dict(os.environ) == snap and os.getcwd() == cwd


# ── the happy path ────────────────────────────────────────────────────────────────

def test_the_modules_come_back_and_the_process_is_untouched(tmp_path, monkeypatch, fresh):
    f = Fake(tmp_path, monkeypatch, fresh)
    up = f.run(tmp_path)
    assert up.package == f.name and up.commit == f.commit and up.install == "git site-packages"
    for attr in ("config", "spec", "proposer", "recursion", "schedule", "tree"):
        assert getattr(up, attr).__name__.startswith(f.name + ".")
    assert up.config.RUNS == str(tmp_path / "runs")           # the controlled variables were what config saw
    assert up.config.IMPROVER_MODEL == ui.PLACEHOLDER_MODEL
    # (`checked` already proved os.environ is exactly as before: upstream's setdefault and the controlled variables did not leak)


def test_the_environment_the_import_sees_has_no_vertex_or_harness2_variables(tmp_path, monkeypatch, fresh):
    f = Fake(tmp_path, monkeypatch, fresh)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-vertex-project")
    monkeypatch.setenv("HARNESS2_IMPROVER_MODEL", "vertex/gemini")
    up = f.run(tmp_path)
    assert up.config.VERTEX_PROJECT == ""                     # the real project never froze into a constant
    assert up.config.IMPROVER_MODEL == ui.PLACEHOLDER_MODEL
    assert os.environ["GOOGLE_CLOUD_PROJECT"] == "my-vertex-project"     # ... and the real one is back afterwards


def test_a_second_call_returns_the_first_result(tmp_path, monkeypatch, fresh):
    f = Fake(tmp_path, monkeypatch, fresh)
    assert f.run(tmp_path) is f.run(tmp_path)


def test_a_module_imported_by_someone_else_first_is_refused(tmp_path, monkeypatch, fresh):
    f = Fake(tmp_path, monkeypatch, fresh)
    importlib.import_module(f.name)
    with pytest.raises(UpstreamImportError, match="already imported"):
        f.run(tmp_path)


# ── no stray .env ─────────────────────────────────────────────────────────────────

def test_a_dotenv_in_the_current_directory_is_not_read(tmp_path, monkeypatch, fresh):
    stray = tmp_path / "elsewhere"
    stray.mkdir()
    (stray / ".env").write_text("STRAY_FROM_CWD=1\nGOOGLE_CLOUD_PROJECT=leaked\n")
    monkeypatch.chdir(stray)
    up = Fake(tmp_path, monkeypatch, fresh).run(tmp_path)
    assert up.config.LOADED == {} and up.config.VERTEX_PROJECT == ""
    assert "STRAY_FROM_CWD" not in os.environ


def test_a_dotenv_read_from_an_absolute_path_is_caught_and_undone(tmp_path, monkeypatch, fresh):
    dotenv = tmp_path / "abs.env"
    dotenv.write_text("STRAY_ABSOLUTE=1\n")
    f = Fake(tmp_path, monkeypatch, fresh, config_extra=f"\nload_dotenv({str(dotenv)!r})\n")
    with pytest.raises(UpstreamImportError, match="STRAY_ABSOLUTE"):
        f.run(tmp_path)
    assert "STRAY_ABSOLUTE" not in os.environ
    assert not _modules_of(f.name)                            # nothing half-trusted stays importable


def test_import_code_that_mutates_the_environment_is_caught_and_undone(tmp_path, monkeypatch, fresh):
    monkeypatch.setenv("KEEP_ME", "original")
    f = Fake(tmp_path, monkeypatch, fresh,
             config_extra="\nos.environ['SNEAKY_NEW'] = '1'\nos.environ['KEEP_ME'] = 'changed'\n")
    with pytest.raises(UpstreamImportError) as e:
        f.run(tmp_path)
    assert "SNEAKY_NEW" in str(e.value) and "KEEP_ME" in str(e.value) and "changed" not in str(e.value).replace("changed ", "")
    assert os.environ["KEEP_ME"] == "original" and "SNEAKY_NEW" not in os.environ


def test_import_code_that_deletes_a_variable_is_caught(tmp_path, monkeypatch, fresh):
    monkeypatch.setenv("KEEP_ME", "original")
    f = Fake(tmp_path, monkeypatch, fresh, config_extra="\nos.environ.pop('PATH', None)\n")
    with pytest.raises(UpstreamImportError, match="removed"):
        f.run(tmp_path)


def test_a_failing_import_restores_everything(tmp_path, monkeypatch, fresh):
    f = Fake(tmp_path, monkeypatch, fresh, config_extra="\nraise RuntimeError('boom')\n")
    with pytest.raises(UpstreamImportError, match="boom"):
        f.run(tmp_path)
    assert not _modules_of(f.name)


def test_heavy_modules_are_refused(tmp_path, monkeypatch, fresh):
    f = Fake(tmp_path, monkeypatch, fresh, recursion_extra="from .. import benchmarks\n")
    (f.pkg / "benchmarks.py").write_text("X = 1\n")
    with pytest.raises(UpstreamImportError, match="benchmarks"):
        f.run(tmp_path)
    assert not _modules_of(f.name)


# ── which upstream is it ──────────────────────────────────────────────────────────

def test_the_wrong_commit_is_refused_naming_both(tmp_path, monkeypatch, fresh):
    f = Fake(tmp_path, monkeypatch, fresh, commit="a" * 40)
    with pytest.raises(UpstreamImportError) as e:
        checked(runs_dir=tmp_path / "r", package=f.name, expected_commit="b" * 40)
    assert "a" * 12 in str(e.value) and "b" * 12 in str(e.value)


def test_a_missing_distribution_says_how_to_install_it(tmp_path, monkeypatch, fresh):
    f = Fake(tmp_path, monkeypatch, fresh, with_dist=False)
    with pytest.raises(UpstreamImportError, match="uv sync --extra upstream"):
        f.run(tmp_path)


def test_a_distribution_with_no_direct_url_is_refused(tmp_path, monkeypatch, fresh):
    f = Fake(tmp_path, monkeypatch, fresh)
    (f.site / f"{f.name}-0.0.dist-info" / "direct_url.json").unlink()
    with pytest.raises(UpstreamImportError, match="direct_url"):
        f.run(tmp_path)


def test_a_package_shadowed_earlier_on_sys_path_is_refused(tmp_path, monkeypatch, fresh):
    f = Fake(tmp_path, monkeypatch, fresh)
    shadow = tmp_path / "shadow"
    (shadow / f.name).mkdir(parents=True)
    (shadow / f.name / "__init__.py").write_text("")
    monkeypatch.syspath_prepend(str(shadow))
    importlib.invalidate_caches()
    with pytest.raises(UpstreamImportError, match="shadows"):
        f.run(tmp_path)


def test_config_loaded_from_elsewhere_is_refused(tmp_path, monkeypatch, fresh):
    f = Fake(tmp_path, monkeypatch, fresh)
    other = tmp_path / "other-config.py"
    other.write_text(CONFIG)
    (f.pkg / "config.py").unlink()
    (f.pkg / "config.py").symlink_to(other)               # config.py resolves outside the installed directory
    with pytest.raises(UpstreamImportError, match="not from"):
        f.run(tmp_path)


def test_a_relative_runs_dir_is_refused(tmp_path, monkeypatch, fresh):
    f = Fake(tmp_path, monkeypatch, fresh)
    with pytest.raises(UpstreamImportError, match="absolute"):
        checked(runs_dir="runs", package=f.name, expected_commit=f.commit)


# ── an editable install is a git checkout ─────────────────────────────────────────

def _git(d: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", "-C", str(d), *args], capture_output=True, text=True, check=True, env=env).stdout.strip()


def _editable(tmp_path, monkeypatch, fresh, **kw) -> Fake:
    f = Fake(tmp_path, monkeypatch, fresh, editable=True, **kw)
    (f.site / "pyproject.toml").write_text("[project]\nname='x'\n")
    _git(f.site, "init", "-q")
    _git(f.site, "add", "-A")
    _git(f.site, "commit", "-q", "-m", "x")
    f.commit = _git(f.site, "rev-parse", "HEAD")
    return f


def test_an_editable_checkout_at_the_pinned_head_is_accepted(tmp_path, monkeypatch, fresh):
    f = _editable(tmp_path, monkeypatch, fresh)
    up = f.run(tmp_path)
    assert up.install == "editable git checkout" and up.commit == f.commit
    assert up.config.LOADED == {}


def test_an_editable_checkout_at_another_head_is_refused(tmp_path, monkeypatch, fresh):
    f = _editable(tmp_path, monkeypatch, fresh)
    with pytest.raises(UpstreamImportError, match="pinned"):
        checked(runs_dir=tmp_path / "r", package=f.name, expected_commit="d" * 40)


def test_an_editable_checkout_with_modified_tracked_files_is_refused(tmp_path, monkeypatch, fresh):
    f = _editable(tmp_path, monkeypatch, fresh)
    (f.pkg / "config.py").write_text(CONFIG + "\n# edited\n")
    with pytest.raises(UpstreamImportError, match="modified tracked files"):
        f.run(tmp_path)


def test_an_editable_checkout_with_a_dotenv_is_refused_before_it_is_read(tmp_path, monkeypatch, fresh):
    f = _editable(tmp_path, monkeypatch, fresh)
    (f.site / ".env").write_text("STRAY_IN_CHECKOUT=1\n")
    with pytest.raises(UpstreamImportError, match=r"\.env"):
        f.run(tmp_path)
    assert "STRAY_IN_CHECKOUT" not in os.environ


# ── the real, installed upstream ──────────────────────────────────────────────────

def test_the_pin_matches_pyproject():
    rev = tomllib.loads(PYPROJECT.read_text())["tool"]["uv"]["sources"]["harness2"]["rev"]
    assert PINNED_COMMIT == rev, "upstream_import.PINNED_COMMIT and pyproject.toml's [tool.uv.sources].harness2.rev differ"


def test_the_real_installed_upstream_imports_cleanly(tmp_path, fresh, monkeypatch):
    try:
        metadata.distribution("harness2")
    except metadata.PackageNotFoundError:
        pytest.skip("OPTIONAL EXTRA ABSENT: the `upstream` extra is not installed, so the real-upstream import was NOT tested")
    stray = tmp_path / "cwd"
    stray.mkdir()
    (stray / ".env").write_text("STRAY_REAL=1\nGOOGLE_CLOUD_PROJECT=leaked\n")
    monkeypatch.chdir(stray)
    up = checked(runs_dir=tmp_path / "runs")
    assert up.commit == PINNED_COMMIT
    assert up.config.RUNS == (tmp_path / "runs").resolve()
    assert up.config.VERTEX_PROJECT == "" and "STRAY_REAL" not in os.environ
    assert hasattr(up.proposer, "Proposer") and hasattr(up.schedule, "run_task") and hasattr(up.spec, "ChannelSpec")
    assert not _modules_of("harness2.benchmarks") and not _modules_of("harness2.cli")
