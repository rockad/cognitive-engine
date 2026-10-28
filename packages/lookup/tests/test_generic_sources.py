"""A source is a source: no per-corpus variables, no hand-written mounts.

Decision 2026-09-07: *"lookup should just have 'sources', not projects or
archive specifically, I should be able to add new source also."* These tests pin
the two mechanisms that make that true — any environment variable resolves in a
root, and the container's read-only mounts are derived from the manifest rather
than maintained beside it.

unittest, not pytest: the Dockerfile's build stage runs `python3 -m unittest`
with nothing installed but this package. See test_search_relax.py.
"""
from __future__ import annotations

import os
import pathlib
import tempfile
import unittest

from lookup import cli, config, db, manifest


MANIFEST = """
[[source]]
name = "under_a_var"
description = "A root written relative to an arbitrary variable."
root = "${SOME_BASE}/notes"
include = ["*.md"]
extractor = "docs.markdown"

[[source]]
name = "nested"
description = "A subtree of another declared root."
root = "${SOME_BASE}/notes/deeper"
include = ["*.md"]
extractor = "docs.markdown"

[[source]]
name = "elsewhere_absolute"
description = "A plain absolute path, no variable at all."
root = "@ABSOLUTE@"
include = ["*.md"]
extractor = "docs.markdown"

[[source]]
name = "other_var"
description = "A second, unrelated variable — no code knows its name."
root = "${OTHER_BASE}/archive"
include = ["*.md"]
extractor = "docs.markdown"
"""


class ExpandTests(unittest.TestCase):
    """Any variable, not a fixed table of blessed names."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.tmp.name)
        (self.base / "notes" / "deeper").mkdir(parents=True)
        (self.base / "archive").mkdir()
        (self.base / "absolute").mkdir()
        self.path = self.base / "m.toml"
        # ⚠️ str.replace, not str.format: the manifest text contains ${VAR},
        # and format() reads the inner {VAR} as a field and raises KeyError.
        self.path.write_text(
            MANIFEST.replace("@ABSOLUTE@", str(self.base / "absolute")),
            encoding="utf-8")
        self._env = dict(os.environ)
        os.environ["SOME_BASE"] = str(self.base)
        os.environ["OTHER_BASE"] = str(self.base)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self.tmp.cleanup()

    def test_an_arbitrary_variable_resolves(self):
        srcs = manifest.load(self.path)
        self.assertEqual(srcs["under_a_var"].root, self.base / "notes")

    def test_a_second_unrelated_variable_also_resolves(self):
        # Nothing in the code knows the name OTHER_BASE. That is the point:
        # adding a source needs no code change and no blessed variable.
        srcs = manifest.load(self.path)
        self.assertEqual(srcs["other_var"].root, self.base / "archive")

    def test_a_plain_absolute_path_needs_no_variable(self):
        srcs = manifest.load(self.path)
        self.assertEqual(srcs["elsewhere_absolute"].root, self.base / "absolute")

    def test_an_unset_variable_is_an_error_not_an_empty_string(self):
        # A root that silently became "/notes" would index nothing, or index the
        # wrong tree — which is what the inclusion principle exists to prevent.
        del os.environ["OTHER_BASE"]
        with self.assertRaises(manifest.ManifestError) as e:
            manifest.load(self.path)
        self.assertIn("OTHER_BASE", str(e.exception))


class DotenvTests(unittest.TestCase):
    """`.env` must reach the host-side CLI, and a wrong result must not be silent.

    ⚠️ Both of these are regressions from one real failure. `docker compose`
    reads `.env` on its own but nothing else did, so
    `uv run lookup mounts --write` — the one bring-up step that must not be
    skipped — ran with LOOKUP_HOME unset AND LOOKUP_MANIFEST unset, fell back to the
    packaged sources.toml (core's manifest, whose roots are in-container paths),
    wrote mounts for /data/converted and friends, and exited 0.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.tmp.name)
        self._env = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self.tmp.cleanup()

    def _write_env(self, body, at=None):
        d = at or self.base
        (d / ".env").write_text(body, encoding="utf-8")
        return d

    def test_values_reach_the_environment(self):
        d = self._write_env("SOME_BASE=/tmp/x\n")
        os.environ.pop("SOME_BASE", None)
        loaded = config.load_dotenv(d)
        self.assertEqual(loaded["SOME_BASE"], "/tmp/x")
        self.assertEqual(os.environ["SOME_BASE"], "/tmp/x")

    def test_an_already_set_variable_wins(self):
        d = self._write_env("SOME_BASE=/from/file\n")
        os.environ["SOME_BASE"] = "/from/shell"
        config.load_dotenv(d)
        self.assertEqual(os.environ["SOME_BASE"], "/from/shell")

    def test_comments_blanks_and_quotes(self):
        d = self._write_env('# a comment\n\nA_VAR="/quoted"\nB_VAR=\'/single\'\n')
        for k in ("A_VAR", "B_VAR"):
            os.environ.pop(k, None)
        config.load_dotenv(d)
        self.assertEqual(os.environ["A_VAR"], "/quoted")
        self.assertEqual(os.environ["B_VAR"], "/single")

    def test_a_relative_pi_path_resolves_against_the_env_file(self):
        # So LOOKUP_MANIFEST=sources-local.toml works wherever the repo is cloned.
        d = self._write_env("LOOKUP_MANIFEST=sources-local.toml\n")
        os.environ.pop("LOOKUP_MANIFEST", None)
        config.load_dotenv(d)
        self.assertEqual(os.environ["LOOKUP_MANIFEST"],
                         str((d / "sources-local.toml").resolve()))

    def test_a_non_lookup_relative_value_is_left_alone(self):
        d = self._write_env("SOME_FLAG=yes\n")
        os.environ.pop("SOME_FLAG", None)
        config.load_dotenv(d)
        self.assertEqual(os.environ["SOME_FLAG"], "yes")

    def test_it_searches_upwards(self):
        deep = self.base / "a" / "b"
        deep.mkdir(parents=True)
        self._write_env("FOUND_UP=/yes\n")
        os.environ.pop("FOUND_UP", None)
        config.load_dotenv(deep)
        self.assertEqual(os.environ["FOUND_UP"], "/yes")

    def test_no_env_file_is_not_an_error(self):
        self.assertEqual(config.load_dotenv(self.base), {})


class PruneTests(unittest.TestCase):
    """An index whose residency follows its files has to be able to forget.

    ⚠️ Ingestion was purely additive until 2026-09-07, and that was a defect
    with a concrete symptom: removing the archive mirror left its 530 rows
    behind, so `sources` reported `archive MISSING`, a refresh reported
    "8 source(s), 673 unchanged", and `archive_stats` still claimed 530
    documents while searches handed back paths that were GONE.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.tmp.name)
        self._db = config.DB
        config.DB = self.base / "index.db"
        self.con = db.connect()
        self.cur = self.con.cursor()

    def tearDown(self):
        self.con.close()
        config.DB = self._db
        self.tmp.cleanup()

    def _add(self, key, source, body="text here"):
        db.upsert_ex(self.cur, key, {"source": source, "path": f"/x/{key}.md",
                                     "title": key, "date": "2026-09-01",
                                     "body": body, "meta": {}})

    def test_keep_set_deletes_only_the_unseen(self):
        for k in ("a", "b", "c"):
            self._add(k, "vault")
        self.assertEqual(db.prune_source(self.cur, "vault", {"a", "c"}), 1)
        rows = {r["doc_key"] for r in self.cur.execute(
            "SELECT doc_key FROM docs").fetchall()}
        self.assertEqual(rows, {"a", "c"})

    def test_keep_none_deletes_the_whole_source(self):
        self._add("a", "vault")
        self._add("b", "archive")
        self.assertEqual(db.prune_source(self.cur, "archive", None), 1)
        self.assertEqual([r["source"] for r in self.cur.execute(
            "SELECT source FROM docs").fetchall()], ["vault"])

    def test_it_leaves_other_sources_alone(self):
        self._add("a", "vault")
        self._add("b", "memory")
        db.prune_source(self.cur, "vault", set())
        self.assertEqual([r["source"] for r in self.cur.execute(
            "SELECT source FROM docs").fetchall()], ["memory"])

    def test_the_fts_entry_goes_with_the_row(self):
        # Otherwise a pruned document is still findable, which is the defect
        # wearing a different hat.
        self._add("a", "vault", body="passphrase keychain unrecoverable")
        self.con.commit()
        self.assertTrue(db.search(self.con, '"passphrase"', ["vault"]))
        db.prune_source(self.cur, "vault", set())
        self.con.commit()
        self.assertEqual(db.search(self.con, '"passphrase"', ["vault"]), [])

    def test_nothing_to_prune_reports_zero(self):
        self._add("a", "vault")
        self.assertEqual(db.prune_source(self.cur, "vault", {"a"}), 0)

    def test_more_rows_than_sqlites_parameter_limit(self):
        # The default limit is 999; a source can hold tens of thousands.
        for i in range(1200):
            self._add(f"k{i}", "vault")
        self.assertEqual(db.prune_source(self.cur, "vault", set()), 1200)
        self.assertEqual(self.cur.execute(
            "SELECT COUNT(*) FROM docs").fetchone()[0], 0)

    def test_sources_in_index_lists_what_is_held(self):
        self._add("a", "vault")
        self._add("b", "archive")
        self.con.commit()
        self.assertEqual(sorted(db.sources_in_index(self.con)),
                         ["archive", "vault"])


class WalkPruningTests(unittest.TestCase):
    """An excluded directory must be pruned during the walk, not after it.

    ⚠️ `root.glob("**/*.md")` visits every directory and only then discards the
    excluded ones. Invisible on local disk, brutal over a network share:
    walking the knowledge archive over SMB took 34 s for 3,610 files, of which
    3,080 sat under an excluded `_sources/`. Pruning takes 3 s for the 530 that
    were wanted.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.tmp.name)
        (self.base / "keep").mkdir()
        (self.base / "keep" / "a.md").write_text("a", encoding="utf-8")
        (self.base / "_sources" / "deep" / "deeper").mkdir(parents=True)
        (self.base / "_sources" / "b.md").write_text("b", encoding="utf-8")
        (self.base / "_sources" / "deep" / "deeper" / "c.md").write_text(
            "c", encoding="utf-8")
        (self.base / "top.md").write_text("t", encoding="utf-8")
        self.mpath = self.base / "m.toml"
        self.mpath.write_text(
            '[[source]]\nname = "s"\ndescription = "d"\n'
            f'root = "{self.base}"\ninclude = ["*.md", "**/*.md"]\n'
            'exclude_names = ["_sources"]\nextractor = "docs.markdown"\n',
            encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_excluded_subtree_yields_nothing(self):
        src = manifest.load(self.mpath)["s"]
        names = sorted(p.name for p in src.files())
        self.assertEqual(names, ["a.md", "top.md"])

    def test_it_never_descends_into_the_excluded_directory(self):
        # The behavioural claim, not just the result: os.walk is asked for the
        # tree and must not be handed the pruned branch.
        seen_dirs = []
        real_walk = os.walk

        def spy(top, **kw):
            for dirpath, dirnames, filenames in real_walk(top, **kw):
                seen_dirs.append(dirpath)
                yield dirpath, dirnames, filenames

        src = manifest.load(self.mpath)["s"]
        manifest.os.walk = spy
        try:
            list(src.files())
        finally:
            manifest.os.walk = real_walk
        self.assertFalse([d for d in seen_dirs if "deeper" in d],
                         f"descended into a pruned subtree: {seen_dirs}")

    def test_a_double_star_pattern_still_spans_directories(self):
        # PurePath.match does not treat ** as spanning; full_match does, and
        # the include patterns rely on it.
        (self.base / "keep" / "nested").mkdir()
        (self.base / "keep" / "nested" / "d.md").write_text("d", encoding="utf-8")
        src = manifest.load(self.mpath)["s"]
        self.assertIn("d.md", [p.name for p in src.files()])


class AliasTests(unittest.TestCase):
    """Several addresses can name ONE source.

    Decision 2026-09-07: *"check that lookup sources include aliases for the
    sources, as smb://core/knowledge-archive/ is the same as
    smb://192.168.1.90/knowledge-archive/."*

    Purely declarative. Document identity is address-independent already —
    `sha1(source|path|key)` with paths under a root — so an alias changes
    nothing about what is indexed. It buys a check: an unlisted address is
    refused, so a typo cannot mount a different share under a source name that
    already holds documents.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.tmp.name)
        self.mpath = self.base / "m.toml"
        self.mpath.write_text(
            '[[source]]\nname = "share"\ndescription = "d"\n'
            'root = "/Volumes/share"\n'
            'aliases = ["smb://core/share", "smb://core.lan/share",'
            ' "smb://192.168.1.90/share"]\n'
            'include = ["*.md"]\nextractor = "docs.markdown"\n'
            '[source.volume]\ntype = "cifs"\n'
            'device = "smb://core/share"\noptions = "ro"\n'
            'username = "${SHARE_USER}"\npassword = "${SHARE_PASS}"\n\n'
            '[[source]]\nname = "plain"\ndescription = "d"\n'
            f'root = "{self.base}"\ninclude = ["*.md"]\n'
            'extractor = "docs.markdown"\n', encoding="utf-8")
        self._env = dict(os.environ)
        os.environ.update({"SHARE_USER": "test-user", "SHARE_PASS": "test-pass"})
        self._m = config.MANIFEST
        config.MANIFEST = self.mpath
        self.out = self.base / "override.yml"

    def tearDown(self):
        config.MANIFEST = self._m
        os.environ.clear()
        os.environ.update(self._env)
        self.tmp.cleanup()

    def test_normalise_accepts_the_three_forms_people_write(self):
        n = manifest.normalise_address
        for form in ("smb://core/share/", "//core/share", "\\\\CORE\\share"):
            self.assertEqual(n(form), "//core/share", form)

    def test_a_local_path_passes_through_untouched(self):
        self.assertEqual(manifest.normalise_address("/Volumes/x"), "/Volumes/x")

    def test_addresses_are_normalised_deduped_and_ordered(self):
        src = manifest.load(self.mpath)["share"]
        self.assertEqual(src.addresses(),
                         ("//core/share", "//core.lan/share", "//192.168.1.90/share"))

    def test_any_declared_alias_is_accepted(self):
        src = manifest.load(self.mpath)["share"]
        for form in ("smb://core/share/", "//core.lan/share",
                     "//192.168.1.90/share"):
            self.assertTrue(src.accepts_address(form), form)

    def test_an_unlisted_address_is_not(self):
        src = manifest.load(self.mpath)["share"]
        self.assertFalse(src.accepts_address("//cor/share"))
        self.assertFalse(src.accepts_address("//core/other-share"))

    def test_a_source_with_no_aliases_judges_nothing(self):
        # No declaration means no claim either way.
        src = manifest.load(self.mpath)["plain"]
        self.assertTrue(src.accepts_address("//anything/at/all"))

    def test_the_volume_name_is_derived_from_the_source_name(self):
        src = manifest.load(self.mpath)["share"]
        self.assertEqual(src.volume_name(), "src_share")
        self.assertIsNone(manifest.load(self.mpath)["plain"].volume_name())

    def test_the_declared_device_is_normalised_on_the_source(self):
        src = manifest.load(self.mpath)["share"]
        self.assertEqual(src.device(), "//core/share")
        self.assertEqual(src.volume.get("type"), "cifs")

