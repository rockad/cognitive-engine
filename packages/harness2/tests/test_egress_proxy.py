"""The allow-list forward proxy: the decision table (pure) and the server on loopback (in process).

The pure part opens nothing. The server tests bind 127.0.0.1 only, and use `test_allow_cidrs` to let
the proxy reach a throwaway upstream on loopback, which the default policy would refuse.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json

import pytest

from cognitive_harness2 import egress_proxy as ep
from cognitive_harness2.egress_proxy import Limits, Policy, Refused

API = ("api.anthropic.com", 443)


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
           "unix_routes": [{"host": "ollama.test", "port": 11434, "unix": "/bridge/o.sock"}], "limits": {"max_connections": 3}}
    p = tmp_path / "c.json"
    p.write_text(json.dumps(cfg))
    policy_, host, port, listeners, limits = ep.load_config(str(p))
    assert host == "0.0.0.0" and port == 3128 and listeners == [(11434, "/bridge/o.sock")] and limits.max_connections == 3
    assert policy_.decide("ollama.test", 11434).unix == "/bridge/o.sock"
    for bad in ({"alow": []}, {"allow": [["a.example", 443]], "limits": {"nope": 1}}, [], {"listeners": [{"port": 1}]},
                {"unix_routes": [{"host": "a.example", "port": 1, "unix": "relative"}]}, {"test_allow_cidrs": ["10.0.0.1/8"]}):
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


async def test_oversized_request_line_and_header_blocks_are_cut_off():
    async with Running([["allowed.test", 80]]) as px:
        out = await talk(px.port, b"GET http://allowed.test/" + b"a" * 20000)
        assert out.startswith(b"HTTP/1.1 414 ")
        out = await talk(px.port, b"GET http://allowed.test/ HTTP/1.1\r\n" + b"X-A: " + b"b" * 40000)
        assert out.startswith(b"HTTP/1.1 431 ")
        out = await talk(px.port, b"GET http://allowed.test/ HTTP/1.1\r\n" + b"".join(b"X-%d: v\r\n" % i for i in range(200)) + b"\r\n")
        assert out.startswith(b"HTTP/1.1 431 ")


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


async def test_a_unix_route_and_a_unix_listener_reach_the_socket_without_resolving(tmp_path):
    sock = str(tmp_path / "o.sock")
    seen = []

    async def echo(r, w):
        data = await r.read(1024)
        seen.append(data)
        w.write(b"OLLAMA:" + data)
        await w.drain()
        w.close()
    server = await asyncio.start_unix_server(echo, sock)
    try:
        async with Running([], unix_routes=[{"host": "ollama.test", "port": 11434, "unix": sock}],
                           listeners=[{"port": 11434, "unix": sock}]) as px:
            out = await talk(px.port, b"CONNECT ollama.test:11434 HTTP/1.1\r\n\r\nhello")
            assert out.endswith(b"OLLAMA:hello")
            out = await talk(px.ports[1], b"raw bytes")
            assert out == b"OLLAMA:raw bytes"
            out = await talk(px.port, b"CONNECT ollama.test:11435 HTTP/1.1\r\n\r\n")
            assert out.startswith(b"HTTP/1.1 403 ")
            assert px.resolved == []
    finally:
        server.close()


async def test_a_dead_unix_socket_is_a_502_not_a_hang(tmp_path):
    async with Running([], unix_routes=[{"host": "ollama.test", "port": 11434, "unix": str(tmp_path / "gone.sock")}]) as px:
        out = await talk(px.port, b"CONNECT ollama.test:11434 HTTP/1.1\r\n\r\n")
        assert out.startswith(b"HTTP/1.1 502 ")
