"""Toy MCP servers for testing the conformance runner: one compliant, several broken.

    python toy_server.py <variant> <port>

Variants (each breaks exactly one thing, except `good`):
    good          FastMCP, loopback, Host/Origin guard on, stateless
    no_guard      FastMCP without host_origin_protection (the Lookup bug)
    official_lax  official MCPServer: silently ignores unknown arguments
    wide_bind     FastMCP bound to 0.0.0.0
    stateful      FastMCP issuing an Mcp-Session-Id
"""
import sys
from typing import Literal

variant, port = sys.argv[1], int(sys.argv[2])


def echo(text: str, mode: Literal["a", "b"] = "a", n: int = 1) -> str:
    return f"{mode}:{text * n}"


def noop() -> str:
    return "ok"


if variant == "official_lax":
    from mcp.server.mcpserver import MCPServer

    server = MCPServer("toy")
    server.tool()(echo)
    server.tool()(noop)
    server.run("streamable-http", host="127.0.0.1", port=port, streamable_http_path="/mcp", stateless_http=True)
else:
    from fastmcp import FastMCP

    server = FastMCP("toy")
    server.tool()(echo)
    server.tool()(noop)
    extra = {} if variant == "no_guard" else {"host_origin_protection": "auto"}
    server.run(transport="http", host="0.0.0.0" if variant == "wide_bind" else "127.0.0.1", port=port,
               path="/mcp", stateless_http=variant != "stateful", show_banner=False, **extra)
