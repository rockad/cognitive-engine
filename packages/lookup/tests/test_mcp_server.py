"""The four Lookup tools over FastMCP, with `docs.visibility` enforced end to end.

In-process for the tool behaviour (a FastMCP `Client` bound straight to
`build_server()`), and one real subprocess for the transport guarantees: loopback
bind only, no `--host` flag, tools answer over HTTP.
"""
from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath

import psutil
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from lookup import config, mcp as lookup_mcp, manifest, pipeline
from lookup.manifest import full_match

MANIFEST = """
[[source]]
name = "open_src"
description = "Company-wide material."
root = "@ROOT@/open"
include = ["*.md"]
extractor = "docs.markdown"
visibility = "open"

[[source]]
name = "gated_src"
description = "A DM-like corpus. A document claiming open here is refused."
root = "@ROOT@/gated"
include = ["*.md"]
extractor = "docs.markdown"
visibility = "gated"

[[source]]
name = "unstated_src"
description = "Declares nothing, so it is handled as gated."
root = "@ROOT@/unstated"
include = ["*.md"]
extractor = "docs.markdown"

[[source]]
name = "mixed_src"
description = "Each document declares its own."
root = "@ROOT@/mixed"
include = ["*.md"]
extractor = "docs.markdown"
visibility = "mixed"
"""

DOCS = {
    "open/roadmap.md": (None, "Roadmap", "zebrafish roadmap openmarker"),
    "gated/dm.md": (None, "Direct message", "zebrafish dm gatedmarker"),
    # Claims open inside a gated source: widening, so ingest must refuse it.
    "gated/leak.md": ("open", "Leaked claim", "zebrafish leak wideningmarker"),
    "unstated/misc.md": (None, "Miscellany", "zebrafish misc unstatedmarker"),
    "mixed/public.md": ("open", "Mixed public", "zebrafish mixedpublic mixedopenmarker"),
    "mixed/private.md": ("gated", "Mixed private", "zebrafish mixedprivate mixedgatedmarker"),
    "mixed/silent.md": (None, "Mixed silent", "zebrafish mixedsilent mixedsilentmarker"),
}


def write_corpus(base: Path) -> Path:
    for rel, (vis, title, body) in DOCS.items():
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        fm = f"---\nvisibility: {vis}\n---\n\n" if vis else ""
        p.write_text(f"{fm}# {title}\n\n{body}\n", encoding="utf-8")
    m = base / "sources.toml"
    m.write_text(MANIFEST.replace("@ROOT@", str(base)), encoding="utf-8")
    return m


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    base = tmp_path_factory.mktemp("lookup-corpus")
    mpath = write_corpus(base)
    saved = (config.DB, config.MANIFEST, lookup_mcp._VISIBILITY, lookup_mcp._REMOTE_SOURCES)
    config.DB, config.MANIFEST = base / "index.db", mpath
    lookup_mcp._VISIBILITY = lookup_mcp._REMOTE_SOURCES = None
    result = pipeline.ingest_all()
    yield {"base": base, "manifest": mpath, "ingest": result}
    config.DB, config.MANIFEST, lookup_mcp._VISIBILITY, lookup_mcp._REMOTE_SOURCES = saved


@pytest.fixture
async def client(corpus):
    async with Client(lookup_mcp.build_server()) as c:
        yield c


async def search(client, **args) -> str:
    r = await client.call_tool("search_archive", {"query": "zebrafish", **args})
    return r.content[0].text


def markers(text: str) -> set[str]:
    return set(re.findall(r"(\w+marker)", text))


async def test_exactly_the_four_lookup_tools(client):
    tools = {t.name: t for t in await client.list_tools()}
    assert set(tools) == {"search_archive", "get_document", "refresh_index", "archive_stats"}
    assert "visibility" in tools["search_archive"].input_schema["properties"]
    assert "gated" in tools["search_archive"].description


async def test_widening_document_is_refused_at_ingest(corpus, client):
    assert corpus["ingest"]["refused"] == 1
    assert "wideningmarker" not in await search(client, limit=10)


async def test_unfiltered_search_labels_every_hit_and_warns(client):
    text = await search(client, limit=10)
    assert markers(text) == {"openmarker", "gatedmarker", "unstatedmarker",
                             "mixedopenmarker", "mixedgatedmarker", "mixedsilentmarker"}
    assert "Not every result below is free to reuse" in text
    labels = re.findall(r"visibility: (\w+)", text)
    assert labels and set(labels) <= {"open", "gated", "unstated"}, "`mixed` must never label a hit"


async def test_open_filter_returns_only_open_documents(client):
    text = await search(client, visibility="open", limit=10)
    assert markers(text) == {"openmarker", "mixedopenmarker"}
    assert set(re.findall(r"visibility: (\w+)", text)) == {"open"}
    assert "Not every result below is free to reuse" not in text


async def test_gated_filter_returns_only_gated_documents(client):
    text = await search(client, visibility="gated", limit=10)
    assert markers(text) == {"gatedmarker", "mixedgatedmarker"}


async def test_unstated_filter_includes_silent_documents_in_mixed_sources(client):
    text = await search(client, visibility="unstated", limit=10)
    assert markers(text) == {"unstatedmarker", "mixedsilentmarker"}


async def test_unrecognised_visibility_filter_is_an_error_not_an_unfiltered_answer(client):
    # Deliberate change from the legacy server, which dropped an unknown value and
    # answered unfiltered. The schema now enumerates the values; a caller who typo'd
    # the filter that keeps gated material out must be told, not handed it.
    with pytest.raises(ToolError):
        await search(client, visibility="public")


async def test_limit_is_bounded_by_the_schema(client):
    with pytest.raises(ToolError):
        await search(client, limit=11)
    with pytest.raises(ToolError):
        await search(client, limit=0)


async def test_single_source_search_filters_too(client):
    text = await search(client, source=["mixed_src"], visibility="open", limit=10)
    assert markers(text) == {"mixedopenmarker"}


async def test_get_document_carries_the_effective_label(client):
    text = await search(client, source=["mixed_src"], limit=10)
    keys = dict(re.findall(r"### (Mixed \w+)\ndate: [^|]*\|\s+doc_key: (\w+)", text))
    assert set(keys) == {"Mixed public", "Mixed private", "Mixed silent"}
    want = {"Mixed public": "open", "Mixed private": "gated", "Mixed silent": "unstated"}
    for title, label in want.items():
        doc = (await client.call_tool("get_document", {"doc_key": keys[title]})).content[0].text
        assert f"visibility: {label}" in doc
        assert "visibility: mixed" not in doc


async def test_get_document_unknown_key(client):
    r = await client.call_tool("get_document", {"doc_key": "nope"})
    assert "No document with doc_key nope" in r.content[0].text


async def test_archive_stats_reports_source_visibility(client):
    text = (await client.call_tool("archive_stats", {})).content[0].text
    rows = {m.group(1): m.group(2) for m in
            re.finditer(r"^\| (\w+_src) \|.*\| (\w+) \|$", text, re.M)}
    assert rows == {"open_src": "open", "gated_src": "gated",
                    "unstated_src": "unstated", "mixed_src": "mixed"}


async def test_refresh_index_reports_a_pass(client):
    text = (await client.call_tool("refresh_index", {})).content[0].text
    assert text.startswith("Re-ingested 4 source(s)")
    assert "0 added" in text


async def test_unmounted_network_volume_is_reported_loudly(corpus, client):
    m = corpus["manifest"]
    original = m.read_text(encoding="utf-8")
    m.write_text(original + f"""
[[source]]
name = "share_src"
description = "A network share the host has not mounted."
root = "{corpus['base']}/not-mounted"
include = ["*.md"]
extractor = "docs.markdown"
visibility = "gated"
[source.volume]
type = "cifs"
device = "//fileserver/share"
""", encoding="utf-8")
    try:
        text = (await client.call_tool("refresh_index", {})).content[0].text
    finally:
        m.write_text(original, encoding="utf-8")
    assert "share_src" in text
    assert "does not mount" in text
    assert "not the same as empty" in text


# ── transport guarantees: a real process ─────────────────────────────────────────


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def http_daemon(corpus, tmp_path_factory):
    home = tmp_path_factory.mktemp("lookup-home")
    port = _free_port()
    env = {**os.environ, "HOME": str(home), "LOOKUP_NO_DOTENV": "1",
           "LOOKUP_DB": str(corpus["base"] / "daemon-index.db"),
           "LOOKUP_MANIFEST": str(corpus["manifest"])}
    proc = subprocess.Popen(
        [sys.executable, "-m", "lookup.cli", "mcp", "--http", "--port", str(port),
         "--reingest-interval", "0"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.time() + 30
    while time.time() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"daemon exited early: {proc.stderr.read().decode()}")
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                break
        time.sleep(0.2)
    else:
        proc.kill()
        pytest.fail("daemon never listened")
    yield {"proc": proc, "port": port, "home": home}
    proc.terminate()
    proc.wait(timeout=10)


async def test_http_daemon_binds_loopback_only(http_daemon):
    conns = psutil.Process(http_daemon["proc"].pid).net_connections(kind="tcp")
    listening = [(c.laddr.ip, c.laddr.port) for c in conns if c.status == psutil.CONN_LISTEN]
    assert listening == [("127.0.0.1", http_daemon["port"])]


async def test_http_daemon_answers_tool_calls_with_visibility_enforced(http_daemon):
    async with Client(f"http://127.0.0.1:{http_daemon['port']}/mcp") as c:
        assert {t.name for t in await c.list_tools()} == {
            "search_archive", "get_document", "refresh_index", "archive_stats"}
        r = await c.call_tool("search_archive", {"query": "zebrafish", "visibility": "open",
                                                 "limit": 10})
    assert markers(r.content[0].text) == {"openmarker", "mixedopenmarker"}


def test_http_daemon_wrote_nothing_to_home(http_daemon):
    assert list(http_daemon["home"].iterdir()) == []


def test_there_is_no_host_flag():
    p = subprocess.run([sys.executable, "-m", "lookup.cli", "mcp", "--http", "--host", "0.0.0.0"],
                       env={**os.environ, "LOOKUP_NO_DOTENV": "1"}, capture_output=True)
    assert p.returncode == 2
    assert b"unrecognized arguments: --host" in p.stderr


# ── defaults and the 3.12 glob ──────────────────────────────────────────────────


def test_default_paths_are_out_of_tree_xdg(tmp_path):
    env = {k: v for k, v in os.environ.items()
           if k not in ("XDG_DATA_HOME", "XDG_CONFIG_HOME") and not k.startswith("LOOKUP_")}
    env.update(HOME=str(tmp_path), LOOKUP_NO_DOTENV="1")
    out = subprocess.run(
        [sys.executable, "-c", "from lookup import config; print(config.DB); print(config.MANIFEST)"],
        env=env, capture_output=True, text=True, check=True).stdout.split()
    assert Path(out[0]) == tmp_path / ".local/share/cognitive-engine/lookup/index.db"
    assert Path(out[1]).name == "sources.example.toml"  # no user manifest yet


GLOB_CASES = [
    ("*.md", "a.md", True), ("*.md", "x/a.md", False),
    ("**/*.md", "a.md", True), ("**/*.md", "a/b/c.md", True), ("**/*.md", "a.txt", False),
    ("a/**/*.md", "a/b/c.md", True), ("a/**/*.md", "a/c.md", True), ("a/**/*.md", "b/c.md", False),
    ("a/**", "a/b/c", True), ("a/**", "a", False),
    ("[ab]*.md", "ab.md", True), ("[ab]*.md", "c.md", False), ("[!a]*.md", "a.md", False),
    ("?.md", "a.md", True), ("?.md", "ab.md", False),
    ("*", ".hidden", True), ("*.md", "A.MD", False),
]


@pytest.mark.parametrize("pattern,path,expected", GLOB_CASES)
def test_glob_matches_pathlib_full_match_semantics(pattern, path, expected):
    assert full_match(PurePosixPath(path), pattern) is expected
    if hasattr(PurePosixPath, "full_match"):  # 3.13+: the reference implementation
        assert PurePosixPath(path).full_match(pattern) is expected
