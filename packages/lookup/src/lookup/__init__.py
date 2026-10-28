"""lookup — search over declared sources, exposed to Claude over MCP."""

from importlib.metadata import PackageNotFoundError, version as _version

# ⚠️ ONE version, and pyproject.toml is it. The version used to be written out
# in three places -- pyproject, this file and mcp.py's serverInfo -- and they
# drifted: 0.4.0, 0.3.0 and 0.5.0 were all true at once, so a client asking the
# server which version it was talking to got an answer no build agreed with.
# The plugin manifest is the fourth place and must match too, which is exactly
# the argument for deriving rather than repeating.
try:
    __version__ = _version("cognitive-lookup")
except PackageNotFoundError:      # running from a source tree, not installed
    __version__ = "0.0.0+source"
