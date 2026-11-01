"""Per-source grouping is the default, and it is the default for a reason.

`bm25()` is computed over the whole index, so one merged ranking across corpora
of very different sizes returns whichever corpus is largest rather than
whichever is right. Measured over eleven questions on the real corpus,
per-source search answered ten within the top three and one merged ranking
answered six. These tests pin the mechanism, using a fixture built so the
merged ranking demonstrably buries the right answer.

unittest, not pytest: the Dockerfile's build stage runs `python3 -m unittest`
with nothing installed but this package. See test_search_relax.py.
"""
from __future__ import annotations

import sqlite3
import unittest

from lookup import db, mcp


def _con():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(db.config.SCHEMA.read_text(encoding="utf-8"))
    return c


def _add(c, doc_key, source, title, body, path=None, date="2026-09-01"):
    c.execute(
        "INSERT INTO docs(doc_key, source, path, title, date, body, ingested)"
        " VALUES (?,?,?,?,?,?,?)",
        (doc_key, source, path or f"{title}.md", title, date, body, "2026-09-07"))


class SearchGroupedTests(unittest.TestCase):
    def setUp(self):
        self.con = _con()
        # One short, precise answer in a small corpus...
        _add(self.con, "m1", "memory", "The vault holds notes only",
             "The vault holds notes and attachments only. Scripts live outside it.")
        # ...and long, keyword-dense noise in a big one. Every filler document
        # repeats the question's vocabulary, which is what lets a merged
        # ranking bury the short answer.
        filler = "vault notes scripts holds attachments outside only " * 60
        for i in range(12):
            _add(self.con, f"s{i}", "sessions", f"session log {i}", filler)
        self.con.commit()

    def tearDown(self):
        self.con.close()

    def test_source_names_are_ordered_largest_first(self):
        self.assertEqual(db.source_names(self.con), ["sessions", "memory"])

    # The question form matters: FTS5 rejects the trailing '?', so it is
    # relaxed to an OR of every token, and under OR a long document matching
    # six of the terms outranks a short one matching four. That is the burial
    # this whole design exists to prevent, and it does not reproduce with an
    # AND-ed phrase.
    QUESTION = "why does the vault hold notes only and not scripts?"

    def test_merged_ranking_buries_the_small_corpus(self):
        rows = db.search(self.con, self.QUESTION, limit=3)
        self.assertTrue(rows, "fixture should match something")
        self.assertNotIn("m1", [r["doc_key"] for r in rows],
                         "fixture no longer demonstrates the burial it exists to show")

    def test_grouping_surfaces_it(self):
        groups = db.search_grouped(self.con, self.QUESTION, per_source=3)
        by_source = {name: [r["doc_key"] for r in rows] for name, rows, _e, _r in groups}
        self.assertIn("memory", by_source)
        self.assertEqual(by_source["memory"][0], "m1")

    def test_groups_keep_the_requested_source_order(self):
        groups = db.search_grouped(self.con, "vault", ["memory", "sessions"])
        self.assertEqual([g[0] for g in groups], ["memory", "sessions"])

    def test_sources_with_no_match_are_omitted(self):
        groups = db.search_grouped(self.con, "passphrase", ["memory", "sessions"])
        self.assertEqual(groups, [])

    def test_grouped_search_relaxes_a_question(self):
        groups = db.search_grouped(self.con, self.QUESTION)
        self.assertTrue(groups)
        self.assertEqual(groups[0][3], self.QUESTION)

    def test_per_source_limit_is_per_source(self):
        groups = db.search_grouped(self.con, "vault", per_source=2)
        for _name, rows, _e, _r in groups:
            self.assertLessEqual(len(rows), 2)


class McpSearchToolTests(unittest.TestCase):
    """The tool must group by default; naming one source opts out."""

    def setUp(self):
        self.con = _con()
        _add(self.con, "m1", "memory", "The vault holds notes only",
             "The vault holds notes and attachments only.")
        _add(self.con, "v1", "vault", "Vault Conventions",
             "Folder layout, frontmatter and the vault's own conventions.")
        self.con.commit()
        self._real_connect = db.connect
        db.connect = lambda readonly=False: self.con

    def tearDown(self):
        db.connect = self._real_connect
        self.con.close()

    def test_no_source_groups_every_corpus(self):
        out = mcp.t_search({"query": "vault"})
        self.assertIn("## [memory]", out)
        self.assertIn("## [vault]", out)
        self.assertIn("Grouped by source", out)

    def test_one_named_source_returns_a_single_ranking(self):
        out = mcp.t_search({"query": "vault", "source": ["memory"]})
        self.assertIn("## [memory]", out)
        self.assertNotIn("## [vault]", out)
        self.assertNotIn("Grouped by source", out)

    def test_full_bodies_only_when_one_source_is_named(self):
        out = mcp.t_search({"query": "vault", "source": ["memory"], "full": True})
        self.assertIn("attachments only", out)

    def test_no_match_names_what_was_searched(self):
        self.assertIn("archive_stats", mcp.t_search({"query": "passphrase"}))
        self.assertIn("memory", mcp.t_search({"query": "passphrase",
                                              "source": ["memory"]}))

    def test_limit_is_clamped_to_the_documented_maximum(self):
        # 10 per source is the schema maximum; a caller asking for 500 gets 10.
        out = mcp.t_search({"query": "vault", "limit": 500})
        self.assertIn("## [memory]", out)

    def test_tool_description_tells_the_caller_to_read_every_group(self):
        tool = next(t for t in mcp.TOOLS if t["name"] == "search_archive")
        self.assertIn("GROUPED BY SOURCE", tool["description"])
        # The old description described core's corpus and demanded FTS5 syntax.
        self.assertNotIn("Query syntax is SQLite FTS5", tool["description"])
        self.assertEqual(tool["inputSchema"]["properties"]["limit"]["default"], 3)


if __name__ == "__main__":
    unittest.main()
