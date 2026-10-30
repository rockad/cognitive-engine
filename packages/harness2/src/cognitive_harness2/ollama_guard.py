"""What a loopback-bound Ollama refuses before any handler runs: the DNS-rebinding `Host` guard and the CORS `Origin` guard.

Every stub model server in this package (the selftest's `FakeOllama`, the real-CLI probe's `StubModel`) calls `refusal()`
first, so a stub cannot be more permissive than the Ollama it stands in for. The second real stage-1 run died on exactly
this: the session's provider URL named the proxy alias `harness2-proxy`, the proxy forwards the request with that name as
`Host`, and Ollama answered HTTP 403 to it, while every stub in the tests answered 200 to any `Host`.

MEASURED, read-only, against ollama 0.32.11 listening on 127.0.0.1:11434 (`GET /api/tags` with a chosen header):

  Host                          result    Host                          result
  127.0.0.1:11434               200       harness2-proxy:11434          403
  harness2-proxy.internal:11434 200       evil.example.com:11434        403
  localhost, LOCALHOST:11434    200       localhost.:11434 (dot)        403
  x.localhost, x.local:1        200       internal (no label)           403
  FOO.INTERNAL, .internal       200       foo.internal. (dot)           403
  [::1]:11434, ::1              200       [::1] (no port)               403
  10.1.2.3, 172.16.0.1:5        200       8.8.8.8, 169.254.1.1          403
  192.168.1.1, 0.0.0.0, [::]:1  200       100.64.0.1, 127.1, 0127.0.0.1 403
  fd00::1, [fc00::1]:1          200       fe80::1%eth0                  403
  the machine's hostname        200       u@127.0.0.1 / "a b" / a#b     400 (net/http, before Ollama)
  empty Host value              200       a/b, a\b, a"b, a<b, a|b, a?b  400 (so is any non-ASCII byte)
  127.0.0.1: and 127.0.0.1:abc  200       a,b a'b a;b a=b a~b a!b a$b   200 (the port is never validated)

  Origin                        result
  (none), empty                 200       http://evil.example.com, null 403
  http://localhost[:anything]   200       http://harness2-proxy.internal, http://x.local, http://[::1]:3000 403
  https://127.0.0.1, http://0.0.0.0:1    200   HTTP://LOCALHOST, http://LOCALHOST (case), http://localhost. 403
  app:// file:// tauri:// vscode-webview:// vscode-file://  (anything after) 200
  A refused request has an EMPTY body (Content-Length: 0). A bad `Host` wins over a good `Origin`.
  Authorization is never looked at on these routes (Bearer, Basic and garbage all 200).

Reconstructed from that table and from Ollama's `allowedHostsMiddleware` / `allowedHost` and its default CORS origins:
host = `net.SplitHostPort(Host)`, else the whole value; an IP literal (`netip.ParseAddr`) is allowed when loopback,
private (10/8, 172.16/12, 192.168/16, fc00::/7), unspecified or an address of this machine; otherwise a name is allowed
when, lower-cased, it is empty, `localhost`, this machine's hostname, or ends in `.localhost`, `.local` or `.internal`.
NOT modelled: an address that is merely assigned to a local interface (Ollama allows it, this guard does not unless
passed in `local_ips`), and a listener that is not bound to loopback (Ollama then skips the Host check entirely; every
stub here binds 127.0.0.1).
"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
from typing import Iterable, Mapping

ALLOWED_SUFFIXES = (".localhost", ".local", ".internal")
# bytes net/http accepts in a Host header (httpguts.ValidHostHeader); anything else is a 400 from Go, not a 403 from Ollama
_HOST_BYTES = re.compile(r"[A-Za-z0-9!$%&'()*+,\-.:;=\[\]_~]*")
_ORIGIN_HTTP = re.compile(r"https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0)(?::.*)?\Z", re.S)
_ORIGIN_SCHEMES = ("app://", "file://", "tauri://", "vscode-webview://", "vscode-file://")


def split_host_port(hostport: str) -> str:
    """Ollama's host extraction: Go's `net.SplitHostPort`, and the WHOLE value when that fails (no port, several colons,
    `[v6]` without a port). The port is not validated."""
    i = hostport.rfind(":")
    if i < 0:
        return hostport
    if hostport.startswith("["):
        end = hostport.find("]")
        if end < 0 or end + 1 != i:
            return hostport
        host, j, k = hostport[1:end], 1, end + 1
    else:
        host, j, k = hostport[:i], 0, 0
        if ":" in host:
            return hostport
    if "[" in hostport[j:] or "]" in hostport[k:]:
        return hostport
    return host


def _is_private_or_local(ip: ipaddress._BaseAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped                                  # Go's netip methods see through ::ffff:a.b.c.d
    if ip.is_loopback or ip.is_unspecified:
        return True
    if isinstance(ip, ipaddress.IPv4Address):
        return any(ip in ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
    return ip in ipaddress.ip_network("fc00::/7")            # NOT link-local: python's is_private would be wrong here


def _parse_addr(host: str) -> ipaddress._BaseAddress | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def allowed_host(host_header: str, *, hostname: str | None = None, local_ips: Iterable[str] = ()) -> bool:
    """True when a loopback-bound Ollama serves a request whose `Host` header is `host_header` (400-level malformed values
    are `host_status`'s business)."""
    host = split_host_port(host_header)
    ip = _parse_addr(host)
    if ip is not None and (_is_private_or_local(ip) or str(ip) in set(local_ips)):
        return True
    low = host.lower()
    me = (socket.gethostname() if hostname is None else hostname).lower()
    return low == "" or low == "localhost" or (bool(me) and low == me) or low.endswith(ALLOWED_SUFFIXES)


def host_status(host_header: str | None, *, hostname: str | None = None, local_ips: Iterable[str] = ()) -> int:
    """200 when served, 403 when Ollama refuses the name, 400 when Go's HTTP server would not even parse it."""
    value = host_header or ""
    if not _HOST_BYTES.fullmatch(value):
        return 400
    return 200 if allowed_host(value, hostname=hostname, local_ips=local_ips) else 403


def allowed_origin(origin: str | None) -> bool:
    """Ollama's default CORS origins (no `OLLAMA_ORIGINS`). A request with no `Origin` is not a CORS request: allowed.
    Case-sensitive, exact scheme://host[:anything] for the three loopback names, scheme://anything for the app schemes."""
    if not origin:
        return True
    return bool(_ORIGIN_HTTP.match(origin)) or origin.startswith(_ORIGIN_SCHEMES)


def refusal(headers: Mapping[str, str] | object, *, hostname: str | None = None, local_ips: Iterable[str] = ()) -> int | None:
    """The status a loopback-bound Ollama answers INSTEAD of serving the request (empty body), or None when it serves it.
    `headers` is anything with a case-insensitive `.get` (an `http.client.HTTPMessage`) or a plain dict with lower-case keys."""
    get = headers.get  # type: ignore[union-attr]
    host = get("Host") if get("Host") is not None else get("host")
    status = host_status(host, hostname=hostname, local_ips=local_ips)
    if status != 200:
        return status
    origin = get("Origin") if get("Origin") is not None else get("origin")
    return None if allowed_origin(origin) else 403


# ── the request body of POST /v1/chat/completions ─────────────────────────────────────────────────────────────
#
# MEASURED the same way (read-only: only a model name that does not exist was ever sent, so nothing was loaded or run):
#   not JSON, truncated JSON, an array, an empty body   400 {"error":{"message":<go's reason>,"type":"invalid_request_error",...}}
#   no `model`                                          400 "model is required"
#   no or empty `messages`                              400 "[] is too short - 'messages'"
#   a numeric message `content`                         400 "invalid message content type: float64"
#   an unknown model                                    404 {"error":{"message":"model 'X' not found","type":"not_found_error",...}}
#   a 3 MB body, no Content-Type, `tools`, `stream`, `stream_options`, `store`, `max_completion_tokens`, an unknown role: all accepted
# Validation runs before the model lookup. Everything else about a body (tool schemas, other content shapes, context length)
# is NOT checked by the stub and NOT known to be accepted by a real model: see the README's list of uncertain points.

def _error(status: int, message: str, kind: str = "invalid_request_error") -> tuple[int, bytes]:
    return status, json.dumps({"error": {"message": message, "type": kind, "param": None, "code": None}}).encode()


def chat_request_error(raw: bytes, known_models: Iterable[str]) -> tuple[int, bytes] | None:
    """The (status, JSON body) a real Ollama answers to this `/v1/chat/completions` body INSTEAD of generating, or None."""
    try:
        body = json.loads(raw or b"")
    except ValueError as e:
        return _error(400, f"invalid JSON: {e}")
    if not isinstance(body, dict):
        return _error(400, "json: cannot unmarshal into Go value of type openai.ChatCompletionRequest")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return _error(400, "[] is too short - 'messages'")
    if not body.get("model"):
        return _error(400, "model is required")
    for m in messages:
        c = m.get("content") if isinstance(m, dict) else None
        if isinstance(c, (int, float)) and not isinstance(c, bool):
            return _error(400, "invalid message content type: float64")
    if body["model"] not in set(known_models):
        return _error(404, f"model '{body['model']}' not found", "not_found_error")
    return None
