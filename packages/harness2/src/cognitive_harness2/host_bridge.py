"""The host bridge: ONE unix socket forwarded to ONE loopback TCP address, and nothing else (RFC 004 6.2.3).

Under rootless Docker with `--net=gvisor-tap-vsock --disable-host-loopback`, a container cannot reach the
host's 127.0.0.1 over TCP by any route (measured: 127.0.0.1, 172.17.0.1, 10.0.2.2, 192.168.127.1 and
`host.docker.internal` all fail). A local model (ollama on 127.0.0.1:11434) is therefore reached through
a unix socket on the host that is bind-mounted into the PROXY container only; the proxy forwards the
session's local-model traffic to it. Every other host loopback service stays unreachable.

This process exposes exactly the target it was constructed with:

  * the target is fixed at construction and must be an IP LITERAL on loopback (no name, no lookup, nothing
    that can be pointed elsewhere by DNS), with a valid port;
  * the socket lives in a private directory created here (mode 0700) and is chmod 0600 before the first
    accept; the directory is what gets mounted, and it holds nothing else;
  * caps: concurrent connections, bytes per connection and direction, idle time, connect time;
  * it logs nothing. Bytes are forwarded verbatim and never inspected or recorded.

Use it as a context manager from the session runner. It runs its own event loop in a daemon thread and
removes the socket and the directory on exit, also when the body raised.
"""
from __future__ import annotations

import asyncio
import ipaddress
import os
import secrets
import stat
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TARGET = ("127.0.0.1", 11434)
SOCKET_NAME = "ollama.sock"
_SUN_PATH_MAX = 100       # sockaddr_un.sun_path is 108 bytes on Linux; leave room


class BridgeError(RuntimeError):
    pass


def validate_target(host: object, port: object) -> tuple[str, int]:
    """(canonical loopback IP literal, port) or ValueError. Names are refused: a bridge never resolves anything."""
    if not isinstance(host, str) or not host or host != host.strip():
        raise ValueError("the bridge target host must be an IP literal string")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError(f"the bridge target {host!r} is not an IP literal (names are never resolved)") from None
    if not ip.is_loopback or (ip.version == 6 and ip.ipv4_mapped is not None):
        raise ValueError(f"the bridge target {host} is not a loopback address")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("the bridge target port must be an integer from 1 to 65535")
    return str(ip), port


@dataclass(frozen=True)
class BridgeLimits:
    max_connections: int = 8
    max_bytes: int = 1 << 30          # per connection and direction
    idle_timeout_s: float = 900.0     # a local model may take minutes to load before its first byte
    connect_timeout_s: float = 5.0


def _runtime_base() -> Path:
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg and os.path.isdir(xdg) and os.access(xdg, os.W_OK):
        return Path(xdg)
    return Path(tempfile.gettempdir())


class HostBridge:
    """`with HostBridge() as b:` -> `b.directory` (mount this, read-only) and `b.socket_path`."""

    def __init__(self, target_host: str = DEFAULT_TARGET[0], target_port: int = DEFAULT_TARGET[1], *,
                 base_dir: str | os.PathLike[str] | None = None, limits: BridgeLimits = BridgeLimits()):
        self.target = validate_target(target_host, target_port)
        self.limits = limits
        self._base = Path(base_dir) if base_dir is not None else _runtime_base()
        self.directory: Path | None = None
        self.socket_path: Path | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._server: asyncio.AbstractServer | None = None
        self._active = 0
        self._tasks: set[asyncio.Task] = set()
        self._error: BaseException | None = None

    # ── lifecycle ─────────────────────────────────────────────────────────────────

    def __enter__(self) -> "HostBridge":
        if self._thread is not None:
            raise BridgeError("the bridge is already running")
        self.directory = Path(tempfile.mkdtemp(prefix="h2bridge-", dir=self._base)).resolve()
        try:
            os.chmod(self.directory, 0o700)
            self.socket_path = self.directory / SOCKET_NAME
            if len(os.fsencode(self.socket_path)) > _SUN_PATH_MAX:
                raise BridgeError(f"the socket path is too long for a unix socket ({self.socket_path})")
            started = threading.Event()
            self._thread = threading.Thread(target=self._run, args=(started,), name="h2-host-bridge", daemon=True)
            self._thread.start()
            if not started.wait(10) or self._error is not None:
                raise BridgeError(f"the host bridge did not start: {self._error!r}")
            mode = stat.S_IMODE(os.stat(self.socket_path).st_mode)
            if mode != 0o600:
                raise BridgeError(f"the bridge socket has mode {mode:o}, not 600")
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _run(self, started: threading.Event) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._start())
        except BaseException as e:  # noqa: BLE001 - reported to the starting thread
            self._error = e
            started.set()
            loop.close()
            return
        started.set()
        try:
            loop.run_forever()
        finally:
            try:
                loop.run_until_complete(self._shutdown())
            finally:
                loop.close()

    async def _start(self) -> None:
        old = os.umask(0o177)               # the socket is born 0600; the 0700 directory already hides it
        try:
            self._server = await asyncio.start_unix_server(self._handle, path=str(self.socket_path))
        finally:
            os.umask(old)
        os.chmod(self.socket_path, 0o600)

    async def _shutdown(self) -> None:
        if self._server is not None:
            self._server.close()
        for _ in range(5):                 # a connection accepted a moment ago may not have started its handler yet
            await asyncio.sleep(0)
            for t in list(self._tasks):
                t.cancel()
            if self._tasks:
                await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._server is not None:       # 3.12+: wait_closed waits for live connections, so only after the cancel
            try:
                await asyncio.wait_for(self._server.wait_closed(), 2)
            except asyncio.TimeoutError:
                pass

    def close(self) -> None:
        loop, thread = self._loop, self._thread
        if loop is not None and thread is not None and thread.is_alive():
            loop.call_soon_threadsafe(loop.stop)
            thread.join(15)
        self._thread = self._loop = None
        if self.socket_path is not None:
            try:
                os.unlink(self.socket_path)
            except FileNotFoundError:
                pass
        if self.directory is not None:
            try:
                os.rmdir(self.directory)
            except FileNotFoundError:
                pass
            self.directory = None

    # ── forwarding ────────────────────────────────────────────────────────────────

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        upstream_writer = None
        try:
            if self._active >= self.limits.max_connections:
                return
            self._active += 1
            try:
                host, port = self.target
                ur, upstream_writer = await asyncio.wait_for(asyncio.open_connection(host, port),
                                                             self.limits.connect_timeout_s)
            except (OSError, asyncio.TimeoutError):
                self._active -= 1
                return
            try:
                await asyncio.gather(self._copy(reader, upstream_writer), self._copy(ur, writer))
            finally:
                self._active -= 1
        except asyncio.CancelledError:
            pass
        finally:
            for w in (upstream_writer, writer):
                if w is not None:
                    w.close()
            if task is not None:
                self._tasks.discard(task)

    async def _copy(self, src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
        total = 0
        try:
            while True:
                data = await asyncio.wait_for(src.read(65536), self.limits.idle_timeout_s)
                if not data:
                    break
                total += len(data)
                if total > self.limits.max_bytes:
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


def private_name() -> str:
    """A random, unguessable file name for per-session private files."""
    return secrets.token_hex(12)
