"""The host bridge: target validation (pure), then a real bridge on a loopback echo server."""
from __future__ import annotations

import asyncio
import os
import socket
import stat
import threading

import pytest

from cognitive_harness2.host_bridge import BridgeError, BridgeLimits, HostBridge, validate_target


@pytest.mark.parametrize("host,port", [("127.0.0.1", 11434), ("127.0.0.2", 1), ("::1", 65535)])
def test_loopback_literals_are_accepted(host, port):
    assert validate_target(host, port) == (host, port)


@pytest.mark.parametrize("host", [
    "localhost", "ollama.local", "0.0.0.0", "10.0.0.1", "172.17.0.1", "192.168.127.1", "169.254.169.254", "8.8.8.8",
    "::", "::ffff:127.0.0.1", "fe80::1", "127.1", "2130706433", "0x7f000001", " 127.0.0.1", "127.0.0.1 ", "127.0.0.1:11434",
    "", None, 127, b"127.0.0.1", "127.0.0.1\n", "[::1]",
])
def test_everything_else_is_refused_at_construction(host):
    with pytest.raises(ValueError):
        validate_target(host, 11434)
    with pytest.raises(ValueError):
        HostBridge(host, 11434)


@pytest.mark.parametrize("port", [0, -1, 65536, "11434", None, 1.5, True])
def test_bad_ports(port):
    with pytest.raises(ValueError):
        validate_target("127.0.0.1", port)


def test_the_target_is_fixed_after_construction():
    b = HostBridge("127.0.0.1", 11434)
    assert b.target == ("127.0.0.1", 11434)
    assert not hasattr(b, "set_target")


class Echo:
    """A throwaway loopback server standing in for ollama."""

    def __enter__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.accepted = 0
        threading.Thread(target=self._serve, daemon=True).start()
        return self

    def _serve(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            self.accepted += 1
            threading.Thread(target=self._echo, args=(c,), daemon=True).start()

    @staticmethod
    def _echo(c):
        with c:
            while data := c.recv(65536):
                c.sendall(b"ECHO:" + data)

    def __exit__(self, *a):
        self.sock.close()


def ask(path, payload: bytes, timeout=5.0) -> bytes:
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(timeout)
        s.connect(str(path))
        s.sendall(payload)
        s.shutdown(socket.SHUT_WR)
        out = b""
        try:
            while chunk := s.recv(65536):
                out += chunk
        except (socket.timeout, ConnectionError):
            pass
        return out


def test_bytes_go_to_the_target_and_back_and_the_socket_is_private(tmp_path):
    with Echo() as echo, HostBridge("127.0.0.1", echo.port, base_dir=tmp_path) as b:
        assert stat.S_IMODE(os.stat(b.directory).st_mode) == 0o700
        assert stat.S_IMODE(os.stat(b.socket_path).st_mode) == 0o600
        assert stat.S_ISSOCK(os.stat(b.socket_path).st_mode)
        assert sorted(os.listdir(b.directory)) == ["ollama.sock"]
        assert ask(b.socket_path, b"hello") == b"ECHO:hello"
        assert echo.accepted == 1
        d, s = b.directory, b.socket_path
    assert not os.path.exists(s) and not os.path.exists(d)
    assert list(tmp_path.iterdir()) == []


def test_clean_shutdown_also_when_the_body_raises_and_with_live_connections(tmp_path):
    with Echo() as echo:
        with pytest.raises(RuntimeError, match="boom"):
            with HostBridge("127.0.0.1", echo.port, base_dir=tmp_path) as b:
                held = socket.socket(socket.AF_UNIX)
                held.connect(str(b.socket_path))
                held.sendall(b"x")
                raise RuntimeError("boom")
        held.close()
    assert list(tmp_path.iterdir()) == []


def test_a_dead_target_closes_the_connection_instead_of_hanging(tmp_path):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                                  # nothing listens here now
    with HostBridge("127.0.0.1", port, base_dir=tmp_path) as b:
        assert ask(b.socket_path, b"hello", timeout=5) == b""


def test_the_connection_cap(tmp_path):
    with Echo() as echo, HostBridge("127.0.0.1", echo.port, base_dir=tmp_path,
                                    limits=BridgeLimits(max_connections=2)) as b:
        held = []
        for _ in range(2):
            c = socket.socket(socket.AF_UNIX)
            c.connect(str(b.socket_path))
            c.sendall(b"a")
            assert c.recv(100) == b"ECHO:a"
            held.append(c)
        assert ask(b.socket_path, b"third") == b""            # refused: closed without forwarding
        assert echo.accepted == 2
        for c in held:
            c.close()


def test_the_byte_cap_and_the_idle_timeout(tmp_path):
    with Echo() as echo, HostBridge("127.0.0.1", echo.port, base_dir=tmp_path,
                                    limits=BridgeLimits(max_bytes=100, idle_timeout_s=0.3)) as b:
        out = ask(b.socket_path, b"x" * 5000)
        assert len(out) < 5000                                # a chunk that crosses the cap is never forwarded
        c = socket.socket(socket.AF_UNIX)
        c.settimeout(5)
        c.connect(str(b.socket_path))
        assert c.recv(10) == b""                              # silent peer: closed by the idle cap, not left open
        c.close()


def test_the_bridge_reaches_only_its_one_target(tmp_path):
    """A second listener on loopback is never contacted, whatever the client sends."""
    with Echo() as target, Echo() as other, HostBridge("127.0.0.1", target.port, base_dir=tmp_path) as b:
        ask(b.socket_path, f"CONNECT 127.0.0.1:{other.port} HTTP/1.1\r\n\r\n".encode())
        ask(b.socket_path, b"GET http://127.0.0.1:%d/ HTTP/1.1\r\n\r\n" % other.port)
        assert other.accepted == 0 and target.accepted == 2


def test_nothing_is_logged(tmp_path, capsys):
    with Echo() as echo, HostBridge("127.0.0.1", echo.port, base_dir=tmp_path) as b:
        ask(b.socket_path, b"SECRET-PAYLOAD-123")
    cap = capsys.readouterr()
    assert "SECRET-PAYLOAD-123" not in cap.out + cap.err and cap.out + cap.err == ""


def test_a_long_base_path_is_refused_before_binding(tmp_path):
    deep = tmp_path / ("d" * 90)
    deep.mkdir()
    with pytest.raises(BridgeError, match="too long"):
        HostBridge(base_dir=deep).__enter__()
    assert list(deep.iterdir()) == []


def test_it_cannot_be_entered_twice(tmp_path):
    with Echo() as echo, HostBridge("127.0.0.1", echo.port, base_dir=tmp_path) as b:
        with pytest.raises(BridgeError):
            b.__enter__()
