"""A stand-in for the in-VM client in the fake-sbx tests: it talks to the host egress proxy the way the sbx policy proxy does
(origin-form request, `Host: localhost:<port>`), one request per argument, and prints `<status> <METHOD> <path>` for each.

argv: PORT then any number of METHOD:PATH or METHOD:PATH:MODEL (a POST with a JSON body naming MODEL)."""
import http.client
import json
import sys

port = int(sys.argv[1])
for arg in sys.argv[2:]:
    method, path, *rest = arg.split(":")
    body = json.dumps({"model": rest[0], "messages": []}).encode() if rest else None
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.putrequest(method, path, skip_host=True)
        conn.putheader("Host", f"localhost:{port}")
        if body is not None:
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body)
        resp = conn.getresponse()
        resp.read()
        print(resp.status, method, path, flush=True)
    except OSError as e:
        print("ERR", method, path, type(e).__name__, flush=True)
    finally:
        conn.close()
