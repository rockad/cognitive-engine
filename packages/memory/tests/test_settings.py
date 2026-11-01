"""The wrapper's policy: bounds, out-of-tree state, loopback-only, pinned defaults."""
from pathlib import Path

import pytest

from cognitive_memory import settings
from cognitive_memory.settings import SettingsError, from_env


def env(tmp_path, **extra):
    return {settings.ENV_ROOT: str(tmp_path / "mem"), **extra}


def test_defaults_are_the_tuned_values_not_upstreams(tmp_path):
    s = from_env(env(tmp_path))
    assert (s.candidates, s.max_document_chars, s.port) == (10, 1000, 8105)
    e = settings.basic_memory_env(s)
    # Upstream 0.24.1 ships reranker off, 20 candidates, 2000 chars, auto_update on.
    assert e["BASIC_MEMORY_RERANKER_ENABLED"] == "true"
    assert e["BASIC_MEMORY_RERANKER_CANDIDATES"] == "10"
    assert e["BASIC_MEMORY_RERANKER_MAX_DOCUMENT_CHARS"] == "1000"
    assert e["BASIC_MEMORY_AUTO_UPDATE"] == "false"
    assert e["BASIC_MEMORY_SEMANTIC_SEARCH_ENABLED"] == "true"
    assert e["BASIC_MEMORY_SEMANTIC_EMBEDDING_MODEL"] == "bge-small-en-v1.5"
    assert e["BASIC_MEMORY_RERANKER_MODEL"] == "jinaai/jina-reranker-v1-tiny-en"


@pytest.mark.parametrize("value", ["5", "7", "10"])
def test_candidates_accepts_the_range(tmp_path, value):
    s = from_env(env(tmp_path, BASIC_MEMORY_RERANKER_CANDIDATES=value))
    assert s.candidates == int(value)


@pytest.mark.parametrize("value", ["4", "11", "20", "0", "-1", "ten", "5.5"])
def test_candidates_outside_the_range_is_refused_not_clamped(tmp_path, value):
    with pytest.raises(SettingsError, match="RERANKER_CANDIDATES"):
        from_env(env(tmp_path, BASIC_MEMORY_RERANKER_CANDIDATES=value))


@pytest.mark.parametrize("value", ["500", "750", "1000"])
def test_max_document_chars_accepts_the_range(tmp_path, value):
    s = from_env(env(tmp_path, BASIC_MEMORY_RERANKER_MAX_DOCUMENT_CHARS=value))
    assert s.max_document_chars == int(value)


@pytest.mark.parametrize("value", ["499", "1001", "2000", "0", "x"])
def test_max_document_chars_outside_the_range_is_refused(tmp_path, value):
    with pytest.raises(SettingsError, match="MAX_DOCUMENT_CHARS"):
        from_env(env(tmp_path, BASIC_MEMORY_RERANKER_MAX_DOCUMENT_CHARS=value))


def test_state_is_rooted_out_of_tree_and_upstream_dirs_are_redirected(tmp_path):
    s = from_env(env(tmp_path))
    e = settings.basic_memory_env(s)
    root = (tmp_path / "mem").resolve()
    assert e["BASIC_MEMORY_CONFIG_DIR"] == str(root / "config")
    assert e["BASIC_MEMORY_HOME"] == str(root / "notes")
    assert e["BASIC_MEMORY_PROJECT_ROOT"] == str(root)
    assert e["XDG_CACHE_HOME"] == str(root / "cache")
    assert e["XDG_DATA_HOME"] == str(root / "data")
    assert e["FASTMCP_CHECK_FOR_UPDATES"] == "off"


def test_default_root_is_the_xdg_data_path():
    assert settings.DEFAULT_ROOT == Path("~/.local/share/cognitive-engine/memory")
    s = from_env({})
    assert s.root == (Path.home() / ".local/share/cognitive-engine/memory").resolve()


def test_root_inside_the_git_checkout_is_refused():
    repo = Path(__file__).resolve().parents[3]
    with pytest.raises(SettingsError, match="inside the git checkout"):
        from_env({settings.ENV_ROOT: str(repo / "packages/memory/state")})
    with pytest.raises(SettingsError, match="inside the git checkout"):
        from_env({settings.ENV_ROOT: str(repo)})


def test_relative_root_is_refused():
    with pytest.raises(SettingsError, match="absolute"):
        from_env({settings.ENV_ROOT: "state"})


def test_http_argv_binds_loopback_8105_and_has_no_host_override(tmp_path):
    s = from_env(env(tmp_path))
    argv = settings.serve_argv(s, "http")
    assert argv[argv.index("--host") + 1] == "127.0.0.1"
    assert argv[argv.index("--port") + 1] == "8105"
    assert argv[argv.index("--path") + 1] == "/mcp"
    assert argv[argv.index("--transport") + 1] == "streamable-http"
    # No environment variable can widen the bind.
    wide = from_env(env(tmp_path, COGNITIVE_MEMORY_HOST="0.0.0.0", BASIC_MEMORY_HOST="0.0.0.0"))
    assert settings.serve_argv(wide, "http")[argv.index("--host") + 1] == "127.0.0.1"


def test_stdio_argv_has_no_network_flags(tmp_path):
    argv = settings.serve_argv(from_env(env(tmp_path)), "stdio")
    assert argv == ["mcp", "--transport", "stdio"]


def test_unknown_transport_is_an_error(tmp_path):
    with pytest.raises(SettingsError, match="transport"):
        settings.serve_argv(from_env(env(tmp_path)), "sse")


def test_port_override_is_range_checked(tmp_path):
    assert from_env(env(tmp_path, COGNITIVE_MEMORY_PORT="8111")).port == 8111
    with pytest.raises(SettingsError):
        from_env(env(tmp_path, COGNITIVE_MEMORY_PORT="80"))
