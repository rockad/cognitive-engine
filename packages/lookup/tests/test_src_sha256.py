"""Tests for the source-file digest that backs the ledger's content join.

`docs.src_sha256` is what lets an external reconcile prove "nothing lost" by
content rather than by filename. Three properties have to hold, and each has
already been a bug in this file's history:

  * the column reaches an EXISTING database, which `CREATE TABLE IF NOT EXISTS`
    can never do — hence db.migrate, and hence its ordering against the schema
    script, which declares an index over the column being added
  * NULL means "not hashed", never "no hash exists", so nothing may quietly
    fill it with a wrong value or drop a verified one
  * updating the digest must not churn the FTS index

Run with: python3 -m unittest discover -s tests -t .
"""
from __future__ import annotations

import hashlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

LEGACY_DDL = """
CREATE TABLE docs (id INTEGER PRIMARY KEY, doc_key TEXT NOT NULL UNIQUE,
 source TEXT NOT NULL, path TEXT, title TEXT, date TEXT, body TEXT NOT NULL,
 meta TEXT, bytes INTEGER, ingested TEXT NOT NULL);
CREATE VIRTUAL TABLE fts USING fts5(title, body, content='docs',
 content_rowid='id', tokenize="unicode61 remove_diacritics 2");
CREATE TRIGGER docs_ai AFTER INSERT ON docs BEGIN
  INSERT INTO fts(rowid, title, body) VALUES (new.id, new.title, new.body); END;
CREATE TRIGGER docs_au AFTER UPDATE ON docs BEGIN
  INSERT INTO fts(fts, rowid, title, body) VALUES('delete', old.id, old.title, old.body);
  INSERT INTO fts(rowid, title, body) VALUES (new.id, new.title, new.body); END;
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.dbp = self.base / "index.db"
        # config reads LOOKUP_DB at import, so point it before importing db.
        import os
        os.environ["LOOKUP_DB"] = str(self.dbp)
        import importlib
        from lookup import config
        importlib.reload(config)
        from lookup import db
        importlib.reload(db)
        self.db = db

    def tearDown(self):
        self.tmp.cleanup()

    def legacy_db(self, rows=(("k1", "/a/b.md", "searchable body"),)) -> None:
        con = sqlite3.connect(self.dbp)
        con.executescript(LEGACY_DDL)
        for key, path, body in rows:
            con.execute("INSERT INTO docs(doc_key,source,path,title,body,ingested)"
                        " VALUES(?,?,?,?,?,?)", (key, "vault", path, "T", body, "2026-01-01"))
        con.commit()
        con.close()

    def columns(self, con) -> set[str]:
        return {r[1] for r in con.execute("PRAGMA table_info(docs)")}


class TestMigration(Base):
    def test_column_added_to_existing_database(self):
        self.legacy_db()
        con = self.db.connect()
        self.assertIn("src_sha256", self.columns(con))
        con.close()

    def test_existing_rows_survive_with_null(self):
        self.legacy_db()
        con = self.db.connect()
        row = con.execute("SELECT doc_key, body, src_sha256 FROM docs").fetchone()
        self.assertEqual(row["doc_key"], "k1")
        self.assertEqual(row["body"], "searchable body")
        self.assertIsNone(row["src_sha256"])
        con.close()

    def test_index_over_the_new_column_is_created(self):
        """Regression: the schema declares docs_sha over src_sha256, so running
        the schema script BEFORE the ALTER fails with `no such column`."""
        self.legacy_db()
        con = self.db.connect()
        names = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertIn("docs_sha", names)
        con.close()

    def test_migrate_is_idempotent(self):
        self.legacy_db()
        con = self.db.connect()
        self.assertEqual(self.db.migrate(con), [])
        con.close()

    def test_fresh_database_needs_no_migration(self):
        con = self.db.connect()
        self.assertIn("src_sha256", self.columns(con))
        self.assertEqual(self.db.migrate(con), [])
        con.close()

    def test_fts_survives_migration(self):
        self.legacy_db()
        con = self.db.connect()
        self.assertEqual(
            con.execute("SELECT COUNT(*) FROM fts WHERE fts MATCH 'searchable'").fetchone()[0], 1)
        con.execute("INSERT INTO fts(fts) VALUES('integrity-check')")
        con.close()

    def test_old_bare_update_trigger_is_narrowed(self):
        """A bare AFTER UPDATE trigger rewrites the FTS row for every column
        write, so backfilling one scalar would churn the whole index."""
        self.legacy_db()
        con = self.db.connect()
        sql = con.execute(
            "SELECT sql FROM sqlite_master WHERE name='docs_au'").fetchone()[0]
        self.assertIn("UPDATE OF", sql)
        con.close()

    def test_writing_the_digest_does_not_touch_fts(self):
        self.legacy_db()
        con = self.db.connect()
        before = con.execute("SELECT COUNT(*) FROM fts WHERE fts MATCH 'searchable'").fetchone()[0]
        con.execute("UPDATE docs SET src_sha256='deadbeef' WHERE doc_key='k1'")
        con.commit()
        after = con.execute("SELECT COUNT(*) FROM fts WHERE fts MATCH 'searchable'").fetchone()[0]
        self.assertEqual((before, after), (1, 1))
        con.execute("INSERT INTO fts(fts) VALUES('integrity-check')")
        con.close()


class TestFileSha256(Base):
    def test_digest_matches_hashlib(self):
        p = self.base / "a.md"
        p.write_bytes(b"alpha content")
        self.assertEqual(self.db.file_sha256(p),
                         hashlib.sha256(b"alpha content").hexdigest())

    def test_identical_bytes_at_different_paths_share_a_digest(self):
        """The whole point: this is what makes a moved file provably present."""
        a, b = self.base / "a.md", self.base / "b.md"
        a.write_bytes(b"same"); b.write_bytes(b"same")
        self.assertEqual(self.db.file_sha256(a), self.db.file_sha256(b))

    def test_missing_file_is_none_not_an_error(self):
        self.assertIsNone(self.db.file_sha256(self.base / "nope.md"))

    def test_directory_is_none(self):
        self.assertIsNone(self.db.file_sha256(self.base))

    def test_empty_file_hashes_rather_than_returning_none(self):
        p = self.base / "empty.md"
        p.write_bytes(b"")
        self.assertEqual(self.db.file_sha256(p), hashlib.sha256(b"").hexdigest())

    def test_cache_is_bounded(self):
        for i in range(self.db._SHA_CACHE_MAX * 2 + 5):
            p = self.base / f"f{i}.md"
            p.write_bytes(f"body {i}".encode())
            self.db.file_sha256(p)
        self.assertLessEqual(len(self.db._SHA_CACHE), self.db._SHA_CACHE_MAX)

    def test_cache_returns_the_same_digest_for_a_repeated_path(self):
        p = self.base / "a.md"
        p.write_bytes(b"alpha")
        self.assertEqual(self.db.file_sha256(p), self.db.file_sha256(p))


class TestUpsertCarriesDigest(Base):
    def doc(self, **kw) -> dict:
        d = {"source": "vault", "path": "/a/b.md", "title": "T", "body": "body"}
        d.update(kw)
        return d

    def test_insert_stores_the_digest(self):
        con = self.db.connect(); cur = con.cursor()
        self.db.upsert(cur, "k", self.doc(src_sha256="a" * 64))
        con.commit()
        self.assertEqual(
            con.execute("SELECT src_sha256 FROM docs WHERE doc_key='k'").fetchone()[0],
            "a" * 64)
        con.close()

    def test_insert_without_a_digest_stores_null(self):
        con = self.db.connect(); cur = con.cursor()
        self.db.upsert(cur, "k", self.doc())
        con.commit()
        self.assertIsNone(
            con.execute("SELECT src_sha256 FROM docs WHERE doc_key='k'").fetchone()[0])
        con.close()

    def test_reingest_without_a_digest_clears_a_stale_one(self):
        """A row re-ingested from a file that can no longer be read must not go
        on claiming a digest nobody re-verified."""
        con = self.db.connect(); cur = con.cursor()
        self.db.upsert(cur, "k", self.doc(src_sha256="a" * 64))
        self.db.upsert(cur, "k", self.doc())
        con.commit()
        self.assertIsNone(
            con.execute("SELECT src_sha256 FROM docs WHERE doc_key='k'").fetchone()[0])
        con.close()

    def test_reingest_updates_a_changed_digest(self):
        con = self.db.connect(); cur = con.cursor()
        self.db.upsert(cur, "k", self.doc(src_sha256="a" * 64))
        self.db.upsert(cur, "k", self.doc(src_sha256="b" * 64))
        con.commit()
        self.assertEqual(
            con.execute("SELECT src_sha256 FROM docs WHERE doc_key='k'").fetchone()[0],
            "b" * 64)
        con.close()

    def test_digest_does_not_affect_document_identity(self):
        """doc_key must stay path-and-ordinal based — hashing content as identity
        is what collapsed 5,660 vcards into 3,971 rows."""
        a = self.db.doc_key(self.doc(src_sha256="a" * 64), 0)
        b = self.db.doc_key(self.doc(src_sha256="b" * 64), 0)
        self.assertEqual(a, b)


class TestBackfill(Base):
    def test_fills_readable_and_counts_the_rest(self):
        from lookup import cli
        good = self.base / "good.md"
        good.write_bytes(b"real content")
        con = self.db.connect(); cur = con.cursor()
        self.db.upsert(cur, "k1", {"source": "v", "path": str(good), "title": "t", "body": "b"})
        self.db.upsert(cur, "k2", {"source": "v", "path": str(self.base / "gone.md"),
                                   "title": "t", "body": "b"})
        self.db.upsert(cur, "k3", {"source": "v", "path": None, "title": "t", "body": "b"})
        con.commit(); con.close()

        self.assertEqual(cli.main(["backfill-hashes"]), 0)
        con = sqlite3.connect(self.dbp)
        got = con.execute("SELECT doc_key, src_sha256 FROM docs ORDER BY doc_key").fetchall()
        self.assertEqual(got[0][1], hashlib.sha256(b"real content").hexdigest())
        self.assertIsNone(got[1][1], "a moved source file must stay NULL, not be invented")
        self.assertIsNone(got[2][1])
        con.close()

    def test_is_idempotent(self):
        from lookup import cli
        p = self.base / "a.md"
        p.write_bytes(b"x")
        con = self.db.connect(); cur = con.cursor()
        self.db.upsert(cur, "k", {"source": "v", "path": str(p), "title": "t", "body": "b"})
        con.commit(); con.close()
        cli.main(["backfill-hashes"])
        cli.main(["backfill-hashes"])
        con = sqlite3.connect(self.dbp)
        self.assertEqual(con.execute("SELECT src_sha256 FROM docs").fetchone()[0],
                         hashlib.sha256(b"x").hexdigest())
        con.close()

    def test_runs_on_a_legacy_database(self):
        from lookup import cli
        self.legacy_db()
        self.assertEqual(cli.main(["backfill-hashes"]), 0)


if __name__ == "__main__":
    unittest.main()
