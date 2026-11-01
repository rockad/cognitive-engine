#!/usr/bin/env python3
"""The in-VM client of the live sbx model-route test (runs inside the microVM as the session's "CLI", Python 3 stdlib only).

It does what opencode would do and then everything a hostile session might try, and prints ONE JSON object with what it
got: an HTTP status, or the name of the error, or DATA/NODATA for a raw socket. Nothing here needs a model."""
import http.client
import json
import os
import socket
import urllib.parse

proxy = urllib.parse.urlparse(os.environ["HTTP_PROXY"])
model = urllib.parse.urlparse(os.environ["H2_MODEL_URL"])        # http://host.docker.internal:<port>/v1 (the runner rewrote it)
HOST, PORT = model.hostname, model.port
CHAT = json.dumps({"model": os.environ["H2_MODEL"], "messages": [{"role": "user", "content": "hi"}], "stream": False})
OTHER = json.dumps({"model": "some-other-model:70b", "messages": [], "stream": False})


def via_proxy(method, url, body=None):
    c = http.client.HTTPConnection(proxy.hostname, proxy.port, timeout=12)
    try:
        c.request(method, url, body=body, headers={"Content-Type": "application/json"} if body else {})
        r = c.getresponse()
        r.read(4000)
        return r.status
    except Exception as e:  # noqa: BLE001
        return type(e).__name__
    finally:
        c.close()


def raw(host, port, request=b"GET /v1/models HTTP/1.0\r\n\r\n"):
    s = socket.socket()
    s.settimeout(6)
    try:
        s.connect((host, port))
        s.sendall(request)
        return "DATA" if s.recv(100) else "NODATA"
    except ConnectionRefusedError:
        return "refused"
    except Exception as e:  # noqa: BLE001
        return "NODATA:" + type(e).__name__
    finally:
        s.close()


base = f"http://{HOST}:{PORT}"
out = {
    "models": via_proxy("GET", f"{base}/v1/models"),
    "tags": via_proxy("GET", f"{base}/api/tags"),
    "chat": via_proxy("POST", f"{base}/v1/chat/completions", CHAT),
    "chat_other_model": via_proxy("POST", f"{base}/v1/chat/completions", OTHER),
    "delete": via_proxy("POST", f"{base}/api/delete", json.dumps({"name": "x"})),
    "pull": via_proxy("POST", f"{base}/api/pull", json.dumps({"name": "x"})),
    "ps": via_proxy("GET", f"{base}/api/ps"),
    "example_com": via_proxy("GET", "http://example.com/"),
    "host_ssh_port": via_proxy("GET", f"http://{HOST}:22/"),
    "real_ollama_port": via_proxy("GET", f"http://{HOST}:11434/api/tags"),
    "next_port": via_proxy("GET", f"http://{HOST}:{PORT + 1}/v1/models"),
    "connect_tunnel": via_proxy("CONNECT", f"{HOST}:{PORT}"),
    "direct_to_model_port": raw(HOST, PORT),
    "direct_to_gateway_model_port": raw("gateway.docker.internal", PORT),
    "vm_loopback_model_port": raw("127.0.0.1", PORT),
}
print(json.dumps(out), flush=True)
