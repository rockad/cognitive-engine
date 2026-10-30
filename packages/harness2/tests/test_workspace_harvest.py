"""Reading a workspace back: every refusal rule, on real files (lstat only, nothing followed)."""
from __future__ import annotations

import os
import socket

import pytest

from cognitive_harness2.workspace_harvest import HarvestLimits, inspect_workspace


def w(root, rel, data=b"x", mode=0o644):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    p.chmod(mode)
    return p


def rules(rep):
    return sorted(f.rule for f in rep.findings)


def test_ordinary_files_are_accepted(tmp_path):
    w(tmp_path, "prompt.md", b"hello")
    w(tmp_path, "skills/a.md", b"world")
    rep = inspect_workspace(str(tmp_path))
    assert rep.ok and rep.files == ("prompt.md", "skills/a.md") and rep.total_bytes == 10


def test_fifos_sockets_are_refused_and_never_opened(tmp_path):
    os.mkfifo(tmp_path / "pipe")
    s = socket.socket(socket.AF_UNIX)
    s.bind(str(tmp_path / "sock"))
    s.close()
    rep = inspect_workspace(str(tmp_path))
    assert rules(rep) == ["special-file", "special-file"] and rep.files == ()


def test_symlinks_that_escape_are_refused_and_inner_ones_listed_not_followed(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("S")
    ws = tmp_path / "ws"
    ws.mkdir()
    w(ws, "real.md")
    os.symlink("real.md", ws / "inner")
    os.symlink("../outside/secret", ws / "up")
    os.symlink(str(outside / "secret"), ws / "abs")
    os.symlink("/etc/passwd", ws / "etc")
    os.symlink("..", ws / "dotdot")
    os.symlink("sub/../../outside", ws / "sneaky")
    rep = inspect_workspace(str(ws))
    assert rules(rep) == ["symlink-escape"] * 5 and rep.links == ("inner",) and rep.files == ("real.md",)


def test_a_symlinked_directory_is_not_descended_into(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    w(outside, "a.md")
    ws = tmp_path / "ws"
    ws.mkdir()
    os.symlink(str(outside), ws / "d")
    rep = inspect_workspace(str(ws))
    assert rep.files == () and rules(rep) == ["symlink-escape"]


def test_suid_sgid_sticky_and_executable_files_are_refused(tmp_path):
    w(tmp_path, "suid", mode=0o4644)
    w(tmp_path, "sgid", mode=0o2644)
    w(tmp_path, "sticky", mode=0o1644)
    w(tmp_path, "run.sh", mode=0o755)
    w(tmp_path, "ok.md")
    rep = inspect_workspace(str(tmp_path))
    assert rep.files == ("ok.md",) and rules(rep) == ["executable", "suid-bit", "suid-bit", "suid-bit"]
    assert inspect_workspace(str(tmp_path), HarvestLimits(allow_exec=True)).files == ("ok.md", "run.sh")


def test_hardlinks_and_oversized_files(tmp_path):
    a = w(tmp_path, "a.md")
    os.link(a, tmp_path / "b.md")
    w(tmp_path, "big.md", b"x" * (512 * 1024 + 1))
    w(tmp_path, "edge.md", b"x" * (512 * 1024))
    rep = inspect_workspace(str(tmp_path))
    assert rep.files == ("edge.md",) and rules(rep) == ["file-too-large", "hardlink", "hardlink"]


def test_count_depth_and_total_caps(tmp_path):
    for i in range(10):
        w(tmp_path, f"f{i}.md")
    assert "too-many-files" in rules(inspect_workspace(str(tmp_path), HarvestLimits(max_files=5)))
    deep = tmp_path / "d"
    w(deep / "1" / "2" / "3", "x.md")
    assert "too-deep" in rules(inspect_workspace(str(tmp_path), HarvestLimits(max_depth=2)))
    assert "total-too-large" in rules(inspect_workspace(str(tmp_path), HarvestLimits(max_total_bytes=5)))


@pytest.mark.parametrize("name", ["\x1b[2Jclear.md", "tab\there.md", "nl\nname.md", "back\\slash.md", " lead.md",
                                  "trail.md ", "bell\x07.md", "zero​width.md"])
def test_hostile_names_are_refused_and_shown_escaped(tmp_path, name):
    try:
        w(tmp_path, name)
    except OSError:
        pytest.skip("filesystem refuses this name")
    rep = inspect_workspace(str(tmp_path))
    assert rules(rep) == ["bad-name"] and rep.files == ()
    assert all("\x1b" not in f.path and "\n" not in f.path for f in rep.findings)


def test_undecodable_file_names_are_refused(tmp_path):
    os.close(os.open(os.fsencode(str(tmp_path)) + b"/\xff\xfe.md", os.O_CREAT | os.O_WRONLY))
    assert rules(inspect_workspace(str(tmp_path))) == ["bad-name"]


@pytest.mark.parametrize("rel", [".git/config", "hooks/pre-commit", "a/hooks.json", ".github/workflows/x.yml",
                                 "egress.md", ".claude/settings.json", "x/approval.txt"])
def test_protected_paths_are_refused(tmp_path, rel):
    w(tmp_path, rel)
    assert "protected-path" in rules(inspect_workspace(str(tmp_path)))


def test_a_credential_value_in_a_file_is_refused_without_naming_it(tmp_path):
    w(tmp_path, "notes.md", b"the token is TOKEN-VALUE-12345 ok")
    w(tmp_path, "fine.md", b"nothing here")
    rep = inspect_workspace(str(tmp_path), credentials=["TOKEN-VALUE-12345"])
    assert rep.files == ("fine.md",) and rules(rep) == ["credential-in-file"]
    assert "TOKEN-VALUE" not in repr(rep)


def test_an_unreadable_directory_is_a_finding_not_a_crash(tmp_path):
    d = tmp_path / "locked"
    d.mkdir()
    w(d, "a.md")
    d.chmod(0)
    try:
        rep = inspect_workspace(str(tmp_path))
    finally:
        d.chmod(0o755)
    assert "unreadable" in rules(rep)


def test_the_root_must_be_a_directory(tmp_path):
    f = w(tmp_path, "f")
    with pytest.raises(NotADirectoryError):
        inspect_workspace(str(f))


# ── protected files that were STAGED by the caller: unchanged is fine, anything else is not ──────

from cognitive_harness2.workspace_harvest import accept_unchanged_protected, protected_changes, protected_manifest  # noqa: E402


def staged(tmp_path):
    w(tmp_path, "context/harness/systemprompt.md", b"prompt")
    w(tmp_path, "context/harness/guardrails.md", b"never guess")
    w(tmp_path, "context/harness/hooks/x.md", b"hook text")
    return protected_manifest(str(tmp_path))


def test_the_manifest_lists_protected_named_regular_files_by_hash(tmp_path):
    m = staged(tmp_path)
    assert set(m) == {"context/harness/guardrails.md", "context/harness/hooks/x.md"}
    assert all(len(v) == 64 for v in m.values())


def test_unchanged_staged_protected_files_are_accepted_and_returned_as_files(tmp_path):
    before = staged(tmp_path)
    w(tmp_path, "output/answer.md", b"done")
    rep = inspect_workspace(str(tmp_path))
    assert rules(rep) == ["protected-path", "protected-path"] and "context/harness/guardrails.md" not in rep.files
    after = protected_manifest(str(tmp_path))
    assert protected_changes(before, after) == []
    fixed = accept_unchanged_protected(rep, before, after)
    assert fixed.ok and {"context/harness/guardrails.md", "context/harness/hooks/x.md"} <= set(fixed.files)
    assert "output/answer.md" in fixed.files and fixed.files == tuple(sorted(fixed.files))


@pytest.mark.parametrize("how", ["edit", "delete", "replace-by-link", "make-exec", "hardlink"])
def test_a_changed_or_removed_staged_protected_file_is_never_accepted(tmp_path, how):
    before = staged(tmp_path)
    g = tmp_path / "context/harness/guardrails.md"
    if how == "edit":
        g.write_bytes(b"always guess")
    elif how == "delete":
        g.unlink()
    elif how == "replace-by-link":
        g.unlink()
        g.symlink_to("systemprompt.md")
    elif how == "make-exec":
        g.chmod(0o755)
    else:
        os.link(g, tmp_path / "copy.md")
    rep = inspect_workspace(str(tmp_path))
    after = protected_manifest(str(tmp_path))
    assert protected_changes(before, after) == ["context/harness/guardrails.md"]
    fixed = accept_unchanged_protected(rep, before, after)
    assert "context/harness/guardrails.md" not in fixed.files
    if how != "delete":
        assert any(f.rule == "protected-path" and f.path == "context/harness/guardrails.md" for f in fixed.findings)


def test_a_new_protected_name_is_still_a_finding(tmp_path):
    before = staged(tmp_path)
    w(tmp_path, "context/harness/guard.md", b"new")
    w(tmp_path, ".git/config", b"[x]")
    rep = inspect_workspace(str(tmp_path))
    fixed = accept_unchanged_protected(rep, before, protected_manifest(str(tmp_path)))
    bad = sorted(f.path for f in fixed.findings if f.rule == "protected-path")
    assert bad == [".git", "context/harness/guard.md"]


def test_a_protected_directory_is_accepted_only_if_everything_under_it_is_unchanged(tmp_path):
    before = staged(tmp_path)
    w(tmp_path, "context/harness/hooks/extra.md", b"planted")
    rep = inspect_workspace(str(tmp_path))
    after = protected_manifest(str(tmp_path))
    fixed = accept_unchanged_protected(rep, before, after)
    assert any(f.path == "context/harness/hooks" for f in fixed.findings)
    assert "context/harness/hooks/x.md" not in fixed.files


def test_an_empty_manifest_changes_nothing(tmp_path):
    w(tmp_path, "a.md", b"a")
    w(tmp_path, ".git/config", b"x")
    rep = inspect_workspace(str(tmp_path))
    assert accept_unchanged_protected(rep, {}, protected_manifest(str(tmp_path))) == rep
