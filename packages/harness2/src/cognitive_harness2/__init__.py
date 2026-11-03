"""System 2 for Cognitive Engine: failure-driven Propose-Probe-Compose behind a strict MCP server."""
from importlib.metadata import PackageNotFoundError, version as _version

try:
    __version__ = _version("cognitive-harness2")
except PackageNotFoundError:  # running from a source tree
    __version__ = "0.0.0+source"
