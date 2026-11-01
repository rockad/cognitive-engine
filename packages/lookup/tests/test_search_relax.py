"""A natural-language question must not be a hard error.

FTS5 rejects a trailing '?' outright and reads a hyphenated word as a column
filter, so questions arrive as `sqlite3.OperationalError` rather than as poor
results. These tests pin the fallback that turns them into a usable query.

⚠️ unittest, not pytest, and deliberately so. The Dockerfile's build stage runs
`python3 -m unittest discover` with nothing installed but this package, because
the project is stdlib-only and that is what keeps the image architecture-
independent. A pytest import here fails the image build while passing locally
under `uv run --with pytest` — which is exactly what happened.
"""
from __future__ import annotations

import sqlite3
import unittest

from lookup import db


class RelaxTests(unittest.TestCase):
    def test_relax_drops_punctuation_and_stopwords(self):
        out = db.relax("How is the repo backed up off-site?")
        self.assertIsNotNone(out)
        self.assertNotIn("?", out)
        # 'off-site' must survive as two quoted tokens, never as a column filter.
        self.assertIn('"off"', out)
        self.assertIn('"site"', out)
        # Stopwords carry no signal and must not be AND-ed into the query.
        self.assertNotIn('"the"', out)
        self.assertNotIn('"is"', out)
        self.assertIn(" OR ", out)

    def test_relax_deduplicates_preserving_order(self):
        self.assertEqual(db.relax("index the index again"), '"index" OR "again"')

    def test_relax_returns_none_when_nothing_is_left(self):
        self.assertIsNone(db.relax("the and of ?"))


class SearchExTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(db.config.SCHEMA.read_text(encoding="utf-8"))
        self.con.execute(
            "INSERT INTO docs(doc_key, source, path, title, date, body, ingested)"
            " VALUES (?,?,?,?,?,?,?)",
            ("k1", "repo-docs", "RESTORE.md", "Restoring the doppelganger repo",
             "2026-09-03", "Each backup is one encrypted tar. The passphrase lives "
             "in the Keychain; losing it makes every snapshot unrecoverable.",
             "2026-09-07"))
        self.con.commit()

    def tearDown(self):
        self.con.close()

    def test_question_with_a_question_mark_would_fail_raw(self):
        with self.assertRaises(sqlite3.OperationalError):
            db.search_ex(self.con, "what happens if the passphrase is lost?",
                         relaxed=False)

    def test_question_with_a_question_mark_succeeds_relaxed(self):
        rows, effective, relaxed_from = db.search_ex(
            self.con, "what happens if the passphrase is lost?")
        self.assertEqual(relaxed_from, "what happens if the passphrase is lost?")
        self.assertNotEqual(effective, relaxed_from)
        self.assertEqual([r["doc_key"] for r in rows], ["k1"])

    def test_hyphenated_term_is_not_read_as_a_column(self):
        # Raw, "off-site" is read as a column filter and raises `no such column:
        # site`. Relaxed, it becomes two quoted tokens OR-ed with the rest, so
        # the document is found on the terms it does carry.
        with self.assertRaises(sqlite3.OperationalError):
            db.search_ex(self.con, "encrypted off-site tar", relaxed=False)
        rows, effective, relaxed_from = db.search_ex(
            self.con, "encrypted off-site tar")
        self.assertEqual(relaxed_from, "encrypted off-site tar")
        self.assertIn('"site"', effective)
        self.assertIn('"off"', effective)
        self.assertEqual([r["doc_key"] for r in rows], ["k1"])

    def test_valid_fts5_syntax_is_left_alone(self):
        rows, effective, relaxed_from = db.search_ex(
            self.con, '"passphrase" AND backup')
        self.assertIsNone(relaxed_from)
        self.assertEqual(effective, '"passphrase" AND backup')
        self.assertEqual([r["doc_key"] for r in rows], ["k1"])



class EmptyResultRelaxTests(unittest.TestCase):
    """A query FTS5 accepts but that matches nothing is also a failure.

    Bare terms are AND-ed, so a plain phrase demands every word appear in one
    document. `staging rule when committing` matched nothing in a corpus that
    plainly holds the answer, and unlike the punctuation cases it raised no
    error at all — the eleven evaluation questions never caught it because
    every one of them ended in a question mark.
    """

    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(db.config.SCHEMA.read_text(encoding="utf-8"))
        self.con.execute(
            "INSERT INTO docs(doc_key, source, path, title, date, body, ingested)"
            " VALUES (?,?,?,?,?,?,?)",
            ("k1", "memory", "blanket-staging.md", "Blanket staging on request",
             "2026-09-03", "Stage only the paths this conversation touched. "
             "Blanket staging has produced cross-contaminated commits.",
             "2026-09-07"))
        self.con.commit()

    def tearDown(self):
        self.con.close()

    def test_and_of_every_word_finds_nothing(self):
        rows = db._run(self.con, "staging rule when committing", None, None,
                       None, 3)
        self.assertEqual(rows, [])

    def test_empty_result_is_retried_relaxed(self):
        rows, effective, relaxed_from = db.search_ex(
            self.con, "staging rule when committing")
        self.assertEqual(relaxed_from, "staging rule when committing")
        self.assertNotEqual(effective, relaxed_from)
        self.assertEqual([r["doc_key"] for r in rows], ["k1"])

    def test_a_deliberate_fts5_query_is_not_second_guessed(self):
        # An explicit operator means the empty result is the caller's answer.
        rows, effective, relaxed_from = db.search_ex(
            self.con, '"staging" AND "kubernetes"')
        self.assertEqual(rows, [])
        self.assertIsNone(relaxed_from)
        self.assertEqual(effective, '"staging" AND "kubernetes"')

    def test_looks_like_fts5(self):
        for q in ['"quoted"', "a OR b", "a NOT b", "a AND b", "term*",
                  "source:vault", "a NEAR b"]:
            self.assertTrue(db.looks_like_fts5(q), q)
        for q in ["staging rule when committing", "why is the vault notes only",
                  "off-site backup"]:
            self.assertFalse(db.looks_like_fts5(q), q)

    def test_a_genuine_miss_stays_a_miss(self):
        rows, effective, relaxed_from = db.search_ex(self.con, "kubernetes helm")
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
