"""The allow-list forward proxy: the decision table (pure) and the server on loopback (in process).

The pure part opens nothing. The server tests bind 127.0.0.1 only, and use `test_allow_cidrs` to let
the proxy reach a throwaway upstream on loopback, which the default policy would refuse.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import re

import pytest

from cognitive_harness2 import egress_proxy as ep
from cognitive_harness2.egress_proxy import Limits, Policy, Refused

API = ("api.anthropic.com", 443)
ALLOW = [list(p) for p in ep.OLLAMA_ROUTE_ALLOW]
MODELS = ["m", "gemma4:e4b"]          # the fixtures' bodies name these two


def policy(*allow, **kw) -> Policy:
    return Policy.build(list(allow) or [API], **kw)


# ── host normalisation and the name-level decision ───────────────────────────────

@pytest.mark.parametrize("host", [
    "api.anthropic.com", "API.Anthropic.COM", "api.anthropic.com.", "Api.AnthropiC.com.",
    "api。anthropic。com", "api．anthropic．com",   # ideographic and full-width full stops
])
def test_the_allow_listed_name_passes_in_every_spelling(host):
    d = policy().decide(host, 443)
    assert d.allowed and (d.host, d.port) == API


def test_idna_names_are_listed_and_matched_in_punycode():
    p = policy(("bücher.example", 443))
    assert p.decide("xn--bcher-kva.example", 443).allowed
    assert p.decide("BÜCHER.example.", 443).allowed


@pytest.mark.parametrize("host,port", [
    ("api.anthropic.com", 80), ("api.anthropic.com", 8443), ("api.anthropic.com", 0), ("api.anthropic.com", 65536),
    ("evil-api.anthropic.com", 443), ("xapi.anthropic.com", 443), ("anthropic.com", 443),
    ("api.anthropic.com.evil.com", 443), ("sub.api.anthropic.com", 443), ("evil.com", 443),
    ("api.anthropic.com@evil.com", 443), ("evil.com@api.anthropic.com", 443), ("api.anthropic.com/x", 443),
    ("api.anthropic.com\\@evil.com", 443), ("api.anthropic.com\x00.evil.com", 443), ("api.anthropic.com ", 443),
    ("api.anthropic.com..", 443), ("", 443), (".", 443), ("api..anthropic.com", 443), ("*.anthropic.com", 443),
    ("api.anthropic.com%00", 443), ("api.anthropic.com\r\nHost: evil", 443),
])
def test_everything_else_is_denied(host, port):
    assert not policy().decide(host, port).allowed


@pytest.mark.parametrize("host", [
    "127.0.0.1", "127.1", "2130706433", "0x7f000001", "0x7f.1", "0177.0.0.1", "017700000001", "0.0.0.0",
    "169.254.169.254", "169.254.169.254.", "2852039166", "[::1]", "[::ffff:127.0.0.1]", "[::ffff:7f00:1]",
    "[fd00:ec2::254]", "10.0.0.1", "192.168.127.1", "172.17.0.1", "localhost", "127.0.0.1.nip.io", "1.1",
    "８.８.８.８",       # full-width digits
])
def test_ip_literals_and_numeric_tricks_are_denied_unless_listed(host):
    assert not policy().decide(host, 443).allowed


@pytest.mark.parametrize("host", ["127.1", "2130706433", "0x7f000001", "0177.0.0.1", "1.1"])
def test_ambiguous_numeric_hosts_are_refused_outright_even_if_someone_lists_them(host):
    with pytest.raises(ValueError):
        Policy.build([[host, 443]])
    with pytest.raises(Refused) as e:
        ep.normalize_host(host)
    assert e.value.code == "ambiguous-numeric-host"


def test_an_ip_literal_passes_only_when_that_literal_is_itself_listed():
    p = policy(("203.0.113.9", 443))
    assert p.decide("203.0.113.9", 443).allowed
    assert not p.decide("203.0.113.10", 443).allowed
    assert not p.decide("203.0.113.9", 80).allowed


def test_a_listed_ip_literal_in_a_forbidden_range_is_still_refused_at_connect(monkeypatch):
    p = policy(("10.0.0.5", 443))
    assert p.decide("10.0.0.5", 443).allowed       # name level: listed
    with pytest.raises(Refused) as e:
        ep.validated_addresses(["10.0.0.5"], p)    # address level: private
    assert e.value.code == "address-private"


def test_unlisted_or_wildcard_config_is_refused_loudly():
    for bad in ([["*.anthropic.com", 443]], [["a.example"]], [["a.example", "x"]], ["a.example:443"], [["a.example", 0]]):
        with pytest.raises(ValueError):
            Policy.build(bad)


# ── the address rule ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("addr,code", [
    ("127.0.0.1", "address-loopback"), ("127.255.255.254", "address-loopback"), ("::1", "address-loopback"),
    ("10.0.0.1", "address-private"), ("172.16.0.1", "address-private"), ("192.168.1.1", "address-private"),
    ("172.17.0.1", "address-private"), ("fd00::1", "address-private"), ("fd00:ec2::254", "address-private"),
    ("169.254.169.254", "address-link-local"), ("169.254.0.1", "address-link-local"), ("fe80::1", "address-link-local"),
    ("224.0.0.1", "address-multicast"), ("239.255.255.250", "address-multicast"), ("ff02::1", "address-multicast"),
    ("0.0.0.0", "address-unspecified"), ("::", "address-unspecified"),
    ("240.0.0.1", "address-reserved"), ("255.255.255.255", "address-reserved"),
    ("100.64.0.1", "address-not-global"), ("100.127.255.254", "address-not-global"), ("192.0.2.1", "address-private"),
    ("::ffff:127.0.0.1", "address-loopback"), ("::ffff:10.0.0.1", "address-private"),
    ("::ffff:169.254.169.254", "address-link-local"),
    ("2002:a00:1::", "address-private"), ("2002:7f00:1::", "address-loopback"),      # 6to4 around 10.0.0.1, 127.0.0.1
    ("64:ff9b::a00:1", "address-private"), ("64:ff9b::7f00:1", "address-loopback"),  # NAT64
    ("::127.0.0.1", "address-loopback"),
])
def test_forbidden_addresses(addr, code):
    assert ep.address_problem(ipaddress.ip_address(addr)) == code


@pytest.mark.parametrize("addr", ["8.8.8.8", "1.1.1.1", "160.79.104.10", "2606:4700:4700::1111", "2a00:1450:4001::1"])
def test_public_addresses_pass(addr):
    assert ep.address_problem(ipaddress.ip_address(addr)) is None


def test_test_allow_cidrs_exempt_only_the_named_network():
    nets = (ipaddress.ip_network("172.30.0.0/16"),)
    assert ep.address_problem(ipaddress.ip_address("172.30.1.2"), nets) is None
    assert ep.address_problem(ipaddress.ip_address("172.31.1.2"), nets) == "address-private"
    assert ep.address_problem(ipaddress.ip_address("169.254.169.254"), nets) == "address-link-local"


def test_a_resolved_answer_with_any_forbidden_address_is_refused_whole():
    p = policy()
    assert ep.validated_addresses(["8.8.8.8", "1.1.1.1"], p) == ["8.8.8.8", "1.1.1.1"]
    for answer in (["127.0.0.1"], ["8.8.8.8", "10.0.0.1"], ["8.8.8.8", "169.254.169.254"], ["::ffff:192.168.0.1"], ["not-an-ip"]):
        with pytest.raises(Refused):
            ep.validated_addresses(answer, p)


async def test_an_ip_literal_is_not_resolved():
    async def resolver(h, p):
        raise AssertionError("must not resolve a literal")
    assert await ep.resolve_once("203.0.113.9", 443, resolver) == ["203.0.113.9"]


async def test_resolution_failure_and_empty_answers_are_refusals():
    async def boom(h, p):
        raise OSError("nxdomain")

    async def empty(h, p):
        return []
    for r in (boom, empty):
        with pytest.raises(Refused) as e:
            await ep.resolve_once("x.example", 443, r)
        assert e.value.code == "unresolvable"


# ── request parsing ──────────────────────────────────────────────────────────────

def head(*lines: str, request="GET http://api.anthropic.com/x HTTP/1.1") -> bytes:
    return "\r\n".join([request, *lines]).encode("latin-1")


def test_a_wellformed_head_parses():
    r = ep.parse_head(head("Host: api.anthropic.com", "X-A: b"))
    assert (r.method, r.version) == ("GET", "HTTP/1.1") and r.headers[1] == ("X-A", "b")


@pytest.mark.parametrize("raw,status", [
    (b"GET /" + b"a" * 9000 + b" HTTP/1.1", 414),
    (head(*[f"X-{i}: v" for i in range(101)]), 431),
    (b"GET http://a.example/ HTTP/1.1\nHost: a.example", 400),                 # bare LF
    (head("Host: a", " folded: header"), 400), (head("Host: a", "\tfolded: header"), 400),
    (head("Content-Length: 1", "Content-Length: 2"), 400),
    (head("Content-Length: 1", "Transfer-Encoding: chunked"), 400),
    (head("Bad Header: x"), 400), (head("NoColon"), 400), (head("X: a\x00b"), 400),
    (b"get http://a.example/ HTTP/1.1", 400), (b"GET http://a.example/ HTTP/2.0", 400),
    (b"GET http://a.example/", 400), (b"GET  http://a.example/ HTTP/1.1", 400),
    (b"GET http://a.example/\x7f HTTP/1.1", 400), (b"GET http://a.example/\\x HTTP/1.1", 400),
])
def test_malformed_or_oversized_heads_are_refused(raw, status):
    with pytest.raises(Refused) as e:
        ep.parse_head(raw)
    assert e.value.status == status


@pytest.mark.parametrize("uri,expected", [
    ("http://a.example/p?q=1", ("a.example", 80, "/p?q=1")),
    ("http://A.Example.:8080/", ("a.example", 8080, "/")),
    ("http://a.example", ("a.example", 80, "/")),
    ("http://a.example?x=1", ("a.example", 80, "/?x=1")),
    ("HTTP://a.example/", ("a.example", 80, "/")),
    ("http://[2001:db8::1]:81/z", ("2001:db8::1", 81, "/z")),
])
def test_absolute_uris(uri, expected):
    assert ep.split_absolute_uri(uri) == expected


@pytest.mark.parametrize("uri", [
    "/just/a/path", "https://a.example/", "ftp://a.example/", "http://user@a.example/", "http://a.example@evil.com/",
    "http://a.example/#frag", "http://a.example:99999/", "http://a.example:80:80/", "http://[::1/", "http:///x",
    "http://a.example:-1/", "http://a.example:+80/", "//a.example/",
])
def test_unacceptable_uris(uri):
    with pytest.raises(Refused):
        ep.split_absolute_uri(uri)


def test_connect_authorities():
    assert ep.split_authority("API.anthropic.com:443") == API
    assert ep.split_authority("[2001:db8::1]:443") == ("2001:db8::1", 443)
    for bad in ("api.anthropic.com", "api.anthropic.com:", "a:b:c", "[::1]443", "api.anthropic.com:44 3", "a.example:٤٤٣"):
        with pytest.raises(Refused):
            ep.split_authority(bad)


def test_the_rewritten_request_pins_host_drops_hop_by_hop_and_closes():
    req = ep.parse_head(head("Host: evil.example", "Proxy-Authorization: Basic x", "Connection: keep-alive",
                             "Authorization: Bearer t", "Content-Length: 3", "Upgrade: websocket",
                             request="POST http://api.anthropic.com:8080/v1 HTTP/1.1"))
    out = ep.rewrite_request(req, "api.anthropic.com", 8080, "/v1").decode()
    lines = out.split("\r\n")
    assert lines[0] == "POST /v1 HTTP/1.1" and lines[1] == "Host: api.anthropic.com:8080"
    assert "Authorization: Bearer t" in lines and "Content-Length: 3" in lines
    assert "evil.example" not in out and "Proxy-Authorization" not in out and "Upgrade" not in out
    assert lines.count("Connection: close") == 1 and out.endswith("\r\n\r\n")


def test_the_token_bucket_limits_and_refills():
    now = [0.0]
    b = ep.TokenBucket(2.0, 3, lambda: now[0])
    assert [b.take() for _ in range(4)] == [True, True, True, False]
    now[0] += 1.0
    assert [b.take() for _ in range(3)] == [True, True, False]


# ── config ───────────────────────────────────────────────────────────────────────

def test_config_round_trip_and_loud_failures(tmp_path):
    cfg = {"allow": [["api.anthropic.com", 443]], "listeners": [{"port": 11434, "unix": "/bridge/o.sock"}],
           "unix_routes": [{"host": "ollama.test", "port": 11434, "unix": "/bridge/o.sock", "allow": ALLOW, "allowed_models": MODELS}],
           "limits": {"max_connections": 3}}
    p = tmp_path / "c.json"
    p.write_text(json.dumps(cfg))
    policy_, host, port, listeners, limits = ep.load_config(str(p))
    assert host == "0.0.0.0" and port == 3128 and listeners == [(11434, "/bridge/o.sock")] and limits.max_connections == 3
    assert policy_.decide("ollama.test", 11434).unix == "/bridge/o.sock"
    for bad in ({"alow": []}, {"allow": [["a.example", 443]], "limits": {"nope": 1}}, [], {"listeners": [{"port": 1}]},
                {"unix_routes": [{"host": "a.example", "port": 1, "unix": "relative", "allow": ALLOW, "allowed_models": MODELS}]},
                {"unix_routes": [{"host": "a.example", "port": 1, "unix": "/s"}]},       # no allow list: refused, not defaulted
                {"listeners": [{"port": 1, "unix": "/s"}]},                               # a listener with no route has no allow list
                {"test_allow_cidrs": ["10.0.0.1/8"]}):
        with pytest.raises(ValueError):
            ep.parse_config(bad)
    p.write_bytes(b"x" * (ep.MAX_CONFIG_BYTES + 1))
    with pytest.raises(ValueError):
        ep.load_config(str(p))


def test_main_refuses_to_start_on_a_bad_config(tmp_path, capsys):
    p = tmp_path / "c.json"
    p.write_text('{"allow": [["*", 1]]}')
    assert ep.main(["--config", str(p)]) == 2
    assert "refusing to start" in capsys.readouterr().err


# ── the server, in process, on loopback ──────────────────────────────────────────

class Upstream:
    """A throwaway TCP server: records what it received, answers with a fixed HTTP response."""

    def __init__(self, response: bytes = b"HTTP/1.1 200 OK\r\nContent-Length: 11\r\nConnection: close\r\n\r\nUPSTREAM-OK"):
        self.response, self.received = response, []

    async def __aenter__(self):
        async def handler(r, w):
            data = await r.read(65536)
            self.received.append(data)
            w.write(self.response)
            await w.drain()
            w.close()
        self.server = await asyncio.start_server(handler, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *a):
        self.server.close()
        await self.server.wait_closed()


class Running:
    def __init__(self, allow, *, limits=None, unix_routes=(), listeners=(), resolver=None):
        self.log: list[str] = []
        cfg = {"allow": allow, "unix_routes": list(unix_routes), "listeners": list(listeners),
               "test_allow_cidrs": ["127.0.0.0/8"], "listen_host": "127.0.0.1",
               "limits": limits or {}}
        self.config = ep.parse_config(cfg)
        self.resolver, self.resolved = resolver, []

    async def __aenter__(self):
        self.stop = asyncio.Event()
        bound = asyncio.get_running_loop().create_future()

        async def resolver(h, p):
            self.resolved.append(h)
            return await (self.resolver or (lambda h, p: asyncio.sleep(0, ["127.0.0.1"])))(h, p)

        async def run():
            policy_, host, port, listeners, limits = self.config
            self.proxy = ep.Proxy(policy_, limits, resolver=resolver, log=self.log.append)
            srv = await asyncio.start_server(self.proxy.handle, host, 0)
            servers = [srv]
            for lport, unix in listeners:
                async def relay(r, w, unix=unix):
                    await self.proxy.relay_unix(r, w, unix, "unix-listener")
                servers.append(await asyncio.start_server(relay, host, 0))
            self.ports = [s.sockets[0].getsockname()[1] for s in servers]
            bound.set_result(None)
            await self.stop.wait()
            for s in servers:
                s.close()
        self.task = asyncio.ensure_future(run())
        await bound
        self.port = self.ports[0]
        return self

    async def __aexit__(self, *a):
        self.stop.set()
        await self.task


async def talk(port: int, payload: bytes, *, read: int = 65536, timeout: float = 5.0) -> bytes:
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(payload)
    await w.drain()
    out = b""
    try:
        async with asyncio.timeout(timeout):
            while True:
                chunk = await r.read(read)
                if not chunk:
                    break
                out += chunk
    except TimeoutError:
        pass
    w.close()
    return out


async def test_connect_to_the_listed_host_and_port_is_tunnelled():
    async with Upstream() as up, Running([["allowed.test", up.port]]) as px:
        out = await talk(px.port, f"CONNECT allowed.test:{up.port} HTTP/1.1\r\nHost: x\r\n\r\nGET / HTTP/1.1\r\n\r\n".encode())
        assert out.startswith(b"HTTP/1.1 200 Connection Established\r\n\r\n") and out.endswith(b"UPSTREAM-OK")
        assert up.received[0].startswith(b"GET / HTTP/1.1")
        assert px.resolved == ["allowed.test"]                       # resolved once, by name
        assert px.log == [f"ACCESS decision=allow host=allowed.test port={up.port} reason=allow-listed"]


async def test_connect_to_anything_else_is_denied_and_nothing_is_resolved():
    async with Upstream() as up, Running([["allowed.test", up.port]]) as px:
        for target in (f"allowed.test:{up.port + 1}", "other.test:443", f"127.0.0.1:{up.port}", "169.254.169.254:80",
                       f"[::1]:{up.port}", f"2130706433:{up.port}", f"localhost:{up.port}"):
            out = await talk(px.port, f"CONNECT {target} HTTP/1.1\r\n\r\n".encode())
            assert out.startswith(b"HTTP/1.1 403 ") or out.startswith(b"HTTP/1.1 400 "), (target, out)
        assert up.received == [] and px.resolved == []
        assert all(line.startswith("ACCESS decision=deny") for line in px.log) and len(px.log) == 7


async def test_a_listed_name_that_resolves_inward_is_refused_and_the_upstream_is_never_contacted():
    async with Upstream() as up:
        for answer in (["127.0.0.1"], ["10.0.0.1"], ["169.254.169.254"], ["8.8.8.8", "127.0.0.1"], ["::1"]):
            async def resolver(h, p, answer=answer):
                return answer
            cfg = ep.parse_config({"allow": [["allowed.test", up.port]], "listen_host": "127.0.0.1"})   # NO test cidrs
            log: list[str] = []
            proxy = ep.Proxy(cfg[0], cfg[4], resolver=resolver, log=log.append)
            srv = await asyncio.start_server(proxy.handle, "127.0.0.1", 0)
            out = await talk(srv.sockets[0].getsockname()[1], f"CONNECT allowed.test:{up.port} HTTP/1.1\r\n\r\n".encode())
            srv.close()
            assert out.startswith(b"HTTP/1.1 403 "), (answer, out)
            assert "reason=address-" in log[0]
        assert up.received == []


async def test_the_connection_goes_to_the_address_resolved_once_not_to_the_name_again():
    async with Upstream() as up:
        calls = []

        async def resolver(h, p):
            calls.append(h)
            return ["127.0.0.1"] if len(calls) == 1 else ["10.0.0.1"]    # a rebinding resolver
        async with Running([["allowed.test", up.port]], resolver=resolver) as px:
            out = await talk(px.port, f"CONNECT allowed.test:{up.port} HTTP/1.1\r\n\r\nGET / HTTP/1.1\r\n\r\n".encode())
        assert out.endswith(b"UPSTREAM-OK") and calls == ["allowed.test"]


async def test_plain_http_is_rewritten_to_origin_form_with_a_pinned_host_and_close():
    async with Upstream() as up, Running([["allowed.test", up.port]]) as px:
        req = (f"GET http://allowed.test:{up.port}/v1/x?secret=QUERYSECRET HTTP/1.1\r\nHost: evil.example\r\n"
               "Proxy-Authorization: Basic cHJveHk6c2VjcmV0\r\nAuthorization: Bearer TOKENSECRET\r\n"
               "Connection: keep-alive\r\n\r\n").encode()
        out = await talk(px.port, req)
        assert out.endswith(b"UPSTREAM-OK")
        got = up.received[0].decode()
        assert got.startswith("GET /v1/x?secret=QUERYSECRET HTTP/1.1\r\n")
        assert f"Host: allowed.test:{up.port}" in got and "evil.example" not in got
        assert "Proxy-Authorization" not in got and "Connection: close" in got and "Bearer TOKENSECRET" in got
        joined = "\n".join(px.log)       # the log never carries a header, a path, a query or a token
        for secret in ("QUERYSECRET", "TOKENSECRET", "cHJveHk6", "/v1/x", "Authorization", "evil.example"):
            assert secret not in joined


async def test_origin_form_requests_and_userinfo_uris_are_refused():
    async with Upstream() as up, Running([["allowed.test", up.port]]) as px:
        for line in ("GET /index HTTP/1.1", f"GET http://user:pw@allowed.test:{up.port}/ HTTP/1.1",
                     f"GET http://allowed.test:{up.port}@evil.test/ HTTP/1.1", f"GET https://allowed.test:{up.port}/ HTTP/1.1"):
            out = await talk(px.port, f"{line}\r\nHost: allowed.test\r\n\r\n".encode())
            assert out.startswith(b"HTTP/1.1 400 "), (line, out)
        assert up.received == []


async def test_a_redirect_to_a_private_address_is_refused_when_the_client_follows_it():
    redirect = b"HTTP/1.1 302 Found\r\nLocation: http://169.254.169.254/latest/meta-data\r\nContent-Length: 0\r\n\r\n"
    async with Upstream(redirect) as up, Running([["allowed.test", up.port]]) as px:
        out = await talk(px.port, f"GET http://allowed.test:{up.port}/ HTTP/1.1\r\n\r\n".encode())
        assert b"302 Found" in out                          # the proxy does not follow redirects itself
        follow = await talk(px.port, b"GET http://169.254.169.254/latest/meta-data HTTP/1.1\r\n\r\n")
        assert follow.startswith(b"HTTP/1.1 403 ")
        follow = await talk(px.port, b"CONNECT 169.254.169.254:80 HTTP/1.1\r\n\r\n")
        assert follow.startswith(b"HTTP/1.1 403 ")
        assert len(up.received) == 1


class Exchange:
    """What a client saw after sending one request: the bytes, and HOW the connection ended."""

    def __init__(self, data: bytes, eof: bool, reset: bool):
        self.data, self.eof, self.reset = data, eof, reset

    @property
    def response(self) -> tuple[bytes, bytes] | None:
        """(status line, body) once a COMPLETE response (head plus Content-Length bytes) arrived, else None."""
        head, sep, body = self.data.partition(b"\r\n\r\n")
        if not sep:
            return None
        lengths = [int(line.split(b":", 1)[1]) for line in head.split(b"\r\n")[1:] if line.lower().startswith(b"content-length:")]
        if len(lengths) != 1 or len(body) < lengths[0]:
            return None
        return head.split(b"\r\n")[0], body[:lengths[0]]


async def exchange(port: int, payload: bytes, *, read_delay: float = 0.0, timeout: float = 5.0) -> Exchange:
    """Send `payload` in the background and read the reply, tolerating a reset the way a real client must.

    The writer runs concurrently, so a refusal that arrives while we are still sending is read, not lost. A
    reset is reported (`reset`), never raised: whether it is acceptable is the caller's assertion."""
    r, w = await asyncio.open_connection("127.0.0.1", port)

    async def send() -> None:
        try:
            w.write(payload)
            await w.drain()
        except (ConnectionError, OSError):
            pass
    sender = asyncio.ensure_future(send())
    await asyncio.sleep(read_delay)
    data, eof, reset = b"", False, False
    try:
        async with asyncio.timeout(timeout):
            while True:
                chunk = await r.read(65536)
                if not chunk:
                    eof = True
                    break
                data += chunk
    except (ConnectionError, OSError):
        reset = True
    except TimeoutError:
        pass
    sender.cancel()
    w.close()
    return Exchange(data, eof, reset)


OVERSIZED = {
    "request line": (b"GET http://allowed.test/" + b"a" * 20000, b"HTTP/1.1 414 ", "request-line-too-long"),
    "header block": (b"GET http://allowed.test/ HTTP/1.1\r\n" + b"X-A: " + b"b" * 40000, b"HTTP/1.1 431 ", "headers-too-large"),
    "header count": (b"GET http://allowed.test/ HTTP/1.1\r\n" + b"".join(b"X-%d: v\r\n" % i for i in range(200)) + b"\r\n",
                     b"HTTP/1.1 431 ", None),
}


def assert_refused_and_nothing_forwarded(ex: Exchange, status: bytes, up, px, reason: str | None) -> None:
    """The security property, strictly: a complete refusal, the connection is over, nothing reached the upstream."""
    assert ex.response is not None, f"no complete refusal (data={ex.data[:60]!r}, reset={ex.reset})"
    assert ex.response[0].startswith(status.rstrip()), ex.response
    assert ex.eof or ex.reset, "the connection was left open"
    assert up.received == [], "request bytes were forwarded upstream"
    assert [x for x in px.log if "decision=allow" in x] == []
    denies = [x for x in px.log if "decision=deny" in x]
    assert len(denies) == 1 and (reason is None or f"reason={reason}" in denies[0]), px.log


@pytest.mark.parametrize("name", OVERSIZED)
async def test_oversized_request_line_and_header_blocks_are_cut_off(name):
    """Refused (414/431), nothing forwarded, connection closed. A RST AFTER the complete refusal is tolerated here
    (the client may still be writing); `test_a_refusal_is_delivered_and_the_close_is_clean...` pins the clean close."""
    payload, status, reason = OVERSIZED[name]
    async with Upstream() as up, Running([["allowed.test", up.port]]) as px:
        ex = await exchange(px.port, payload)
        assert_refused_and_nothing_forwarded(ex, status, up, px, reason)


@pytest.mark.parametrize("name", OVERSIZED)
async def test_a_refusal_is_delivered_and_the_close_is_clean_even_to_a_slow_reader_still_sending(name):
    """Regression: the proxy used to close with request bytes still unread, so the kernel sent a RST that could
    discard the 4xx the client had not read yet (a measured ~2% of rounds lost the reply entirely, ~40% of the
    old test's runs saw ECONNRESET). The refusal must now arrive intact, followed by a clean EOF, no reset."""
    payload, status, reason = OVERSIZED[name]
    filler = b"c" * 300_000      # far more than one 64 KiB read: the old one-shot drain could not keep up
    async with Upstream() as up, Running([["allowed.test", up.port]]) as px:
        ex = await exchange(px.port, payload + filler, read_delay=0.8)   # reads only after the proxy has closed
        assert_refused_and_nothing_forwarded(ex, status, up, px, reason)
        assert ex.eof and not ex.reset, (ex.eof, ex.reset)


async def test_a_client_that_never_stops_sending_cannot_hold_the_proxy_open():
    """The linger is bounded in bytes and in time: a client sending without end is cut off, still refused, never forwarded."""
    async with Upstream() as up, Running([["allowed.test", up.port]]) as px:
        r, w = await asyncio.open_connection("127.0.0.1", px.port)
        w.write(b"GET http://allowed.test/" + b"a" * 20000)

        async def flood() -> None:
            try:
                while True:
                    w.write(b"z" * 65536)
                    await w.drain()
            except (ConnectionError, OSError):
                pass
        task = asyncio.ensure_future(flood())
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        async with asyncio.timeout(10):
            while px.proxy.active:
                await asyncio.sleep(0.05)
        assert loop.time() - t0 < 5
        task.cancel()
        w.close()
        assert up.received == []


async def test_a_slowloris_client_is_timed_out_on_the_whole_head_not_per_byte():
    async with Running([["allowed.test", 80]], limits={"head_timeout_s": 0.5}) as px:
        r, w = await asyncio.open_connection("127.0.0.1", px.port)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        data = b""
        for byte in b"GET http://allowed.test/ HTTP/1.1\r\nX: aaaaaaaaaaaaaaaaaaaaaaaaaaaaa":
            w.write(bytes([byte]))
            try:
                await w.drain()
                data = await asyncio.wait_for(r.read(4096), 0.1)   # a byte every 100 ms; the reply is awaited in between
            except asyncio.TimeoutError:
                continue
            except ConnectionError:
                break
            break
        assert data.startswith(b"HTTP/1.1 408 ") and loop.time() - t0 < 3
        w.close()


async def test_connection_and_rate_caps():
    async with Running([["allowed.test", 80]], limits={"max_connections": 2, "head_timeout_s": 5}) as px:
        held = [await asyncio.open_connection("127.0.0.1", px.port) for _ in range(2)]
        await asyncio.sleep(0.1)
        out = await talk(px.port, b"CONNECT allowed.test:80 HTTP/1.1\r\n\r\n")
        assert out.startswith(b"HTTP/1.1 503 ")
        for _, w in held:
            w.close()
    async with Running([["allowed.test", 80]], limits={"rate_per_s": 0.001, "burst": 2}) as px:
        outs = [await talk(px.port, b"CONNECT other.test:80 HTTP/1.1\r\n\r\n") for _ in range(4)]
        assert [o[:12] for o in outs[:2]] == [b"HTTP/1.1 403"] * 2 and [o[:12] for o in outs[2:]] == [b"HTTP/1.1 429"] * 2


async def test_idle_and_byte_caps_end_a_tunnel():
    async with Upstream(b"x" * 5000) as up, Running([["allowed.test", up.port]], limits={"max_bytes": 1000}) as px:
        out = await talk(px.port, f"CONNECT allowed.test:{up.port} HTTP/1.1\r\n\r\nping".encode())
        body = out.split(b"\r\n\r\n", 1)[1]
        assert len(body) <= 1000
    # an upstream that never answers: the tunnel is closed by the idle cap
    quiet = await asyncio.start_server(lambda r, w: asyncio.sleep(5), "127.0.0.1", 0)
    port = quiet.sockets[0].getsockname()[1]
    async with Running([["allowed.test", port]], limits={"idle_timeout_s": 0.3}) as px:
        t0 = asyncio.get_running_loop().time()
        out = await talk(px.port, f"CONNECT allowed.test:{port} HTTP/1.1\r\n\r\n".encode(), timeout=4)
        assert out.startswith(b"HTTP/1.1 200 ") and asyncio.get_running_loop().time() - t0 < 3
    quiet.close()




async def test_a_dead_unix_socket_is_a_502_not_a_hang(tmp_path):
    route = {"host": "ollama.test", "port": 11434, "unix": str(tmp_path / "gone.sock"), "allow": ALLOW, "allowed_models": MODELS}
    async with Running([], unix_routes=[route]) as px:
        out = await talk(px.port, rq("GET", "/v1/models", absolute=True))
        assert out.startswith(b"HTTP/1.1 502 ") and b"upstream-unreachable" in out


# ══ the unix route is HTTP, not a tunnel (the host bridge reaches ollama's WHOLE API) ═══════════════════════════════

ROUTE_HOST = "ollama.test"
ROUTE_PORT = 11434
CHAT = ("POST", "/v1/chat/completions")
BODY = b'{"model":"m","messages":[{"role":"user","content":"SECRET-BODY-TEXT"}]}'
JSON_OK = b'{"ok":true}'
PAD100 = b'{"model":"m"}' + b" " * 87          # a valid body of exactly 100 bytes
CLOSE = object()          # in a UnixOllama responder's parts: hang up after answering


def rq(method: str, target: str, *, host: str | None = f"{ROUTE_HOST}:{ROUTE_PORT}", body: bytes = b"", extra=(),
       version: str = "HTTP/1.1", close: bool = True, absolute: bool = False, chunks: list[bytes] | None = None) -> bytes:
    """One request, as bytes. `absolute` writes the proxy form `http://ollama.test:11434<target>`; `chunks` sends the body chunked."""
    lines = [f"{method} {'http://' + ROUTE_HOST + ':' + str(ROUTE_PORT) if absolute else ''}{target} {version}"]
    if host is not None:
        lines.append(f"Host: {host}")
    if chunks is not None:
        lines.append("Transfer-Encoding: chunked")
        body = b"".join(f"{len(c):x}\r\n".encode() + c + b"\r\n" for c in chunks) + b"0\r\n\r\n"
    elif body or method == "POST":
        lines.append(f"Content-Length: {len(body)}")
    lines += list(extra)
    if close:
        lines.append("Connection: close")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body


def responses(raw: bytes) -> list[tuple[int, dict[str, str], bytes]]:
    """Split what a client read into (status, lower-cased headers, body); Content-Length, chunked and close-delimited bodies."""
    out, rest = [], raw
    while rest.startswith(b"HTTP/1."):
        head, _, rest = rest.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        headers = {k.lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:])}
        status = int(lines[0].split()[1])
        if "content-length" in headers:
            n = int(headers["content-length"])
            body, rest = rest[:n], rest[n:]
        elif headers.get("transfer-encoding") == "chunked":
            body = b""
            while True:
                size_line, _, rest = rest.partition(b"\r\n")
                size = int(size_line, 16)
                if not size:
                    rest = rest[2:]
                    break
                body, rest = body + rest[:size], rest[size + 2:]
        else:
            body, rest = rest, b""
        out.append((status, headers, body))
    return out


class UnixOllama:
    """A throwaway HTTP/1.1 server on a unix socket (what the host bridge leads to): parses every request, records it as
    the proxy delivered it, answers from `responder(record)` (bytes, or a list of bytes and float sleeps), keeps alive."""

    def __init__(self, path: str, responder=None):
        self.path, self.requests, self.connections = path, [], 0
        self.responder = responder or (lambda rec: b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                                       b"Content-Length: %d\r\n\r\n" % len(JSON_OK) + JSON_OK)

    async def __aenter__(self):
        self.server = await asyncio.start_unix_server(self._handle, self.path)
        return self

    async def __aexit__(self, *a):
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, r, w):
        self.connections += 1
        try:
            while True:
                head = (await r.readuntil(b"\r\n\r\n"))[:-4].decode("latin-1").split("\r\n")
                method, target, version = head[0].split(" ")
                headers = [tuple(ln.split(": ", 1)) for ln in head[1:]]
                low = {k.lower(): v for k, v in headers}
                body = await r.readexactly(int(low.get("content-length", 0)))
                rec = {"method": method, "target": target, "version": version, "headers": headers, "low": low, "body": body}
                self.requests.append(rec)
                resp = self.responder(rec)
                hang_up = low.get("connection") == "close"
                for part in [resp] if isinstance(resp, bytes) else resp:
                    if isinstance(part, float):
                        await asyncio.sleep(part)
                    elif part is CLOSE:
                        hang_up = True
                    else:
                        w.write(part)
                        await w.drain()
                if hang_up:
                    break
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            w.close()


class Routed:
    """A proxy with one unix route (`ollama.test:11434`, and `127.0.0.1:11434`) and a listener, in front of a `UnixOllama`."""

    def __init__(self, tmp_path, responder=None, models=None, **limits):
        self.up = UnixOllama(str(tmp_path / "o.sock"), responder)
        self.limits, self.models = limits, models or MODELS

    async def __aenter__(self):
        await self.up.__aenter__()
        routes = [{"host": h, "port": ROUTE_PORT, "unix": self.up.path, "allow": ALLOW, "allowed_models": self.models} for h in (ROUTE_HOST, "127.0.0.1")]
        self.px = await Running([], unix_routes=routes, listeners=[{"port": ROUTE_PORT, "unix": self.up.path}],
                                limits={"head_timeout_s": 5, "idle_timeout_s": 5, **self.limits}).__aenter__()
        self.log, self.port, self.listener = self.px.log, self.px.ports[0], self.px.ports[1]
        return self

    async def __aexit__(self, *a):
        await self.px.__aexit__(*a)
        await self.up.__aexit__(*a)

    def denied(self) -> list[str]:
        return [x for x in self.log if "decision=deny" in x]


@pytest.fixture
def routed(tmp_path):
    return lambda responder=None, **limits: Routed(tmp_path, responder, **limits)


MANAGEMENT = [
    ("DELETE", "/api/delete"), ("POST", "/api/delete"), ("POST", "/api/pull"), ("POST", "/api/push"), ("POST", "/api/create"),
    ("POST", "/api/copy"), ("POST", "/api/generate"), ("POST", "/api/chat"), ("POST", "/api/embed"),
    ("POST", "/api/embeddings"), ("GET", "/api/ps"), ("GET", "/api/version"), ("GET", "/"), ("HEAD", "/api/tags"),
    # POST /api/show is allowed (2026-10-31); everything near it is not: other methods, other spellings, other names
    ("GET", "/api/show"), ("HEAD", "/api/show"), ("PUT", "/api/show"), ("PATCH", "/api/show"), ("DELETE", "/api/show"),
    ("OPTIONS", "/api/show"), ("POST", "/api/show/"), ("POST", "/api/shows"), ("POST", "/api/show/x"), ("POST", "/api/Show"),
    ("POST", "/API/show"), ("POST", "/api/sho"), ("POST", "/api/show.json"), ("POST", "/api/ps"), ("POST", "/api/version"),
    ("POST", "/api/me"), ("POST", "/api/status"), ("POST", "/api/embed/show"), ("POST", "/show"), ("POST", "/v1/show"),
    ("HEAD", "/api/blobs/sha256:0000"), ("POST", "/api/blobs/sha256:0000"), ("POST", "/v1/embeddings"),
    ("GET", "/v1/models/gemma4:e4b"), ("DELETE", "/v1/models/gemma4:e4b"), ("GET", "/api/tags/x"), ("POST", "/api/tags"),
    # the allowed paths with the wrong method
    ("GET", "/v1/chat/completions"), ("DELETE", "/v1/chat/completions"), ("PUT", "/v1/chat/completions"),
    ("PATCH", "/v1/completions"), ("POST", "/v1/models"), ("DELETE", "/v1/models"), ("OPTIONS", "/v1/models"),
    ("POST", "/api/tags"), ("TRACE", "/v1/models"),
]


@pytest.mark.parametrize("via", ["absolute-uri", "origin-form", "listener"])
@pytest.mark.parametrize("method,path", MANAGEMENT)
async def test_every_ollama_management_endpoint_and_every_wrong_method_is_a_logged_403_that_never_reaches_ollama(routed, via, method, path):
    async with routed() as rt:
        body = BODY if method in ("POST", "PUT", "PATCH") else b""
        payload = rq(method, path, body=body, absolute=via == "absolute-uri")
        out = await talk(rt.listener if via == "listener" else rt.port, payload)
        (status, headers, text), = responses(out)
        assert status == 403 and text == b"403 unix-route-path-denied\n" and headers["connection"] == "close"
        assert rt.up.requests == [] and rt.up.connections == 0
        assert rt.log == [f"ACCESS decision=deny host={ROUTE_HOST} port={ROUTE_PORT} reason=unix-route-path-denied "
                          f"method={method} path={path}"], rt.log


SHOW = ("POST", "/api/show")
SHOW_BODY = b'{"model":"gemma4:e4b"}'          # what opencode sends: the model name, nothing else


@pytest.mark.parametrize("method,path", [("GET", "/api/tags"), ("GET", "/v1/models"), CHAT, ("POST", "/v1/completions"), SHOW])
@pytest.mark.parametrize("via", ["absolute-uri", "origin-form", "listener"])
async def test_the_five_allowed_pairs_pass_every_way_in_and_arrive_rebuilt(routed, via, method, path):
    async with routed() as rt:
        body = (SHOW_BODY if (method, path) == SHOW else BODY) if method == "POST" else b""
        payload = rq(method, path, body=body, absolute=via == "absolute-uri",
                     extra=["Accept: application/json", "X-Custom: kept"])
        out = await talk(rt.listener if via == "listener" else rt.port, payload)
        (status, _, text), = responses(out)
        assert (status, text) == (200, JSON_OK)
        (got,) = rt.up.requests
        assert (got["method"], got["target"], got["version"]) == (method, path, "HTTP/1.1")
        assert got["body"] == body
        names = [k.lower() for k, _ in got["headers"]]
        assert names.count("host") == 1 and got["low"]["host"] == f"{ROUTE_HOST}:{ROUTE_PORT}"        # pinned, one only
        assert got["low"]["x-custom"] == "kept" and got["low"]["connection"] == "close"
        host_allow = "unix-listener" if via == "listener" else "unix-route"
        assert len(rt.log) == 1 and rt.log[0].endswith(f"reason={host_allow}") and "decision=allow" in rt.log[0]


@pytest.mark.parametrize("target", [
    "/v1/chat/completions?stream=true", "/v1/chat/completions?", "/api/tags?x=1", "/v1/models?limit=1",
    "/v1/chat/completions/", "/v1/models/", "/api/tags/", "/V1/chat/completions", "/v1/Chat/Completions", "/API/TAGS",
    "/v1/chat%2Fcompletions", "/v1/%63hat/completions", "/v1/chat/completions%00", "/v1/chat/completions%20", "/%76%31/chat/completions",
    "//v1/chat/completions", "/v1//chat/completions", "/v1/chat//completions", "///api/tags",
    "/v1/./chat/completions", "/./v1/chat/completions", "/v1/chat/../chat/completions", "/api/../v1/models",
    "/api/show/", "/api/show?x", "/api/show?", "/api/show?model=x", "/API/show", "/api/SHOW", "/api/Show", "/api/show%2f", "/api/show%2F",
    "/api/show%00", "/api/show%20", "/api/%73how", "/api/sh%6fw", "/api%2fshow", "/api/shows", "/api/show.", "/api/show;x=1",
    "/api/show#frag", "/api/show\t", "//api/show", "/api//show", "/./api/show", "/api/./show", "/api/show/.", "/api/show/..",
    "/api/show/../pull", "/api/show/../../api/delete", "/api/show;/../../api/delete", "/api/show?/../../api/delete",
    "/v1/models/../../api/delete", "/v1/chat/completions/.","/v1/chat/completions/..", "/v1/chat/completions/../../api/pull",
    "/v1/chat/completions;x=1", "/v1/chat/completions;/../../api/delete", "/api/tags%2e%2e/delete", "/api/%2e%2e/api/delete",
    "/v1/chat/completions#frag", "/v1/chat/completions\t", "/v1/chat/completions%0d%0aX: y", "/v1/chat/completions.", "/v1/chat/completionsx",
    "/ v1/chat/completions".replace(" ", "%20"), "/v1/chat/completions?/../../api/delete", "/v1/models?/api/delete",
])
@pytest.mark.parametrize("absolute", [True, False])
async def test_an_allowed_path_in_any_other_spelling_is_denied_exactly_not_normalised(routed, target, absolute):
    async with routed() as rt:
        method = "POST" if "chat" in target or "show" in target else "GET"
        out = await talk(rt.port, rq(method, target, body=BODY if method == "POST" else b"", absolute=absolute))
        statuses = [s for s, _, _ in responses(out)]
        assert statuses and statuses[0] in (400, 403), out
        assert rt.up.requests == [] and rt.up.connections == 0
        assert not any(x for x in rt.log if "decision=allow" in x)


async def test_a_lower_case_method_or_a_bad_request_line_is_a_400_and_nothing_is_forwarded(routed):
    async with routed() as rt:
        for payload in (b"post /v1/chat/completions HTTP/1.1\r\nHost: ollama.test:11434\r\nContent-Length: 0\r\n\r\n",
                        b"GET /v1/models  HTTP/1.1\r\nHost: ollama.test:11434\r\n\r\n",
                        b"GET /v1/models HTTP/2.0\r\nHost: ollama.test:11434\r\n\r\n",
                        b"GET /v1/models HTTP/1.1 extra\r\nHost: ollama.test:11434\r\n\r\n",
                        b"GET /v1/models\x00 HTTP/1.1\r\nHost: ollama.test:11434\r\n\r\n",
                        b"GET /v1/models HTTP/1.1\r\nHost: ollama.test:11434\r\nX\r\n\r\n"):
            assert responses(await talk(rt.port, payload))[0][0] == 400, payload
        assert rt.up.requests == []


# ── absolute-URI vs origin-form, Host header forms, CONNECT, HTTP/1.0 ───────────────────────────────

@pytest.mark.parametrize("host", ["ollama.test:11434", "OLLAMA.TEST:11434", "ollama.test.:11434", "Ollama.Test.:11434"])
async def test_the_host_header_of_an_origin_form_request_may_be_written_any_way_that_normalises_to_the_route(routed, host):
    async with routed() as rt:
        assert responses(await talk(rt.port, rq("GET", "/v1/models", host=host)))[0][0] == 200
        assert responses(await talk(rt.listener, rq("GET", "/v1/models", host=host)))[0][0] == 200
        assert [r["low"]["host"] for r in rt.up.requests] == [f"{ROUTE_HOST}:{ROUTE_PORT}"] * 2


@pytest.mark.parametrize("host,status,code", [
    ("evil.example:11434", 400, "not-a-proxy-request"), ("ollama.test:11435", 400, "not-a-proxy-request"),
    ("ollama.test", 400, "not-a-proxy-request"), ("127.1:11434", 400, "ambiguous-numeric-host"),
    ("user@ollama.test:11434", 400, "bad-"), ("ollama.test:11434:1", 400, "bad-authority"),
    ("ollama.test:abc", 400, "bad-port"), ("", 400, "bad-authority"), ("[::1]:11434", 400, "not-a-proxy-request"),
    ("ollama.test:11434, evil.example", 400, "bad-authority"), ("ollama.test/x", 400, "bad-host"),
    (None, 400, "missing-host"),
])
async def test_an_origin_form_request_whose_host_is_not_the_route_is_refused_on_the_proxy_port(routed, host, status, code):
    async with routed() as rt:
        (s, _, text), = responses(await talk(rt.port, rq("GET", "/v1/models", host=host)))
        assert s == status and code.encode() in text, text
        assert rt.up.requests == []


@pytest.mark.parametrize("host,code", [("evil.example:11434", "unix-route-host-mismatch"), ("ollama.test:11435", "unix-route-host-mismatch"),
                                       ("ollama.test", "unix-route-host-mismatch"), ("172.30.0.2:11434", "unix-route-host-mismatch")])
async def test_the_listener_only_serves_the_names_routed_to_its_socket(routed, host, code):
    async with routed() as rt:
        (s, _, text), = responses(await talk(rt.listener, rq("GET", "/v1/models", host=host)))
        assert s == 403 and code.encode() in text and rt.up.requests == []


async def test_a_duplicate_host_header_is_a_400(routed):
    async with routed() as rt:
        out = await talk(rt.port, rq("GET", "/v1/models", extra=["Host: ollama.test:11434"]))
        assert responses(out)[0][0] == 400 and b"duplicate-host" in out and rt.up.requests == []


@pytest.mark.parametrize("host,ok", [(None, True), ("ollama.test:11434", True), ("OLLAMA.test:11434", True), ("evil.example:11434", False),
                                     ("ollama.test:11435", False), ("ollama.test", False)])
async def test_an_absolute_uri_must_agree_with_its_host_header_when_there_is_one(routed, host, ok):
    async with routed() as rt:
        (s, _, text), = responses(await talk(rt.port, rq("GET", "/v1/models", host=host, absolute=True)))
        assert (s == 200) is ok
        if not ok:
            assert s == 403 and b"unix-route-host-mismatch" in text and rt.up.requests == []
            assert rt.log == [f"ACCESS decision=deny host=<invalid> port=0 reason=unix-route-host-mismatch"] or "unix-route-host-mismatch" in rt.log[0]


@pytest.mark.parametrize("target", ["https://ollama.test:11434/v1/models", "http://evil.example:11434/v1/models",
                                    "http://ollama.test:11435/v1/models", "http://user@ollama.test:11434/v1/models",
                                    "http://ollama.test:11434/v1/models#x", "*", "ollama.test:11434", "ftp://ollama.test:11434/v1/models"])
async def test_a_request_target_that_is_not_plain_http_to_the_route_is_refused(routed, target):
    async with routed() as rt:
        out = await talk(rt.port, f"GET {target} HTTP/1.1\r\nHost: ollama.test:11434\r\nConnection: close\r\n\r\n".encode())
        assert responses(out)[0][0] in (400, 403) and rt.up.requests == []


@pytest.mark.parametrize("name", [f"{ROUTE_HOST}:{ROUTE_PORT}", f"127.0.0.1:{ROUTE_PORT}"])
async def test_connect_to_a_unix_route_is_refused_because_a_tunnel_would_carry_any_request(routed, name):
    async with routed() as rt:
        out = await talk(rt.port, f"CONNECT {name} HTTP/1.1\r\nHost: {name}\r\n\r\nDELETE /api/delete HTTP/1.1\r\n\r\n".encode())
        (s, _, text), = responses(out)
        assert s == 403 and text == b"403 unix-route-connect-denied\n"
        assert rt.up.connections == 0 and rt.up.requests == []
        assert rt.log == [f"ACCESS decision=deny host={name.rsplit(':', 1)[0]} port={ROUTE_PORT} reason=unix-route-connect-denied"]


async def test_http_1_0_is_served_once_and_closed(routed):
    async with routed() as rt:
        for port, host in ((rt.port, f"{ROUTE_HOST}:{ROUTE_PORT}"), (rt.port, None), (rt.listener, None)):
            out = await talk(port, rq("GET", "/v1/models", version="HTTP/1.0", host=host, absolute=host is None and port == rt.port))
            assert responses(out)[0][:1] == (200,) and len(responses(out)) == 1
        got = rt.up.requests[0]
        assert got["version"] == "HTTP/1.0" and got["low"]["connection"] == "close"
        # origin-form HTTP/1.0 without a Host on the proxy port has no name to be placed by; on the listener it means its socket
        out = await talk(rt.port, rq("GET", "/v1/models", version="HTTP/1.0", host=None))
        assert responses(out)[0][0] == 400 and b"not-a-proxy-request" in out
        # HTTP/1.0 keep-alive is not honoured: the request is the last on its connection
        out = await talk(rt.port, rq("GET", "/v1/models", version="HTTP/1.0", close=False, extra=["Connection: keep-alive"]) * 2)
        assert len(responses(out)) == 1


# ── keep-alive, pipelining, streaming ──────────────────────────────────────────────────────────────

async def call(r, w, payload: bytes):
    """Write one request, read ONE complete response off an open connection (Content-Length or chunked)."""
    w.write(payload)
    await w.drain()
    head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 5)
    raw = head
    h = head.lower()
    if b"content-length:" in h:
        n = int(h.split(b"content-length:")[1].split(b"\r\n")[0])
        raw += await asyncio.wait_for(r.readexactly(n), 5)
    elif b"chunked" in h:
        while not raw.endswith(b"0\r\n\r\n"):
            raw += await asyncio.wait_for(r.read(65536), 5)
    return responses(raw)[0]


async def test_two_allowed_requests_share_one_keep_alive_connection_and_one_upstream_connection(routed):
    async with routed() as rt:
        r, w = await asyncio.open_connection("127.0.0.1", rt.port)
        a = await call(r, w, rq("GET", "/api/tags", close=False, absolute=True))
        b = await call(r, w, rq(*CHAT, body=BODY, close=False, absolute=True))
        c = await call(r, w, rq("GET", "/v1/models", close=True))
        assert [x[0] for x in (a, b, c)] == [200, 200, 200] and a[2] == b[2] == c[2] == JSON_OK
        assert await asyncio.wait_for(r.read(), 5) == b""                      # Connection: close on the last: the proxy closes
        assert rt.up.connections == 1 and [x["target"] for x in rt.up.requests] == ["/api/tags", CHAT[1], "/v1/models"]
        assert [x["low"].get("connection") for x in rt.up.requests] == [None, None, "close"]
        assert rt.log.count("ACCESS decision=allow host=ollama.test port=11434 reason=unix-route") == 3 and not rt.denied()
        w.close()


async def test_a_denied_request_in_the_middle_of_a_keep_alive_stream_ends_the_stream_and_is_never_forwarded(routed):
    async with routed() as rt:
        r, w = await asyncio.open_connection("127.0.0.1", rt.port)
        assert (await call(r, w, rq("GET", "/api/tags", close=False)))[0] == 200
        (s, h, text) = await call(r, w, rq("DELETE", "/api/delete", close=False, body=BODY))
        assert s == 403 and h["connection"] == "close" and text == b"403 unix-route-path-denied\n"
        assert await asyncio.wait_for(r.read(), 5) == b""
        assert [x["target"] for x in rt.up.requests] == ["/api/tags"]
        w.close()


async def test_a_request_may_name_the_route_by_either_of_its_names_on_one_connection(routed):
    async with routed() as rt:
        r, w = await asyncio.open_connection("127.0.0.1", rt.port)
        assert (await call(r, w, rq("GET", "/api/tags", close=False, host="ollama.test:11434")))[0] == 200
        assert (await call(r, w, rq("GET", "/api/tags", close=False, host="127.0.0.1:11434")))[0] == 200
        assert [x["low"]["host"] for x in rt.up.requests] == ["ollama.test:11434", "127.0.0.1:11434"]
        w.close()


async def test_pipelined_requests_are_answered_in_order_up_to_the_cap_and_a_denied_one_stops_the_rest(routed):
    async with routed() as rt:
        ok = lambda t, **k: rq("GET", t, close=k.get("close", False))            # noqa: E731
        out = await talk(rt.port, ok("/api/tags") + ok("/v1/models") + ok("/api/tags", close=True))
        assert [s for s, _, _ in responses(out)] == [200, 200, 200] and len(rt.up.requests) == 3
        # a fourth request already queued behind three is over the cap (route_max_pipelined = 2): answered 429, not forwarded
        rt.up.requests.clear()
        out = await talk(rt.port, b"".join(ok("/api/tags") for _ in range(4)))
        got = responses(out)
        assert [s for s, _, _ in got] == [200, 200, 200, 429] and b"unix-route-pipeline-limit" in got[-1][2]
        assert len(rt.up.requests) == 3
        # allowed, then denied, then allowed: the allowed one behind the denied one is never forwarded
        rt.up.requests.clear()
        out = await talk(rt.port, ok("/api/tags") + rq("DELETE", "/api/delete", close=False) + ok("/v1/models"))
        got = responses(out)
        assert [s for s, _, _ in got] == [200, 403] and [x["target"] for x in rt.up.requests] == ["/api/tags"]
        # denied first: nothing behind it is forwarded either
        rt.up.requests.clear()
        out = await talk(rt.port, rq("POST", "/api/pull", close=False, body=b"{}") + ok("/api/tags"))
        assert [s for s, _, _ in responses(out)] == [403] and rt.up.requests == []


async def test_a_response_streams_through_unchanged_as_it_is_produced(routed):
    events = [b'data: {"n":%d}\n\n' % i for i in range(5)] + [b"data: [DONE]\n\n"]
    head = (b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-cache\r\nTransfer-Encoding: chunked\r\n\r\n")
    wire = [head]
    for ev in events:
        wire += [b"%x\r\n" % len(ev) + ev + b"\r\n", 0.25]
    wire += [b"0\r\n\r\n"]
    async with routed(lambda rec: wire) as rt:
        r, w = await asyncio.open_connection("127.0.0.1", rt.port)
        w.write(rq(*CHAT, body=BODY, close=False, absolute=True, extra=['Accept: text/event-stream']))
        t0 = asyncio.get_running_loop().time()
        raw, first = b"", None
        while not raw.endswith(b"0\r\n\r\n"):
            raw += await asyncio.wait_for(r.read(65536), 5)
            if first is None and b"data: " in raw:
                first = asyncio.get_running_loop().time() - t0
        total = asyncio.get_running_loop().time() - t0
        assert raw == head + b"".join(b"%x\r\n" % len(e) + e + b"\r\n" for e in events) + b"0\r\n\r\n"      # byte for byte
        assert first < 0.25 and total > 1.0                                  # the first event arrived while the stream was still going
        # and the connection is still good for the next request
        assert (await call(r, w, rq("GET", "/api/tags", close=True)))[0] == 200
        w.close()


async def test_a_close_delimited_and_a_connection_close_response_end_the_client_connection(routed):
    plain = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n" + JSON_OK         # no length, no chunking: ends at EOF
    async def serve(raw, extra_close):
        async with routed(lambda rec: [raw, CLOSE]) as rt:
            r, w = await asyncio.open_connection("127.0.0.1", rt.port)
            w.write(rq("GET", "/api/tags", close=False))
            data = await asyncio.wait_for(r.read(), 5)
            w.close()
            return data, rt
    data, rt = await serve(plain, False)
    assert data == plain
    shut = b"HTTP/1.1 200 OK\r\nContent-Length: 11\r\nConnection: close\r\n\r\n" + JSON_OK
    data, rt = await serve(shut, True)
    assert data == shut


@pytest.mark.parametrize("name,raw", [
    ("garbage", b"not http at all\r\n\r\n"),
    ("both framings", b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nTransfer-Encoding: chunked\r\n\r\nok"),
    ("two lengths", b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Length: 2\r\n\r\nok"),
    ("100 continue", b"HTTP/1.1 100 Continue\r\n\r\n"),
    ("101 switching", b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: x\r\n\r\n"),
    ("unknown coding", b"HTTP/1.1 200 OK\r\nTransfer-Encoding: gzip\r\n\r\nx"),
    ("signed length", b"HTTP/1.1 200 OK\r\nContent-Length: +2\r\n\r\nok"),
    ("obs-fold", b"HTTP/1.1 200 OK\r\nX: a\r\n b\r\nContent-Length: 0\r\n\r\n"),
])
async def test_a_malformed_response_from_the_upstream_becomes_a_clean_502(routed, name, raw):
    async with routed(lambda rec: raw) as rt:
        out = await talk(rt.port, rq("GET", "/api/tags"))
        (s, _, text), = responses(out)
        assert s == 502 and b"unix-route-bad-response" in text, (name, out)


async def test_an_upstream_that_closes_before_answering_is_a_502_and_one_that_dies_mid_body_just_ends_the_stream(routed):
    async with routed(lambda rec: b"") as rt:
        (s, _, text), = responses(await talk(rt.port, rq("GET", "/api/tags")))
        assert s == 502 and b"upstream-closed" in text
    async with routed(lambda rec: b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\npartial") as rt:
        out = await talk(rt.port, rq("GET", "/api/tags"))
        assert out.endswith(b"\r\n\r\npartial") and b"502" not in out
        assert any("reason=unix-route-response-aborted" in x for x in rt.denied())


async def test_response_size_cap_cuts_an_endless_response_off(routed):
    big = b"x" * 5000
    async with routed(lambda rec: b"HTTP/1.1 200 OK\r\nContent-Length: 5000\r\n\r\n" + big, route_max_response_bytes=1000) as rt:
        out = await talk(rt.port, rq("GET", "/api/tags"))
        assert len(out.split(b"\r\n\r\n", 1)[1]) <= 1000 + 65536 and b"x" * 5000 not in out
        assert any("reason=unix-route-response-too-large" in x for x in rt.denied())
    chunk = b"%x\r\n" % 600 + b"y" * 600 + b"\r\n"
    async with routed(lambda rec: [b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n", chunk, chunk, chunk, 0.1, chunk, b"0\r\n\r\n"],
                      route_max_response_bytes=1000) as rt:
        out = await talk(rt.port, rq("GET", "/api/tags"))
        assert out.count(b"y" * 600) < 4 and not out.endswith(b"0\r\n\r\n")


# ── bodies: Content-Length, chunked, caps ──────────────────────────────────────────────────────────

async def test_a_chunked_request_body_is_decoded_and_forwarded_as_one_content_length_body(routed):
    async with routed() as rt:
        pieces = [BODY[:10], BODY[10:11], BODY[11:]]
        out = await talk(rt.port, rq(*CHAT, chunks=pieces))
        assert responses(out)[0][0] == 200
        (got,) = rt.up.requests
        assert got["body"] == BODY and got["low"]["content-length"] == str(len(BODY)) and "transfer-encoding" not in got["low"]


async def test_a_body_arriving_in_pieces_and_a_chunked_body_split_anywhere_are_reassembled(routed):
    async with routed() as rt:
        payload = rq(*CHAT, chunks=[BODY[:7], BODY[7:]])
        r, w = await asyncio.open_connection("127.0.0.1", rt.port)
        for i in range(0, len(payload), 3):                           # three bytes at a time
            w.write(payload[i:i + 3])
            await w.drain()
            await asyncio.sleep(0)
        assert responses(await asyncio.wait_for(r.read(), 5))[0][0] == 200
        assert rt.up.requests[0]["body"] == BODY
        w.close()
        out = await talk(rt.port, rq(*CHAT, body=BODY).replace(b"Content-Length", b"Content-Length", 1))
        assert responses(out)[0][0] == 200 and rt.up.requests[1]["body"] == BODY


CHUNK = lambda n, data: f"{n}\r\n".encode() + data + b"\r\n"             # noqa: E731
BAD_CHUNKED = {
    "non-hex size": CHUNK("zz", b"ab") + b"0\r\n\r\n",
    "extension": CHUNK("2;ext=1", b"ab") + b"0\r\n\r\n",
    "0x prefix": CHUNK("0x2", b"ab") + b"0\r\n\r\n",
    "signed size": CHUNK("+2", b"ab") + b"0\r\n\r\n",
    "spaced size": CHUNK(" 2", b"ab") + b"0\r\n\r\n",
    "empty size": CHUNK("", b"ab") + b"0\r\n\r\n",
    "nine digit size": CHUNK("000000002", b"ab") + b"0\r\n\r\n",
    "trailer": CHUNK("2", b"ab") + b"0\r\nX-Trailer: 1\r\n\r\n",
    "data longer than size": CHUNK("2", b"abc") + b"0\r\n\r\n",
    "data shorter than size": CHUNK("4", b"ab") + b"0\r\n\r\n",
    "bare LF": b"2\nab\r\n0\r\n\r\n",
    "bare CR in size": b"2\rX\r\nab\r\n0\r\n\r\n",
    "missing final CRLF": CHUNK("2", b"ab") + b"0\r\n",
}


@pytest.mark.parametrize("name", BAD_CHUNKED)
async def test_a_malformed_chunked_body_is_refused_before_anything_reaches_ollama(routed, name):
    async with routed(body_timeout_s=1.0) as rt:
        head = (f"POST /v1/chat/completions HTTP/1.1\r\nHost: {ROUTE_HOST}:{ROUTE_PORT}\r\nTransfer-Encoding: chunked\r\n"
                "Connection: close\r\n\r\n").encode()
        out = await talk(rt.port, head + BAD_CHUNKED[name])
        statuses = [s for s, _, _ in responses(out)]
        assert statuses == [400] or statuses == [408], (name, out)
        assert rt.up.requests == [] and rt.up.connections == 0


SMUGGLING = {
    "TE and CL": ["Transfer-Encoding: chunked", "Content-Length: 4"],
    "two CL, different": ["Content-Length: 4", "Content-Length: 5"],
    "two CL, same": ["Content-Length: 4", "Content-Length: 4"],
    "CL list": ["Content-Length: 4, 4"],
    "CL signed": ["Content-Length: +4"],
    "CL hex": ["Content-Length: 0x4"],
    "CL negative": ["Content-Length: -1"],
    "CL huge": ["Content-Length: 99999999999999999999"],
    "CL empty": ["Content-Length:"],
    "space before colon": ["Content-Length : 4"],
    "tab before colon": ["Content-Length\t: 4"],
    "obs-fold": ["Content-Length: 4", " X-Fold: 1"],
    "TE chunked, identity": ["Transfer-Encoding: chunked, identity"],
    "TE identity, chunked": ["Transfer-Encoding: identity, chunked"],
    "TE identity": ["Transfer-Encoding: identity"],
    "TE gzip": ["Transfer-Encoding: gzip"],
    "TE padded": ["Transfer-Encoding: xchunked"],
    "two TE": ["Transfer-Encoding: chunked", "Transfer-Encoding: chunked"],
    "TE header trick": ["Transfer-Encoding:\tchunked\x0b"],
    "Expect": ["Expect: 100-continue", "Content-Length: 4"],
    "Upgrade": ["Upgrade: websocket", "Connection: Upgrade", "Content-Length: 4"],
    "Upgrade alone": ["Upgrade: h2c"],
    "TE trailers": ["TE: trailers"],
    "Trailer": ["Trailer: X"],
    "Connection hides a header": ["Connection: close, Content-Length", "Content-Length: 4"],
    "Connection with a name": ["Connection: X-Foo", "Content-Length: 4"],
    "Connection: upgrade": ["Connection: upgrade", "Content-Length: 4"],
    "NUL in a value": ["X-A: a\x00b", "Content-Length: 4"],
    "bare CR in a value": ["X-A: a\rb", "Content-Length: 4"],
    "non-token name": ["X A: b", "Content-Length: 4"],
    "non-ascii name": ["X-\xe9: b", "Content-Length: 4"],
}


@pytest.mark.parametrize("name", SMUGGLING)
async def test_framing_smuggling_attempts_are_refused_and_never_forwarded(routed, name):
    async with routed(head_timeout_s=1.0) as rt:
        head = (f"POST /v1/chat/completions HTTP/1.1\r\nHost: {ROUTE_HOST}:{ROUTE_PORT}\r\n" + "\r\n".join(SMUGGLING[name])
                + "\r\nConnection: close\r\n\r\nabcd").encode("latin-1")
        tail = b"0\r\n\r\nDELETE /api/delete HTTP/1.1\r\nHost: ollama.test:11434\r\n\r\n"
        out = await talk(rt.port, head + tail)
        statuses = [s for s, _, _ in responses(out)]
        assert statuses and statuses[0] == 400 and 200 not in statuses and len(statuses) == 1, (name, out)
        assert rt.up.requests == [] and rt.up.connections == 0


async def test_a_second_request_hidden_after_the_declared_body_is_judged_on_its_own_and_denied(routed):
    async with routed() as rt:
        smuggled = b"DELETE /api/delete HTTP/1.1\r\nHost: ollama.test:11434\r\n\r\n"
        body = b'{"model":"m"} '
        payload = (f"POST /v1/chat/completions HTTP/1.1\r\nHost: {ROUTE_HOST}:{ROUTE_PORT}\r\nContent-Length: {len(body)}\r\n\r\n").encode() + body + smuggled
        out = await talk(rt.port, payload)
        assert [s for s, _, _ in responses(out)] == [200, 403]
        assert [(x["method"], x["target"]) for x in rt.up.requests] == [CHAT]
        # a body longer than declared leaves junk in front of the next head, which is not a request at all
        rt.up.requests.clear()
        out = await talk(rt.port, payload.replace(f"Content-Length: {len(body)}".encode(), f"Content-Length: {len(body) - 1}".encode()))
        assert [s for s, _, _ in responses(out)] == [200, 400] and len(rt.up.requests) == 1 and rt.up.requests[0]["body"] == body[:-1]


async def test_post_api_show_has_the_same_strict_framing_and_caps_as_the_other_posts(routed):
    """`POST /api/show` is not a lighter door: chunked bodies are decoded and re-sent as a length, the body cap and the
    smuggling refusals apply, and a request hidden behind its body is judged on its own. (The cap is the shared one: the
    route table has no per-entry caps.)"""
    async with routed() as rt:
        out = await talk(rt.port, rq(*SHOW, chunks=[SHOW_BODY[:5], SHOW_BODY[5:]]))
        assert responses(out)[0][0] == 200
        (got,) = rt.up.requests
        assert got["body"] == SHOW_BODY and got["low"]["content-length"] == str(len(SHOW_BODY)) and "transfer-encoding" not in got["low"]
        out = await talk(rt.port, rq(*SHOW))                                              # a missing body names no model: refused
        assert responses(out)[0][0] == 403 and b"unix-route-model-denied" in out and len(rt.up.requests) == 1
        rt.up.requests.clear()
        for extra in (["Expect: 100-continue"], ["Transfer-Encoding: chunked"], ["Upgrade: websocket", "Connection: Upgrade"], ["TE: trailers"]):
            head = (f"POST /api/show HTTP/1.1\r\nHost: {ROUTE_HOST}:{ROUTE_PORT}\r\nContent-Length: 4\r\n" + "\r\n".join(extra)
                    + "\r\nConnection: close\r\n\r\nabcd").encode()
            out = await talk(rt.port, head)
            assert [s for s, _, _ in responses(out)][:1] == [400], (extra, out)
        assert rt.up.requests == []                                                       # none of the refused ones was forwarded
        smuggled = b"DELETE /api/delete HTTP/1.1\r\nHost: ollama.test:11434\r\n\r\n"
        payload = (f"POST /api/show HTTP/1.1\r\nHost: {ROUTE_HOST}:{ROUTE_PORT}\r\nContent-Length: {len(SHOW_BODY)}\r\n\r\n").encode() + SHOW_BODY + smuggled
        out = await talk(rt.port, payload)
        assert [s for s, _, _ in responses(out)] == [200, 403]
        assert [(x["method"], x["target"]) for x in rt.up.requests] == [SHOW]
    async with routed(route_max_request_bytes=100) as rt:
        assert responses(await talk(rt.port, rq(*SHOW, body=b"x" * 101)))[0][0] == 413
        assert responses(await talk(rt.port, rq(*SHOW, chunks=[b"x" * 60, b"x" * 41])))[0][0] == 413
        assert responses(await talk(rt.port, rq(*SHOW, body=PAD100)))[0][0] == 200 and len(rt.up.requests) == 1


async def test_a_get_with_a_body_and_a_missing_post_length_are_handled_strictly(routed):
    async with routed() as rt:
        out = await talk(rt.port, rq("GET", "/api/tags", extra=["Content-Length: 4"]) + b"abcd")
        assert responses(out)[0][0] == 400 and rt.up.requests == []
        out = await talk(rt.port, rq("GET", "/api/tags", chunks=[b"ab"]))
        assert responses(out)[0][0] == 400 and rt.up.requests == []
        out = await talk(rt.port, rq("GET", "/api/tags", extra=["Content-Length: 0"]))        # an empty one is fine
        assert responses(out)[0][0] == 200
        rt.up.requests.clear()
        out = await talk(rt.port, b"POST /v1/chat/completions HTTP/1.1\r\nHost: ollama.test:11434\r\nConnection: close\r\n\r\n")
        assert responses(out)[0][0] == 403 and b"unix-route-model-denied" in out and rt.up.requests == []      # no body, no model


async def test_the_request_body_cap_applies_to_declared_and_to_chunked_bodies(routed):
    async with routed(route_max_request_bytes=100) as rt:
        out = await talk(rt.port, rq(*CHAT, body=b"x" * 101))
        assert responses(out)[0][0] == 413 and b"unix-route-body-too-large" in out
        out = await talk(rt.port, rq(*CHAT, chunks=[b"x" * 60, b"x" * 41]))
        assert responses(out)[0][0] == 413
        out = await talk(rt.port, rq(*CHAT, chunks=[PAD100]))
        assert responses(out)[0][0] == 200 and len(rt.up.requests) == 1
        out = await talk(rt.port, rq(*CHAT, body=PAD100))
        assert responses(out)[0][0] == 200 and len(rt.up.requests) == 2
    async with routed(route_max_request_bytes=1000) as rt:
        head = f"POST /v1/chat/completions HTTP/1.1\r\nHost: {ROUTE_HOST}:{ROUTE_PORT}\r\nContent-Length: 5000\r\nConnection: close\r\n\r\n"
        out = await talk(rt.port, head.encode() + b"x" * 5000)          # refused on the declared length, before the body is read
        assert responses(out)[0][0] == 413 and rt.up.requests == []


async def test_a_body_that_stalls_or_stops_short_is_never_forwarded(routed):
    async with routed(body_timeout_s=0.5) as rt:
        r, w = await asyncio.open_connection("127.0.0.1", rt.port)
        w.write(rq(*CHAT, body=BODY)[:-5])                               # five bytes of the body never come
        out = await asyncio.wait_for(r.read(), 5)
        assert responses(out)[0][0] == 408 and rt.up.requests == []
        w.close()
        r, w = await asyncio.open_connection("127.0.0.1", rt.port)
        w.write(rq(*CHAT, body=BODY)[:-5])
        w.write_eof()
        assert responses(await asyncio.wait_for(r.read(), 5))[0][0] == 400 and rt.up.requests == []
        w.close()


async def test_the_request_line_and_header_caps_hold_on_every_request_of_a_keep_alive_stream(routed):
    async with routed() as rt:
        r, w = await asyncio.open_connection("127.0.0.1", rt.port)
        assert (await call(r, w, rq("GET", "/api/tags", close=False)))[0] == 200
        w.write(b"GET /" + b"a" * 9000 + b" HTTP/1.1\r\nHost: ollama.test:11434\r\n\r\n")
        out = await asyncio.wait_for(r.read(), 5)
        assert responses(out)[0][0] == 414 and len(rt.up.requests) == 1
        w.close()
        r, w = await asyncio.open_connection("127.0.0.1", rt.port)
        assert (await call(r, w, rq("GET", "/api/tags", close=False)))[0] == 200
        w.write(b"GET /api/tags HTTP/1.1\r\nHost: ollama.test:11434\r\n" + b"X-A: " + b"a" * 40000 + b"\r\n\r\n")
        assert responses(await asyncio.wait_for(r.read(), 5))[0][0] == 431
        w.close()


async def test_the_request_count_is_per_session_over_all_connections(routed):
    async with routed(route_max_requests=3) as rt:
        for _ in range(2):
            assert responses(await talk(rt.port, rq("GET", "/api/tags")))[0][0] == 200
        # the third is the last this proxy will forward: it is served, and its connection ends after it
        r, w = await asyncio.open_connection("127.0.0.1", rt.port)
        (s, _, _) = await call(r, w, rq("GET", "/api/tags", close=False))
        assert s == 200 and await asyncio.wait_for(r.read(), 5) == b""
        w.close()
        out = await talk(rt.port, rq("GET", "/api/tags"))
        assert responses(out)[0][0] == 429 and b"unix-route-request-limit" in out
        out = await talk(rt.listener, rq("GET", "/api/tags"))
        assert responses(out)[0][0] == 429
        assert len(rt.up.requests) == 3


async def test_an_idle_keep_alive_connection_is_dropped_quietly(routed):
    async with routed(idle_timeout_s=0.3) as rt:
        r, w = await asyncio.open_connection("127.0.0.1", rt.port)
        assert (await call(r, w, rq("GET", "/api/tags", close=False)))[0] == 200
        assert await asyncio.wait_for(r.read(), 5) == b""                    # closed by the proxy after the idle time, no reply
        w.close()
        assert not rt.denied()


# ── what the log may say ───────────────────────────────────────────────────────────────────────────

async def test_the_log_has_decisions_hosts_ports_and_codes_a_path_only_on_a_path_denial_and_never_a_header_a_body_or_a_query(routed):
    async with routed() as rt:
        secret = ["Authorization: Bearer SECRET-TOKEN-123", "X-Api-Key: SECRET-KEY-456"]
        await talk(rt.port, rq(*CHAT, body=BODY, extra=secret))
        await talk(rt.port, rq("DELETE", "/api/delete?key=SECRET-PATH-789", body=BODY, extra=secret))
        await talk(rt.listener, rq("POST", "/api/pull", body=BODY, extra=secret))
        await talk(rt.port, rq(*CHAT, body=BODY, extra=secret, host="evil.example:11434"))
        await talk(rt.port, rq(*CHAT, extra=secret + ["Transfer-Encoding: chunked"]))
        text = "\n".join(rt.log)
        assert all(re.fullmatch(r"ACCESS decision=(allow|deny) host=\S+ port=\d+ reason=[a-z-]+"
                                r"( method=[A-Z?]+ path=\S+)?", x) for x in rt.log), rt.log
        for x in rt.log:                                      # method and path ride on the path denials, and on nothing else
            assert (" path=" in x) == x.endswith(tuple(f"path={p}" for p in ("/api/delete?<19>", "/api/pull"))), x
        for leak in ("SECRET", "Bearer", "messages", "Authorization"):
            assert leak not in text, leak
        assert "reason=unix-route-path-denied" in text and "host=ollama.test port=11434" in text
        denied = [x for x in rt.log if "decision=deny" in x and "path-denied" in x]
        assert denied == ["ACCESS decision=deny host=ollama.test port=11434 reason=unix-route-path-denied method=DELETE path=/api/delete?<19>",
                          "ACCESS decision=deny host=ollama.test port=11434 reason=unix-route-path-denied method=POST path=/api/pull"]


import re   # noqa: E402  (used by the log test above)


# ── what a path denial says about the request (method and a sanitised path, so an unknown request is diagnosable) ──

@pytest.mark.parametrize("target,shown", [
    ("/v1/models", "/v1/models"),
    ("/api/show", "/api/show"),
    ("/v1/models/", "/v1/models/"),
    ("/", "/"),
    ("/v1/models?", "/v1/models?<0>"),                                      # an empty query is still marked
    ("/v1/models?x=SECRET", "/v1/models?<8>"),                              # the query is never logged, only its length
    ("/v1/models?" + "q" * 123, "/v1/models?<123>"),
    ("/a?b?c", "/a?<3>"),                                                   # the first ? starts the query
    ("/v1/models/gemma4:e4b", "/v1/models/gemma4:e4b"),
    ("/x%0d%0aACCESS", "/x%0d%0aACCESS"),                                   # already-encoded text is passed through as text
    ("/a b", "/a%20b"),                                                     # space, control, DEL and non-ASCII are escaped per byte
    ("/a\r\nACCESS decision=allow", "/a%0D%0AACCESS%20decision=allow"),
    ("/a\x00\x1f\x7f", "/a%00%1F%7F"),
    ("/café", "/caf%C3%A9"),
    ("/ \u0085", "/%E2%80%A8%C2%85"),
    ("", ""),
])
def test_the_denied_target_is_logged_escaped_and_without_its_query(target, shown):
    assert ep.route_log_target(target) == shown
    assert re.fullmatch(r"[\x21-\x7e]*", ep.route_log_target(target))


def test_an_oversized_target_is_cut_to_80_characters_without_splitting_an_escape():
    assert ep.route_log_target("/" + "a" * 5000) == "/" + "a" * 79
    assert ep.route_log_target("/" + "\n" * 5000) == "/" + "%0A" * 26            # 1 + 78 = 79 chars: no half escape
    assert len(ep.route_log_target("/" + "\n" * 5000)) == 79
    assert len(ep.route_log_target("/é" * 5000)) <= 80
    assert ep.route_log_target("/" + "a" * 5000 + "?" + "z" * 30).endswith("?<30>")
    assert len(ep.route_log_target("/" + "a" * 5000 + "?" + "z" * 30)) == 80
    assert len(ep.route_log_target("/" + "a" * 79)) == 80 and ep.route_log_target("/" + "a" * 79) == "/" + "a" * 79


@pytest.mark.parametrize("method,shown", [
    ("GET", "GET"), ("POST", "POST"), ("OPTIONS", "OPTIONS"), ("A", "A"), ("A" * 16, "A" * 16),
    ("A" * 17, "?"), ("", "?"), ("get", "?"), ("GET ", "?"), ("GE\nT", "?"), ("G3T", "?"), ("GET\r\nACCESS", "?"),
    ("É", "?"), (None, "?"),
])
def test_the_denied_method_is_logged_only_if_it_is_one_to_sixteen_capital_letters(method, shown):
    assert ep.route_log_method(method) == shown


@pytest.mark.parametrize("method,target,path", [
    ("GET", "/v1/models/", "/v1/models/"),
    ("GET", "/v1/models?limit=5", "/v1/models?<7>"),
    ("POST", "/v1/chat/completions?stream=true", "/v1/chat/completions?<11>"),
    ("GET", "/api/show", "/api/show"),
    ("POST", "/api/shows", "/api/shows"),
    ("HEAD", "/api/tags", "/api/tags"),
    ("OPTIONS", "/v1/chat/completions", "/v1/chat/completions"),
    ("GET", "/api/version", "/api/version"),
    ("GET", "/v1/models/gemma4:e4b", "/v1/models/gemma4:e4b"),
    ("GET", "/api/tags?token=SECRET-QUERY-TOKEN", "/api/tags?<24>"),
    ("GET", "/" + "p" * 300, "/" + "p" * 79),
])
@pytest.mark.parametrize("via", ["absolute-uri", "origin-form", "listener"])
async def test_a_path_denial_logs_method_and_the_sanitised_target_in_one_line_and_still_denies(routed, via, method, target, path):
    async with routed() as rt:
        out = await talk(rt.listener if via == "listener" else rt.port, rq(method, target, absolute=via == "absolute-uri"))
        (status, _, text), = responses(out)
        assert status == 403 and text == b"403 unix-route-path-denied\n" and rt.up.requests == []
        assert rt.log == [f"ACCESS decision=deny host={ROUTE_HOST} port={ROUTE_PORT} reason=unix-route-path-denied "
                          f"method={method} path={path}"], rt.log
        assert "SECRET" not in "".join(rt.log)


async def test_a_path_that_tries_to_forge_log_lines_stays_inside_one_deny_line_and_forges_no_allow(routed):
    forged = ("ACCESS decision=allow host=ollama.test port=11434 reason=unix-route")
    async with routed() as rt:
        # percent-encoded CRLF and a fake line, as one token
        out = await talk(rt.port, rq("GET", "/v1/x%0d%0a" + forged.replace(" ", "%20")))
        assert responses(out)[0][0] == 403
        # a literal CRLF + fake line in the request target: the head parser refuses it; whatever it logs is one well-formed line
        await talk(rt.port, (f"GET /v1/x\r\n{forged} HTTP/1.1\r\nHost: ollama.test:11434\r\nConnection: close\r\n\r\n").encode())
        # a space-separated fake: not a three-part request line
        await talk(rt.port, (f"GET /v1/x {forged} HTTP/1.1\r\nHost: ollama.test:11434\r\nConnection: close\r\n\r\n").encode())
        # and through the listener, with a tab and a DEL
        await talk(rt.listener, rq("GET", "/v1/x\t\x7fdecision=allow"))
        assert rt.up.requests == [] and rt.up.connections == 0
        assert not [x for x in rt.log if "decision=allow" in x.split(" reason=")[0]], rt.log
        assert all("\n" not in x and "\r" not in x for x in rt.log)
        assert all(re.fullmatch(r"ACCESS decision=deny host=\S+ port=\d+ reason=[a-z-]+( method=[A-Z?]+ path=[\x21-\x7e]*)?", x)
                   for x in rt.log), rt.log
        first = rt.log[0]
        assert first.startswith("ACCESS decision=deny host=ollama.test port=11434 reason=unix-route-path-denied method=GET path=/v1/x%0d%0a")
        assert first.count("ACCESS") == 2 and first.count(" ") == 6                     # the fake text is inside path=, defanged
        assert sum("decision=deny" in x for x in rt.log) == len(rt.log)


# ── configuration and the pure parts ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("allow", [
    [], [["DELETE", "/api/delete"]], [["GET", "/api/*"]], [["GET", "/v1/models?x=1"]], [["GET", "/v1/models/"]] if False else [["GET", "v1/models"]],
    [["GET", "/v1/../api/tags"]], [["GET", "/v1/./models"]], [["GET", "//api/tags"]], [["GET", "/api/%74ags"]], [["get", "/api/tags"]],
    [["GET"]], [["GET", "/api/tags", "x"]], ["GET /api/tags"], [["GET", 5]], "GET /api/tags", [["GET", "/api/ tags"]], [["GET", "/" + "a" * 300]],
    [["CONNECT", "/api/tags"]], [["GET", "/api/tags#x"]],
])
def test_a_bad_route_allow_list_is_refused_loudly(allow):
    with pytest.raises(ValueError):
        Policy.build([], [{"host": "ollama.test", "port": 11434, "unix": "/s", "allow": allow, "allowed_models": ["m"]}])


def test_routes_to_one_socket_must_agree_on_the_allow_list_and_route_keys_are_exact():
    base = {"host": "ollama.test", "port": 11434, "unix": "/s", "allowed_models": ["m"]}
    ok = Policy.build([], [{**base, "allow": [["GET", "/api/tags"]]}, {**base, "host": "b.test", "allow": [["GET", "/api/tags"]]}])
    assert ok.route_allow[("ollama.test", 11434)] == frozenset({("GET", "/api/tags")})
    with pytest.raises(ValueError, match="same allow list"):
        Policy.build([], [{**base, "allow": [["GET", "/api/tags"]]}, {**base, "host": "b.test", "allow": [["GET", "/v1/models"]]}])
    with pytest.raises(ValueError):
        Policy.build([], [{**base, "allow": [["GET", "/api/tags"]], "extra": 1}])


def test_the_shipped_allow_list_is_exactly_the_five_pairs_and_the_session_runner_uses_it():
    assert set(ep.OLLAMA_ROUTE_ALLOW) == {("GET", "/api/tags"), ("GET", "/v1/models"), ("POST", "/v1/chat/completions"),
                                          ("POST", "/v1/completions"), ("POST", "/api/show")}
    assert len(ep.OLLAMA_ROUTE_ALLOW) == 5
    # of ollama's native /api/* exactly two are reachable: the read-only model list and the read-only model info
    assert sorted(p for _, p in ep.OLLAMA_ROUTE_ALLOW if p.startswith("/api/")) == ["/api/show", "/api/tags"]
    for method, path in ep.OLLAMA_ROUTE_ALLOW:
        assert method in ("GET", "POST") and "?" not in path and not path.endswith("/")
    assert ep.Policy.build([], [{"host": "a.test", "port": 1, "unix": "/s", "allow": ALLOW, "allowed_models": MODELS}]).route_allow[("a.test", 1)] == set(ep.OLLAMA_ROUTE_ALLOW)


def test_the_limits_for_routes_are_configurable_and_default_sane():
    lim = ep.Limits()
    assert lim.route_max_requests >= 500 and 1 << 20 <= lim.route_max_request_bytes <= 16 << 20
    assert lim.route_max_response_bytes >= 1 << 20 and 0 <= lim.route_max_pipelined <= 4
    assert ep.parse_config({"limits": {"route_max_requests": 5}})[4].route_max_requests == 5


def _feed_all(dec, data, step):
    used = 0
    for i in range(0, len(data), step):
        if dec.done:
            break
        used += dec.feed(data[i:i + step])
    return used


@pytest.mark.parametrize("step", [1, 2, 3, 7, 1000])
def test_the_chunked_decoder_gives_the_same_body_however_the_bytes_are_cut(step):
    wire = b"5\r\nhello\r\n1\r\n \r\n5\r\nworld\r\n0\r\n\r\n"
    dec = ep.ChunkedDecoder(limit=100, keep=True)
    assert _feed_all(dec, wire, step) == len(wire) and dec.done and bytes(dec.data) == b"hello world" and dec.size == 11


def test_the_chunked_decoder_stops_at_the_end_of_the_body_and_reports_what_follows_it():
    dec = ep.ChunkedDecoder(limit=100, keep=False)
    wire = b"3\r\nabc\r\n0\r\n\r\nGET / HTTP/1.1\r\n"
    assert dec.feed(wire) == wire.index(b"GET") and dec.done and dec.data == b"" and dec.size == 3


@pytest.mark.parametrize("wire", [b"1" * 9 + b"\r\n", b"G\r\n", b"1;x\r\nA\r\n", b"-1\r\n", b"1\r\nAB\r\n", b"1\r\nA\n", b"0\r\nX: y\r\n\r\n", b"0\n\n",
                                  b"2\r\nab\r\n\r\n", b"\r\n"])
def test_the_chunked_decoder_refuses_every_irregular_form(wire):
    with pytest.raises(Refused):
        ep.ChunkedDecoder(limit=100, keep=True).feed(wire)


def test_the_chunked_decoder_enforces_its_size_limit_on_the_declared_sizes():
    with pytest.raises(Refused) as e:
        ep.ChunkedDecoder(limit=10, keep=False).feed(b"b\r\n")
    assert e.value.code == "unix-route-body-too-large" and e.value.status == 413
    dec = ep.ChunkedDecoder(limit=10, keep=False)
    dec.feed(b"5\r\nabcde\r\n5\r\nabcde\r\n")
    with pytest.raises(Refused):
        dec.feed(b"1\r\n")


def test_request_framing_table():
    def req(method="POST", version="HTTP/1.1", **h):
        return ep.Request(method, "/x", version, tuple((k.replace("_", "-"), v) for k, v in h.items()))
    assert ep.request_framing(req()) == ("none", 0, False)
    assert ep.request_framing(req(Content_Length="0")) == ("none", 0, False)
    assert ep.request_framing(req(Content_Length="12")) == ("length", 12, False)
    assert ep.request_framing(req(Transfer_Encoding="Chunked")) == ("chunked", 0, False)
    assert ep.request_framing(req(Connection="Keep-Alive")) == ("none", 0, False)
    assert ep.request_framing(req(Connection="close")) == ("none", 0, True)
    assert ep.request_framing(req(version="HTTP/1.0")) == ("none", 0, True)
    for bad in (req(Transfer_Encoding="chunked", version="HTTP/1.0"), req(Transfer_Encoding="chunked", Content_Length="3"),
                req("GET", Content_Length="3"), req("GET", Transfer_Encoding="chunked"), req(Content_Length="3 "), req(Content_Length="١٢"),
                req(Content_Length="1" * 13), req(Expect="100-continue"), req(Upgrade="x"), req(TE="trailers"), req(Connection="te")):
        with pytest.raises(Refused):
            ep.request_framing(bad)


def test_response_framing_table():
    assert ep.response_framing(200, [("Content-Length", "5")]) == ("length", 5, False)
    assert ep.response_framing(200, [("Content-Length", "0")]) == ("none", 0, False)
    assert ep.response_framing(200, [("Transfer-Encoding", "chunked"), ("Connection", "close")]) == ("chunked", 0, True)
    assert ep.response_framing(200, []) == ("close", 0, True)
    assert ep.response_framing(204, [("Content-Length", "5")]) == ("none", 0, False)
    assert ep.response_framing(304, []) == ("none", 0, False)
    for status, headers in ((100, []), (101, []), (200, [("Transfer-Encoding", "gzip")]), (200, [("Content-Length", "1"), ("Transfer-Encoding", "chunked")]),
                            (200, [("Content-Length", "1"), ("Content-Length", "1")]), (200, [("Content-Length", "-1")])):
        with pytest.raises(Refused):
            ep.response_framing(status, headers)
    assert ep.parse_response_head(b"HTTP/1.1 200 OK\r\nA: b")[0] == 200
    assert ep.parse_response_head(b"HTTP/1.0 404")[0] == 404
    for bad in (b"HTTP/2 200 OK", b"HTTP/1.1 20 OK", b"HTTP/1.1 600 X", b"junk", b"HTTP/1.1 200 OK\r\n folded: x", b"HTTP/1.1 200 \xff"):
        with pytest.raises(Refused):
            ep.parse_response_head(bad)


def test_routed_target_table():
    def req(target, host=None, version="HTTP/1.1"):
        return ep.Request("GET", target, version, (("Host", host),) if host is not None else ())
    assert ep.routed_target(req("/v1/models", "A.test.:11434"), None) == ("a.test", 11434, "/v1/models")
    assert ep.routed_target(req("http://a.test:11434/v1/models?x=1"), None) == ("a.test", 11434, "/v1/models?x=1")
    assert ep.routed_target(req("http://a.test:11434"), None) == ("a.test", 11434, "/")
    assert ep.routed_target(req("/x", None, "HTTP/1.0"), ("d.test", 7)) == ("d.test", 7, "/x")
    for bad in (req("/x"), req("/x", None, "HTTP/1.0"), req("http://a.test:11434/x", "b.test:11434"), req("http://a.test:11434/x", "a.test"),
                req("*", "a.test:11434"), req("a.test:11434", "a.test:11434"), req("https://a.test:11434/", None)):
        with pytest.raises(Refused):
            ep.routed_target(bad, None)


async def test_a_client_that_stops_reading_cannot_hold_a_streaming_response_open(routed):
    chunk = b"x" * 65536
    stream = [b"HTTP/1.1 200 OK\r\nContent-Length: 40000000\r\n\r\n"] + [chunk] * 500          # far more than the socket buffers hold
    async with routed(lambda rec: stream, idle_timeout_s=0.5) as rt:
        r, w = await asyncio.open_connection("127.0.0.1", rt.port)
        w.write(rq("GET", "/api/tags", close=False))
        await w.drain()
        for _ in range(100):                                                    # never read: the proxy must give up by itself
            await asyncio.sleep(0.1)
            if any("reason=unix-route-response-aborted" in x for x in rt.denied()):
                break
        assert any("reason=unix-route-response-aborted" in x for x in rt.denied()), rt.log
        w.close()


# ══ the model name in a POST body is checked against the route's allowed_models (F3) ════════════════════════════════════
# The host bridge reaches the host's WHOLE ollama. A signed-in ollama relays a model named `x:cloud` off the box, and any
# large local model could be picked: so the body of the three model-naming POSTs is parsed and its model must be exactly
# one of the names the session's channel was approved for.

LOCAL = "gemma4-e4b-16k"
LOCAL_MODELS = [LOCAL, LOCAL + ":latest"]            # ollama accepts the bare name with ':latest' appended
CHAT_PATH, COMP_PATH, SHOW_PATH = "/v1/chat/completions", "/v1/completions", "/api/show"
DENIED_BODY = b"403 unix-route-model-denied\n"


def jb(obj) -> bytes:
    return json.dumps(obj).encode()


# (id, path, body, allowed): `allowed` is what the proxy must do
MODEL_CASES = [
    ("exact", CHAT_PATH, jb({"model": LOCAL, "messages": []}), True),
    ("latest-spelling", CHAT_PATH, jb({"model": LOCAL + ":latest", "messages": []}), True),
    ("completions-exact", COMP_PATH, jb({"model": LOCAL, "prompt": "x"}), True),
    ("completions-latest", COMP_PATH, jb({"model": LOCAL + ":latest", "prompt": "x"}), True),
    ("show-model", SHOW_PATH, jb({"model": LOCAL}), True),
    ("show-name-instead-of-model", SHOW_PATH, jb({"name": LOCAL}), True),
    ("show-name-latest", SHOW_PATH, jb({"name": LOCAL + ":latest"}), True),
    ("show-both-same", SHOW_PATH, jb({"model": LOCAL, "name": LOCAL}), True),
    ("json-escaped-spelling-of-the-same-name", CHAT_PATH, b'{"model":"gemma4\\u002de4b-16k","messages":[]}', True),
    ("whitespace-around-json-is-fine", CHAT_PATH, b' \n{"model": "' + LOCAL.encode() + b'"}\n ', True),
    ("other-local-model", CHAT_PATH, jb({"model": "llama3:70b"}), False),
    ("other-local-model-completions", COMP_PATH, jb({"model": "qwen3:30b"}), False),
    ("other-local-model-show", SHOW_PATH, jb({"model": "qwen3:30b"}), False),
    ("colon-cloud", CHAT_PATH, jb({"model": LOCAL + ":cloud"}), False),
    ("dash-cloud", CHAT_PATH, jb({"model": LOCAL + "-cloud"}), False),
    ("upper-cloud", CHAT_PATH, jb({"model": "gpt-oss:120B-CLOUD"}), False),
    ("cloud-show", SHOW_PATH, jb({"model": "kimi-k2:cloud"}), False),
    ("cloud-in-name-only", SHOW_PATH, jb({"name": "x:cloud"}), False),
    ("show-model-ok-name-cloud", SHOW_PATH, jb({"model": LOCAL, "name": "x:cloud"}), False),
    ("show-name-ok-model-other", SHOW_PATH, jb({"model": "llama3:70b", "name": LOCAL}), False),
    ("missing-model", CHAT_PATH, jb({"messages": []}), False),
    ("empty-object", CHAT_PATH, b"{}", False),
    ("missing-model-show", SHOW_PATH, jb({"verbose": True}), False),
    ("name-is-not-a-model-for-chat", CHAT_PATH, jb({"name": LOCAL}), False),
    ("model-null", CHAT_PATH, b'{"model":null}', False),
    ("model-number", CHAT_PATH, b'{"model":1}', False),
    ("model-true", CHAT_PATH, b'{"model":true}', False),
    ("model-list", CHAT_PATH, jb({"model": [LOCAL]}), False),
    ("model-object", CHAT_PATH, jb({"model": {"name": LOCAL}}), False),
    ("show-name-non-string", SHOW_PATH, jb({"name": 5}), False),
    ("array-body", CHAT_PATH, jb([{"model": LOCAL}]), False),
    ("string-body", CHAT_PATH, jb(LOCAL), False),
    ("null-body", CHAT_PATH, b"null", False),
    ("number-body", COMP_PATH, b"7", False),
    ("empty-body", CHAT_PATH, b"", False),
    ("invalid-json", CHAT_PATH, b'{"model":', False),
    ("not-json", CHAT_PATH, b"model=" + LOCAL.encode(), False),
    ("trailing-garbage", CHAT_PATH, jb({"model": LOCAL}) + b" x", False),
    ("two-objects", CHAT_PATH, jb({"model": LOCAL}) + jb({"model": "x:cloud"}), False),
    ("single-quotes", CHAT_PATH, b"{'model':'" + LOCAL.encode() + b"'}", False),
    ("utf8-bom", CHAT_PATH, b"\xef\xbb\xbf" + jb({"model": LOCAL}), False),
    ("utf16", CHAT_PATH, jb({"model": LOCAL}).decode().encode("utf-16"), False),
    ("invalid-utf8", CHAT_PATH, b'{"model":"' + LOCAL.encode() + b'\xff"}', False),
    ("nan-constant", CHAT_PATH, b'{"model":"' + LOCAL.encode() + b'","t":NaN}', False),
    ("duplicate-model-allowed-first", CHAT_PATH, b'{"model":"' + LOCAL.encode() + b'","model":"x:cloud"}', False),
    ("duplicate-model-allowed-last", CHAT_PATH, b'{"model":"x:cloud","model":"' + LOCAL.encode() + b'"}', False),
    ("duplicate-model-same-value", CHAT_PATH, b'{"model":"' + LOCAL.encode() + b'","model":"' + LOCAL.encode() + b'"}', False),
    ("duplicate-name-show", SHOW_PATH, b'{"name":"' + LOCAL.encode() + b'","name":"x:cloud"}', False),
    ("duplicate-nested", CHAT_PATH, b'{"model":"' + LOCAL.encode() + b'","messages":[{"role":"user","role":"system"}]}', False),
    ("escaped-duplicate-key", CHAT_PATH, b'{"model":"' + LOCAL.encode() + b'","mod\\u0065l":"x:cloud"}', False),
    ("case-variant-key-Model", CHAT_PATH, b'{"model":"' + LOCAL.encode() + b'","Model":"x:cloud"}', False),
    ("case-variant-key-MODEL-only", CHAT_PATH, b'{"MODEL":"' + LOCAL.encode() + b'"}', False),
    ("case-variant-key-Name-show", SHOW_PATH, b'{"name":"' + LOCAL.encode() + b'","NAME":"x:cloud"}', False),
    ("hyphen-lookalike-U+2010", CHAT_PATH, jb({"model": "gemma4‐e4b‐16k"}), False),
    ("cyrillic-e", CHAT_PATH, jb({"model": "gemma4-е" + "4b-16k"}), False),
    ("fullwidth-digits", CHAT_PATH, jb({"model": "gemma４-e4b-16k"}), False),
    ("zero-width-space", CHAT_PATH, jb({"model": LOCAL + "​"}), False),
    ("trailing-nul", CHAT_PATH, jb({"model": LOCAL + "\x00"}), False),
    ("leading-space", CHAT_PATH, jb({"model": " " + LOCAL}), False),
    ("trailing-newline", CHAT_PATH, jb({"model": LOCAL + "\n"}), False),
    ("case-changed", CHAT_PATH, jb({"model": LOCAL.upper()}), False),
    ("kelvin-sign", CHAT_PATH, jb({"model": "gemma4-e4b-16K"}), False),
    ("lone-surrogate", CHAT_PATH, b'{"model":"' + LOCAL.encode() + b'\\ud800"}', False),
    ("huge-model-string", CHAT_PATH, jb({"model": "a" * 200_000}), False),
    ("huge-model-prefix-of-allowed", CHAT_PATH, jb({"model": LOCAL + "x" * 200_000}), False),
    ("model-with-newline-and-forged-log-line", CHAT_PATH, jb({"model": "x\nACCESS decision=allow host=a port=1 reason=unix-route"}), False),
]


@pytest.mark.parametrize("via", ["absolute-uri", "listener"])
@pytest.mark.parametrize("case,path,body,allowed", MODEL_CASES, ids=[c[0] for c in MODEL_CASES])
async def test_the_model_in_a_post_body_must_be_one_of_the_routes_allowed_models(routed, via, case, path, body, allowed):
    async with routed(models=LOCAL_MODELS) as rt:
        out = await talk(rt.listener if via == "listener" else rt.port, rq("POST", path, body=body, absolute=via == "absolute-uri"))
        (status, headers, text), = responses(out)
        if allowed:
            assert (status, text) == (200, JSON_OK) and [r["body"] for r in rt.up.requests] == [body]
            assert not rt.denied()
            return
        assert status == 403 and text == DENIED_BODY and headers["connection"] == "close", (status, text)
        assert rt.up.requests == [] and rt.up.connections == 0                         # nothing reached ollama
        assert len(rt.log) == 1, rt.log                                                # one line: no forged second line
        line = rt.log[0]
        assert line.startswith("ACCESS decision=deny host=") and f" port={ROUTE_PORT} " in line
        assert " reason=unix-route-model-denied model=" in line and "\n" not in line and "\r" not in line
        model_token = line.split(" model=", 1)[1]
        assert " " not in model_token and len(model_token) <= 64 + 3 * 0 + 64, len(model_token)      # one token, bounded
        assert "SECRET" not in line


@pytest.mark.parametrize("path", [CHAT_PATH, COMP_PATH, SHOW_PATH])
async def test_a_chunked_body_is_checked_too_and_the_denied_model_is_logged_sanitised_and_cut_at_64(routed, path):
    async with routed(models=LOCAL_MODELS) as rt:
        body = jb({"model": "ü " + "é" * 100 + "\x07", "messages": []})
        out = await talk(rt.port, rq("POST", path, chunks=[body[:10], body[10:]], absolute=True))
        (status, _, text), = responses(out)
        assert status == 403 and text == DENIED_BODY and rt.up.requests == []
        token = rt.log[0].split(" model=", 1)[1]
        assert token.startswith("%C3%BC%20%C3%A9") and len(token) <= 64 and re.fullmatch(r"[!-~]+", token) and token.endswith("%A9")


async def test_a_chunked_allowed_model_passes_with_its_body_intact(routed):
    async with routed(models=LOCAL_MODELS) as rt:
        body = jb({"model": LOCAL, "messages": [{"role": "user", "content": "hi"}]})
        out = await talk(rt.port, rq("POST", CHAT_PATH, chunks=[body[:7], body[7:]], absolute=True))
        assert responses(out)[0][0] == 200 and rt.up.requests[0]["body"] == body


@pytest.mark.parametrize("method,path", [("GET", "/api/tags"), ("GET", "/v1/models")])
async def test_the_get_paths_have_no_body_and_need_no_model(routed, method, path):
    async with routed(models=LOCAL_MODELS) as rt:
        assert responses(await talk(rt.port, rq(method, path, absolute=True)))[0][0] == 200


async def test_the_model_check_runs_per_request_on_a_keep_alive_connection(routed):
    async with routed(models=LOCAL_MODELS) as rt:
        good, bad = jb({"model": LOCAL}), jb({"model": LOCAL + ":cloud"})
        out = await talk(rt.port, rq("POST", CHAT_PATH, body=good, close=False, absolute=True) + rq("POST", CHAT_PATH, body=bad, absolute=True))
        rs = responses(out)
        assert [r[0] for r in rs] == [200, 403] and rs[1][2] == DENIED_BODY
        assert [r["body"] for r in rt.up.requests] == [good]


def test_the_log_helper_escapes_and_cuts_the_model_string():
    assert ep.route_log_model(LOCAL) == LOCAL
    assert ep.route_log_model("a b\n") == "a%20b%0A"
    assert ep.route_log_model("a?b=c") == "a%3Fb%3Dc" or ep.route_log_model("a?b=c") == "a?b=c"
    assert len(ep.route_log_model("x" * 1000)) == 64 and len(ep.route_log_model("é" * 1000)) <= 64
    assert ep.route_log_model("é" * 1000).endswith("%A9")                 # never a split escape
    assert ep.route_log_model(5) == "(non-string)" and ep.route_log_model(None) == "(non-string)"


@pytest.mark.parametrize("raw", [
    None, [], "m", ["m", 5], ["m", ""], [" m"], ["m "], ["a:cloud"], ["a-CLOUD"], ["cloud"], ["x" * 129], ["m\n"], ["m", "m\x00"],
    ["m/../x"], ["é"], ["a b"], ["*"], ["m", "*"], [["m"]], {"m": 1}, ["m"] * 33,
])
def test_a_bad_allowed_models_list_is_refused_loudly(raw):
    route = {"host": "ollama.test", "port": 11434, "unix": "/s", "allow": ALLOW, "allowed_models": raw}
    with pytest.raises(ValueError):
        Policy.build([], [route])


def test_a_route_without_allowed_models_fails_to_load_like_a_route_without_allow():
    base = {"host": "ollama.test", "port": 11434, "unix": "/s", "allow": ALLOW}
    with pytest.raises(ValueError):
        Policy.build([], [base])
    with pytest.raises(ValueError):
        ep.parse_config({"unix_routes": [base], "listeners": [{"port": 11434, "unix": "/s"}]})
    p = Policy.build([], [{**base, "allowed_models": ["m", "m:latest"]}])
    assert p.route_models[("ollama.test", 11434)] == frozenset({"m", "m:latest"})


def test_routes_to_one_socket_must_agree_on_the_allowed_models_too():
    base = {"host": "ollama.test", "port": 11434, "unix": "/s", "allow": ALLOW}
    Policy.build([], [{**base, "allowed_models": ["m"]}, {**base, "host": "b.test", "allowed_models": ["m"]}])
    with pytest.raises(ValueError, match="same allowed_models"):
        Policy.build([], [{**base, "allowed_models": ["m"]}, {**base, "host": "b.test", "allowed_models": ["other"]}])


@pytest.mark.parametrize("path", [CHAT_PATH, COMP_PATH, SHOW_PATH])
def test_check_model_body_is_pure_and_names_the_rule(path):
    allowed = frozenset(LOCAL_MODELS)
    ep.check_model_body(path, jb({"model": LOCAL}), allowed)
    with pytest.raises(Refused) as ei:
        ep.check_model_body(path, jb({"model": "x:cloud"}), allowed)
    assert ei.value.code == "unix-route-model-denied" and ei.value.status == 403 and ei.value.detail == "model=x:cloud"
