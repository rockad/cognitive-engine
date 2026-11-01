"""Resolve and validate the environment the native basic-memory daemon runs under.

This module is the whole of the wrapper's policy, and it is deliberately pure:
no I/O beyond path resolution, no imports from upstream. Upstream's `BASIC_MEMORY_*`
settings are plain environment variables, so "wrapping" means computing the right
ones, refusing the wrong ones, and `exec`-ing upstream with them (see `cli.py`).

⚠️ Four upstream defaults are wrong for this deployment, and each one is pinned
here rather than left to inherit:

    setting                 upstream 0.24.1     here
    reranker_enabled        False               True
    reranker_candidates     20                  10   (accepted range 5-10)
    reranker_max_doc_chars  2000                1000 (accepted range 500-1000)
    auto_update             True                False

The reranker is OFF upstream, so without this module a "tuned" daemon would
silently run plain hybrid retrieval and still answer every call. The two bounds
are what cap the ONNX CPU arena (RFC 003, invariant 1); a value outside them is
an error rather than a clamp, because a clamped value looks tuned and is not
what the operator asked for. `auto_update` is off because the version is pinned
in pyproject.toml and a daemon must not replace itself.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

HOST = "127.0.0.1"
PORT = 8105
PATH = "/mcp"

CANDIDATES_RANGE = (5, 10)
MAX_DOCUMENT_CHARS_RANGE = (500, 1000)
DEFAULT_CANDIDATES = 10
DEFAULT_MAX_DOCUMENT_CHARS = 1000

DEFAULT_ROOT = Path("~/.local/share/cognitive-engine/memory")

ENV_ROOT = "COGNITIVE_MEMORY_ROOT"
ENV_PORT = "COGNITIVE_MEMORY_PORT"
ENV_CANDIDATES = "BASIC_MEMORY_RERANKER_CANDIDATES"
ENV_MAX_DOCUMENT_CHARS = "BASIC_MEMORY_RERANKER_MAX_DOCUMENT_CHARS"

# The checkout this package lives in. Persistent state must not be written inside
# it (RFC 003, invariant 3). Resolved once, from this file's own location.
_REPO_ROOT = Path(__file__).resolve().parents[4]


class SettingsError(ValueError):
    """A setting is invalid. Raised, never defaulted: the daemon must not start."""


@dataclass(frozen=True)
class MemorySettings:
    root: Path
    candidates: int = DEFAULT_CANDIDATES
    max_document_chars: int = DEFAULT_MAX_DOCUMENT_CHARS
    port: int = PORT

    @property
    def config_dir(self) -> Path:
        """Upstream's per-user state: config.json, memory.db, fastembed_cache."""
        return self.root / "config"

    @property
    def notes_dir(self) -> Path:
        """The default project's markdown files."""
        return self.root / "notes"

    @property
    def cache_dir(self) -> Path:
        """`XDG_CACHE_HOME` for the daemon: onnxruntime and Hugging Face scratch."""
        return self.root / "cache"

    @property
    def data_dir(self) -> Path:
        """`XDG_DATA_HOME` for the daemon: library bookkeeping, not note data."""
        return self.root / "data"


def _int_in_range(raw: str, name: str, lo: int, hi: int) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise SettingsError(f"{name}={raw!r} is not an integer") from None
    if not lo <= value <= hi:
        raise SettingsError(
            f"{name}={value} is outside the accepted range {lo}-{hi}. Values below "
            f"{lo} cost recall; values above {hi} let the ONNX memory arena grow past "
            f"the 2-3 GB budget. Refusing to clamp.")
    return value


def _resolve_root(raw: str | None) -> Path:
    root = Path(raw).expanduser() if raw else DEFAULT_ROOT.expanduser()
    if not root.is_absolute():
        raise SettingsError(f"{ENV_ROOT}={raw!r} must be an absolute path")
    root = root.resolve()
    if root == _REPO_ROOT or _REPO_ROOT in root.parents:
        raise SettingsError(
            f"{ENV_ROOT}={root} is inside the git checkout {_REPO_ROOT}. Persistent "
            f"state lives out of tree (default {DEFAULT_ROOT}).")
    return root


def from_env(environ: Mapping[str, str] | None = None) -> MemorySettings:
    env = os.environ if environ is None else environ
    candidates = env.get(ENV_CANDIDATES)
    chars = env.get(ENV_MAX_DOCUMENT_CHARS)
    port = env.get(ENV_PORT)
    return MemorySettings(
        root=_resolve_root(env.get(ENV_ROOT)),
        candidates=(_int_in_range(candidates, ENV_CANDIDATES, *CANDIDATES_RANGE)
                    if candidates else DEFAULT_CANDIDATES),
        max_document_chars=(_int_in_range(chars, ENV_MAX_DOCUMENT_CHARS,
                                          *MAX_DOCUMENT_CHARS_RANGE)
                            if chars else DEFAULT_MAX_DOCUMENT_CHARS),
        port=_int_in_range(port, ENV_PORT, 1024, 65535) if port else PORT,
    )


def basic_memory_env(s: MemorySettings) -> dict[str, str]:
    """The `BASIC_MEMORY_*` variables that make upstream behave as specified.

    ⚠️ `BASIC_MEMORY_CONFIG_DIR` is the one that matters most. Without it upstream
    writes `~/.basic-memory` and creates `~/basic-memory` on ANY invocation — even
    `--help` did, while this wrapper was being written — which is exactly the
    stray state the out-of-tree rule forbids. `PROJECT_ROOT` additionally
    confines every project the daemon can create to `root`.
    """
    return {
        "BASIC_MEMORY_CONFIG_DIR": str(s.config_dir),
        "BASIC_MEMORY_HOME": str(s.notes_dir),
        "BASIC_MEMORY_PROJECT_ROOT": str(s.root),
        "BASIC_MEMORY_DEFAULT_PROJECT": "main",
        "BASIC_MEMORY_SEMANTIC_SEARCH_ENABLED": "true",
        "BASIC_MEMORY_SEMANTIC_EMBEDDING_PROVIDER": "fastembed",
        "BASIC_MEMORY_SEMANTIC_EMBEDDING_MODEL": "bge-small-en-v1.5",
        "BASIC_MEMORY_SEMANTIC_EMBEDDING_DIMENSIONS": "384",
        "BASIC_MEMORY_RERANKER_ENABLED": "true",
        "BASIC_MEMORY_RERANKER_PROVIDER": "fastembed",
        "BASIC_MEMORY_RERANKER_MODEL": "jinaai/jina-reranker-v1-tiny-en",
        ENV_CANDIDATES: str(s.candidates),
        ENV_MAX_DOCUMENT_CHARS: str(s.max_document_chars),
        "BASIC_MEMORY_AUTO_UPDATE": "false",
        "BASIC_MEMORY_NO_PROMOS": "1",
        "FASTMCP_STATELESS_HTTP": "true",
        # ⚠️ Third-party side state, found by running the daemon under a fake $HOME:
        # onnxruntime writes a Microsoft "DeveloperTools" device-id database and
        # Hugging Face writes logs under ~/.cache, and fastmcp writes
        # ~/.local/share/fastmcp/version_cache.json after phoning PyPI for its
        # update banner. None of it is note data, but none of it belongs in $HOME
        # either, and the update check is outbound traffic from a local-only daemon.
        "XDG_CACHE_HOME": str(s.cache_dir),
        "XDG_DATA_HOME": str(s.data_dir),
        "FASTMCP_CHECK_FOR_UPDATES": "off",
    }


def serve_argv(s: MemorySettings, transport: str) -> list[str]:
    """Arguments after `basic-memory`. The host is not a parameter: loopback only."""
    if transport == "stdio":
        return ["mcp", "--transport", "stdio"]
    if transport == "http":
        return ["mcp", "--transport", "streamable-http", "--host", HOST,
                "--port", str(s.port), "--path", PATH]
    raise SettingsError(f"unknown transport {transport!r}; use 'http' or 'stdio'")
