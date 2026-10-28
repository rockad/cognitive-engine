#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["mcp>=2.3,<3", "httpx>=0.28", "psutil>=5"]
# ///
"""Black-box MCP conformance runner (RFC 001 D30; plan §3.5).

Checks one running MCP server against the contract every daemon in this repo must
meet, whatever language it is written in:

  1. binds loopback only                    (HTTP)
  2. refuses a forged Host                  (HTTP, 403 or 421)
  3. refuses a foreign Origin               (HTTP, 403)
  4. is stateless                           (HTTP, no Mcp-Session-Id)
  5. rejects a misspelled argument          (a tool error, never ignored)
  6. rejects a wrong-typed argument
  7. rejects a value outside an enum
  8. rejects a missing required argument
  9. rejects any argument to an argument-less tool
 10. advertises closed schemas              (additionalProperties: false)

Why it exists: Lookup's HTTP daemon accepted a forged Host and a foreign Origin, and
Jev's tools accepted any arguments. Both shipped because nothing enforced a shared
contract. The two official SDKs also disagree on defaults (the Python `MCPServer`
and the TypeScript `registerTool` raw shape both drop unknown keys silently), so the
contract is checked against the running server rather than assumed from a library.

Usage:
    uv run tools/mcp_conformance.py --probe tools/probes/lookup.toml http://127.0.0.1:8101/mcp
    uv run tools/mcp_conformance.py --probe tools/probes/jev.toml -- bun run packages/jev/src/index.ts

Exit status: 0 when every check passes, 1 on any FAIL or UNVERIFIABLE, 2 on bad usage.

It fails closed: a check the runner cannot perform is UNVERIFIABLE and counts as a
failure unless explicitly allowed with --allow-unverifiable CHECK. A deliberate,
documented exception goes in the probe's [waive] table and is printed as WAIVED.

Negative probes must be harmless if the server wrongly ACCEPTS them. Choose tools that
are read-only or fail fast without a network.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import socket
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import httpx
import psutil
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

PASS, FAIL, NA, UNVERIFIABLE, WAIVED = "PASS", "FAIL", "N/A", "UNVERIFIABLE", "WAIVED"

CHECKS = (
    "baseline_call", "loopback_bind", "forged_host", "foreign_origin", "legit_requests",
    "stateless", "unknown_argument", "wrong_type", "bad_enum", "missing_required",
    "argless_rejects_args", "closed_schemas",
)


class ProbeError(ValueError):
    """The probe spec is malformed. Raised so a bad spec can never read as a pass."""


@dataclass(frozen=True)
class Probe:
    name: str
    ok_tool: str
    ok_args: dict
    strict_tool: str
    strict_args: dict
    unknown_key: str
    wrong_type: tuple[str, object] | None
    bad_enum: tuple[str, object] | None
    missing_required: str
    argless_tool: str | None
    waive: dict[str, str] = field(default_factory=dict)


def load_probe(path: Path) -> Probe:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ProbeError(f"cannot read probe {path}: {e}") from e

    def need(table: dict, key: str, where: str):
        if key not in table:
            raise ProbeError(f"{path}: missing {where}.{key}")
        return table[key]

    ok, strict = raw.get("ok", {}), raw.get("strict", {})
    pair = lambda t: (t["field"], t["value"]) if t else None  # noqa: E731
    for t, where in ((strict.get("wrong_type"), "strict.wrong_type"), (strict.get("bad_enum"), "strict.bad_enum")):
        if t and not {"field", "value"} <= set(t):
            raise ProbeError(f"{path}: {where} needs field and value")
    waive = raw.get("waive", {})
    for check, reason in waive.items():
        if check not in CHECKS or not str(reason).strip():
            raise ProbeError(f"{path}: [waive] {check!r} must name a known check and give a reason")
    return Probe(
        name=need(raw, "name", "(top level)"),
        ok_tool=need(ok, "tool", "ok"), ok_args=ok.get("args", {}),
        strict_tool=need(strict, "tool", "strict"), strict_args=need(strict, "args", "strict"),
        unknown_key=strict.get("unknown_key", "visiblity"),
        wrong_type=pair(strict.get("wrong_type")), bad_enum=pair(strict.get("bad_enum")),
        missing_required=need(strict, "missing_required", "strict"),
        argless_tool=raw.get("argless", {}).get("tool"),
        waive=waive,
    )


@dataclass
class Result:
    check: str
    status: str
    detail: str = ""


def _client(target: list[str]) -> Client:
    if target[0].startswith(("http://", "https://")):
        return Client(target[0])
    return Client(StdioServerParameters(command=target[0], args=target[1:]))


async def _rejected(client: Client, tool: str, args: dict) -> tuple[bool, str]:
    """True when the server refuses the call, by a tool error or a protocol error."""
    try:
        res = await client.call_tool(tool, args)
    except Exception as e:  # MCPError, or a transport-level validation error
        return True, f"{type(e).__name__}: {str(e)[:90]}"
    text = " ".join(getattr(c, "text", "") for c in res.content)[:90].replace("\n", " ")
    if getattr(res, "is_error", False):
        return True, text
    return False, f"ACCEPTED, returned: {text!r}"


def _open_objects(schema: object, path: str = "") -> list[str]:
    """Paths of object schemas that declare properties but allow unknown ones."""
    out: list[str] = []
    if isinstance(schema, dict):
        if schema.get("properties") and schema.get("additionalProperties") is not False:
            out.append(path or "(root)")
        for k, v in schema.items():
            out += _open_objects(v, f"{path}.{k}" if path else k)
    elif isinstance(schema, list):
        for i, v in enumerate(schema):
            out += _open_objects(v, f"{path}[{i}]")
    return out


def _non_loopback_addresses() -> list[str]:
    out = []
    for iface, addrs in psutil.net_if_addrs().items():
        if iface == "lo":
            continue
        out += [a.address for a in addrs
                if a.family == socket.AF_INET and not a.address.startswith("127.")]
    return out


def _can_connect(addr: str, port: int) -> bool:
    try:
        with socket.create_connection((addr, port), timeout=1.0):
            return True
    except OSError:
        return False


INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {},
    "clientInfo": {"name": "mcp-conformance", "version": "1"}}}
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


async def _http_checks(url: str) -> list[Result]:
    u = urlparse(url)
    port = u.port or (443 if u.scheme == "https" else 80)
    out: list[Result] = []

    addrs = _non_loopback_addresses()
    if not addrs:
        out.append(Result("loopback_bind", UNVERIFIABLE, "this host has no non-loopback interface to probe"))
    else:
        reachable = [a for a in addrs if _can_connect(a, port)]
        out.append(Result("loopback_bind", FAIL, f"port {port} answers on {', '.join(reachable)}")
                   if reachable else Result("loopback_bind", PASS, f"refused on {len(addrs)} other address(es)"))

    async with httpx.AsyncClient(timeout=10.0) as h:
        async def post(extra: dict | None = None) -> httpx.Response:
            return await h.post(url, json=INIT, headers={**HEADERS, **(extra or {})})

        try:
            base = await post()
        except httpx.HTTPError as e:
            msg = f"cannot reach {url}: {type(e).__name__}"
            return out + [Result(c, UNVERIFIABLE, msg) for c in ("forged_host", "foreign_origin", "legit_requests", "stateless")]

        sane = base.status_code < 300
        local = await post({"Host": f"localhost:{port}"})
        legit_ok = sane and local.status_code < 300
        out.append(Result("legit_requests", PASS if legit_ok else FAIL,
                          "127.0.0.1 and localhost Host headers accepted" if legit_ok
                          else f"baseline {base.status_code}, localhost {local.status_code}"))
        if not sane:
            why = f"baseline request returned {base.status_code}, so the refusals below mean nothing"
            return out + [Result(c, UNVERIFIABLE, why) for c in ("forged_host", "foreign_origin", "stateless")]

        forged = await post({"Host": "evil.example"})
        out.append(Result("forged_host", PASS if forged.status_code in (403, 421) else FAIL,
                          f"forged Host -> {forged.status_code} (want 403 or 421)"))
        foreign = await post({"Origin": "http://evil.example"})
        out.append(Result("foreign_origin", PASS if foreign.status_code == 403 else FAIL,
                          f"foreign Origin -> {foreign.status_code} (want 403)"))
        has_session = "mcp-session-id" in {k.lower() for k in base.headers}
        out.append(Result("stateless", FAIL if has_session else PASS,
                          "server issued an Mcp-Session-Id" if has_session else "no session id issued"))
    return out


async def _tool_checks(target: list[str], probe: Probe) -> list[Result]:
    out: list[Result] = []
    try:
        async with _client(target) as client:
            tools = (await client.list_tools()).tools

            ok, detail = await _rejected(client, probe.ok_tool, probe.ok_args)
            out.append(Result("baseline_call", FAIL if ok else PASS,
                              f"{probe.ok_tool} did not succeed: {detail}" if ok else f"{probe.ok_tool} ok"))
            baseline_ok = not ok

            # A rejection only proves validation if the UNMODIFIED call to the same tool
            # is accepted: otherwise "note not found" or a missing index would make every
            # negative check pass for the wrong reason.
            accepted_base: dict[str, bool] = {}

            async def base_accepted(tool: str, args: dict) -> tuple[bool, str]:
                key = f"{tool}:{json.dumps(args, sort_keys=True)}"
                if key not in accepted_base:
                    rejected, d = await _rejected(client, tool, args)
                    accepted_base[key] = not rejected
                    accepted_base[key + "!"] = d
                return accepted_base[key], accepted_base[key + "!"]

            async def negative(check: str, tool: str, base: dict, args: dict, what: str) -> None:
                if not baseline_ok:
                    out.append(Result(check, UNVERIFIABLE, "baseline call failed, so a rejection proves nothing"))
                    return
                ok_, why = await base_accepted(tool, base)
                if not ok_:
                    out.append(Result(check, UNVERIFIABLE, f"{tool} rejects its own unmodified call ({why}), "
                                                           "so a rejection proves nothing"))
                    return
                rejected, d = await _rejected(client, tool, args)
                out.append(Result(check, PASS if rejected else FAIL, f"{what}: {d}"))

            t, base = probe.strict_tool, probe.strict_args
            await negative("unknown_argument", t, base, {**base, probe.unknown_key: "open"}, f"{t} + {probe.unknown_key}")
            if probe.wrong_type:
                f, v = probe.wrong_type
                await negative("wrong_type", t, base, {**base, f: v}, f"{t}.{f}={v!r}")
            else:
                out.append(Result("wrong_type", UNVERIFIABLE, "probe defines no strict.wrong_type"))
            if probe.bad_enum:
                f, v = probe.bad_enum
                await negative("bad_enum", t, base, {**base, f: v}, f"{t}.{f}={v!r}")
            else:
                out.append(Result("bad_enum", UNVERIFIABLE, "probe defines no strict.bad_enum"))
            missing = {k: v for k, v in base.items() if k != probe.missing_required}
            await negative("missing_required", t, base, missing, f"{t} without {probe.missing_required}")
            if probe.argless_tool:
                await negative("argless_rejects_args", probe.argless_tool, {}, {probe.unknown_key: "open"},
                               f"{probe.argless_tool} + argument")
            else:
                out.append(Result("argless_rejects_args", NA, "probe names no argument-less tool"))

            open_ = {}
            for tool in tools:
                schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None) or {}
                bad = _open_objects(schema)
                if bad:
                    open_[tool.name] = bad
            out.append(Result("closed_schemas", FAIL if open_ else PASS,
                              "open schemas: " + "; ".join(f"{k} {v}" for k, v in sorted(open_.items()))
                              if open_ else f"{len(tools)} tool schema(s) closed"))
    except Exception as e:
        msg = f"could not talk to the server: {type(e).__name__}: {str(e)[:100]}"
        have = {r.check for r in out}
        out += [Result(c, UNVERIFIABLE, msg) for c in CHECKS[0:1] + CHECKS[6:] if c not in have]
    return out


async def run(target: list[str], probe: Probe) -> list[Result]:
    is_http = target[0].startswith(("http://", "https://"))
    results = await _http_checks(target[0]) if is_http else [
        Result(c, NA, "stdio server: no network listener") for c in
        ("loopback_bind", "forged_host", "foreign_origin", "legit_requests", "stateless")]
    results += await _tool_checks(target, probe)
    order = {c: i for i, c in enumerate(CHECKS)}
    results.sort(key=lambda r: order.get(r.check, 99))
    return [Result(r.check, WAIVED, f"{probe.waive[r.check]} (would be {r.status}: {r.detail})")
            if r.check in probe.waive and r.status in (FAIL, UNVERIFIABLE) else r
            for r in results]


def exit_code(results: list[Result], allow_unverifiable: frozenset[str] = frozenset()) -> int:
    bad = [r for r in results if r.status == FAIL
           or (r.status == UNVERIFIABLE and r.check not in allow_unverifiable)]
    return 1 if bad else 0


def _render(probe: Probe, results: list[Result]) -> str:
    lines = [f"MCP conformance: {probe.name}"]
    lines += [f"  {r.status:<12} {r.check:<22} {r.detail}" for r in results]
    counts = {s: sum(r.status == s for r in results) for s in (PASS, FAIL, UNVERIFIABLE, WAIVED, NA)}
    lines.append("  " + ", ".join(f"{n} {s}" for s, n in counts.items() if n))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("target", nargs="+", help="a URL (http://127.0.0.1:8101/mcp) or a stdio command")
    ap.add_argument("--probe", required=True, type=Path, help="probe spec (tools/probes/*.toml)")
    ap.add_argument("--allow-unverifiable", action="append", default=[], metavar="CHECK",
                    help="treat this check as passing when it cannot be performed (repeatable)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    a = ap.parse_args(argv)
    for c in a.allow_unverifiable:
        if c not in CHECKS:
            ap.error(f"unknown check {c!r}; choose from {', '.join(CHECKS)}")
    try:
        probe = load_probe(a.probe)
    except ProbeError as e:
        print(f"mcp_conformance: {e}", file=sys.stderr)
        return 2
    results = asyncio.run(run(a.target, probe))
    if a.json:
        print(json.dumps([r.__dict__ for r in results], indent=2))
    else:
        print(_render(probe, results))
    return exit_code(results, frozenset(a.allow_unverifiable))


if __name__ == "__main__":
    raise SystemExit(main())
