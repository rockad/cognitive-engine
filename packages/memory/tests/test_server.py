"""End to end: `cognitive-memory serve` as a real subprocess, driven over MCP.

One daemon for the whole module. The first search downloads the fastembed models
into `<root>/config/fastembed_cache`, so this needs network once per fresh root;
that is also the proof the cache stays out of tree.
"""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest
from fastmcp import Client

RAM_CEILING_BYTES = 3 * 1024**3
EXPECTED_TOOLS = {"read_note", "write_note", "search_notes", "list_directory",
                  "delete_note", "build_context"}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def daemon(tmp_path_factory):
    base = tmp_path_factory.mktemp("memory-e2e")
    root, home = base / "state", base / "home"
    home.mkdir()
    port = free_port()
    env = {**os.environ, "HOME": str(home), "COGNITIVE_MEMORY_ROOT": str(root),
           "COGNITIVE_MEMORY_PORT": str(port)}
    for k in [k for k in env if k.startswith("BASIC_MEMORY_")]:
        del env[k]
    log = open(base / "daemon.log", "wb")
    proc = subprocess.Popen([sys.executable, "-m", "cognitive_memory.cli", "serve"],
                            env=env, stdout=log, stderr=subprocess.STDOUT)
    deadline = time.time() + 60
    while time.time() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"daemon exited early:\n{(base / 'daemon.log').read_text()}")
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                break
        time.sleep(0.25)
    else:
        proc.kill()
        pytest.fail("daemon never listened")
    yield {"proc": proc, "port": port, "root": root, "home": home}
    proc.terminate()
    proc.wait(timeout=20)
    log.close()


def client(d) -> Client:
    return Client(f"http://127.0.0.1:{d['port']}/mcp")


async def test_binds_loopback_only(daemon):
    conns = psutil.Process(daemon["proc"].pid).net_connections(kind="tcp")
    listening = [c.laddr for c in conns if c.status == psutil.CONN_LISTEN]
    assert [(a.ip, a.port) for a in listening] == [("127.0.0.1", daemon["port"])]


async def test_lists_the_required_tools(daemon):
    async with client(daemon) as c:
        names = {t.name for t in await c.list_tools()}
    assert EXPECTED_TOOLS <= names
    # The spec's `list_notes` does not exist upstream; `list_directory` is its equivalent.
    assert "list_notes" not in names


async def test_note_lifecycle(daemon):
    async with client(daemon) as c:
        w = await c.call_tool("write_note", {
            "title": "Reranker budget", "directory": "tests", "project": "main",
            "content": "# Reranker budget\n\nThe ONNX arena is capped by candidates "
                       "and document characters."})
        assert "permalink: main/tests/reranker-budget" in w.content[0].text

        r = await c.call_tool("read_note", {"identifier": "main/tests/reranker-budget",
                                            "project": "main"})
        assert "ONNX arena" in r.content[0].text

        s = await c.call_tool("search_notes", {"query": "onnx arena candidates",
                                               "project": "main"})
        assert "Reranker budget" in s.content[0].text

        ls = await c.call_tool("list_directory", {"dir_name": "/tests", "project": "main"})
        assert "Reranker budget" in ls.content[0].text

        ctx = await c.call_tool("build_context", {"url": "memory://main/tests/reranker-budget",
                                                  "project": "main"})
        assert not ctx.is_error

        d = await c.call_tool("delete_note", {"identifier": "main/tests/reranker-budget",
                                              "project": "main"})
        assert "true" in d.content[0].text.lower() or "deleted" in d.content[0].text.lower()
        gone = await c.call_tool("search_notes", {"query": "onnx arena", "project": "main"})
        assert "Reranker budget" not in gone.content[0].text


async def test_state_stays_under_root_and_out_of_home(daemon):
    root, home = daemon["root"], daemon["home"]
    assert (root / "config" / "config.json").is_file()
    assert (root / "config" / "memory.db").is_file()
    assert (root / "config" / "fastembed_cache").is_dir()
    assert (root / "notes").is_dir()
    assert list(home.iterdir()) == [], "upstream wrote into $HOME"


async def test_reranker_is_actually_enabled_with_tuned_bounds(daemon):
    import json
    cfg = json.loads((daemon["root"] / "config" / "config.json").read_text())
    # config.json records what upstream resolved; env overrides win at runtime, so
    # assert on the process environment the wrapper handed over.
    env = Path(f"/proc/{daemon['proc'].pid}/environ").read_bytes().split(b"\0")
    env = dict(e.decode().split("=", 1) for e in env if b"=" in e)
    assert env["BASIC_MEMORY_RERANKER_ENABLED"] == "true"
    assert env["BASIC_MEMORY_RERANKER_CANDIDATES"] == "10"
    assert env["BASIC_MEMORY_RERANKER_MAX_DOCUMENT_CHARS"] == "1000"
    assert cfg.get("auto_update") in (False, None) or env["BASIC_MEMORY_AUTO_UPDATE"] == "false"


@pytest.mark.skipif(sys.platform != "linux", reason="RSS read from psutil is fine, but budget measured on linux")
async def test_sustained_searches_stay_under_the_ram_ceiling(daemon):
    async with client(daemon) as c:
        for i in range(8):
            await c.call_tool("write_note", {
                "title": f"Sustained {i}", "directory": "sustained", "project": "main",
                "content": f"Note {i}. " + ("retrieval reranker embedding corpus " * 60)})
        for i in range(40):
            await c.call_tool("search_notes", {"query": f"reranker embedding corpus {i % 8}",
                                               "project": "main"})
    proc = psutil.Process(daemon["proc"].pid)
    rss = proc.memory_info().rss + sum(c.memory_info().rss for c in proc.children(recursive=True))
    print(f"daemon RSS after 40 searches: {rss / 1024**2:.0f} MiB")
    assert rss < RAM_CEILING_BYTES
