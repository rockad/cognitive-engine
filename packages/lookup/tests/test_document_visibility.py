"""Per-document visibility: a document may narrow its source, never widen it.

Decided on 2026-09-09, over the cheaper alternative of splitting a
mixed corpus into two sources. Slack forced it: a company-wide channel is `open`
and a DM is `gated`, so one word for the whole source mislabels most of it
either way, and leaving private channels out to dodge the problem breaks the
rule that nothing is banned as a source, only gated.

Four properties, and each of them is a way the feature could be worse than not
having it:

  * a document may NARROW its source's value and may never WIDEN it — widening
    cannot be taken back, so it must not be reachable by editing front matter
  * an edited label must actually REACH the index; `upsert_ex`'s change
    comparison decides "unchanged", and "unchanged" writes nothing
  * `mixed` must never appear as a HIT's label — it describes a corpus and says
    nothing about the document in hand
  * `--visibility open` must filter in SQL, because LIMIT applies before any
    post-filter would and the caller would silently get fewer results

unittest-style: these were written for a stdlib-only image build and run
unchanged under pytest.
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from lookup import config, db, manifest, pipeline
from lookup.extract.common import front_matter_visibility


MANIFEST = """
[[source]]
name = "open_src"
description = "An open ceiling: a document may narrow, never widen."
root = "@ROOT@/open"
include = ["*.md"]
extractor = "docs.markdown"
visibility = "open"

[[source]]
name = "gated_src"
description = "A gated ceiling: a document claiming open is refused."
root = "@ROOT@/gated"
include = ["*.md"]
extractor = "docs.markdown"
visibility = "gated"

[[source]]
name = "mixed_src"
description = "No ceiling: each document declares its own."
root = "@ROOT@/mixed"
include = ["*.md"]
extractor = "docs.markdown"
visibility = "mixed"
"""


class FrontMatterTests(unittest.TestCase):
    """The only authoring channel, and its failure direction."""

    def test_a_declared_value_is_read(self):
        self.assertEqual(
            front_matter_visibility("---\ntitle: x\nvisibility: gated\n---\n\nbody"),
            "gated")

    def test_quotes_and_trailing_space_are_tolerated(self):
        self.assertEqual(
            front_matter_visibility('---\nvisibility: "open"  \n---\n'), "open")

    def test_no_front_matter_is_none(self):
        self.assertIsNone(front_matter_visibility("# just a heading\n\nbody"))

    def test_a_horizontal_rule_is_not_front_matter(self):
        # A `---` further down a document is a rule. Reading one as a fence
        # would turn prose into metadata.
        self.assertIsNone(
            front_matter_visibility("# title\n\ntext\n\n---\nvisibility: open\n---\n"))

    def test_an_unrecognised_value_is_none_rather_than_an_error(self):
        # None means inherit, which is never wider than the source. A typo in
        # one file must not stop that file being indexed.
        self.assertIsNone(front_matter_visibility("---\nvisibility: publik\n---\n"))

    def test_mixed_is_not_a_document_value(self):
        # `mixed` describes a corpus. A document claiming it would be answering
        # a question about itself that it cannot answer.
        self.assertIsNone(front_matter_visibility("---\nvisibility: mixed\n---\n"))


class CeilingTests(unittest.TestCase):
    def test_open_ceiling_permits_narrowing(self):
        self.assertFalse(manifest.widens("gated", "open"))

    def test_gated_ceiling_refuses_widening(self):
        self.assertTrue(manifest.widens("open", "gated"))

    def test_mixed_and_unstated_are_not_ceilings(self):
        # Neither has claimed anything about the corpus, so there is nothing
        # for a document to widen past.
        self.assertFalse(manifest.widens("open", "mixed"))
        self.assertFalse(manifest.widens("open", "unstated"))

    def test_a_hit_under_mixed_with_no_value_is_unstated_not_mixed(self):
        self.assertEqual(manifest.effective_visibility(None, "mixed"), "unstated")

    def test_a_document_value_wins_over_its_source(self):
        self.assertEqual(manifest.effective_visibility("gated", "open"), "gated")

    def test_no_document_value_inherits_the_source(self):
        self.assertEqual(manifest.effective_visibility(None, "open"), "open")

    def test_mixed_validates_as_a_source_value(self):
        self.assertIn("mixed", manifest.VISIBILITIES)


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.tmp.name)
        for name in ("open", "gated", "mixed"):
            (self.base / name).mkdir()
        self.mpath = self.base / "m.toml"
        self.mpath.write_text(MANIFEST.replace("@ROOT@", str(self.base)),
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

    def write(self, source, name, vis=None, body="searchable body text"):
        fm = f"---\nvisibility: {vis}\n---\n\n" if vis else ""
        p = self.base / source / name
        p.write_text(f"{fm}# {name}\n\n{body}\n", encoding="utf-8")
        return p

    def ingest(self, source):
        con = db.connect()
        res = pipeline.ingest_source(con, manifest.load(self.mpath)[source],
                                     progress=False)
        con.commit()
        con.close()
        return res

    def rows(self, source):
        con = db.connect(readonly=True)
        out = {r["path"].rsplit("/", 1)[-1]: r["visibility"] for r in
               con.execute("SELECT path, visibility FROM docs WHERE source=?",
                           (source,))}
        con.close()
        return out

    def test_a_document_narrowing_an_open_source_is_indexed_as_narrowed(self):
        self.write("open", "public.md")
        self.write("open", "held-back.md", vis="gated")
        res = self.ingest("open_src")
        self.assertEqual(res.refused, 0)
        self.assertEqual(self.rows("open_src"),
                         {"public.md": None, "held-back.md": "gated"})

    def test_a_document_widening_a_gated_source_is_refused_and_reported(self):
        self.write("gated", "ordinary.md")
        self.write("gated", "overreaching.md", vis="open")
        res = self.ingest("gated_src")
        self.assertEqual(res.refused, 1)
        self.assertIn("overreaching.md", " ".join(res.problems))
        self.assertIn("widens", " ".join(res.problems))
        # Refused means NOT WRITTEN, not written-and-flagged.
        self.assertEqual(list(self.rows("gated_src")), ["ordinary.md"])

    def test_a_mixed_source_carries_both_kinds(self):
        self.write("mixed", "channel-general.md", vis="open")
        self.write("mixed", "dm-with-someone.md", vis="gated")
        self.write("mixed", "undeclared.md")
        res = self.ingest("mixed_src")
        self.assertEqual(res.refused, 0)
        self.assertEqual(self.rows("mixed_src"),
                         {"channel-general.md": "open",
                          "dm-with-someone.md": "gated",
                          "undeclared.md": None})

    def test_editing_a_label_reaches_the_index(self):
        """⚠️ The one that fails silently if `upsert_ex` forgets the column.

        `upsert_ex`'s comparison decides "unchanged", and "unchanged" writes
        nothing — so a document edited from open to gated would keep serving the
        wider label, and the resident server's re-ingest timer would never
        correct it. That failure looks exactly like the feature working.
        """
        p = self.write("mixed", "turns-private.md", vis="open")
        self.ingest("mixed_src")
        self.assertEqual(self.rows("mixed_src")["turns-private.md"], "open")

        # Same body, same length, only the label changes — and the body has to
        # stay identical or the test passes for the wrong reason.
        p.write_text(p.read_text(encoding="utf-8").replace("visibility: open",
                                                           "visibility: gate"),
                     encoding="utf-8")
        p.write_text(p.read_text(encoding="utf-8").replace("visibility: gate",
                                                           "visibility: gated"),
                     encoding="utf-8")
        res = self.ingest("mixed_src")
        self.assertEqual(res.updated, 1, "the label change must be an update")
        self.assertEqual(self.rows("mixed_src")["turns-private.md"], "gated")

    def test_the_search_filter_runs_in_sql_not_over_the_results(self):
        # LIMIT applies before a post-filter would, so a post-filter returns
        # fewer rows than asked for. Three gated documents rank ahead of the
        # open one here by nothing but insertion order; asking for one open
        # result must still return it.
        for i in range(3):
            self.write("mixed", f"gated-{i}.md", vis="gated")
        self.write("mixed", "the-open-one.md", vis="open")
        self.ingest("mixed_src")
        con = db.connect(readonly=True)
        rows = db.search(con, "searchable", ["mixed_src"], limit=1,
                         visibility="open")
        con.close()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["path"].endswith("the-open-one.md"))

    def test_the_filter_matches_inherited_values_too(self):
        # A row with no value of its own is still `open` when its source is.
        self.write("open", "inherits.md")
        self.ingest("open_src")
        con = db.connect(readonly=True)
        got = db.search(con, "searchable", ["open_src"], limit=5,
                        visibility="open", inherit_sources=("open_src",))
        none = db.search(con, "searchable", ["open_src"], limit=5,
                         visibility="open")
        con.close()
        self.assertEqual(len(got), 1)
        # Without the source resolution, an inherited row does not match — which
        # is why the caller has to pass it, and why failing to is narrower
        # rather than wider.
        self.assertEqual(len(none), 0)


class ShaCacheTests(IngestTests):
    """The digest cache must not outlive an ingest run.

    Found on 2026-09-09 by `files --verify`: 25 rows across the live index
    carried a digest one revision behind text that was current. The resident
    server re-ingests every 300 s in ONE process, so a cached digest from an
    earlier pass survived into a later one — the body was re-read because mtime
    had changed, and paired with the pre-edit digest. No search could reveal it,
    and it broke the one thing the digest exists for.
    """

    def test_an_edited_file_gets_the_digest_of_its_new_content(self):
        p = self.write("open", "edited-between-passes.md")
        self.ingest("open_src")
        first = db.file_sha256(p)

        # Same process, as the resident server would be. A longer body so mtime
        # AND size both move and the ledger cannot skip the file.
        p.write_text(p.read_text(encoding="utf-8") + "\n\nan added paragraph\n",
                     encoding="utf-8")
        self.ingest("open_src")

        con = db.connect(readonly=True)
        stored = con.execute("SELECT src_sha256 FROM docs WHERE path=?",
                             (str(p),)).fetchone()["src_sha256"]
        con.close()
        self.assertNotEqual(stored, first, "the digest is one revision behind")
        self.assertEqual(stored, db.file_sha256(p, cached=False))


if __name__ == "__main__":
    unittest.main()
