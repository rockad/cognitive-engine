"""Seam 2, working directory (review F2): a Proposer session runs agent tools in this directory.

It must (1) pass the protected-path rules after normalisation AND after resolving symlinks, and
(2) resolve to a directory under a workspace root declared in the channel profile. No roots declared
means no explicit working directory is accepted.
"""
from __future__ import annotations

import os

import pytest
from h2_profiles import LOCAL, make_profile

from cognitive_harness2 import channels
from cognitive_harness2.upstream_egress import check_proposer

ENV = {"PATH": "/usr/bin"}


@pytest.fixture
def ws(tmp_path_factory):
    """A workspace root with ordinary and protected-looking directories, and symlinks. Short, neutral names."""
    root = tmp_path_factory.mktemp("w") / "ws"
    for d in ("work", "work/sub", ".git", ".git/hooks", "hooks", "guard", "egress", "pkg/hooks",
              "work/.github", ".claude/settings", "elsewhere/inner"):
        (root / d).mkdir(parents=True)
    outside = root.parent / "outside"
    outside.mkdir()
    (root / "to-hooks").symlink_to(root / "hooks")
    (root / "to-git").symlink_to(root / ".git")
    (root / "work" / "to-guard").symlink_to(root / "guard")
    (root / "to-outside").symlink_to(outside)
    (root / "to-work").symlink_to(root / "work")
    (root / "dangling").symlink_to(root / "nowhere")
    (root / "loop-a").symlink_to(root / "loop-b")
    (root / "loop-b").symlink_to(root / "loop-a")
    (root / "a-file").write_text("x")
    return root


def run(cwd, ws, *, roots=None):
    profile = make_profile(roots=[str(ws)] if roots is None else roots)
    return check_proposer(dict(kind="opencode", model="ollama/qwen3:30b", base_url=LOCAL, env=ENV, cwd=cwd),
                          profile=profile, channel="local", classification="open")


def why(d):
    return " | ".join(r for r in d.reasons if "working-directory" in r)


@pytest.mark.parametrize("rel", ["", "work", "work/sub", "elsewhere/inner", "to-work", "to-work/sub", "work/../work"])
def test_a_directory_under_a_declared_root_is_allowed(ws, rel):
    cwd = str(ws / rel) if rel else str(ws)
    d = run(cwd, ws)
    assert d.allowed, d.reasons
    assert d.view["cwd"] == cwd


@pytest.mark.parametrize("rel", [
    ".git", ".git/hooks", "hooks", "guard", "egress", "pkg/hooks", "work/.github", ".claude/settings",
    "hooks/", ".git/", "work/../.git", "work/../hooks/", "work/./../guard",
])
def test_a_protected_directory_is_refused(ws, rel):
    d = run(str(ws / rel), ws)
    assert not d.allowed and "protected" in why(d), d.reasons


@pytest.mark.parametrize("rel", ["to-hooks", "to-git", "work/to-guard"])
def test_a_symlink_into_a_protected_directory_is_refused(ws, rel):
    d = run(str(ws / rel), ws)
    assert not d.allowed and "resolves to" in why(d) and "protected" in why(d), d.reasons


def test_a_symlink_out_of_the_workspace_root_is_refused(ws):
    d = run(str(ws / "to-outside"), ws)
    assert not d.allowed and "outside every declared workspace root" in why(d)


@pytest.mark.parametrize("rel", ["nope", "dangling", "loop-a", "a-file", "work/missing/deeper"])
def test_a_missing_dangling_looping_or_non_directory_is_refused(ws, rel):
    assert not run(str(ws / rel), ws).allowed


def test_a_sibling_with_a_common_prefix_is_not_under_the_root(ws):
    sibling = ws.parent / "ws-extra"
    sibling.mkdir()
    d = run(str(sibling), ws)
    assert not d.allowed and "outside every declared workspace root" in why(d)


def test_the_parent_of_the_root_is_outside_it(ws):
    assert not run(str(ws.parent), ws).allowed


def test_no_declared_root_means_no_explicit_working_directory(ws):
    d = run(str(ws / "work"), ws, roots=[])
    assert not d.allowed and "no workspace_roots" in why(d)


def test_a_root_that_does_not_exist_grants_nothing(ws):
    d = run(str(ws / "work"), ws, roots=[str(ws.parent / "gone")])
    assert not d.allowed and "outside every declared workspace root" in why(d)


def test_a_root_that_is_a_symlink_is_compared_resolved(ws):
    d = run(str(ws / "work" / "sub"), ws, roots=[str(ws / "to-work")])
    assert d.allowed, d.reasons


def test_any_of_several_roots_will_do(ws):
    d = run(str(ws / "elsewhere"), ws, roots=[str(ws / "work"), str(ws / "elsewhere")])
    assert d.allowed


@pytest.mark.parametrize("cwd", ["", "work", "./work", "../x", "/", "~", "/tmp/a\x00b", "/tmp/a\nb", "/tmp/a‮b", " "])
def test_text_rules_still_apply(ws, cwd):
    assert not run(cwd, ws).allowed


def test_home_and_its_ancestors_are_refused_even_inside_a_root(ws, monkeypatch):
    home = ws / "work"
    monkeypatch.setenv("HOME", str(home))
    roots = [str(home / "sub")]                         # a root under home is fine; home itself or above is not
    d = run(str(home), ws, roots=roots)
    assert not d.allowed and "home directory" in why(d)
    assert run(str(home / "sub"), ws, roots=roots).allowed
    assert not run(str(ws), ws, roots=roots).allowed    # an ancestor of home


def test_no_working_directory_means_the_fresh_temp_dir_upstream_makes(ws):
    d = check_proposer(dict(kind="opencode", model="ollama/qwen3:30b", base_url=LOCAL, env=ENV),
                       profile=make_profile(roots=[]), channel="local", classification="open")
    assert d.allowed and d.view["cwd"] == "fresh temporary directory (upstream default)"


def test_the_decision_never_shows_a_refused_path_in_the_view(ws):
    d = run(str(ws / "to-hooks"), ws)
    assert d.view["cwd"] is None


# ── workspace_roots in the profile itself ─────────────────────────────────────────

@pytest.mark.parametrize("root", ["/x/.git", "/x/hooks", "/x/guard/y", "/", "relative/path",
                                  "/x/../y", "/x//y", "/x/y/", "/x/\x07"])
def test_a_protected_or_malformed_root_is_malformed_in_the_profile(root):
    with pytest.raises(channels.ChannelProfileError):
        make_profile(roots=[root])


def test_the_realpath_check_does_not_follow_a_race_to_a_different_target(ws):
    # resolving happens once per decision on the path given: a path that resolves under the root is accepted,
    # and the Decision carries the path as given (the caller must pass the same string to the session)
    d = run(str(ws / "to-work"), ws)
    assert d.allowed and d.view["cwd"] == str(ws / "to-work")
    assert os.path.realpath(d.view["cwd"]) == os.path.realpath(ws / "work")
