"""The shared Host/Origin rule every stub model server enforces. The table is what was MEASURED against ollama 0.32.11 on
127.0.0.1:11434 (read-only `GET /api/tags` with a chosen header), so a stub cannot be more permissive than the real thing."""
from __future__ import annotations

import http.client
import http.server
import threading

import pytest

from cognitive_harness2 import ollama_guard as og

ME = "alyx-wsl"

HOSTS = [
    # the four cases the second real stage-1 run turned on
    ("127.0.0.1:11434", 200), ("harness2-proxy:11434", 403), ("harness2-proxy.internal:11434", 200), ("evil.example.com:11434", 403),
    # names
    ("localhost", 200), ("localhost:11434", 200), ("LOCALHOST:11434", 200), ("x.localhost", 200), ("x.local:1", 200),
    ("FOO.INTERNAL", 200), ("x.INTERNAL:1", 200), ("a_b.internal", 200), (".internal", 200), ("a.b.internal:99999", 200),
    ("internal", 403), ("localhost.:11434", 403), ("foo.internal.:11434", 403), ("harness2.internal.evil.com", 403),
    ("harness2-proxy.internal.evil.com:11434", 403), ("evil.internal.example.com", 403), ("notlocalhost", 403),
    (ME, 200), (ME.upper() + ":11434", 200), (ME + ".evil.com", 403),
    # IP literals
    ("[::1]:11434", 200), ("::1", 200), ("[::1]", 403), ("10.1.2.3", 200), ("172.16.0.1:5", 200), ("192.168.1.1", 200),
    ("0.0.0.0", 200), ("[::]:1", 200), ("fd00::1", 200), ("[fc00::1]:1", 200), ("[::ffff:127.0.0.1]:1", 200),
    ("8.8.8.8", 403), ("8.8.8.8:11434", 403), ("169.254.1.1", 403), ("100.64.0.1", 403), ("172.32.0.1", 403), ("127.1", 403),
    ("0127.0.0.1", 403), ("[fe80::1]", 403), ("fe80::1%eth0", 403),
    # ports are never validated; an empty value is served
    ("127.0.0.1:", 200), ("127.0.0.1:abc", 200), ("", 200), (None, 200),
    # not even parsed by Go's HTTP server: a 400 before Ollama's middleware
    ("u@127.0.0.1:11434", 400), ("a b.internal", 400), ("a#b.internal", 400), ("host/../x", 400), ("a\\b.internal", 400), ("é.internal", 400), ("a,b;c=d~e!f.internal", 200),
]


@pytest.mark.parametrize("host,status", HOSTS, ids=[f"{h!r}" for h, _ in HOSTS])
def test_host_rule_matches_what_ollama_answered(host, status):
    assert og.host_status(host, hostname=ME) == status


def test_the_machine_hostname_comes_from_the_machine_by_default():
    import socket
    assert og.host_status(socket.gethostname() + ":1") == 200
    assert og.host_status("definitely-not-" + socket.gethostname()) == 403


def test_an_address_of_this_machine_can_be_declared():
    assert og.host_status("203.0.113.9", hostname=ME) == 403
    assert og.host_status("203.0.113.9", hostname=ME, local_ips=["203.0.113.9"]) == 200


def test_the_proxy_alias_stage_1_uses_is_an_allowed_name_and_the_old_one_is_not():
    from cognitive_harness2 import session_runner as sr
    assert og.host_status(f"{sr.PROXY_ALIAS}:11434", hostname=ME) == 200
    assert og.host_status("harness2-proxy:11434", hostname=ME) == 403


ORIGINS = [
    (None, True), ("", True),
    ("http://localhost", True), ("http://localhost:3000", True), ("https://127.0.0.1", True), ("http://0.0.0.0:1", True),
    ("http://localhost:3000/path", True), ("http://localhost:abc", True),
    ("app://anything", True), ("app://", True), ("file://x", True), ("file:///etc/passwd", True), ("tauri://x", True),
    ("vscode-webview://x", True), ("vscode-file://x", True),
    ("http://evil.example.com", False), ("null", False), ("http://harness2-proxy.internal", False), ("http://x.local", False),
    ("http://[::1]:3000", False), ("https://localhost.evil.com", False), ("HTTP://LOCALHOST:1", False), ("http://LOCALHOST", False),
    ("http://localhost.:1", False), ("http://localhost/path", False), ("ftp://localhost", False), ("capacitor://localhost", False),
    ("http://127.0.0.2", False), ("http://user@localhost", False), ("localhost", False), ("chrome-extension://abc", False),
]


@pytest.mark.parametrize("origin,ok", ORIGINS, ids=[f"{o!r}" for o, _ in ORIGINS])
def test_origin_rule_matches_what_ollama_answered(origin, ok):
    assert og.allowed_origin(origin) is ok


def test_refusal_combines_host_then_origin_and_a_bad_host_wins():
    assert og.refusal({"host": "127.0.0.1:1"}, hostname=ME) is None
    assert og.refusal({"host": "harness2-proxy:1"}, hostname=ME) == 403
    assert og.refusal({"host": "127.0.0.1:1", "origin": "http://evil.example.com"}, hostname=ME) == 403
    assert og.refusal({"host": "evil.com", "origin": "http://localhost"}, hostname=ME) == 403
    assert og.refusal({"host": "u@x"}, hostname=ME) == 400
    assert og.refusal({"Host": "x.internal", "Origin": "http://localhost"}, hostname=ME) is None
    assert og.refusal({}, hostname=ME) is None                                     # a client that sends neither (no Origin: not CORS)


def test_split_host_port_follows_gos_net_splithostport():
    for raw, host in [("a:1", "a"), ("a", "a"), ("[::1]:5", "::1"), ("[::1]", "[::1]"), ("::1", "::1"), ("a:b:c", "a:b:c"),
                      ("[a]b:1", "[a]b:1"), ("x[:1", "x[:1"), (":1", ""), ("a:", "a")]:
        assert og.split_host_port(raw) == host, raw


def test_it_is_enforced_by_a_real_http_server_with_an_empty_403():
    """The guard works through `http.server` headers (case-insensitive `.get`), as the stubs use it."""
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):               # noqa: N802
            code = og.refusal(self.headers, hostname=ME)
            self.send_response(code or 200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        for host, origin, want in [("127.0.0.1", None, 200), ("harness2-proxy.internal", None, 200), ("harness2-proxy", None, 403),
                                   ("127.0.0.1", "http://evil.example.com", 403)]:
            c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
            c.putrequest("GET", "/api/tags", skip_host=True)
            c.putheader("Host", host)
            if origin:
                c.putheader("Origin", origin)
            c.endheaders()
            assert c.getresponse().status == want, (host, origin)
            c.close()
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(5)


CHAT = {"model": "gemma4:12b", "messages": [{"role": "user", "content": "hi"}]}


@pytest.mark.parametrize("raw,status,needle", [
    (b'{"model":', 400, "invalid JSON"), (b"", 400, "invalid JSON"), (b"[]", 400, "openai.ChatCompletionRequest"),
    (b'{"model":"gemma4:12b"}', 400, "too short - 'messages'"), (b'{"model":"gemma4:12b","messages":[]}', 400, "too short"),
    (b'{"messages":[{"role":"user","content":"x"}]}', 400, "model is required"),
    (b'{"model":"gemma4:12b","messages":[{"role":"user","content":123}]}', 400, "invalid message content type"),
    (b'{"model":"nope","messages":[{"role":"user","content":"x"}]}', 404, "model 'nope' not found"),
])
def test_a_chat_body_real_ollama_refuses_is_refused_in_its_error_shape(raw, status, needle):
    import json
    got = og.chat_request_error(raw, ["gemma4:12b"])
    assert got is not None and got[0] == status
    err = json.loads(got[1])["error"]
    assert needle in err["message"] and err["type"] in ("invalid_request_error", "not_found_error") and err["param"] is None


def test_a_body_with_tools_streaming_and_unusual_roles_is_accepted():
    import json
    body = {**CHAT, "stream": True, "stream_options": {"include_usage": True}, "store": False, "max_completion_tokens": 32000,
            "tools": [{"type": "function", "function": {"name": "a", "parameters": {"type": "object", "properties": {}}}}],
            "messages": [{"role": "bogus", "content": [{"type": "text", "text": "x"}]}, {"role": "user", "content": None},
                         {"role": "user", "content": True}]}
    assert og.chat_request_error(json.dumps(body).encode(), ["gemma4:12b"]) is None
    assert og.chat_request_error(json.dumps({**CHAT, "messages": [{"role": "user", "content": "a" * 3_000_000}]}).encode(), ["gemma4:12b"]) is None
