"""Runtime settings, environment-driven, validated, with out-of-tree state.

Harness² has no port in RFC 004; `8120` is chosen here (clear of 8100, 8105, 8110, 8115)
and is overridable. The bind is `127.0.0.1` and is not configurable.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

HOST = "127.0.0.1"
PORT = 8120
PATH = "/mcp"

ENV_PORT = "COGNITIVE_HARNESS2_PORT"
ENV_EXPERIENCE_ROOT = "COGNITIVE_HARNESS2_EXPERIENCE_ROOT"
ENV_BASE_URL = "COGNITIVE_HARNESS2_BASE_URL"
ENV_MODEL = "COGNITIVE_HARNESS2_MODEL"
ENV_ZDR_ENDPOINTS = "COGNITIVE_HARNESS2_ZDR_ENDPOINTS"
DEFAULT_EXPERIENCE_ROOT = Path("~/.local/share/cognitive-engine/experiences")
DEFAULT_BASE_URL = "http://127.0.0.1:11434/v1"  # local Ollama, OpenAI-compatible

_REPO_ROOT = Path(__file__).resolve().parents[4]


class SettingsError(ValueError):
    """A setting is invalid. Raised, never defaulted."""


@dataclass(frozen=True)
class Settings:
    port: int
    experience_root: Path
    base_url: str
    model: str | None
    zdr_endpoints: frozenset[str]


def from_env(environ: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if environ is None else environ
    port_raw = env.get(ENV_PORT)
    try:
        port = int(port_raw) if port_raw else PORT
    except ValueError:
        raise SettingsError(f"{ENV_PORT}={port_raw!r} is not an integer") from None
    if not 1024 <= port <= 65535:
        raise SettingsError(f"{ENV_PORT}={port} is outside 1024-65535")

    raw_root = env.get(ENV_EXPERIENCE_ROOT)
    root = (Path(raw_root).expanduser() if raw_root else DEFAULT_EXPERIENCE_ROOT.expanduser())
    if not root.is_absolute():
        raise SettingsError(f"{ENV_EXPERIENCE_ROOT}={raw_root!r} must be an absolute path")
    root = root.resolve()
    if root == _REPO_ROOT or _REPO_ROOT in root.parents:
        raise SettingsError(
            f"{ENV_EXPERIENCE_ROOT}={root} is inside the git checkout {_REPO_ROOT}; experiences "
            f"live out of tree so private code patterns never reach a commit")

    endpoints = frozenset(h.strip().lower() for h in env.get(ENV_ZDR_ENDPOINTS, "").split(",") if h.strip())
    return Settings(port=port, experience_root=root, base_url=env.get(ENV_BASE_URL, DEFAULT_BASE_URL),
                    model=env.get(ENV_MODEL) or None, zdr_endpoints=endpoints)
