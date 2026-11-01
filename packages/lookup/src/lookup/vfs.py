"""The filesystem seam: every read of a declared source goes through a `FileSystem`.

Until now the engine called `os.walk`, `Path.stat`, `Path.read_text` and `open` directly,
which made "a source on a network share" impossible without a host mount. A `FileSystem`
is the one place that knows where bytes come from; `LocalFS` is today's behaviour moved
here verbatim, and a second backend (SMB) plugs in without touching the extractors.

How extractors stay unchanged: a backend yields path objects that DUCK-TYPE the small part
of `pathlib` the engine uses (`name`, `stem`, `suffix`, `parent`, `/`, `relative_to`,
`read_text`, `stat`, `open`, `str()`). `LocalFS` yields ordinary `pathlib.Path`s, so local
sources are byte-for-byte what they were. Any other backend yields `VPath`s, which carry
their filesystem with them, so `read_text(path)` finds the right reader from the path alone.

A path's string form is its LOGICAL path. It is what is stored in the ledger and shown in
results, and it is what `FileSystem.path()` turns back into a path object.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO, Protocol, runtime_checkable


class SourceUnreachable(OSError):
    """A remote source could not be listed or read (share down, network gone, auth refused).

    Raised, never swallowed mid-walk: a half-finished listing must not be mistaken for
    "the other files were deleted". The pipeline keeps the source's rows and moves on.
    """


@dataclass(frozen=True)
class Stat:
    """The two fields the ledger needs: a cheap change signal that never reads the file."""
    st_mtime: float
    st_size: int


@runtime_checkable
class FileSystem(Protocol):
    def path(self, s: str): ...
    def is_dir(self, p) -> bool: ...
    def is_file(self, p) -> bool: ...
    def walk(self, root) -> Iterator[tuple[object, list[str], list[str]]]: ...
    def stat(self, p) -> Stat: ...
    def read_text(self, p, encoding: str = "utf-8", errors: str = "strict") -> str: ...
    def open_text(self, p, mode: str = "r", encoding: str | None = None, errors: str | None = None) -> IO[str]: ...
    def open_bytes(self, p) -> IO[bytes]: ...
    def contains(self, root, p) -> bool: ...


class LocalFS:
    """The local disk. Behaviour is identical to the code this replaced."""

    def path(self, s: str) -> Path:
        return Path(s)

    def is_dir(self, p) -> bool:
        return Path(p).is_dir()

    def is_file(self, p) -> bool:
        return Path(p).is_file()

    def walk(self, root):
        """`os.walk` that yields a path object; mutating `dirnames` prunes, as with os.walk."""
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            yield Path(dirpath), dirnames, filenames

    def stat(self, p):
        return Path(p).stat()

    def read_text(self, p, encoding: str = "utf-8", errors: str = "strict") -> str:
        return Path(p).read_text(encoding=encoding, errors=errors)

    def open_text(self, p, mode: str = "r", encoding: str | None = None, errors: str | None = None):
        return Path(p).open(mode, encoding=encoding, errors=errors)

    def open_bytes(self, p):
        return open(p, "rb")

    def contains(self, root, p) -> bool:
        """True only if `p` really resolves inside `root` (symlinks and `..` included)."""
        try:
            return Path(p).resolve().is_relative_to(Path(root).resolve())
        except (OSError, ValueError):
            return False


LOCAL = LocalFS()


class VPath:
    """A logical POSIX path bound to the `FileSystem` that can read it.

    Implements exactly the `pathlib` surface the engine and its extractors use, no more.
    There is no symlink resolution here: a backend either never produces a link (SMB
    listings flag reparse points and the walker skips them) or checks in `contains`.
    """

    __slots__ = ("fs", "_p")

    def __init__(self, fs: FileSystem, path) -> None:
        self.fs = fs
        self._p = PurePosixPath(str(path))

    # identity and ordering: sources are sorted and de-duplicated by path
    def __str__(self) -> str:
        return str(self._p)

    def __repr__(self) -> str:
        return f"VPath({self._p!s})"

    def __fspath__(self) -> str:  # lets `open(vpath)` fail loudly instead of reading the wrong disk
        raise TypeError("a VPath is not on the local disk; read it through its FileSystem")

    def __eq__(self, other: object) -> bool:
        return isinstance(other, VPath) and self.fs is other.fs and self._p == other._p

    def __hash__(self) -> int:
        return hash(self._p)

    def __lt__(self, other: VPath) -> bool:
        return self._p < other._p

    # pure path operations
    @property
    def name(self) -> str:
        return self._p.name

    @property
    def stem(self) -> str:
        return self._p.stem

    @property
    def suffix(self) -> str:
        return self._p.suffix

    @property
    def parent(self) -> VPath:
        return VPath(self.fs, self._p.parent)

    def __truediv__(self, other) -> VPath:
        return VPath(self.fs, self._p / str(other))

    def relative_to(self, other) -> PurePosixPath:
        return self._p.relative_to(PurePosixPath(str(other)))

    def as_posix(self) -> str:
        return self._p.as_posix()

    # reads, always through the filesystem
    def read_text(self, encoding: str = "utf-8", errors: str = "strict") -> str:
        return self.fs.read_text(self, encoding, errors)

    def stat(self) -> Stat:
        return self.fs.stat(self)

    def open(self, mode: str = "r", encoding: str | None = None, errors: str | None = None):
        if "b" in mode:
            return self.fs.open_bytes(self)
        return self.fs.open_text(self, mode, encoding, errors)

    def is_file(self) -> bool:
        return self.fs.is_file(self)

    def is_dir(self) -> bool:
        return self.fs.is_dir(self)
