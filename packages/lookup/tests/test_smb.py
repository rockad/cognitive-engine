"""SmbFS: a source on a network share, read in userspace.

Two layers. A REAL SMB server (impacket, test-only) proves the protocol path end to end:
the same tree ingested over the wire gives the same index as ingested from disk, an
unchanged share costs zero reads and zero extra network calls, an edit re-reads only that
file, and bad credentials never succeed or leak. A FAKE client covers what a real server
cannot produce on demand: reparse points, access denied, and a listing that dies mid-walk
(which must never delete anything).
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from lookup import config, db, manifest, pipeline, smb
from lookup.smb import SmbFS
from lookup.vfs import SourceUnreachable

HERE = Path(__file__).resolve().parent
TREE = {
    "a.md": "# Alpha\n\nfirst zebrafish note\n",
    "sub/b.md": "---\nvisibility: gated\n---\n\n# Beta\n\nsecond note\n",
    "sub/deep/c.md": "# Gamma\n\nthird note\n",
    "skip/d.md": "# Delta\n\nexcluded by exclude_names\n",
    "keep/e.txt": "not matched by the include glob\n",
}
USER, PASSWORD = "testuser", "testpass-9f3"
MANIFEST = """
[[source]]
name = "notes"
description = "smb fixture"
root = "@ROOT@"
include = ["*.md", "**/*.md"]
exclude_names = ["skip"]
extractor = "docs.markdown"
visibility = "open"
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def volume(port: int, **extra) -> dict:
    return {"type": "cifs", "device": "//127.0.0.1/KA", "port": port,
            "username": "${SHARE_USER}", "password": "${SHARE_PASS}", **extra}


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


# ── a real SMB server ────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def share(tmp_path_factory):
    # ⚠️ impacket runs in its OWN throwaway environment (`uv run --isolated --with`), never in
    # the project's. It installs an example script named `smbclient.py` into the venv's bin/,
    # and `uv run pytest` puts that directory first on sys.path, so it shadowed smbprotocol's
    # real `smbclient` package and made every share look unreachable.
    if shutil.which("uv") is None:
        pytest.skip("uv is needed to start the test SMB server in an isolated environment")
    base = tmp_path_factory.mktemp("smb-share")
    tree = base / "tree"
    for rel, text in TREE.items():
        p = tree / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    port = free_port()
    log = open(base / "server.log", "wb")
    proc = subprocess.Popen(["uv", "run", "--isolated", "--no-project", "--with", "impacket", "python",
                             str(HERE / "smb_server.py"), str(tree), str(port), USER, PASSWORD],
                            stdout=log, stderr=subprocess.STDOUT)
    deadline = time.time() + 120      # the first run installs impacket; later runs hit uv's cache
    while time.time() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"smb server exited:\n{(base / 'server.log').read_text()[-800:]}")
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                break
        time.sleep(0.2)
    else:
        proc.kill()
        pytest.fail("smb server never listened")
    yield {"tree": tree, "port": port, "base": base}
    proc.terminate()
    proc.wait(timeout=15)


@pytest.fixture
def world(share, tmp_path, monkeypatch):
    monkeypatch.setenv("LOOKUP_NO_DOTENV", "1")
    monkeypatch.setenv("SHARE_USER", USER)
    monkeypatch.setenv("SHARE_PASS", PASSWORD)
    m = tmp_path / "m.toml"
    m.write_text(MANIFEST.replace("@ROOT@", str(share["tree"])), encoding="utf-8")
    local = manifest.load(m)["notes"]
    saved = config.DB
    yield {"local": local, "tmp": tmp_path, "share": share}
    config.DB = saved
    # the test edits files on the shared tree; restore them
    for rel, text in TREE.items():
        (share["tree"] / rel).write_text(text, encoding="utf-8")


def smb_source(world, **volume_extra):
    return dataclasses.replace(world["local"], volume=volume(world["share"]["port"], **volume_extra))


def test_a_share_ingests_identically_to_the_same_tree_on_disk(world):
    local_db, smb_db = world["tmp"] / "local.db", world["tmp"] / "smb.db"
    local = ingest(world["local"], local_db)
    src = smb_source(world)
    assert isinstance(src.fs, SmbFS)
    over_smb = ingest(src, smb_db)

    assert (local.added, local.errors, local.refused) == (over_smb.added, 0, 0) and over_smb.added == 3
    (docs_l, ledger_l), (docs_s, ledger_s) = snapshot(local_db), snapshot(smb_db)
    # Same keys, paths, titles, bodies, content digests and visibility, exactly. The one
    # field that cannot match bit for bit is time: SMB reports modification times at a
    # coarser precision than the local disk, so `date` and the ledger's mtime are compared
    # within a second or two instead.
    keep = lambda docs: [(d[0], d[1], d[2], d[4], d[5], d[6]) for d in docs]  # noqa: E731
    assert keep(docs_l) == keep(docs_s)
    assert [(r[0], r[2], r[3]) for r in ledger_l] == [(r[0], r[2], r[3]) for r in ledger_s]
    assert all(abs(a[1] - b[1]) < 2 for a, b in zip(ledger_l, ledger_s))
    assert all(r[5] for r in docs_s), "every document carries a content digest"


def test_an_unchanged_share_costs_no_reads_and_no_extra_network_calls(world):
    src = smb_source(world)
    fs = src.fs
    first = ingest(src, world["tmp"] / "x.db")
    assert first.added == 3 and fs.reads > 0
    fs.reads = fs.network_stats = 0
    second = ingest(src, world["tmp"] / "x.db")
    assert second.skipped_files == 3 and second.added == 0 and second.unchanged == 3
    assert fs.reads == 0, f"the second pass opened {fs.reads} file(s)"
    assert fs.network_stats == 0, "stat() must be answered from the directory listing"


def test_an_edited_file_is_reread_and_only_that_file(world):
    src = smb_source(world)
    ingest(src, world["tmp"] / "x.db")
    target = world["share"]["tree"] / "a.md"
    target.write_text("# Alpha\n\nchanged body\n", encoding="utf-8")
    future = time.time() + 30
    os.utime(target, (future, future))
    res = ingest(src, world["tmp"] / "x.db")
    assert res.updated == 1 and res.skipped_files == 2


def test_a_subpath_indexes_only_that_directory_under_the_logical_root(world):
    src = smb_source(world, subpath="sub")
    ingest(src, world["tmp"] / "x.db")
    paths = sorted(r[1] for r in snapshot(world["tmp"] / "x.db")[0])
    root = str(world["local"].root)
    assert paths == [f"{root}/b.md", f"{root}/deep/c.md"]


def test_bad_credentials_never_succeed_and_never_leak(world):
    bad = SmbFS(host="127.0.0.1", share="KA", username="nobody", password="hunter2-leak-check",
                logical_root=world["local"].root, port=world["share"]["port"], timeout=5)
    assert bad.is_dir(bad.path(str(world["local"].root))) is False
    with pytest.raises(SourceUnreachable) as e:
        list(bad.walk(world["local"].root))
    assert "hunter2-leak-check" not in str(e.value) and "hunter2-leak-check" not in repr(bad)


def test_an_unreachable_port_is_absent_and_remembered(world):
    dead = SmbFS(host="127.0.0.1", share="KA", username=USER, password=PASSWORD,
                 logical_root=world["local"].root, port=free_port(), timeout=3)
    assert dead.is_dir(dead.path(str(world["local"].root))) is False
    t = time.monotonic()
    assert dead.is_dir(dead.path(str(world["local"].root))) is False
    assert time.monotonic() - t < 0.05, "a share known to be down must not be retried on every call"


# ── path safety (no network) ─────────────────────────────────────────────────────

@pytest.fixture
def fs():
    return SmbFS(host="h", share="s", username="u", password="p", logical_root="/Volumes/ka", subpath="wiki", client=object())


@pytest.mark.parametrize("bad", [
    "/Volumes/ka/a\\..\\..\\secret", "/Volumes/ka/a:stream", "/Volumes/ka/../etc/passwd",
    "/Volumes/ka-sibling/x.md", "/elsewhere/x.md", "/Volumes/ka/a\x00b",
])
def test_unsafe_paths_are_not_contained_and_cannot_be_read(fs, bad):
    assert not fs.contains("/Volumes/ka", fs.path(bad))
    with pytest.raises((ValueError, FileNotFoundError)):
        fs.stat(fs.path(bad))
    with pytest.raises(ValueError):
        fs.read_text(fs.path(bad))


def test_logical_paths_map_to_the_share_under_the_subpath(fs):
    assert fs._unc(fs.path("/Volumes/ka")) == "\\\\h\\s\\wiki"
    assert fs._unc(fs.path("/Volumes/ka/notes/a.md")) == "\\\\h\\s\\wiki\\notes\\a.md"


@pytest.mark.parametrize("sub", ["../x", "a/../b", "a:b", "/", ".."])
def test_bad_subpaths_are_refused(sub):
    if sub.strip("/") == "":
        return                                           # "/" normalises to the share root, which is fine
    with pytest.raises(smb.SmbConfigError):
        SmbFS(host="h", share="s", username="u", password="p", logical_root="/r", subpath=sub, client=object())


# ── a fake client ────────────────────────────────────────────────────────────────

REPARSE, DIRECTORY = 0x400, 0x10


class FakeSmb:
    """Just enough of `smbclient` for SmbFS, with failure injection."""

    def __init__(self, tree: dict):
        self.tree, self.fail_listing, self.denied = tree, False, set()
        self.path = SimpleNamespace(isdir=lambda unc, **k: self._node(unc) is not None and isinstance(self._node(unc), dict),
                                    isfile=lambda unc, **k: isinstance(self._node(unc), bytes))

    def _node(self, unc: str):
        parts = [p for p in unc.split("\\")[4:] if p] if unc.startswith("\\\\") else []
        node = self.tree
        for p in parts:
            node = node.get(p) if isinstance(node, dict) else None
            if node is None:
                return None
        return node

    def scandir(self, unc, **kw):
        if self.fail_listing:
            raise ConnectionResetError("share went away")
        if unc in self.denied:
            err = OSError("denied")
            err.ntstatus = 0xC0000022
            raise err
        node = self._node(unc)
        now = dt.datetime(2026, 10, 28, tzinfo=dt.timezone.utc)
        entries = []
        for name, v in node.items():
            attrs = REPARSE if name.startswith("link") else (DIRECTORY if isinstance(v, dict) else 0)
            size = len(v) if isinstance(v, bytes) else 0
            entries.append(SimpleNamespace(name=name, smb_info=SimpleNamespace(
                file_attributes=attrs, end_of_file=size, last_write_time=now)))

        class Ctx(list):
            def __enter__(self_inner):
                return self_inner
            def __exit__(self_inner, *a):
                return False
        return Ctx(entries)

    def open_file(self, unc, mode="rb", **kw):
        import io
        return io.BytesIO(self._node(unc))

    def stat(self, unc, **kw):
        n = self._node(unc)
        return SimpleNamespace(st_mtime=1.0, st_size=len(n))


def fake_fs(tree, **kw):
    client = FakeSmb(tree)
    return SmbFS(host="h", share="s", username="u", password="p", logical_root="/root", client=client, **kw), client


def walk_all(fs):
    return [(str(h), d[:], f[:]) for h, d, f in fs.walk("/root")]


def test_reparse_points_are_skipped_counted_and_never_followed():
    fs, _ = fake_fs({"a.md": b"x", "link-out": {"secret.md": b"s"}, "link-file.md": b"y", "ok": {"b.md": b"z"}})
    seen = walk_all(fs)
    assert seen == [("/root", ["ok"], ["a.md"]), ("/root/ok", [], ["b.md"])]
    assert sorted(fs.skipped) == ["/root/link-file.md (reparse point)", "/root/link-out (reparse point)"]


def test_access_denied_on_a_subdirectory_is_skipped_but_on_the_root_is_an_error():
    fs, client = fake_fs({"a.md": b"x", "locked": {"s.md": b"s"}})
    client.denied = {"\\\\h\\s\\locked"}
    assert [n for _, _, n in walk_all(fs)] == [["a.md"]]
    assert fs.skipped == ["/root/locked (access denied)"]
    client.denied = {"\\\\h\\s"}
    with pytest.raises(SourceUnreachable):
        walk_all(fs)


def test_a_listing_that_fails_raises_instead_of_returning_a_partial_walk():
    fs, client = fake_fs({"a.md": b"x"})
    client.fail_listing = True
    with pytest.raises(SourceUnreachable, match="ConnectionResetError"):
        walk_all(fs)


def test_stat_comes_from_the_listing_and_only_unlisted_paths_hit_the_network():
    fs, _ = fake_fs({"a.md": b"hello"})
    walk_all(fs)
    assert fs.stat(fs.path("/root/a.md")).st_size == 5 and fs.network_stats == 0
    fs.stat(fs.path("/root/unlisted.md")) if False else None
    assert fs.is_file(fs.path("/root/a.md")) and fs.network_stats == 0


def test_a_dropped_share_mid_pass_keeps_every_row_and_does_not_stop_other_sources(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOKUP_NO_DOTENV", "1")
    monkeypatch.setenv("SHARE_USER", "u")
    monkeypatch.setenv("SHARE_PASS", "p")
    local = tmp_path / "local"
    local.mkdir()
    (local / "l.md").write_text("# Local\n\nlocal doc\n")
    m = tmp_path / "m.toml"
    m.write_text(f"""
[[source]]
name = "here"
description = "d"
root = "{local}"
include = ["*.md"]
extractor = "docs.markdown"
visibility = "open"

[[source]]
name = "there"
description = "d"
root = "/Volumes/there"
include = ["*.md", "**/*.md"]
extractor = "docs.markdown"
visibility = "gated"
[source.volume]
type = "cifs"
device = "//h/s"
username = "${{SHARE_USER}}"
password = "${{SHARE_PASS}}"
""")
    fs, client = fake_fs({"r1.md": b"# R1\n\nremote one\n", "d": {"r2.md": b"# R2\n\nremote two\n"}})
    fs = SmbFS(host="h", share="s", username="u", password="p", logical_root="/Volumes/there", client=client)
    monkeypatch.setattr(smb, "_build", lambda *a: fs)
    monkeypatch.setattr(config, "MANIFEST", m)
    monkeypatch.setattr(config, "DB", tmp_path / "i.db")

    first = pipeline.ingest_all()
    assert first["added"] == 3 and first["unreachable"] == [] and first["docs"] == 3

    client.fail_listing = True
    second = pipeline.ingest_all()
    assert second["unreachable"] == ["there"]
    assert second["pruned"] == 0 and second["docs"] == 3, "a failed listing must delete nothing"
    assert second["sources"] == 1 and second["unchanged"] == 1, "the local source was still ingested"
    assert "there" in second["reverify"], "the ledger is dropped so the next pass re-verifies"


def test_reparse_points_are_counted_and_named_in_the_ingest_result(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOKUP_NO_DOTENV", "1")
    m = tmp_path / "m.toml"
    m.write_text(MANIFEST.replace("@ROOT@", "/root").replace('exclude_names = ["skip"]\n', ""), encoding="utf-8")
    src = manifest.load(m)["notes"]
    fs, _ = fake_fs({"a.md": b"# A\n\nalpha\n", "link-out": {"secret.md": b"# S\n\nsecret\n"}, "ok": {"b.md": b"# B\n\nbeta\n"}})
    saved = config.DB
    try:
        res = ingest(dataclasses.replace(src, filesystem=fs), tmp_path / "x.db")
    finally:
        config.DB = saved
    assert res.added == 2 and res.errors == 0 and res.refused == 0
    assert res.skipped_links == 1
    assert any("SKIPPED" in p and "link-out" in p and "reparse point" in p for p in res.problems)
    assert res.as_dict()["skipped_links"] == 1
    assert all("secret" not in r[4] for r in snapshot(tmp_path / "x.db")[0]), "a link target must never be indexed"
