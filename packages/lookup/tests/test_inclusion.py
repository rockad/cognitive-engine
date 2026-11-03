"""Tests for the inclusion principle.

These are the safety-critical tests. The disk holding this archive also holds
data that must never be indexed, so "only declared roots are read" has to be a
property the code enforces, not a convention someone remembers.

Run with: python3 -m unittest discover -s tests -t .
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lookup.manifest import Source, load, ManifestError  # noqa: E402


def src(root: Path, include=("**/*.md",), **kw) -> Source:
    return Source(name="t", description="", root=root, include=tuple(include),
                  extractor="docs.markdown", **kw)


class TestInclusion(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.inside = self.base / "included"
        self.outside = self.base / "private"       # stands in for External
        (self.inside / "sub").mkdir(parents=True)
        self.outside.mkdir()
        (self.inside / "a.md").write_text("alpha")
        (self.inside / "sub" / "b.md").write_text("beta")
        (self.inside / "skip.txt").write_text("not included by glob")
        (self.outside / "secret.md").write_text("MUST NOT BE INDEXED")

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_declared_globs_match(self):
        names = {p.name for p in src(self.inside).files()}
        self.assertEqual(names, {"a.md", "b.md"})
        self.assertNotIn("skip.txt", names)

    def test_exclude_names_matches_a_basename_everywhere(self):
        """The documented behaviour — and the reason `exclude_paths` exists.

        Two directories share a name; one entry removes both. That is right for
        `.git` or `node_modules` and wrong for a particular subtree, and getting
        it wrong cost 19 job-search documents: the vault source
        excluded `Verda` to avoid double-indexing `Work/Verda`, and silently
        dropped `Job search/Applications/Verda` too, which no other source
        declared.
        """
        for d in ("Work/Verda", "Applications/Verda"):
            (self.inside / d).mkdir(parents=True)
            (self.inside / d / "note.md").write_text("in a Verda directory")
        got = {str(p.relative_to(self.inside))
               for p in src(self.inside, exclude_names=("Verda",)).files()}
        self.assertNotIn("Work/Verda/note.md", got)
        self.assertNotIn("Applications/Verda/note.md", got)

    def test_exclude_paths_removes_one_subtree_and_leaves_its_namesakes(self):
        for d in ("Work/Verda", "Applications/Verda"):
            (self.inside / d).mkdir(parents=True)
            (self.inside / d / "note.md").write_text("in a Verda directory")
        got = {str(p.relative_to(self.inside))
               for p in src(self.inside, exclude_paths=("Work/Verda",)).files()}
        self.assertNotIn("Work/Verda/note.md", got)
        self.assertIn("Applications/Verda/note.md", got)

    def test_exclude_paths_tolerates_a_trailing_slash(self):
        (self.inside / "Work" / "Verda").mkdir(parents=True)
        (self.inside / "Work" / "Verda" / "note.md").write_text("x")
        got = {str(p.relative_to(self.inside))
               for p in src(self.inside, exclude_paths=("Work/Verda/",)).files()}
        self.assertNotIn("Work/Verda/note.md", got)

    def test_exclude_paths_is_a_path_not_a_basename(self):
        # `Verda` alone must NOT prune `Work/Verda`. A path entry that quietly
        # behaved like a name entry would reintroduce the bug it exists to fix.
        (self.inside / "Work" / "Verda").mkdir(parents=True)
        (self.inside / "Work" / "Verda" / "note.md").write_text("x")
        got = {str(p.relative_to(self.inside))
               for p in src(self.inside, exclude_paths=("Verda",)).files()}
        self.assertIn("Work/Verda/note.md", got)

    def test_files_outside_root_are_never_yielded(self):
        found = [p for p in src(self.inside).files()]
        self.assertTrue(all("private" not in str(p) for p in found))
        self.assertFalse(any(p.name == "secret.md" for p in found))

    def test_symlink_escape_is_refused(self):
        """A symlink pointing out of the root must not smuggle files in."""
        link = self.inside / "escape.md"
        try:
            link.symlink_to(self.outside / "secret.md")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        found = {p.name for p in src(self.inside).files()}
        self.assertNotIn("escape.md", found,
                         "symlink escaping the declared root was included")

    def test_symlinked_directory_escape_is_refused(self):
        link = self.inside / "sub2"
        try:
            link.symlink_to(self.outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        for p in src(self.inside).files():
            self.assertNotIn("secret.md", p.name)

    def test_contains_rejects_traversal(self):
        s = src(self.inside)
        self.assertFalse(s.contains(self.outside / "secret.md"))
        self.assertFalse(s.contains(self.inside / ".." / "private" / "secret.md"))
        self.assertTrue(s.contains(self.inside / "a.md"))

    def test_exclude_names_skips_subtree(self):
        (self.inside / "Gemini Apps").mkdir()
        (self.inside / "Gemini Apps" / "c.md").write_text("gamma")
        s = src(self.inside, exclude_names=("Gemini Apps",))
        self.assertNotIn("c.md", {p.name for p in s.files()})

    def test_missing_root_yields_nothing_rather_than_raising(self):
        self.assertEqual(list(src(self.base / "nope").files()), [])


# ⚠️ The real manifest's roots resolve ${VAR} from the environment — any
# variable, since the blessed placeholder table is gone — so these tests have to
# supply whatever names THAT file happens to use. Dummy paths are fine: what is
# under test is the manifest's content (no forbidden root, every extractor
# resolves), not whether those directories exist here.
_ENV_FOR_REAL_MANIFEST = {
    "LOOKUP_HOME": "/tmp/lookup-home",
    "ARCHIVE_TAKEOUT": "/tmp/t",
    "ARCHIVE_SOURCES": "/tmp/s",
    "ARCHIVE_CONVERTED": "/tmp/c",
    "ARCHIVE_ROOT": "/tmp/a",
}


class TestManifestValidation(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        os.environ.update(_ENV_FOR_REAL_MANIFEST)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def _write(self, body: str) -> Path:
        p = Path(tempfile.mkdtemp()) / "sources.toml"
        p.write_text(body)
        return p

    def test_rejects_unresolved_placeholder(self):
        p = self._write('[[source]]\nname="x"\nroot="${NOPE}/a"\n'
                        'include=["*.md"]\nextractor="docs.markdown"\n')
        with self.assertRaises(ManifestError):
            load(p)

    def test_rejects_duplicate_names(self):
        e = ('[[source]]\nname="x"\nroot="/tmp"\ninclude=["*.md"]\n'
             'extractor="docs.markdown"\n')
        with self.assertRaises(ManifestError):
            load(self._write(e + e))

    def test_rejects_string_include(self):
        p = self._write('[[source]]\nname="x"\nroot="/tmp"\ninclude="*.md"\n'
                        'extractor="docs.markdown"\n')
        with self.assertRaises(ManifestError):
            load(p)

    def test_real_manifest_loads_and_all_extractors_resolve(self):
        from lookup import config
        from lookup.extract import resolve
        srcs = load(config.MANIFEST)
        self.assertTrue(srcs)
        for s in srcs.values():
            resolve(s.extractor)          # raises KeyError on a typo

    def test_real_manifest_declares_no_forbidden_root(self):
        """External is restricted data: it must appear in no declared root."""
        from lookup import config
        for s in load(config.MANIFEST).values():
            self.assertNotIn("External", str(s.root))


if __name__ == "__main__":
    unittest.main()
