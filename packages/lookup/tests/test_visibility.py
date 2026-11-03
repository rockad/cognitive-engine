"""A source declares whose material it holds, and the label reaches the reader.

A fact inherits the visibility of the system it came from: `open` material — a
company-wide channel, an unrestricted page — is free to reuse, while `gated`
material — a DM, a private channel, a permission-restricted page — is usable
only after the fact is verified against its source and signed off. Nothing is
refused for where it came from.

Two things therefore have to hold, and the second is the point of the first:

  * the manifest carries the value as DATA, validated at load, so a typo is a
    loud error rather than a source that quietly reads as something else
  * every result carries the label, because the agent about to reuse the fact
    cannot see the manifest — a field nothing reads would be worthless

⚠️ The default is `unstated`, not `open`. An undeclared source is of unknown
provenance and unknown is handled as gated; defaulting to `open` would silently
assert the one thing that cannot be taken back once a fact has been reused on
the strength of it.

Run with: python3 -m unittest discover -s tests -t .
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from lookup import db, manifest, mcp  # noqa: E402


def _block(name: str, root: pathlib.Path, visibility: str | None = None) -> str:
    vis = f'visibility = "{visibility}"\n' if visibility is not None else ""
    return (f'[[source]]\nname = "{name}"\ndescription = "d"\n'
            f'root = "{root}"\ninclude = ["*.md"]\n{vis}'
            f'extractor = "docs.markdown"\n\n')


class ManifestVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.tmp.name)
        self.base.joinpath("r").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _load(self, *blocks: str) -> dict[str, manifest.Source]:
        p = self.base / "m.toml"
        p.write_text("".join(blocks), encoding="utf-8")
        return manifest.load(p)

    def test_open_is_accepted_and_needs_no_signoff(self):
        s = self._load(_block("o", self.base / "r", "open"))["o"]
        self.assertEqual(s.visibility, "open")
        self.assertFalse(s.needs_signoff())

    def test_gated_is_accepted_and_needs_signoff(self):
        s = self._load(_block("g", self.base / "r", "gated"))["g"]
        self.assertEqual(s.visibility, "gated")
        self.assertTrue(s.needs_signoff())

    def test_an_omitted_key_is_unstated_and_handled_as_gated(self):
        """Not stated, and reported as such — never silently promoted to open."""
        s = self._load(_block("u", self.base / "r"))["u"]
        self.assertEqual(s.visibility, "unstated")
        self.assertNotEqual(s.visibility, "open")
        self.assertTrue(s.needs_signoff())

    def test_an_invalid_value_is_a_manifest_error(self):
        with self.assertRaises(manifest.ManifestError) as e:
            self._load(_block("x", self.base / "r", "internal"))
        # The message has to say what is allowed; a bare "invalid" leaves the
        # author guessing at the vocabulary.
        self.assertIn("visibility", str(e.exception))
        self.assertIn("open", str(e.exception))
        self.assertIn("gated", str(e.exception))

    def test_a_non_string_value_is_refused_rather_than_coerced(self):
        p = self.base / "m.toml"
        p.write_text(f'[[source]]\nname = "x"\ndescription = "d"\n'
                     f'root = "{self.base / "r"}"\ninclude = ["*.md"]\n'
                     f'visibility = true\nextractor = "docs.markdown"\n',
                     encoding="utf-8")
        with self.assertRaises(manifest.ManifestError):
            manifest.load(p)

    def test_the_existing_sources_still_load(self):
        """The six declared sources predate the field and must be unaffected.

        Roots resolve any environment variable, so the real manifest's own
        names have to be supplied here; dummy paths are fine, since what is
        under test is the file's content and not whether it exists on this
        machine (same fixture as tests/test_inclusion.py).
        """
        import os

        from lookup import config
        env = dict(os.environ)
        try:
            os.environ.update({"LOOKUP_HOME": "/tmp/lookup-home",
                               "ARCHIVE_TAKEOUT": "/tmp/t",
                               "ARCHIVE_SOURCES": "/tmp/s",
                               "ARCHIVE_CONVERTED": "/tmp/c",
                               "ARCHIVE_ROOT": "/tmp/a"})
            srcs = manifest.load(config.MANIFEST)
        finally:
            os.environ.clear()
            os.environ.update(env)
        self.assertTrue(srcs)
        for s in srcs.values():
            self.assertIn(s.visibility, manifest.VISIBILITIES)


class ResultLabelTests(unittest.TestCase):
    """The label has to reach the reply, which is the whole point of the field."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.tmp.name)
        doc = base / "n.md"
        doc.write_text("opsgenie escalation runbook", encoding="utf-8")
        self._real_db = db.config.DB
        db.config.DB = base / "index.db"
        con = db.connect()
        cur = con.cursor()
        for key, source in (("o", "openish"), ("g", "gatedish"), ("u", "quiet")):
            db.upsert_ex(cur, key, {
                "source": source, "path": str(doc), "title": f"{source} note",
                "date": "2026-09-01", "body": "opsgenie escalation runbook",
                "meta": {}})
        con.commit()
        con.close()
        self.con = db.connect(readonly=True)
        self._connect = db.connect
        db.connect = lambda readonly=False: self.con
        self._vis = mcp._VISIBILITY
        mcp._VISIBILITY = {"openish": "open", "gatedish": "gated",
                           "quiet": "unstated"}
        self._remote = mcp._REMOTE_SOURCES
        mcp._REMOTE_SOURCES = set()

    def tearDown(self):
        mcp._VISIBILITY = self._vis
        mcp._REMOTE_SOURCES = self._remote
        db.connect = self._connect
        self.con.close()
        db.config.DB = self._real_db
        self.tmp.cleanup()

    def test_every_hit_carries_the_label(self):
        out = mcp.t_search({"query": '"opsgenie"', "source": ["gatedish"]})
        self.assertIn("visibility: gated", out)

    def test_an_open_source_reply_asks_for_no_ceremony(self):
        out = mcp.t_search({"query": '"opsgenie"', "source": ["openish"]})
        self.assertIn("visibility: open", out)
        self.assertNotIn("sign-off", out)

    def test_a_gated_source_reply_says_what_is_required(self):
        out = mcp.t_search({"query": '"opsgenie"', "source": ["gatedish"]})
        self.assertIn("sign-off", out)
        self.assertIn("gated", out)

    def test_an_unstated_source_is_named_as_unstated_not_as_open(self):
        out = mcp.t_search({"query": '"opsgenie"', "source": ["quiet"]})
        self.assertIn("visibility: unstated", out)
        self.assertIn("sign-off", out)

    def test_the_grouped_reply_names_the_sources_needing_signoff(self):
        out = mcp.t_search({"query": '"runbook"'})
        self.assertIn("gatedish", out)
        self.assertIn("quiet", out)
        self.assertIn("sign-off", out)

    def test_get_document_carries_it_too(self):
        out = mcp.t_get({"doc_key": "g"})
        self.assertIn("visibility: gated", out)
        self.assertIn("opsgenie escalation runbook", out)

    def test_a_source_the_manifest_does_not_know_reads_as_unstated(self):
        """Unknown provenance resolves to unstated, never to open."""
        self.assertEqual(mcp._visibility_of("never-declared"), "unstated")
        self.assertEqual(mcp._visibility_of(None), "unstated")

    def test_stats_lists_it_per_source(self):
        out = mcp.t_stats({})
        self.assertIn("visibility", out)
        self.assertIn("gated", out)

    def test_the_tool_description_explains_the_labels(self):
        tool = next(t for t in mcp.TOOLS if t["name"] == "search_archive")
        for word in ("visibility", "open", "gated", "unstated", "signed it off"):
            self.assertIn(word, tool["description"])


if __name__ == "__main__":
    unittest.main()
