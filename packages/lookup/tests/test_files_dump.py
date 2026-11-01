"""`lookup files` — the two lists an external ledger needs to prove nothing lost.

The inclusion principle guarantees nothing forbidden gets in. It guarantees
nothing about anything wanted being left out, and the three silent-drop paths
(no matching include glob, an empty extraction, a problem list capping at ten)
are invisible from inside the pipeline. A checker outside it can only tell the
difference by comparing what a source DECLARES against what it INDEXED, joined
on content rather than on filename — so both lists have to be obtainable, and
the digest has to survive into the indexed one.

unittest, not pytest: the Dockerfile's build stage runs `python3 -m unittest`
with nothing installed but this package. See test_search_relax.py.
"""
from __future__ import annotations

import contextlib
import io
import os
import pathlib
import tempfile
import unittest

from lookup import cli, config, db, manifest, pipeline


MANIFEST = """
[[source]]
name = "notes"
description = "Markdown only, so a .csv beside it is declared by nothing."
root = "@ROOT@"
include = ["*.md", "**/*.md"]
extractor = "docs.markdown"
"""


class FilesDumpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.tmp.name)
        self.root = self.base / "corpus"
        (self.root / "sub").mkdir(parents=True)
        (self.root / "a.md").write_text("alpha body\n", encoding="utf-8")
        (self.root / "sub" / "b.md").write_text("beta body\n", encoding="utf-8")
        # Matched by no include glob. It must appear in NEITHER list, which is
        # what makes "declared but not indexed" mean something.
        (self.root / "c.csv").write_text("x,y\n1,2\n", encoding="utf-8")

        self.mpath = self.base / "m.toml"
        self.mpath.write_text(MANIFEST.replace("@ROOT@", str(self.root)),
                              encoding="utf-8")
        self._env = dict(os.environ)
        os.environ["LOOKUP_DB"] = str(self.base / "index.db")
        self._db, self._manifest = config.DB, config.MANIFEST
        config.DB = pathlib.Path(os.environ["LOOKUP_DB"])
        config.MANIFEST = self.mpath

    def tearDown(self):
        config.DB, config.MANIFEST = self._db, self._manifest
        os.environ.clear()
        os.environ.update(self._env)
        self.tmp.cleanup()

    def _run(self, **kw):
        args = type("A", (), {"source": ["notes"], "indexed": True,
                              "declared": False, "relative": False,
                              "verify": False})()
        for k, v in kw.items():
            setattr(args, k, v)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            rc = cli.cmd_files(args)
        self.assertEqual(rc, 0)
        lines = buf.getvalue().strip().splitlines()
        return lines[0], [l.split("\t") for l in lines[1:]]

    def _ingest(self):
        con = db.connect()
        pipeline.ingest_source(con, manifest.load(self.mpath)["notes"])
        con.commit()
        con.close()

    def test_declared_lists_the_glob_matches_and_nothing_else(self):
        header, rows = self._run(declared=True, indexed=False, relative=True)
        self.assertEqual(header, "source\tpath")
        self.assertEqual(sorted(r[1] for r in rows), ["a.md", "sub/b.md"])

    def test_declared_needs_no_database(self):
        # It walks the filesystem, so it must work on the host where the index
        # lives inside a container and config.DB names nothing.
        self.assertFalse(config.DB.exists())
        _, rows = self._run(declared=True, indexed=False, relative=True)
        self.assertEqual(len(rows), 2)

    def test_indexed_carries_a_digest_that_matches_the_file(self):
        self._ingest()
        header, rows = self._run()
        self.assertEqual(header, "source\tpath\tsha256\tdocs")
        by_path = {r[1]: r for r in rows}
        want = db.file_sha256(self.root / "a.md")
        self.assertEqual(by_path[str(self.root / "a.md")][2], want)

    def test_the_two_lists_agree_when_nothing_was_dropped(self):
        # The whole point: a reconcile joins these and expects an empty
        # difference. Compared as absolute paths, which is what the index holds.
        self._ingest()
        _, declared = self._run(declared=True, indexed=False)
        _, indexed = self._run()
        self.assertEqual(sorted(r[1] for r in declared),
                         sorted(r[1] for r in indexed))

    def test_a_file_matching_no_glob_is_in_neither_list(self):
        self._ingest()
        _, declared = self._run(declared=True, indexed=False, relative=True)
        _, indexed = self._run()
        self.assertNotIn("c.csv", [r[1] for r in declared])
        self.assertFalse([r for r in indexed if r[1].endswith("c.csv")])

    def test_a_file_under_an_excluded_directory_is_in_neither_list(self):
        """The property an inventory-versus-index comparison depends on.

        The knowledge archive holds ~12,400 tracked markdown files while its
        `archive` source indexes ~530: `google/`, `_inbox/`, `_sources/` and
        `conversations/` are excluded on purpose. That gap must never read as
        loss, and it cannot, because `--declared` prunes excluded directories
        during the walk rather than filtering afterwards — so an excluded file
        is absent from the declared list and can never appear in the ledger's
        missing set.
        """
        (self.root / "excluded_dir").mkdir()
        (self.root / "excluded_dir" / "d.md").write_text("delta\n", encoding="utf-8")
        self.mpath.write_text(
            MANIFEST.replace("@ROOT@", str(self.root)).replace(
                'extractor = "docs.markdown"',
                'exclude_names = ["excluded_dir"]\nextractor = "docs.markdown"'),
            encoding="utf-8")
        self._ingest()
        _, declared = self._run(declared=True, indexed=False, relative=True)
        _, indexed = self._run()
        self.assertNotIn("excluded_dir/d.md", [r[1] for r in declared])
        self.assertFalse([r for r in indexed if "excluded_dir" in r[1]])
        # And the two lists still agree, which is the whole point: an exclusion
        # is not a discrepancy.
        _, declared_abs = self._run(declared=True, indexed=False)
        self.assertEqual(sorted(r[1] for r in declared_abs),
                         sorted(r[1] for r in indexed))

    def test_one_row_per_file_however_many_documents_it_produced(self):
        # `docs` accounts for documents and the ledger accounts for files. One
        # .vcf produced 5,660 documents; the ledger still has one file to prove.
        self._ingest()
        con = db.connect()
        cur = con.cursor()
        d = {"source": "notes", "path": str(self.root / "a.md"),
             "title": "second document from the same file",
             "body": "another body", "src_sha256": "deadbeef"}
        db.upsert_ex(cur, db.doc_key(d, 1), d)
        con.commit()
        con.close()
        _, rows = self._run()
        a = [r for r in rows if r[1] == str(self.root / "a.md")]
        self.assertEqual(len(a), 1, "one row per file, not per document")
        self.assertEqual(a[0][3], "2")

    def test_verify_catches_a_file_the_index_is_behind(self):
        """The check a plain re-ingest cannot make.

        `src_files` skips a file whose mtime AND size both match what it
        recorded — the fast path that makes a network share affordable — so a
        write preserving both would be reported as `unchanged` over an index
        that is behind. A `git checkout` between branches restores content with
        its old mtime, which is how it could happen in practice.

        ⚠️ **This case has never actually been observed**, and the test says so
        deliberately. Every real mismatch found on 2026-09-09 was the other
        fault — current text with a stale digest — and an early account of that
        day wrongly described one of them as day-old text, which is what the
        body comparison in `_verify_indexed` exists to settle. The test pins the
        behaviour, not the anecdote.
        """
        self._ingest()
        p = self.root / "a.md"
        st = p.stat()
        # Same LENGTH as well as same mtime, so size cannot give it away either.
        p.write_text("alphaX body", encoding="utf-8")
        os.utime(p, (st.st_atime, st.st_mtime))
        self.assertEqual(p.stat().st_mtime, st.st_mtime)

        args = type("A", (), {"source": ["notes"], "indexed": True,
                              "declared": False, "relative": False,
                              "verify": True})()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.cmd_files(args)
        self.assertEqual(rc, 3, "a stale index must exit non-zero")
        self.assertIn("INDEX BEHIND", buf.getvalue())
        self.assertIn("--rehash", buf.getvalue())

    def test_a_wrong_digest_over_current_text_is_reported_as_such(self):
        """The distinction that stops a false alarm sending someone re-ingesting.

        A digest mismatch is two faults with opposite fixes. On 2026-09-09 all
        24 mismatches across the live index turned out to be this one: the
        indexed body was identical to the file and only the recorded digest
        disagreed, so search was answering correctly and only a ledger joining
        on content was misled. Re-ingesting would have fixed nothing.
        """
        self._ingest()
        con = db.connect()
        con.execute("UPDATE docs SET src_sha256='0' || substr(src_sha256, 2)"
                    " WHERE path LIKE '%a.md'")
        con.commit()
        con.close()
        args = type("A", (), {"source": ["notes"], "indexed": True,
                              "declared": False, "relative": False,
                              "verify": True})()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            rc = cli.cmd_files(args)
        out = buf.getvalue()
        self.assertIn("DIGEST WRONG", out)
        self.assertIn("backfill-hashes", out)
        self.assertNotIn("INDEX BEHIND", out)
        # Bookkeeping, not a wrong answer to a search, so it must not be fatal.
        self.assertEqual(rc, 0)

    def test_verify_passes_when_the_index_matches_disk(self):
        self._ingest()
        args = type("A", (), {"source": ["notes"], "indexed": True,
                              "declared": False, "relative": False,
                              "verify": True})()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.cmd_files(args)
        self.assertEqual(rc, 0)
        self.assertIn("matches the content on disk", buf.getvalue())

    def test_a_null_digest_is_an_empty_field_not_the_word_none(self):
        # NULL means "not hashed", and a ledger must be able to count it as
        # unknown coverage. "None" in a TSV column would be read as a digest.
        con = db.connect()
        cur = con.cursor()
        d = {"source": "notes", "path": str(self.root / "gone.md"),
             "title": "no digest", "body": "body", "src_sha256": None}
        db.upsert_ex(cur, db.doc_key(d, 0), d)
        con.commit()
        con.close()
        _, rows = self._run()
        row = [r for r in rows if r[1].endswith("gone.md")][0]
        self.assertEqual(row[2], "")


if __name__ == "__main__":
    unittest.main()
