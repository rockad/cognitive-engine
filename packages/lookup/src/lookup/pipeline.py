"""Ingest orchestration.

Enforces the inclusion principle at write time: every document's path is
re-checked against its source's declared root before it reaches the database.
An extractor that wanders — a symlink, a `..`, a bug — is refused, not indexed.
"""
from __future__ import annotations

import datetime as dt
import sys
import time
from dataclasses import dataclass, field

from . import db, manifest
from .extract import resolve
from .manifest import Source


@dataclass
class Result:
    source: str
    added: int = 0
    updated: int = 0
    # Documents already in the index and byte-identical, so not rewritten. The
    # resident server re-ingests on a timer, and without this every pass
    # rewrote every row.
    unchanged: int = 0
    # Files skipped without being read, because mtime and size were unchanged.
    # This is what makes a refresh over a network share affordable.
    skipped_files: int = 0
    # Rows deleted because their file is gone. Ingestion was additive until
    # 2026-09-07, so a deleted document stayed searchable with a path that no
    # longer resolved.
    pruned: int = 0
    refused: int = 0
    errors: int = 0
    files: int = 0
    seconds: float = 0.0
    problems: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = {"added": self.added, "updated": self.updated,
             "unchanged": self.unchanged, "errors": self.errors,
             "files": self.files, "secs": round(self.seconds, 1)}
        if self.skipped_files:
            d["skipped_files"] = self.skipped_files
        if self.pruned:
            d["pruned"] = self.pruned
        if self.refused:
            d["REFUSED"] = self.refused
        return d


def ingest_source(con, src: Source, commit_every: int = 500,
                  progress=True, rehash: bool = False) -> Result:
    extractor = resolve(src.extractor)
    res = Result(source=src.name)
    # ⚠️ Load-bearing, not hygiene. The digest cache is process-lifetime and the
    # resident server re-ingests on a timer in one process, so an entry from an
    # earlier pass would pair a freshly read body with the digest of the version
    # before the file was edited. See db._SHA_CACHE.
    db.clear_sha_cache()
    t0 = time.time()
    cur = con.cursor()
    cur.execute("INSERT INTO runs(started, source) VALUES(?,?)",
                (dt.datetime.now().isoformat(), src.name))
    run_id = cur.lastrowid

    n = 0
    seen: set[str] = set()
    seen_paths: set[str] = set()
    for path in src.files():
        res.files += 1
        seen_paths.add(str(path))

        # ⚠️ Skip the file WITHOUT READING IT when its mtime and size are
        # unchanged. This is the difference between a refresh over a network
        # share being usable or not: reading and hashing the archive's 530
        # markdown files over SMB costs 99 s in a container and 44 s on the
        # host, against ~1 s from local disk. A stat is a few milliseconds.
        #
        # `upsert_ex` already skips a byte-identical document, but it learns
        # that from the bytes -- which is the read this avoids.
        try:
            st = path.stat()
        except OSError:
            st = None
        if st is not None and not rehash:
            prior = db.file_state(cur, src.name, str(path))
            if prior and prior[0] == st.st_mtime and prior[1] == st.st_size:
                seen.update(prior[2])
                res.unchanged += len(prior[2])
                res.skipped_files += 1
                continue

        ordinals: dict[str, int] = {}
        keys_here: list[str] = []
        try:
            stream = extractor(path)
        except Exception as e:
            res.errors += 1
            res.problems.append(f"{path.name}: {type(e).__name__}: {e}")
            continue

        while True:
            try:
                d = next(stream)
            except StopIteration:
                break
            except Exception as e:
                res.errors += 1
                if len(res.problems) < 10:
                    res.problems.append(f"{path.name}: {type(e).__name__}: {e}")
                continue

            try:
                if not (d.get("body") or "").strip():
                    continue
                # Inclusion invariant, re-checked per document.
                if not src.contains(type(path)(d["path"])):
                    res.refused += 1
                    if len(res.problems) < 10:
                        res.problems.append(f"REFUSED (outside root): {d['path']}")
                    continue
                d["source"] = src.name          # manifest is authoritative
                # ⚠️ A document may NARROW its source's visibility and may never
                # WIDEN it. Widening cannot be taken back — anything reused on
                # the strength of an `open` label has already been reused — so
                # it must not be reachable by editing a file's front matter.
                # Refused and reported in the same slot as an escape from the
                # declared root, because it is the same class of violation: a
                # document overreaching what the manifest declared.
                dv = d.get("visibility")
                if dv and manifest.widens(dv, src.ceiling() or ""):
                    res.refused += 1
                    if len(res.problems) < 10:
                        res.problems.append(
                            f"REFUSED (visibility {dv} widens source "
                            f"{src.visibility}): {d['path']}")
                    continue
                slot = d.get("path") or ""
                # Digest of the source file, for the ledger's content join. Taken
                # from d["path"] rather than `path` because those differ for any
                # extractor that emits per-entry paths, and the ledger joins on
                # the path that actually lands in the row. db.file_sha256 caches,
                # so a file yielding 5,660 documents is still hashed once.
                d["src_sha256"] = db.file_sha256(slot) if slot else None
                ordinals[slot] = ordinals.get(slot, -1) + 1
                key = db.doc_key(d, ordinals[slot])
                seen.add(key)
                keys_here.append(key)
                outcome = db.upsert_ex(cur, key, d)
                if outcome == "added":
                    res.added += 1
                elif outcome == "updated":
                    res.updated += 1
                else:
                    res.unchanged += 1
            except Exception as e:
                res.errors += 1
                if len(res.problems) < 10:
                    res.problems.append(f"{path.name}: {type(e).__name__}: {e}")

            n += 1
            if n % commit_every == 0:
                con.commit()
                if progress:
                    print(f"    {src.name}: {n} seen, {res.added} new, "
                          f"{res.updated} updated ({time.time() - t0:.0f}s)", flush=True)

        # Ledger the file only when it yielded documents. A file whose
        # extractor blew up produces no keys, so recording its mtime would
        # make the next pass skip it and inherit the failure silently.
        if st is not None and keys_here:
            db.record_file(cur, src.name, str(path), st.st_mtime, st.st_size,
                           keys_here)

    # Forget documents whose file has gone -- but only on the evidence of a
    # pass that actually read the source.
    #
    # Two guards, and both were learned rather than assumed:
    #
    #   `errors == 0`: an extractor blowing up mid-pass would otherwise make
    #   every document it never reached look deleted, and delete it.
    #
    #   `files > 0`: a source that yields nothing is ambiguous. It may be
    #   genuinely empty, or it may be a share that is not mounted -- and
    #   ⚠️ Docker CREATES a missing bind-mount source directory, so inside a
    #   container an unmounted root looks like a present empty one. Treating
    #   that as "everything was deleted" would drop the corpus every time a
    #   laptop left the LAN, which is the opposite of "the index is what
    #   travels, not the source".
    if res.errors == 0 and res.files > 0:
        res.pruned = db.prune_source(cur, src.name, seen)
        db.forget_files(cur, src.name, seen_paths)
    elif res.errors:
        res.problems.append(
            f"skipped pruning: {res.errors} error(s) this pass, so an unseen "
            f"document cannot be distinguished from an unreadable one")
    elif res.files == 0:
        res.problems.append(
            "skipped pruning: the root yielded no files, which cannot be told "
            "apart from an unmounted share -- rows kept so an offline machine "
            "keeps searching what it indexed at home")

    con.commit()
    res.seconds = time.time() - t0
    cur.execute("UPDATE runs SET finished=?, added=?, updated=?, errors=? WHERE id=?",
                (dt.datetime.now().isoformat(), res.added, res.updated,
                 res.errors + res.refused, run_id))
    con.commit()
    return res


def report(res: Result, src: Source) -> None:
    print(f"  {res.source:11s} {src.description}")
    print(f"    -> {res.as_dict()}")
    if res.refused:
        # Two reasons now, and the problems list says which: escaping the
        # declared root, or claiming a visibility wider than the source's.
        print(f"    !! {res.refused} document(s) REFUSED for overreaching the "
              f"manifest — see the problem lines below", file=sys.stderr)
    for p in res.problems[:5]:
        print(f"    ! {p}", file=sys.stderr)


def ingest_all(optimize: bool = True, vacuum: bool = False,
               rehash: bool = False) -> dict:
    """Ingest every declared source that exists, and report the totals.

    Extracted from the CLI so the resident MCP server can re-ingest without
    shelling out to itself. Returns a dict rather than printing, because the
    caller here is a tool reply or a log line, not a terminal.

    `optimize` runs FTS5's own optimize. `vacuum` is separate and defaults to
    off: rewriting the whole file to reclaim free pages blocks every reader,
    which is not worth a few megabytes on a periodic refresh. It IS worth it at
    startup, before any client has connected — without it the container's index
    sits about 6 MB larger than the host's for the same 1,199 documents, which
    reads like a difference in coverage and is not one.
    """
    from . import config, db, manifest

    srcs = manifest.load()

    con = db.connect()
    totals = {"added": 0, "updated": 0, "unchanged": 0, "errors": 0,
              "refused": 0, "pruned": 0, "skipped_files": 0,
              "sources": 0, "skipped": [], "unmounted": [],
              "dropped": {}, "undeclared": [], "stale": [],
              # Sources whose file ledger was discarded because they were
              # unreachable, so the next pass that finds them re-verifies in
              # full rather than trusting a pre-gap mtime.
              "reverify": []}
    try:
        cur = con.cursor()
        # Whole sources that have gone, in two flavours, both of which used to
        # leave their rows in the index forever:
        #   - declared but the root is absent (a share unmounted, a mirror
        #     deleted) -- `sources` says MISSING while `archive_stats` still
        #     counts the documents and searches hand back unresolvable paths;
        #   - no longer declared at all (removed from the manifest).
        # A source the manifest no longer declares is an explicit human
        # decision, so its rows go. A source that is declared but currently
        # unreachable is NOT: its rows stay, and it is reported as stale.
        # Anything else would empty the index whenever a share was unmounted.
        indexed = set(db.sources_in_index(con))
        for name in sorted(indexed):
            if name not in srcs:
                n = db.prune_source(cur, name, None)
                if n:
                    totals["dropped"][name] = n
                    totals["pruned"] += n
                    totals["undeclared"].append(name)
        for name in sorted(indexed & set(srcs)):
            if not srcs[name].exists():
                totals["stale"].append(name)
        if totals["dropped"]:
            con.commit()

        for name, src in srcs.items():
            if not src.exists():
                totals["skipped"].append(name)
                # ⚠️ A `[source.volume]` used to mean "the container mounts this
                # share itself". Native Lookup has no container to mount into, so
                # the table is metadata now and the root must already resolve on
                # this machine. Say so by name: left unremarked, an unmounted
                # share is indistinguishable from an empty one.
                if src.volume:
                    totals["unmounted"].append(name)
                # ⚠️ Drop this source's FILE ledger, keeping its documents.
                #
                # The documents stay because an unreachable source is not a
                # deleted one -- an offline machine should keep searching what
                # it indexed at home. But the ledger must go, because it is a
                # claim about what the files looked like, and that claim cannot
                # be trusted across a gap in which the remote was free to
                # change. With the ledger empty, the pass that runs when the
                # source comes back re-reads and re-hashes every file rather
                # than skipping it on a stale mtime, and pruning then removes
                # whatever was deleted while we were away.
                #
                # Decision 2026-09-07: "the index should be verified/rebuilt
                # if the remote source becomes available again."
                n = db.forget_files(cur, name, None)
                if n:
                    totals["reverify"].append(name)
                    con.commit()
                continue
            res = ingest_source(con, src, rehash=rehash)
            totals["sources"] += 1
            totals["added"] += res.added
            totals["updated"] += res.updated
            totals["unchanged"] += res.unchanged
            totals["errors"] += res.errors
            totals["refused"] += res.refused
            totals["pruned"] += res.pruned
            totals["skipped_files"] += res.skipped_files
            if res.pruned:
                totals["dropped"][name] = totals["dropped"].get(name, 0) + res.pruned
        if optimize:
            con.execute("INSERT INTO fts(fts) VALUES('optimize')")
            con.commit()
        if vacuum:
            con.execute("VACUUM")
        totals["docs"] = con.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
        totals["db_bytes"] = config.DB.stat().st_size if config.DB.exists() else 0
    finally:
        con.close()
    return totals
