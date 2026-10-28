"""The filesystem seam: a non-disk backend must behave exactly like the local one.

`MemFS` serves the same tree from memory and records every read. The parity test builds a
real tree, ingests it through `LocalFS`, DELETES the tree, and ingests the same logical
paths through `MemFS`: identical documents, identities, digests and ledger rows mean the
engine reaches its bytes only through the seam (a stray `open()` would fail on the deleted
disk). This is the backend-agnostic half of the SMB work; `SmbFS` only has to satisfy the
same contract.
"""
from __future__ import annotations

import dataclasses
import io
import os
import posixpath
import shutil
import sqlite3
from pathlib import Path, PurePosixPath

import pytest

from lookup import config, db, manifest, pipeline
from lookup.vfs import LOCAL, FileSystem, LocalFS, Stat, VPath


class MemFS:
    """An in-memory FileSystem over {logical path: (bytes, mtime)}, counting reads."""

    def __init__(self, files: dict[str, tuple[bytes, float]]):
        self.files = {posixpath.normpath(k): v for k, v in files.items()}
        self.reads: list[str] = []
        self.visited: list[str] = []

    def _dirs(self) -> set[str]:
        out: set[str] = set()
        for f in self.files:
            d = posixpath.dirname(f)
            while d and d != "/" and d not in out:
                out.add(d)
                d = posixpath.dirname(d)
        out.add("/")
        return out

    def path(self, s: str) -> VPath:
        return VPath(self, s)

    def is_dir(self, p) -> bool:
        return posixpath.normpath(str(p)) in self._dirs()

    def is_file(self, p) -> bool:
        return posixpath.normpath(str(p)) in self.files

    def walk(self, root):
        dirs = self._dirs()

        def children(d: str):
            prefix = d.rstrip("/") + "/"
            subdirs = sorted({x[len(prefix):].split("/")[0] for x in dirs if x.startswith(prefix) and x != d})
            names = sorted(posixpath.basename(f) for f in self.files if posixpath.dirname(f) == d)
            return subdirs, names

        def rec(d: str):
            self.visited.append(d)
            subdirs, names = children(d)
            yield VPath(self, d), subdirs, names
            for s in subdirs:          # `subdirs` may have been pruned by the caller
                yield from rec(posixpath.join(d, s))

        yield from rec(posixpath.normpath(str(root)))

    def stat(self, p) -> Stat:
        data, mtime = self.files[posixpath.normpath(str(p))]
        return Stat(mtime, len(data))

    def read_text(self, p, encoding: str = "utf-8", errors: str = "strict") -> str:
        self.reads.append(str(p))
        return self.files[posixpath.normpath(str(p))][0].decode(encoding, errors)

    def open_text(self, p, mode="r", encoding=None, errors=None):
        self.reads.append(str(p))
        return io.StringIO(self.files[posixpath.normpath(str(p))][0].decode(encoding or "utf-8", errors or "strict"))

    def open_bytes(self, p):
        self.reads.append(str(p))
        return io.BytesIO(self.files[posixpath.normpath(str(p))][0])

    def contains(self, root, p) -> bool:
        r, x = posixpath.normpath(str(root)), posixpath.normpath(str(p))
        return x == r or x.startswith(r.rstrip("/") + "/")


TREE = {
    "a.md": "# Alpha\n\nfirst zebrafish note\n",
    "sub/b.md": "---\nvisibility: gated\n---\n\n# Beta\n\nsecond note\n",
    "sub/deep/c.md": "# Gamma\n\nthird note\n",
    "skip/d.md": "# Delta\n\nexcluded by exclude_names\n",
    "keep/e.txt": "not matched by the include glob\n",
    "Work/Private/f.md": "# Phi\n\nexcluded by exclude_paths\n",
    "Work/Public/g.md": "# Gamma two\n\nkept\n",
}

MANIFEST = """
[[source]]
name = "notes"
description = "parity fixture"
root = "@ROOT@"
include = ["*.md", "**/*.md"]
exclude_names = ["skip"]
exclude_paths = ["Work/Private"]
extractor = "docs.markdown"
visibility = "open"
"""


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOKUP_NO_DOTENV", "1")
    root = tmp_path / "tree"
    for rel, text in TREE.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    m = tmp_path / "m.toml"
    m.write_text(MANIFEST.replace("@ROOT@", str(root)), encoding="utf-8")
    src = manifest.load(m)["notes"]
    saved = (config.DB, config.MANIFEST)
    yield {"tmp": tmp_path, "root": root, "src": src}
    config.DB, config.MANIFEST = saved


def ingest(src, db_path: Path):
    config.DB = db_path
    con = db.connect()
    res = pipeline.ingest_source(con, src, progress=False)
    con.commit()
    con.close()
    return res


def snapshot(db_path: Path):
    con = sqlite3.connect(db_path)
    docs = con.execute("SELECT doc_key, path, title, date, body, src_sha256, visibility FROM docs ORDER BY doc_key").fetchall()
    ledger = con.execute("SELECT path, mtime, size, keys FROM src_files ORDER BY path").fetchall()
    con.close()
    return docs, ledger


def memfs_from(root: Path) -> MemFS:
    files = {}
    for p in root.rglob("*"):
        if p.is_file():
            files[str(p)] = (p.read_bytes(), p.stat().st_mtime)
    return MemFS(files)


# ── the seam and the local backend ───────────────────────────────────────────────

def test_the_default_filesystem_is_local_and_satisfies_the_protocol(world):
    assert world["src"].fs is LOCAL and isinstance(LOCAL, LocalFS) and isinstance(LOCAL, FileSystem)
    assert isinstance(MemFS({}), FileSystem)


def test_local_paths_are_plain_pathlib_paths(world):
    files = list(world["src"].files())
    assert files and all(type(p) is type(Path()) for p in files)


# ── parity with a non-disk backend ───────────────────────────────────────────────

def test_a_memory_backend_ingests_identically_and_never_touches_the_disk(world):
    local_db, mem_db = world["tmp"] / "local.db", world["tmp"] / "mem.db"
    local = ingest(world["src"], local_db)
    mem = memfs_from(world["root"])
    shutil.rmtree(world["root"])                       # the disk no longer has the tree
    assert not world["root"].exists()
    via_mem = ingest(dataclasses.replace(world["src"], filesystem=mem), mem_db)

    assert (local.added, local.updated, local.errors, local.refused) == (via_mem.added, via_mem.updated, 0, 0)
    assert local.added == 4                            # a, sub/b, sub/deep/c, Work/Public/g
    assert snapshot(local_db) == snapshot(mem_db)      # same keys, paths, bodies, digests, ledger rows
    docs, _ = snapshot(mem_db)
    assert all(d[5] for d in docs), "every document carries a content digest"
    assert {d[6] for d in docs} == {None, "gated"}      # a document may narrow its source


def test_exclusions_prune_the_walk_instead_of_filtering_afterwards(world):
    mem = memfs_from(world["root"])
    list(dataclasses.replace(world["src"], filesystem=mem).files())
    visited = " ".join(mem.visited)
    assert "/skip" not in visited and "Work/Private" not in visited
    assert "Work/Public" in visited and "sub/deep" in visited


def test_an_unchanged_tree_is_skipped_without_a_single_read(world):
    mem = memfs_from(world["root"])
    src = dataclasses.replace(world["src"], filesystem=mem)
    first = ingest(src, world["tmp"] / "x.db")
    assert first.added == 4 and mem.reads, "the first pass must read the files"
    mem.reads.clear()
    second = ingest(src, world["tmp"] / "x.db")
    assert second.skipped_files == 4 and second.added == 0 and second.unchanged == 4
    assert mem.reads == [], f"the second pass read {mem.reads}"


def test_an_edited_file_is_reread_and_only_that_file(world):
    mem = memfs_from(world["root"])
    src = dataclasses.replace(world["src"], filesystem=mem)
    ingest(src, world["tmp"] / "x.db")
    target = next(k for k in mem.files if k.endswith("a.md"))
    mem.files[target] = (b"# Alpha\n\nchanged body\n", mem.files[target][1] + 5)
    mem.reads.clear()
    res = ingest(src, world["tmp"] / "x.db")
    assert res.updated == 1 and res.skipped_files == 3
    assert {posixpath.basename(r) for r in mem.reads} == {"a.md"}


def test_containment_is_lexical_on_a_non_disk_backend(world):
    src = dataclasses.replace(world["src"], filesystem=MemFS({}))
    root = str(world["src"].root)
    assert src.contains(src.path(f"{root}/sub/x.md"))
    assert not src.contains(src.path(f"{root}/../etc/passwd"))
    assert not src.contains(src.path("/elsewhere/x.md"))
    assert not src.contains(src.path(root + "-sibling/x.md"))


# ── VPath ────────────────────────────────────────────────────────────────────────

def test_vpath_implements_the_pathlib_surface_the_extractors_use():
    fs = MemFS({"/r/sub/Note.v2.md": (b"x", 1.0)})
    p = fs.path("/r/sub/Note.v2.md")
    assert (p.name, p.stem, p.suffix, p.parent.name) == ("Note.v2.md", "Note.v2", ".md", "sub")
    assert str(p.parent / "other.md") == "/r/sub/other.md"
    assert p.relative_to(Path("/r")) == PurePosixPath("sub/Note.v2.md")
    assert p.relative_to("/r/sub").as_posix() == "Note.v2.md"
    assert p.stat().st_size == 1 and p.read_text() == "x"
    with p.open("r", encoding="utf-8") as fh:
        assert fh.read() == "x"
    with p.open("rb") as fh:
        assert fh.read() == b"x"


def test_vpath_identity_hashing_and_ordering():
    fs, other = MemFS({}), MemFS({})
    a, b = fs.path("/r/a"), fs.path("/r/b")
    assert a == fs.path("/r/a") and a != b and a != other.path("/r/a")
    assert len({a, fs.path("/r/a"), b}) == 2
    assert sorted([b, a]) == [a, b]


def test_a_vpath_cannot_be_opened_as_a_local_file():
    p = MemFS({}).path("/etc/passwd")
    with pytest.raises(TypeError, match="not on the local disk"):
        open(p)
    with pytest.raises(TypeError):
        os.fspath(p)
    with pytest.raises(TypeError):
        Path(p)
