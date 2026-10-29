"""Protected-path matching: files, the directories that hold them, everything under those
directories, and every spelling an attacker could use to dodge a string match. Default-deny."""
import pytest

from cognitive_harness2 import invariants
from cognitive_harness2.invariants import InvariantViolation, assert_path_unprotected


def refused(path):
    with pytest.raises(InvariantViolation):
        assert_path_unprotected(path)


# Each protected prefix, spelled as a file, as a directory, and as a child of that directory.
PROTECTED = [
    # (the protected name, is it a directory that holds other things)
    "hooks", "hook", "guard", "hooks.json", "hook.ts", "guard.ts", ".claude/settings", ".claude/settings.json",
    "mcp_config.json", ".mcp.json", "egress", "egress_policy.py", "zdr", "governance", "ceiling", "approval",
]


@pytest.mark.parametrize("name", PROTECTED)
@pytest.mark.parametrize("shape", [
    "{p}",                     # bare, as a file or as the directory itself
    "{p}/",                    # directory with a trailing slash
    "{p}/child.txt",           # a file under it
    "{p}/a/b/c.py",            # deep child
    "pkg/{p}",                 # nested
    "pkg/{p}/",
    "pkg/{p}/child.txt",
    "packages/jev/src/{p}/index.ts",
])
def test_every_protected_prefix_as_file_dir_and_child_is_refused(name, shape):
    refused(shape.format(p=name))


# The reported bypasses, verbatim.
@pytest.mark.parametrize("path", [
    "packages/jev/src/guard/index.ts",     # guard.* needed a dot
    "hooks",                               # */hooks/* and hooks/* needed a child
    "adapters/claude/hooks",
    "x/hooks",
    "x/hooks/",
    "packages/jev/src/guard",
    "packages/jev/src/hook",
    "packages/jev/src/hook/run.ts",
])
def test_reported_directory_bypasses_are_closed(path):
    refused(path)


@pytest.mark.parametrize("path", [
    "hooks/",                              # trailing slash
    "./hooks", "././hooks/x",              # ./ prefix
    "a/../hooks", "a/b/../../hooks/x", "a/./hooks/./x",   # .. and . segments
    "a//hooks", "hooks//x", "a///hooks///x",              # doubled slashes
    "hooks\\x", "a\\hooks", "a\\..\\hooks\\x", "pkg\\guard\\index.ts",   # backslashes
    "HOOKS", "Hooks/X", "PACKAGES/JEV/SRC/GUARD/INDEX.TS", "HoOkS.JsOn",  # mixed case
    "ｈｏｏｋｓ", "ｈｏｏｋｓ/x", "pkg/ｇｕａｒｄ/index.ts", "ＺＤＲ.toml",         # fullwidth, folded by NFKC
    "ﬁle/hooks",                           # ligature in an unrelated segment, protected part intact
    "hooks ", " hooks", "hooks/ ", "a/hooks /x", "a/hooks. /x", "a/hooks../x",   # padding dots and spaces
    ".hooks", ".guard/x",                  # hidden variants
    "hooks.lnk", "hooks.bak/x", "guard.d/x", "hooks.old",   # symlink / backup looking names keep the stem
    "hooks.json/child",                    # a file name used as a directory
    "mcp_config.json/x", "a/.mcp.json/x",
])
def test_spelling_tricks_are_refused(path):
    refused(path)


@pytest.mark.parametrize("path", [
    "/hooks", "/etc/passwd", "//hooks", "//etc/passwd", "///x", "/", "\\hooks", "\\\\server\\share",
    "C:\\hooks", "c:/x", "D:x",
])
def test_absolute_and_drive_paths_are_refused(path):
    refused(path)


@pytest.mark.parametrize("path", [
    "..", "../x", "../../escape", "a/../../x", "..\\x", "a/b/../../..", "./..", "ｘ/../..",
])
def test_escapes_are_refused(path):
    refused(path)


@pytest.mark.parametrize("path", ["", " ", ".", "./", "a/..", "./a/../", "\\"])
def test_the_tree_root_and_empty_paths_are_refused(path):
    refused(path)


@pytest.mark.parametrize("path", [
    "a\0b", "hooks\0", "x/\0", "a\nb", "a\tb", "a\rb", "a\x1bb", "a\x7fb",
    "ho\u200boks", "ho\u200doks", "a\u202eb", "a\ufeffb",     # zero width and bidi controls hide names
])
def test_control_and_invisible_characters_are_refused(path):
    refused(path)


@pytest.mark.parametrize("path", [
    "docs/prompt.md", "prompts/system.txt", "src/decompose.py", "src/hookup.py", "src/guardian/notes.md",
    "src/shooks/x.py", "a/b/c.py", "prompt.md", ".claude/agents/x.md", "README.md", "src/cognitive/context_assembly.py",
])
def test_ordinary_paths_still_pass_and_come_back_normalised(path):
    assert assert_path_unprotected(path) == path.lower()


def test_the_normalised_form_is_returned():
    assert assert_path_unprotected("./Docs//Sub/../Prompt.MD") == "docs/prompt.md"
    assert assert_path_unprotected("ＤＯＣＳ/ｎｏｔｅ.md") == "docs/note.md"


def test_unicode_that_nfkc_does_not_fold_is_a_different_name_and_stays_allowed():
    # Cyrillic о is not folded to Latin o by NFKC; it names a different directory, so it is not
    # the protected one. The rule is "protect what the name says", not "guess intent".
    assert assert_path_unprotected("h\u043e\u043eks/x.py")


def test_no_pattern_table_is_left_to_widen_by_accident():
    assert not hasattr(invariants, "PROTECTED_PATH_PATTERNS")


# Git control files: `.git/config` can set core.hooksPath or a filter driver, `.gitmodules` and
# `.gitattributes` redirect what git runs, `.github` holds CI that runs with the repo's secrets.
GIT_NAMES = [".git", ".gitmodules", ".gitattributes", ".github"]


@pytest.mark.parametrize("name", GIT_NAMES)
@pytest.mark.parametrize("shape", [
    "{p}",                       # as a file (a worktree's `.git` is a file) or the directory itself
    "{p}/",                      # as a directory
    "{p}/config",                # as a parent
    "{p}/a/b/c.yml",             # deep child
    "pkg/{p}", "pkg/{p}/", "pkg/{p}/x", "packages/jev/{p}/hooks/pre-commit",
    "./{p}", "././{p}/x", "a/../{p}", "a/b/../../{p}/x", "a/./{p}/./x",       # . and .. segments
    "a//{p}", "{p}//x",                                                        # doubled slashes
    "{p}\\x", "a\\{p}", "a\\..\\{p}\\x",                                       # backslashes
    "{p} ", " {p}", "{p}.", "{p}..", "{p}. ", "a/{p} /x", "a/{p}. /x", "a/{p}../x",   # trailing dots and spaces
])
def test_git_control_files_and_dirs_are_refused_in_every_spelling(name, shape):
    refused(shape.format(p=name))


@pytest.mark.parametrize("path", [
    ".GIT/config", ".Git/Config", ".GITMODULES", ".GitAttributes", ".GITHUB/workflows/ci.yml", "pkg/.GiT/hooks/x",
    "．git/config", "．github/x", ".ｇｉｔ/config", ".ｇｉｔｍｏｄｕｌｅｓ", "ｐｋｇ/.ｇｉｔ/x",    # fullwidth, folded by NFKC
    ".git​/config", ".g​it/config", ".git‮/x",                                        # invisible characters
    "GIT~1/config", "pkg/git~1/x", ".GIT~2",                                                         # 8.3 short names
    ".git/", ".git\\", "x/.git\\config",
])
def test_git_control_spellings_that_dodge_a_string_match_are_refused(path):
    refused(path)


@pytest.mark.parametrize("path", [
    ".gitignore", "pkg/.gitignore", "src/gitutils.py", "docs/git.md", "git/readme.md", "src/digit/x.py",
    ".githooks-docs/x", "legit/x", ".gitkeep", "github/x", "src/.github-notes.md", ".git-blame-ignore-revs",
])
def test_names_that_merely_contain_git_still_pass(path):
    assert assert_path_unprotected(path) == path.lower()
