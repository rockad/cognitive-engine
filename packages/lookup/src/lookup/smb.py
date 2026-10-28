"""A source on a network share, read in userspace over SMB: no mount, no root, no FUSE.

`SmbFS` implements the `FileSystem` contract (vfs.py) on top of `smbprotocol`. A manifest
declares the share once:

    root = "/Volumes/knowledge-archive"        # the LOGICAL path stored and shown in results
    [source.volume]
    type = "cifs"                              # or "smb"
    device = "//core/knowledge-archive"        # where it is really read from
    subpath = "wiki"                           # optional: a directory inside the share
    username = "${SHARE_USER}"                 # references only, never literal values
    password = "${SHARE_PASS}"
    port = 445                                 # optional

Design points, each earned rather than assumed:

* One round trip per directory. A listing already carries size, last-write time and the
  reparse flag, so `walk` caches a `Stat` per file and `stat()` / `is_file()` are answered
  from it. An unchanged file therefore costs no network call and is never opened.
* Logical vs real path. Results show `root/...`; the share is addressed as
  `\\\\host\\share\\subpath\\...`. `_unc` maps one to the other and REFUSES any segment that
  could change the target: `..`, a backslash (a separator on the wire), `:` (an NTFS
  alternate data stream) or a NUL.
* Reparse points (symlinks, junctions, mount points) are never followed. They are skipped
  and counted in `skipped`, because a link cannot be proven to stay inside the share.
* NTLM only. Kerberos needs system krb5 libraries, which is the host setup this removes.
* A listing failure raises `SourceUnreachable`; it is never swallowed mid-walk, so a dropped
  connection cannot read as "the remaining files were deleted".
* The password is held in memory only and never appears in an error or a log line.
"""
from __future__ import annotations

import datetime as dt
import functools
import posixpath
import re
import time
from pathlib import PurePosixPath

from .vfs import SourceUnreachable, Stat, VPath

_DIRECTORY = 0x10
_REPARSE_POINT = 0x400
_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_DEVICE = re.compile(r"^//(?P<host>[^/]+)/(?P<share>[^/]+)$")
_DOWN_FOR = 30.0


class SmbConfigError(RuntimeError):
    """The manifest's [source.volume] cannot be used (bad shape, literal credential, unset variable)."""


def _credential(table: dict, key: str, source: str, environ=None) -> str:
    import os
    raw = table.get(key)
    if not isinstance(raw, str) or not raw.strip():
        raise SmbConfigError(f"source {source!r}: [source.volume] needs `{key}` as a ${{VAR}} reference")
    m = _REF.match(raw.strip())
    if not m:
        raise SmbConfigError(
            f"source {source!r}: `{key}` must be a ${{VAR}} reference, never a literal value "
            f"(a credential must not live in a manifest)")
    val = (os.environ if environ is None else environ).get(m.group(1))
    if not val:
        raise SmbConfigError(f"source {source!r}: ${{{m.group(1)}}} (for `{key}`) is not set in the environment")
    return val


def _safe_segment(seg: str) -> bool:
    """False for anything that could change the target once mapped onto the wire."""
    return seg not in ("", ".", "..") and "\\" not in seg and ":" not in seg and "\x00" not in seg


def _network_errors() -> tuple[type[BaseException], ...]:
    """What "the share could not be reached or read" looks like.

    ⚠️ Deliberately NOT `Exception`. An earlier `except Exception` turned an AttributeError
    (a missing submodule import) into "share unreachable", so a programming error read as a
    network outage and the source was silently skipped. Only genuine I/O and SMB failures
    count; anything else propagates.
    """
    try:
        from smbprotocol.exceptions import SMBException
        return (OSError, SMBException)
    except ImportError:
        return (OSError,)


def _timestamp(value: dt.datetime) -> float:
    return (value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)).timestamp()


class SmbFS:
    def __init__(self, *, host: str, share: str, username: str, password: str, logical_root,
                 subpath: str = "", port: int = 445, timeout: float = 10.0, client=None):
        sub = subpath.replace("\\", "/").strip("/")
        if any(seg in ("", ".", "..") or ":" in seg or "\x00" in seg for seg in (sub.split("/") if sub else [])):
            raise SmbConfigError(f"subpath {subpath!r} is not a plain relative directory")
        self.host, self.share, self.port = host, share, port
        self._sub = sub
        self._root = PurePosixPath(posixpath.normpath(str(logical_root)))
        self._kw = {"username": username, "password": password, "port": port,
                    "auth_protocol": "ntlm", "connection_timeout": timeout}
        self._client = client
        self._stats: dict[str, Stat] = {}
        self._down_until = 0.0
        self.last_error: str | None = None  # why the share last looked down (never a credential)
        self.skipped: list[str] = []      # reparse points and unreadable directories, this walk
        self.network_stats = 0            # stat() calls that were NOT answered from a listing
        self.reads = 0                    # files opened for reading

    # ── plumbing ─────────────────────────────────────────────────────────────────
    def _smb(self):
        if self._client is not None:
            return self._client
        import smbclient  # lazy: a local-only install never pays for it
        import smbclient.path  # noqa: F401  (a submodule `import smbclient` does not load)
        return smbclient

    def _rel(self, p) -> list[str]:
        s, r = posixpath.normpath(str(p)), str(self._root)
        if s != r and not s.startswith(r.rstrip("/") + "/"):
            raise ValueError(f"{p!s} is outside the share's logical root")
        rel = s[len(r):].strip("/")
        parts = rel.split("/") if rel else []
        if not all(_safe_segment(seg) for seg in parts):
            raise ValueError(f"{p!s} contains a segment that is unsafe on SMB")
        return parts

    def _unc(self, p) -> str:
        parts = ([self._sub] if self._sub else []) + self._rel(p)
        return "\\\\" + self.host + "\\" + self.share + ("\\" + "\\".join(parts) if parts else "")

    def _unreachable(self, e: Exception) -> SourceUnreachable:
        self._down_until = time.monotonic() + _DOWN_FOR
        # Kept so the refresh report can say WHY a share is down. It holds the exception
        # type and message only, never a credential.
        self.last_error = f"{type(e).__name__}: {str(e)[:120]}"
        return SourceUnreachable(f"//{self.host}/{self.share}: {self.last_error}")

    # ── FileSystem ───────────────────────────────────────────────────────────────
    def path(self, s: str) -> VPath:
        return VPath(self, s)

    def contains(self, root, p) -> bool:
        r, x = posixpath.normpath(str(root)), posixpath.normpath(str(p))
        if x != r and not x.startswith(r.rstrip("/") + "/"):
            return False
        rel = x[len(r):].strip("/")
        return all(_safe_segment(seg) for seg in (rel.split("/") if rel else []))

    def is_dir(self, p) -> bool:
        if time.monotonic() < self._down_until:
            return False
        try:
            return bool(self._smb().path.isdir(self._unc(p), **self._kw))
        except ValueError:
            return False
        except _network_errors() as e:  # unreachable, refused, timed out: "not there", rows are kept
            self._unreachable(e)
            return False

    def is_file(self, p) -> bool:
        if str(p) in self._stats:
            return True
        try:
            return bool(self._smb().path.isfile(self._unc(p), **self._kw))
        except _network_errors():
            return False

    def walk(self, root):
        self._stats.clear()
        self.skipped.clear()
        yield from self._walk(PurePosixPath(posixpath.normpath(str(root))))

    def _walk(self, d: PurePosixPath):
        dirs: list[str] = []
        files: list[str] = []
        try:
            with self._smb().scandir(self._unc(d), **self._kw) as it:
                entries = list(it)
        except _network_errors() as e:
            status = getattr(e, "ntstatus", None)
            if status == 0xC0000022 and str(d) != str(self._root):  # ACCESS_DENIED on a subdirectory
                self.skipped.append(f"{d} (access denied)")
                return
            raise self._unreachable(e) from e
        for entry in entries:
            if entry.name in (".", ".."):
                continue
            info = entry.smb_info
            if info.file_attributes & _REPARSE_POINT:
                self.skipped.append(f"{d}/{entry.name} (reparse point)")
                continue
            if info.file_attributes & _DIRECTORY:
                dirs.append(entry.name)
            else:
                files.append(entry.name)
                self._stats[f"{d}/{entry.name}"] = Stat(_timestamp(info.last_write_time), int(info.end_of_file))
        dirs.sort()
        files.sort()
        yield VPath(self, str(d)), dirs, files
        for sub in dirs:  # `dirs` may have been pruned by the caller, as with os.walk
            yield from self._walk(d / sub)

    def stat(self, p) -> Stat:
        cached = self._stats.get(posixpath.normpath(str(p)))
        if cached is not None:
            return cached
        # ⚠️ The path is validated BEFORE the client is touched, in every method below.
        # Python evaluates `self._smb().stat` before the call's arguments, so an unsafe
        # path used to reach the client first.
        try:
            unc = self._unc(p)
        except ValueError as e:
            raise FileNotFoundError(str(e)) from e
        self.network_stats += 1
        st = self._smb().stat(unc, **self._kw)
        return Stat(st.st_mtime, st.st_size)

    def read_text(self, p, encoding: str = "utf-8", errors: str = "strict") -> str:
        unc = self._unc(p)
        self.reads += 1
        with self._smb().open_file(unc, mode="rb", **self._kw) as fh:
            return fh.read().decode(encoding, errors)

    def open_text(self, p, mode: str = "r", encoding: str | None = None, errors: str | None = None):
        unc = self._unc(p)
        self.reads += 1
        return self._smb().open_file(unc, mode=mode, encoding=encoding, errors=errors, **self._kw)

    def open_bytes(self, p):
        unc = self._unc(p)
        self.reads += 1
        return self._smb().open_file(unc, mode="rb", **self._kw)


@functools.cache
def _build(host, share, username, password, port, subpath, root) -> SmbFS:
    return SmbFS(host=host, share=share, username=username, password=password, port=port,
                 subpath=subpath, logical_root=root)


def for_source(src, environ=None) -> SmbFS | None:
    """The SMB filesystem for a source's [source.volume], or None for a local source.

    One instance per distinct configuration, because it holds the per-walk stat cache that
    makes an unchanged file free.
    """
    v = src.volume or {}
    if v.get("legacy_name") or str(v.get("type", "")).lower() not in ("cifs", "smb"):
        return None
    m = _DEVICE.match(src.device() or "")
    if not m:
        raise SmbConfigError(f"source {src.name!r}: device {v.get('device')!r} is not //host/share")
    user = _credential(v, "username", src.name, environ)
    password = _credential(v, "password", src.name, environ)
    try:
        port = int(v.get("port", 445))
    except (TypeError, ValueError):
        raise SmbConfigError(f"source {src.name!r}: port must be an integer") from None
    if not 1 <= port <= 65535:
        raise SmbConfigError(f"source {src.name!r}: port {port} is out of range")
    return _build(m.group("host"), m.group("share"), user, password, port,
                  str(v.get("subpath", "")), str(src.root))
