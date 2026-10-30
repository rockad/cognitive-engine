"""Reading a session's workspace back: inspect first, trust nothing (threat model 3 and 6).

After a contained session the workspace holds whatever the model (or the CLI it drives) left there. It is
inspected with `lstat` only, never following a link, and the report lists what may be read and what was
refused and why. Refused:

    special-file       a FIFO, socket, device node (anything not a regular file or directory or symlink)
    symlink-escape     a symlink whose target is absolute or resolves outside the workspace
    suid-bit           setuid, setgid or sticky on a file
    executable         any execute bit on a file (unless the caller allows it)
    hardlink           a file with more than one link
    file-too-large     over the per-file cap (512 KiB by default)
    too-many-files / too-deep / total-too-large   count, depth and total-size caps
    bad-name           control or invisible characters (ANSI escapes included), a backslash, undecodable bytes,
                       leading or trailing space
    protected-path     a path the invariant set protects (hooks, .git, egress, approval, ...)
    credential-in-file the content contains a credential value the caller names
    unreadable / mount-point

Nothing here reads or writes anything outside `root`, starts a process or opens a socket.
"""
from __future__ import annotations

import os
import stat
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from . import invariants

SUID_MASK = stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX
EXEC_MASK = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH


@dataclass(frozen=True)
class HarvestLimits:
    max_files: int = 2000
    max_depth: int = 16
    max_file_bytes: int = 512 * 1024
    max_total_bytes: int = 16 * 1024 * 1024
    allow_exec: bool = False


@dataclass(frozen=True)
class Finding:
    path: str        # relative, shown with ascii() escapes so a hostile name cannot reach a terminal
    rule: str
    detail: str = ""


@dataclass(frozen=True)
class HarvestReport:
    files: tuple[str, ...]            # accepted regular files (relative paths)
    links: tuple[str, ...]            # symlinks that stay inside the workspace (never followed)
    findings: tuple[Finding, ...]
    total_bytes: int

    @property
    def ok(self) -> bool:
        return not self.findings

    def rules(self) -> frozenset[str]:
        return frozenset(f.rule for f in self.findings)


def _shown(rel: str) -> str:
    return ascii(rel)[1:-1]


def _bad_name(name: str) -> bool:
    if name != name.strip() or "\\" in name:
        return True
    return any(unicodedata.category(c) in ("Cc", "Cf", "Cs", "Co", "Cn") for c in name)


def inspect_workspace(root: str, limits: HarvestLimits = HarvestLimits(), *,
                      credentials: Iterable[str] = ()) -> HarvestReport:
    """Walk `root` with lstat. Never raises for what it finds; raises only if `root` itself is not a directory."""
    root = os.path.realpath(root)
    root_stat = os.lstat(root)
    if not stat.S_ISDIR(root_stat.st_mode):
        raise NotADirectoryError(root)
    secrets = [c.encode("utf-8") for c in credentials if c]
    files: list[str] = []
    links: list[str] = []
    findings: list[Finding] = []
    total = 0
    count = 0

    def add(rel: str, rule: str, detail: str = "") -> None:
        findings.append(Finding(_shown(rel), rule, detail))

    def walk(directory: str, rel_dir: str, depth: int) -> None:
        nonlocal total, count
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name.encode("utf-8", "surrogateescape"))
        except OSError as e:
            add(rel_dir or ".", "unreadable", e.strerror or "")
            return
        for entry in entries:
            name = entry.name
            rel = f"{rel_dir}/{name}" if rel_dir else name
            count += 1
            if count > limits.max_files:
                if not any(f.rule == "too-many-files" for f in findings):
                    add(rel, "too-many-files", f"more than {limits.max_files} entries")
                return
            try:
                st = os.lstat(entry.path)
            except OSError as e:
                add(rel, "unreadable", e.strerror or "")
                continue
            try:
                name.encode("utf-8")
                undecodable = False
            except UnicodeEncodeError:
                undecodable = True
            if undecodable or _bad_name(name):
                add(rel, "bad-name")
                continue
            try:
                invariants.assert_path_unprotected(rel)
            except invariants.InvariantViolation:
                add(rel, "protected-path")
                continue
            mode = st.st_mode
            if stat.S_ISLNK(mode):
                target = os.readlink(entry.path)
                resolved = os.path.normpath(os.path.join(directory, target))
                if os.path.isabs(target) or not (resolved == root or resolved.startswith(root + os.sep)):
                    add(rel, "symlink-escape")
                else:
                    links.append(rel)
                continue
            if stat.S_ISDIR(mode):
                if st.st_dev != root_stat.st_dev:
                    add(rel, "mount-point")
                    continue
                if depth + 1 > limits.max_depth:
                    add(rel, "too-deep", f"deeper than {limits.max_depth}")
                    continue
                walk(entry.path, rel, depth + 1)
                continue
            if not stat.S_ISREG(mode):
                add(rel, "special-file", stat.filemode(mode)[0])
                continue
            problems: list[tuple[str, str]] = []
            if mode & SUID_MASK:
                problems.append(("suid-bit", ""))
            if mode & EXEC_MASK and not limits.allow_exec:
                problems.append(("executable", ""))
            if st.st_nlink > 1:
                problems.append(("hardlink", f"{st.st_nlink} links"))
            if st.st_size > limits.max_file_bytes:
                problems.append(("file-too-large", f"{st.st_size} bytes"))
            elif total + st.st_size > limits.max_total_bytes:
                problems.append(("total-too-large", ""))
            for rule, detail in problems:
                add(rel, rule, detail)
            if problems:
                continue
            if secrets:
                try:
                    with open(entry.path, "rb") as f:
                        data = f.read(limits.max_file_bytes + 1)
                except OSError as e:
                    add(rel, "unreadable", e.strerror or "")
                    continue
                if any(s in data for s in secrets):
                    add(rel, "credential-in-file")
                    continue
            total += st.st_size
            files.append(rel)

    walk(root, "", 0)
    return HarvestReport(tuple(files), tuple(links), tuple(findings), total)


# ── protected files that the CALLER staged ────────────────────────────────────────
#
# `inspect_workspace` refuses every protected-named path, which is right for what a model CREATES. But a caller may
# stage protected-named files on purpose: upstream's proposer context holds the whole harness, `guardrails.md`
# included. Those are acceptable exactly when they come back byte-identical, so the caller takes a manifest of them
# before the session and another after, and `accept_unchanged_protected` clears only the findings those prove harmless.

def protected_manifest(root: str, limit: int = 4 * 1024 * 1024) -> dict[str, str]:
    """rel path -> sha256 of every file whose path is protected by name (itself or under a protected directory).

    A protected-named entry that is not a plain regular file (a link, FIFO, hard-linked, executable or setuid file,
    or one over `limit`) maps to `"!"`, which never equals a hash, so it can neither be staged nor accepted."""
    import hashlib

    root = os.path.realpath(root)
    out: dict[str, str] = {}
    for base, dirs, names in os.walk(root, followlinks=False):
        dirs.sort()
        rel_base = os.path.relpath(base, root)
        for name in sorted([*dirs, *names]):
            rel = name if rel_base == "." else f"{rel_base}/{name}"
            try:
                invariants.assert_path_unprotected(rel)
                continue
            except invariants.InvariantViolation:
                pass
            p = os.path.join(base, name)
            try:
                st = os.lstat(p)
            except OSError:
                out[rel] = "!"
                continue
            if stat.S_ISDIR(st.st_mode):
                continue                                  # its files are listed on their own
            if (not stat.S_ISREG(st.st_mode) or st.st_nlink > 1 or st.st_mode & (SUID_MASK | EXEC_MASK)
                    or st.st_size > limit):
                out[rel] = "!"
                continue
            try:
                with open(p, "rb") as f:
                    out[rel] = hashlib.sha256(f.read(limit + 1)).hexdigest()
            except OSError:
                out[rel] = "!"
    return out


def protected_changes(before: Mapping[str, str], after: Mapping[str, str]) -> list[str]:
    """The staged protected files that were changed, replaced or removed."""
    return sorted(rel for rel, digest in before.items() if digest == "!" or after.get(rel) != digest)


def accept_unchanged_protected(report: HarvestReport, before: Mapping[str, str], after: Mapping[str, str]) -> HarvestReport:
    """`report` without the `protected-path` findings that the manifests prove are the caller's own, unchanged files.

    A finding is cleared only if everything staged at or under its path is present and identical afterwards and
    nothing was added there. The cleared files are returned among `files`. `total_bytes` is not adjusted."""
    if not before:
        return report
    files, kept = list(report.files), []
    for f in report.findings:
        if f.rule != "protected-path":
            kept.append(f)
            continue
        under = f.path + "/"
        was = {k: v for k, v in before.items() if k == f.path or k.startswith(under)}
        now = {k: v for k, v in after.items() if k == f.path or k.startswith(under)}
        if was and now == was and "!" not in was.values():
            files.extend(was)
        else:
            kept.append(f)
    return HarvestReport(tuple(sorted(set(files))), report.links, tuple(kept), report.total_bytes)
