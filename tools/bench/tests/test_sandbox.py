import os

import pytest

from ollama_bench.sandbox import Sandbox
from ollama_bench.tools import validate_args


@pytest.fixture
def sb(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return Sandbox(root)


@pytest.mark.parametrize("path", ["/etc/passwd", "../x", "a/../../x", "..", "a/..", "C:\\x", "\\x", "a\0b", "/"])
@pytest.mark.parametrize("tool,args", [
    ("read_file", {}), ("write_file", {"content": "x"}), ("edit_file", {"old_string": "a", "new_string": "b"}),
    ("list_dir", {})])
def test_path_escapes_refused_by_every_tool(sb, tool, args, path):
    out = sb.execute(tool, {"path": path, **args})
    assert not out.ok and out.category == "escape", (tool, path, out)
    assert sb.escape_attempts == 1
    assert sb.written == []


def test_symlink_escape_refused(sb, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    os.symlink(outside, sb.root / "link")
    os.symlink(outside / "secret.txt", sb.root / "filelink")
    assert sb.execute("read_file", {"path": "link/secret.txt"}).category == "escape"
    assert sb.execute("read_file", {"path": "filelink"}).category == "escape"
    assert sb.execute("write_file", {"path": "link/new.txt", "content": "x"}).category == "escape"
    assert not (outside / "new.txt").exists()
    assert sb.execute("list_dir", {"path": "link"}).category == "escape"


def test_write_creates_parents_and_reads_back(sb):
    out = sb.execute("write_file", {"path": "output/answer.txt", "content": "Total: 1.00\n"})
    assert out.ok and (sb.root / "output/answer.txt").read_text() == "Total: 1.00\n"
    assert sb.written == ["output/answer.txt"]
    assert sb.execute("read_file", {"path": "output/answer.txt"}).text == "Total: 1.00\n"
    assert sb.execute("read_file", {"path": "./output/answer.txt"}).ok


def test_append_and_read_window(sb):
    sb.execute("write_file", {"path": "f.txt", "content": "a\nb\n"})
    sb.execute("write_file", {"path": "f.txt", "content": "c\n", "mode": "append"})
    assert sb.execute("read_file", {"path": "f.txt"}).text == "a\nb\nc\n"
    assert sb.execute("read_file", {"path": "f.txt", "offset": 2, "limit": 1}).text == "b\n"


def test_edit_semantics(sb):
    sb.execute("write_file", {"path": "f.txt", "content": "x = 1\nx = 1\ny = 2\n"})
    dup = sb.execute("edit_file", {"path": "f.txt", "old_string": "x = 1", "new_string": "x = 9"})
    assert not dup.ok and dup.category == "not_unique"
    assert sb.execute("edit_file", {"path": "f.txt", "old_string": "nope", "new_string": "z"}).category == "not_found"
    ok = sb.execute("edit_file", {"path": "f.txt", "old_string": "y = 2", "new_string": "y = 3"})
    assert ok.ok and "y = 3" in (sb.root / "f.txt").read_text()
    allr = sb.execute("edit_file", {"path": "f.txt", "old_string": "x = 1", "new_string": "x = 9", "replace_all": True})
    assert allr.ok and (sb.root / "f.txt").read_text() == "x = 9\nx = 9\ny = 3\n"
    assert sb.execute("edit_file", {"path": "missing.txt", "old_string": "a", "new_string": "b"}).category == "not_found"


def test_list_dir(sb):
    sb.execute("write_file", {"path": "a/b/c.txt", "content": "x"})
    sb.execute("write_file", {"path": "top.txt", "content": "x"})
    assert sb.execute("list_dir", {"path": "."}).text.splitlines() == ["a/", "top.txt"]
    assert sb.execute("list_dir", {"path": ""}).ok
    rec = sb.execute("list_dir", {"path": ".", "recursive": True}).text.splitlines()
    assert "a/b/" in rec and "a/b/c.txt" in rec
    assert sb.execute("list_dir", {"path": "nope"}).category == "not_found"


def test_bash_and_todo_are_unavailable_and_bad_args_rejected(sb):
    for name in ("bash", "todo", "curl"):
        out = sb.execute(name, {"command": "ls"})
        assert not out.ok and out.category == "unavailable"
    assert sb.execute("read_file", {}).category == "bad_args"
    assert sb.execute("read_file", {"path": 3}).category == "bad_args"
    assert sb.execute("write_file", {"path": "f", "content": "x", "mode": "truncate"}).category == "bad_args"
    assert sb.execute("read_file", "not a dict").category == "bad_args"


def test_validate_args_types_enums_unknown():
    assert validate_args("todo", {"action": "add", "item": "x"}) == []
    assert validate_args("todo", {"action": "delete"})
    assert validate_args("read_file", {"path": "a", "limit": "20"})
    assert validate_args("read_file", {"path": "a", "limit": True})
    assert validate_args("read_file", {"path": "a", "bogus": 1})
    assert validate_args("nope", {})
    assert validate_args("list_dir", {"path": "a", "recursive": "true"})
