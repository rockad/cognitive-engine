"""Shared channel-profile fixtures for the seam 2 and sweep plan tests (a plain module, imported by name)."""
from __future__ import annotations

from cognitive_harness2 import channels

LOCAL = "http://127.0.0.1:11434/v1"
CAPS = {"sessions": 30, "wall_s": 3600, "cost_usd": 5.0}
WORKSPACE_ROOT = "/tmp/work"

CHANNELS = {
    "local": dict(kind="opencode", provider="ollama", endpoint=LOCAL, model="ollama/qwen3:30b",
                  retention="local", max_classification="restricted"),
    "zdr-host": dict(kind="opencode", provider="ollama", endpoint="https://zdr.example/v1", model="ollama/x",
                     retention="zdr-attested", max_classification="restricted"),
    "openrouter": dict(kind="opencode", provider="openrouter", endpoint="https://openrouter.ai/api/v1",
                       model="openrouter/anthropic/claude-x", retention="zdr-attested", max_classification="gated"),
    "free": dict(kind="opencode", provider="openrouter", endpoint="https://openrouter.ai/api/v1",
                 model="openrouter/free", retention="zdr-attested", max_classification="restricted"),
    "claude": dict(kind="claude_cli", provider="anthropic", endpoints=["https://api.anthropic.com"],
                   model="claude-x", retention="unknown", max_classification="open"),
    "agy": dict(kind="agy_cli", provider="google", endpoints=["https://generativelanguage.googleapis.com"],
                model="gemini-3-pro", retention="unknown", max_classification="open"),
}


def make_profile(roots=(WORKSPACE_ROOT,), source="/p/channels.toml", **overrides) -> channels.ChannelProfile:
    """The standard profile; `overrides` maps a channel name to a dict merged over it (or None to drop it)."""
    table = {}
    for name, body in {**CHANNELS, **{k: v for k, v in overrides.items()}}.items():
        if body is None:
            continue
        merged = {**CHANNELS.get(name, {}), **body}
        table[name] = {**merged, "approval": "per-sweep", "caps": dict(CAPS)}
    raw = {"channels": table}
    if roots:
        raw["workspace_roots"] = list(roots)
    return channels.validate(raw, source=source)


PROFILE = make_profile()
