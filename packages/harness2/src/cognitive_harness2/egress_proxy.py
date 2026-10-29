"""The allow-list forward proxy of a contained proposer session (RFC 004 6.2.3, slice 3b).

It runs in its OWN container, attached to an `--internal` network (shared with the session container,
no default route) and to an egress network. The session's HTTP(S)_PROXY points here, so everything the
session can say to the outside is said to this process, which applies a strict, default-deny policy.

    HTTP CONNECT host:port                     tunnelled, byte for byte, to the allow-listed host:port
    GET http://host[:port]/path ...            rewritten to origin form, `Connection: close`, one request
    anything else (origin-form, userinfo, ...) refused

Policy, all of it enforced before a single byte reaches an upstream:

  * deny by default: the only reachable targets are the (host, port) pairs listed in the config, exactly;
    no wildcard, no suffix match, no port range;
  * hosts are normalised first (case, one trailing dot, IDNA incl. ideographic full stops); a numeric host
    that is not a canonical dotted quad (127.1, 2130706433, 0x7f.1, 0177.0.0.1) is refused as ambiguous;
    a request for an IP literal passes only if that literal itself is listed;
  * the name is RESOLVED ONCE and the connection is made to the resolved address, never to the name again;
    if ANY resolved address is private, loopback, link-local (incl. 169.254.169.254), multicast, reserved,
    CGNAT, unspecified or an IPv4 address embedded in an IPv6 form of those, the request is refused even though
    the name is allow-listed (DNS rebinding and a hostile resolver cannot redirect an allowed name inward);
  * a route may point an allow-listed host:port at a UNIX SOCKET instead of the network (the host bridge to
    a local ollama); and a `listener` may relay a TCP port of this proxy to such a socket verbatim. Neither
    resolves anything;
  * caps: request line 8 KiB, header block 32 KiB, 100 headers, a total deadline for the request head
    (slowloris), concurrent connections, new connections per second, idle time, lifetime and bytes per
    connection.

The access log has the decision, the normalised host and the port, and a rule code. Never a header, a body,
a path, a query or a token. This module is stdlib-only: it ships inside the image and also runs as a plain
function in the unit tests. Importing it starts nothing.

`test_allow_cidrs` in the config is an explicit opt-in that exempts those networks from the address rule.
It exists so tests can use a container on a private bridge as the "remote" endpoint; the session runner
never sets it for a real session.
"""
from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import re
import signal
import sys
import time
import unicodedata
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field

READY_LINE = "harness2-proxy ready"
PROXY_PORT_DEFAULT = 3128

MAX_REQUEST_LINE = 8 * 1024
MAX_HEADER_BYTES = 32 * 1024
MAX_HEADER_COUNT = 100
MAX_CONFIG_BYTES = 64 * 1024
MAX_ADDRESSES = 8
_FIELD = re.compile(rb"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_METHOD = re.compile(r"^[A-Z]{3,16}$")
_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,61}[a-z0-9])?$")
_NUMERIC_LABEL = re.compile(r"^(?:0x[0-9a-f]*|[0-9]+)$")
_HOP_BY_HOP = frozenset({"connection", "proxy-connection", "keep-alive", "proxy-authorization",
                         "proxy-authenticate", "te", "trailer", "upgrade"})


class Refused(ValueError):
    """A request or target that policy refuses. `.code` is the rule id used in the log and the 4xx reason."""

    def __init__(self, code: str, status: int = 403):
        super().__init__(code)
        self.code, self.status = code, status


@dataclass(frozen=True)
class Limits:
    request_line: int = MAX_REQUEST_LINE
    header_bytes: int = MAX_HEADER_BYTES
    header_count: int = MAX_HEADER_COUNT
    head_timeout_s: float = 10.0         # the WHOLE request head, not per read: this is the slowloris cap
    connect_timeout_s: float = 10.0
    idle_timeout_s: float = 300.0
    lifetime_s: float = 3600.0
    max_bytes: int = 512 * 1024 * 1024   # per connection, per direction
    max_connections: int = 64
    rate_per_s: float = 20.0
    burst: int = 40


# ── host and address normalisation (pure) ─────────────────────────────────────────

def _bad_chars(text: str) -> bool:
    return any(unicodedata.category(c) in ("Cc", "Cf", "Cs", "Co", "Cn", "Zs", "Zl", "Zp") for c in text)


def normalize_host(raw: str) -> str:
    """The canonical form of a host as written in a request, or `Refused`.

    A name comes back lower-case, ASCII (IDNA) and without its one trailing dot; an IPv4 literal comes back
    as a dotted quad and an IPv6 literal compressed, without brackets.
    """
    if not isinstance(raw, str) or not raw:
        raise Refused("bad-host", 400)
    text = raw
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1]
        if "%" in inner or _bad_chars(inner):
            raise Refused("bad-host", 400)
        try:
            return ipaddress.IPv6Address(inner).compressed
        except ValueError:
            raise Refused("bad-host", 400) from None
    if _bad_chars(text) or any(c in text for c in "/\\@?#%:[]\"'<>,;=&+*|^`{}!$()~"):
        raise Refused("bad-host", 400)
    if not text.isascii():
        try:
            text = text.encode("idna").decode("ascii")   # also folds U+3002 and U+FF0E into '.'
        except UnicodeError:
            raise Refused("bad-host", 400) from None
    text = text.lower()
    if text.endswith("."):
        text = text[:-1]
    if not text or len(text) > 253:
        raise Refused("bad-host", 400)
    labels = text.split(".")
    if any(not _LABEL.fullmatch(label) for label in labels):
        raise Refused("bad-host", 400)
    if _NUMERIC_LABEL.fullmatch(labels[-1]):
        # A host whose last label is numeric is an IPv4 address to a browser and to getaddrinfo. Only the
        # canonical form is accepted; 127.1, 2130706433, 0x7f.1 and 0177.0.0.1 all mean something else
        # to different parsers, so they are refused rather than interpreted.
        try:
            return str(ipaddress.IPv4Address(text))
        except ValueError:
            raise Refused("ambiguous-numeric-host", 400) from None
    return text


def parse_port(raw: str | int) -> int:
    if isinstance(raw, bool):
        raise Refused("bad-port", 400)
    if isinstance(raw, int):
        port = raw
    else:
        if not (isinstance(raw, str) and raw.isascii() and raw.isdigit() and len(raw) <= 5):
            raise Refused("bad-port", 400)
        port = int(raw)
    if not 1 <= port <= 65535:
        raise Refused("bad-port", 400)
    return port


def split_authority(authority: str, *, default_port: int | None = None) -> tuple[str, int]:
    """`host[:port]` or `[v6][:port]` -> (normalised host, port). No userinfo, no path."""
    if not authority or _bad_chars(authority):
        raise Refused("bad-authority", 400)
    if authority.startswith("["):
        end = authority.find("]")
        if end < 0:
            raise Refused("bad-authority", 400)
        host_raw, rest = authority[:end + 1], authority[end + 1:]
        if rest and not rest.startswith(":"):
            raise Refused("bad-authority", 400)
        port_raw = rest[1:] if rest else None
    else:
        if authority.count(":") > 1:
            raise Refused("bad-authority", 400)
        host_raw, _, port_raw = authority.partition(":")
        port_raw = port_raw if ":" in authority else None
    if port_raw is None:
        if default_port is None:
            raise Refused("missing-port", 400)
        port = default_port
    else:
        port = parse_port(port_raw)
    return normalize_host(host_raw), port


def _embedded_v4(ip: ipaddress.IPv6Address) -> list[ipaddress.IPv4Address]:
    out = []
    if ip.ipv4_mapped is not None:
        out.append(ip.ipv4_mapped)
    if ip.sixtofour is not None:
        out.append(ip.sixtofour)
    if ip.teredo is not None:
        out.extend(ip.teredo)
    if ip in ipaddress.IPv6Network("64:ff9b::/96") or ip in ipaddress.IPv6Network("64:ff9b:1::/48"):
        out.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    if int(ip) > 1 and ip in ipaddress.IPv6Network("::/96"):                      # deprecated IPv4-compatible
        out.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    return out


def address_problem(ip: ipaddress._BaseAddress, allow_cidrs: Sequence[ipaddress._BaseNetwork] = ()) -> str | None:
    """Why this address may never be connected to (a short rule code), or None."""
    if any(ip.version == n.version and ip in n for n in allow_cidrs):
        return None
    if isinstance(ip, ipaddress.IPv6Address):
        for v4 in _embedded_v4(ip):
            why = address_problem(v4, allow_cidrs)
            if why:
                return why
    for name, bad in (("loopback", ip.is_loopback), ("link-local", ip.is_link_local),
                      ("multicast", ip.is_multicast), ("unspecified", ip.is_unspecified),
                      ("reserved", ip.is_reserved), ("private", ip.is_private)):
        if bad:
            return f"address-{name}"
    if not ip.is_global:        # CGNAT 100.64/10, benchmarking, documentation, other special-purpose blocks
        return "address-not-global"
    return None


# ── policy (pure) ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Decision:
    allowed: bool
    host: str            # normalised, or "<invalid>"
    port: int            # 0 when unreadable
    reason: str          # rule code
    unix: str | None = None


@dataclass(frozen=True)
class Policy:
    allow: frozenset[tuple[str, int]] = frozenset()
    unix_routes: Mapping[tuple[str, int], str] = field(default_factory=dict)
    allow_cidrs: tuple = ()

    @staticmethod
    def build(allow: Sequence[Sequence], unix_routes: Sequence[Mapping] = (),
              test_allow_cidrs: Sequence[str] = ()) -> "Policy":
        """Normalise a config. Anything malformed raises `ValueError`: a policy is never repaired."""
        entries: set[tuple[str, int]] = set()
        for item in allow:
            if not (isinstance(item, (list, tuple)) and len(item) == 2):
                raise ValueError(f"allow entry {item!r} must be [host, port]")
            host, port = item
            if not isinstance(host, str) or "*" in host:
                raise ValueError(f"allow entry host {host!r}: wildcards are not supported")
            try:
                entries.add((normalize_host(host), parse_port(port)))
            except Refused as e:
                raise ValueError(f"allow entry {item!r} is unusable ({e.code})") from None
        routes: dict[tuple[str, int], str] = {}
        for item in unix_routes:
            if not isinstance(item, Mapping) or set(item) != {"host", "port", "unix"}:
                raise ValueError(f"unix route {item!r} must have exactly host, port and unix")
            path = item["unix"]
            if not (isinstance(path, str) and path.startswith("/") and "\0" not in path):
                raise ValueError(f"unix route path {path!r} must be an absolute path")
            try:
                routes[(normalize_host(item["host"]), parse_port(item["port"]))] = path
            except Refused as e:
                raise ValueError(f"unix route {dict(item)!r} is unusable ({e.code})") from None
        try:
            cidrs = tuple(ipaddress.ip_network(c, strict=True) for c in test_allow_cidrs)
        except ValueError as e:
            raise ValueError(f"test_allow_cidrs: {e}") from None
        return Policy(frozenset(entries), dict(routes), cidrs)

    def decide(self, host_raw: str, port: int | str) -> Decision:
        """Name-level decision. Resolution and the address rule come after this, in `connect`."""
        try:
            host, p = normalize_host(host_raw), parse_port(port)
        except Refused as e:
            return Decision(False, "<invalid>", 0, e.code)
        key = (host, p)
        if key in self.unix_routes:
            return Decision(True, host, p, "unix-route", self.unix_routes[key])
        if key in self.allow:
            return Decision(True, host, p, "allow-listed")
        return Decision(False, host, p, "not-allow-listed")


async def resolve_once(host: str, port: int, resolver: Callable[[str, int], Awaitable[list[str]]] | None = None) -> list[str]:
    """The addresses of `host`, resolved exactly once. An IP literal is its own answer (no lookup)."""
    try:
        return [str(ipaddress.ip_address(host))]
    except ValueError:
        pass
    if resolver is None:
        async def resolver(h: str, p: int) -> list[str]:   # noqa: E306
            infos = await asyncio.get_running_loop().getaddrinfo(h, p, type=__import__("socket").SOCK_STREAM)
            return [i[4][0] for i in infos]
    try:
        found = await resolver(host, port)
    except OSError:
        raise Refused("unresolvable", 502) from None
    seen: list[str] = []
    for a in found:
        a = a.split("%", 1)[0]
        if a not in seen:
            seen.append(a)
    if not seen:
        raise Refused("unresolvable", 502)
    return seen[:MAX_ADDRESSES]


def validated_addresses(addresses: Sequence[str], policy: Policy) -> list[str]:
    """Every resolved address must pass the address rule, or the whole answer is refused (never a pick of the safe ones)."""
    out: list[str] = []
    for a in addresses:
        try:
            ip = ipaddress.ip_address(a)
        except ValueError:
            raise Refused("bad-address", 502) from None
        why = address_problem(ip, policy.allow_cidrs)
        if why:
            raise Refused(why, 403)
        out.append(str(ip))
    return out


# ── request parsing (pure) ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Request:
    method: str
    target: str
    version: str
    headers: tuple[tuple[str, str], ...]


def parse_head(head: bytes, limits: Limits = Limits()) -> Request:
    """Parse a request head (without the blank line). Strict: CRLF only, no folding, bounded."""
    first, sep, rest = head.partition(b"\r\n")
    if len(first) > limits.request_line:
        raise Refused("request-line-too-long", 414)
    if b"\n" in first or b"\r" in first or b"\0" in first:
        raise Refused("bad-request", 400)
    try:
        line = first.decode("ascii")
    except UnicodeDecodeError:
        raise Refused("bad-request", 400) from None
    parts = line.split(" ")
    if len(parts) != 3 or not all(parts):
        raise Refused("bad-request", 400)
    method, target, version = parts
    if not _METHOD.fullmatch(method) or version not in ("HTTP/1.0", "HTTP/1.1"):
        raise Refused("bad-request", 400)
    if _bad_chars(target) or "\\" in target:
        raise Refused("bad-request", 400)
    headers: list[tuple[str, str]] = []
    if sep:
        for raw in rest.split(b"\r\n"):
            if len(headers) >= limits.header_count:
                raise Refused("too-many-headers", 431)
            if not raw or raw[:1] in (b" ", b"\t") or b"\n" in raw or b"\r" in raw or b"\0" in raw:
                raise Refused("bad-request", 400)
            name, colon, value = raw.partition(b":")
            if not colon or not _FIELD.fullmatch(name):
                raise Refused("bad-request", 400)
            try:
                headers.append((name.decode("ascii"), value.strip().decode("latin-1")))
            except UnicodeDecodeError:
                raise Refused("bad-request", 400) from None
    lowered = [n.lower() for n, _ in headers]
    if lowered.count("content-length") > 1 or ("content-length" in lowered and "transfer-encoding" in lowered):
        raise Refused("ambiguous-body-framing", 400)
    return Request(method, target, version, tuple(headers))


def split_absolute_uri(target: str) -> tuple[str, int, str]:
    """`http://host[:port]/path?query` -> (host, port, origin-form target). No userinfo, no fragment."""
    if not target.lower().startswith("http://"):
        raise Refused("not-a-proxy-request", 400)
    rest = target[len("http://"):]
    end = next((i for i, c in enumerate(rest) if c in "/?#"), len(rest))
    authority, tail = rest[:end], rest[end:]
    if "@" in authority:
        raise Refused("userinfo-in-uri", 400)
    if "#" in tail:
        raise Refused("fragment-in-uri", 400)
    host, port = split_authority(authority, default_port=80)
    path = tail if tail.startswith("/") else "/" + tail
    return host, port, path


def rewrite_request(req: Request, host: str, port: int, path: str) -> bytes:
    """The origin-form request sent upstream: Host pinned to the checked target, hop-by-hop headers dropped,
    `Connection: close`. Body framing headers are kept as they were."""
    shown = f"[{host}]" if ":" in host else host
    lines = [f"{req.method} {path} HTTP/1.1", f"Host: {shown}" + ("" if port == 80 else f":{port}")]
    for name, value in req.headers:
        low = name.lower()
        if low == "host" or low in _HOP_BY_HOP:
            continue
        lines.append(f"{name}: {value}")
    lines.append("Connection: close")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


class TokenBucket:
    def __init__(self, rate: float, burst: int, clock: Callable[[], float] = time.monotonic):
        self.rate, self.burst, self.clock = rate, float(burst), clock
        self.tokens, self.last = float(burst), clock()

    def take(self) -> bool:
        now = self.clock()
        self.tokens = min(self.burst, self.tokens + (now - self.last) * self.rate)
        self.last = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


# ── the server ────────────────────────────────────────────────────────────────────

_REASONS = {200: "Connection Established", 400: "Bad Request", 403: "Forbidden", 408: "Request Timeout",
            414: "URI Too Long", 429: "Too Many Requests", 431: "Request Header Fields Too Large",
            502: "Bad Gateway", 503: "Service Unavailable", 504: "Gateway Timeout"}


class Proxy:
    def __init__(self, policy: Policy, limits: Limits = Limits(), *,
                 resolver: Callable[[str, int], Awaitable[list[str]]] | None = None,
                 log: Callable[[str], None] | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.policy, self.limits, self.resolver = policy, limits, resolver
        self.log = log or (lambda line: print(line, file=sys.stderr, flush=True))
        self.bucket = TokenBucket(limits.rate_per_s, limits.burst, clock)
        self.active = 0

    def _log(self, decision: str, host: str, port: int, reason: str) -> None:
        self.log(f"ACCESS decision={decision} host={host} port={port} reason={reason}")

    async def _respond(self, writer: asyncio.StreamWriter, status: int, code: str) -> None:
        body = f"{status} {code}\n".encode("ascii")
        head = (f"HTTP/1.1 {status} {_REASONS.get(status, 'Error')}\r\nContent-Type: text/plain\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode("ascii")
        try:
            writer.write(head + body)
            await asyncio.wait_for(writer.drain(), 5)
        except (OSError, asyncio.TimeoutError):
            pass

    @staticmethod
    async def _linger(reader: asyncio.StreamReader) -> None:
        """Swallow what the client already sent, so closing does not RST away the reply it is about to read."""
        try:
            await asyncio.wait_for(reader.read(65536), 0.25)
        except (OSError, asyncio.TimeoutError):
            pass

    async def _read_head(self, reader: asyncio.StreamReader) -> tuple[bytes, bytes]:
        """(head without the blank line, bytes already read past it). Bounded in size AND in total time."""
        lim = self.limits
        buf = b""
        try:
            async with asyncio.timeout(lim.head_timeout_s):
                while True:
                    end = buf.find(b"\r\n\r\n")
                    if end >= 0:
                        return buf[:end], buf[end + 4:]
                    if b"\r\n" not in buf and len(buf) > lim.request_line:
                        raise Refused("request-line-too-long", 414)
                    if len(buf) > lim.header_bytes:
                        raise Refused("headers-too-large", 431)
                    chunk = await reader.read(4096)
                    if not chunk:
                        raise Refused("incomplete-request", 400)
                    buf += chunk
        except TimeoutError:
            raise Refused("head-timeout", 408) from None

    async def connect_target(self, host: str, port: int, unix: str | None):
        """Open the upstream connection for an ALLOWED target: resolve once, validate every address, connect to the IP."""
        if unix is not None:
            return await asyncio.wait_for(asyncio.open_unix_connection(unix), self.limits.connect_timeout_s)
        addresses = validated_addresses(await resolve_once(host, port, self.resolver), self.policy)
        last: Exception | None = None
        for address in addresses:
            try:
                return await asyncio.wait_for(asyncio.open_connection(address, port), self.limits.connect_timeout_s)
            except (OSError, asyncio.TimeoutError) as e:
                last = e
        raise Refused("upstream-unreachable", 502) from last

    async def _pipe(self, cr, cw, ur, uw) -> None:
        lim = self.limits

        async def copy(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            total = 0
            try:
                while True:
                    data = await asyncio.wait_for(src.read(65536), lim.idle_timeout_s)
                    if not data:
                        break
                    total += len(data)
                    if total > lim.max_bytes:
                        break
                    dst.write(data)
                    await dst.drain()
            except (OSError, asyncio.TimeoutError, ConnectionError):
                pass
            finally:
                try:
                    if dst.can_write_eof():
                        dst.write_eof()
                except OSError:
                    pass

        tasks = [asyncio.ensure_future(copy(cr, uw)), asyncio.ensure_future(copy(ur, cw))]
        try:
            await asyncio.wait_for(asyncio.gather(*tasks), lim.lifetime_s)
        except asyncio.TimeoutError:
            pass
        finally:
            for t in tasks:
                t.cancel()
            for w in (uw, cw):
                w.close()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.active >= self.limits.max_connections:
            self._log("deny", "<none>", 0, "connection-limit")
            await self._respond(writer, 503, "connection-limit")
            await self._linger(reader)
            writer.close()
            return
        self.active += 1
        upstream_writer = None
        try:
            if not self.bucket.take():
                self._log("deny", "<none>", 0, "rate-limit")
                await self._respond(writer, 429, "rate-limit")
                await self._linger(reader)
                return
            host, port = "<invalid>", 0
            try:
                head, rest = await self._read_head(reader)
                req = parse_head(head, self.limits)
                if req.method == "CONNECT":
                    host, port = split_authority(req.target)
                    path = None
                else:
                    host, port, path = split_absolute_uri(req.target)
                decision = self.policy.decide(host, port)
                if not decision.allowed:
                    raise Refused(decision.reason, 403)
                ur, upstream_writer = await self.connect_target(decision.host, decision.port, decision.unix)
            except Refused as e:
                self._log("deny", host, port, e.code)
                await self._respond(writer, e.status, e.code)
                await self._linger(reader)
                return
            except (OSError, asyncio.TimeoutError):
                self._log("deny", host, port, "upstream-unreachable")
                await self._respond(writer, 502, "upstream-unreachable")
                await self._linger(reader)
                return
            self._log("allow", host, port, decision.reason)
            if path is None:
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                if rest:
                    upstream_writer.write(rest)
            else:
                upstream_writer.write(rewrite_request(req, host, port, path) + rest)
            await writer.drain()
            await upstream_writer.drain()
            await self._pipe(reader, writer, ur, upstream_writer)
        except (OSError, ConnectionError):
            pass
        finally:
            self.active -= 1
            for w in (upstream_writer, writer):
                if w is not None:
                    w.close()

    async def relay_unix(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, unix: str, label: str) -> None:
        """A `listener`: relay this TCP connection verbatim to a unix socket. Same caps, nothing is parsed."""
        if self.active >= self.limits.max_connections or not self.bucket.take():
            self._log("deny", label, 0, "connection-limit")
            writer.close()
            return
        self.active += 1
        upstream_writer = None
        try:
            try:
                ur, upstream_writer = await self.connect_target("", 0, unix)
            except (OSError, asyncio.TimeoutError):
                self._log("deny", label, 0, "upstream-unreachable")
                return
            self._log("allow", label, 0, "unix-listener")
            await self._pipe(reader, writer, ur, upstream_writer)
        finally:
            self.active -= 1
            for w in (upstream_writer, writer):
                if w is not None:
                    w.close()


# ── configuration and entry point ─────────────────────────────────────────────────

_CONFIG_KEYS = {"listen_host", "listen_port", "allow", "unix_routes", "listeners", "test_allow_cidrs", "limits"}


def parse_config(raw: object) -> tuple[Policy, str, int, list[tuple[int, str]], Limits]:
    """(policy, listen host, listen port, [(port, unix path)] listeners, limits). Unknown keys are an error."""
    if not isinstance(raw, dict):
        raise ValueError("the proxy config must be a JSON object")
    extra = set(raw) - _CONFIG_KEYS
    if extra:
        raise ValueError(f"unknown proxy config keys: {sorted(extra)}")
    policy = Policy.build(raw.get("allow", []), raw.get("unix_routes", []), raw.get("test_allow_cidrs", []))
    host = raw.get("listen_host", "0.0.0.0")
    port = raw.get("listen_port", PROXY_PORT_DEFAULT)
    if not isinstance(host, str) or not host:
        raise ValueError("listen_host must be a string")
    try:
        port = parse_port(port)
    except Refused:
        raise ValueError("listen_port must be 1-65535") from None
    listeners: list[tuple[int, str]] = []
    for item in raw.get("listeners", []):
        if not isinstance(item, dict) or set(item) != {"port", "unix"}:
            raise ValueError(f"listener {item!r} must have exactly port and unix")
        if not (isinstance(item["unix"], str) and item["unix"].startswith("/")):
            raise ValueError("listener unix path must be absolute")
        try:
            listeners.append((parse_port(item["port"]), item["unix"]))
        except Refused:
            raise ValueError("listener port must be 1-65535") from None
    overrides = raw.get("limits", {})
    if not isinstance(overrides, dict) or set(overrides) - set(Limits.__dataclass_fields__):
        raise ValueError("limits must be an object naming only known limits")
    limits = Limits(**overrides)
    return policy, host, port, listeners, limits


def load_config(path: str):
    with open(path, "rb") as f:
        data = f.read(MAX_CONFIG_BYTES + 1)
    if len(data) > MAX_CONFIG_BYTES:
        raise ValueError("the proxy config is too large")
    return parse_config(json.loads(data))


async def serve(config, *, ready: Callable[[], None] | None = None, stop: asyncio.Event | None = None,
                resolver=None, log=None):
    """Run until `stop` is set. Returns the bound (host, port) of the forward proxy."""
    policy, host, port, listeners, limits = config
    proxy = Proxy(policy, limits, resolver=resolver, log=log)
    servers = [await asyncio.start_server(proxy.handle, host, port, limit=limits.request_line * 2)]
    for lport, unix in listeners:
        async def relay(r, w, unix=unix, lport=lport):
            await proxy.relay_unix(r, w, unix, f"unix-listener:{lport}")
        servers.append(await asyncio.start_server(relay, host, lport))
    bound = servers[0].sockets[0].getsockname()[:2]
    if ready:
        ready()
    stop = stop or asyncio.Event()
    try:
        await stop.wait()
    finally:
        for s in servers:
            s.close()
        for s in servers:
            await s.wait_closed()
    return bound


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", required=True)
    args = ap.parse_args(argv)
    try:
        config = load_config(args.config)
    except (OSError, ValueError) as e:
        print(f"harness2-proxy: refusing to start: {e}", file=sys.stderr, flush=True)
        return 2

    async def run() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await serve(config, ready=lambda: print(READY_LINE, file=sys.stderr, flush=True), stop=stop)

    asyncio.run(run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
