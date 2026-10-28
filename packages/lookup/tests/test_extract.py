"""Extractor tests, including the regression that lost 1,689 contacts."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lookup import db  # noqa: E402
from lookup.extract import pim, google, docs, resolve  # noqa: E402


class TestDocKey(unittest.TestCase):
    def test_identical_bodies_get_distinct_keys(self):
        """Regression: keys used to hash the body.

        Two contacts sharing a name with no other fields produced identical
        keys and overwrote each other -- 5,660 vcards collapsed to 3,971.
        """
        a = {"source": "contacts", "path": "/x/All.vcf", "body": "Name: Ivan"}
        b = {"source": "contacts", "path": "/x/All.vcf", "body": "Name: Ivan"}
        self.assertNotEqual(db.doc_key(a, 0), db.doc_key(b, 1))

    def test_same_ordinal_is_stable_across_runs(self):
        a = {"source": "vault", "path": "/x/n.md", "body": "one"}
        self.assertEqual(db.doc_key(a, 3), db.doc_key(dict(a), 3))

    def test_explicit_key_wins_over_ordinal(self):
        a = {"source": "mail", "path": "/x/m.mbox", "body": "b", "key": "<id@host>"}
        self.assertEqual(db.doc_key(a, 0), db.doc_key(a, 99))


class TestExtractors(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_vcard_yields_one_doc_per_card(self):
        p = self.d / "c.vcf"
        p.write_text("BEGIN:VCARD\nFN:Ivan\nEMAIL:i@x.com\nEND:VCARD\n"
                     "BEGIN:VCARD\nFN:Ivan\nEND:VCARD\n")
        out = list(pim.vcard(p))
        self.assertEqual(len(out), 2)
        self.assertIn("i@x.com", out[0]["body"])

    def test_vcard_unfolds_continuation_lines(self):
        p = self.d / "c.vcf"
        p.write_text("BEGIN:VCARD\nFN:Ivan\nNOTE:hello \n world\nEND:VCARD\n")
        self.assertIn("hello world", list(pim.vcard(p))[0]["body"])

    def test_ical_skips_epoch_sentinel_date(self):
        p = self.d / "c.ics"
        p.write_text("BEGIN:VEVENT\nSUMMARY:Old\nDTSTART:19700101T000000Z\nEND:VEVENT\n"
                     "BEGIN:VEVENT\nSUMMARY:Real\nDTSTART:20260813T090000Z\nEND:VEVENT\n")
        out = list(pim.ical(p))
        self.assertIsNone(out[0]["date"])
        self.assertEqual(out[1]["date"], "2026-08-13")

    def test_gemini_splits_prompt_and_response(self):
        p = self.d / "MyActivity.html"
        p.write_text(
            '<div class="outer-cell"><div class="content-cell mdl-cell '
            'mdl-typography--body-1">Prompted what is FTS5?<br>'
            'Jul 30, 2026, 2:19:08 PM EEST<br><p>It is a SQLite module.</p>'
            '</div></div></div>')
        out = list(google.gemini(p))
        self.assertEqual(len(out), 1)
        self.assertIn("PROMPT:\nwhat is FTS5?", out[0]["body"])
        self.assertIn("RESPONSE:", out[0]["body"])
        self.assertEqual(out[0]["date"], "2026-07-30T14:19:08")

    def test_bookmarks_extracts_url_and_label(self):
        p = self.d / "Bookmarks.html"
        p.write_text('<A HREF="https://example.com" ADD_DATE="1700000000">Example</A>')
        out = list(google.bookmarks(p))
        self.assertEqual(out[0]["meta"]["url"], "https://example.com")
        self.assertIn("Example", out[0]["title"])

    def test_markdown_prefers_h1_as_title(self):
        p = self.d / "n.md"
        p.write_text("---\nk: v\n---\n\n# Real Title\n\nbody")
        self.assertEqual(list(docs.markdown(p))[0]["title"], "Real Title")

    def test_empty_files_yield_nothing(self):
        p = self.d / "n.md"
        p.write_text("   \n")
        self.assertEqual(list(docs.markdown(p)), [])

    def test_registry_rejects_unknown_extractor(self):
        with self.assertRaises(KeyError):
            resolve("nope.nope")


if __name__ == "__main__":
    unittest.main()
