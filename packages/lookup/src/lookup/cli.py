"""Command-line interface: ingest, search, stats, sources."""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sqlite3
import sys

from . import config, db, manifest, pipeline


def cmd_sources(a) -> int:
    srcs = manifest.load()
    print(f"manifest: {config.MANIFEST}\n")
    print(f"{'source':12s} {'root exists':12s} {'files':>7s}  description")
    print("-" * 78)
    for s in srcs.values():
        n = sum(1 for _ in s.files()) if a.count else 0
        exists = "yes" if s.exists() else "MISSING"
        print(f"{s.name:12s} {exists:12s} {n if a.count else '-':>7}  {s.description[:38]}")
        print(f"{'':12s} root: {s.root}")
        # `open` is free to reuse; `gated` and the undeclared `unstated` need
        # verification against the source and the user's sign-off.
        note = ''
        if s.visibility == manifest.VISIBILITY_MIXED:
            # `mixed` is not a claim about any document, so 'needs sign-off'
            # would be the wrong summary: some of its documents are open.
            note = '  (each document declares its own; one that does not is gated)'
        elif s.needs_signoff():
            note = '  (needs sign-off before reuse)'
        print(f"{'':12s} visibility: {s.visibility}{note}")
        if s.volume:
            print(f"{'':12s} via volume: {s.volume_name()}  "
                  f"({s.volume.get('type', '?')} {s.device()})")
        if s.aliases:
            print(f"{'':12s} aliases: {', '.join(s.addresses())}")
    return 0


def cmd_ingest(a) -> int:
    srcs = manifest.load()
    names = list(srcs) if a.all else a.source
    unknown = [n for n in names if n not in srcs]
    if unknown:
        sys.exit(f"unknown source(s): {', '.join(unknown)}\nknown: {', '.join(srcs)}")

    con = db.connect()
    totals = {"added": 0, "updated": 0, "unchanged": 0, "pruned": 0,
              "skipped_files": 0, "errors": 0, "refused": 0}
    for n in names:
        s = srcs[n]
        if not s.exists():
            print(f"  {n:11s} SKIP — root not present: {s.root}")
            continue
        res = pipeline.ingest_source(con, s, rehash=a.rehash)
        pipeline.report(res, s)
        totals["added"] += res.added
        totals["updated"] += res.updated
        totals["unchanged"] += res.unchanged
        totals["pruned"] += res.pruned
        totals["skipped_files"] += res.skipped_files
        totals["errors"] += res.errors
        totals["refused"] += res.refused

    if a.optimize:
        print("  optimizing FTS index ...", flush=True)
        con.execute("INSERT INTO fts(fts) VALUES('optimize')")
        con.commit()
        con.execute("VACUUM")

    n_docs = con.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    size = config.DB.stat().st_size / 1048576 if config.DB.exists() else 0
    print(f"\nTOTAL {totals} | docs: {n_docs:,} | db: {size:.1f} MB")
    con.close()
    return 1 if totals["refused"] else 0


def cmd_stats(_a) -> int:
    con = db.connect(readonly=True)
    rows = db.stats(con)
    tot = con.execute("SELECT COUNT(*), SUM(bytes) FROM docs").fetchone()
    print(f"{'source':12s} {'docs':>9s} {'text':>10s}  date range")
    print("-" * 62)
    for r in rows:
        span = f"{(r['lo'] or '')[:10]} -> {(r['hi'] or '')[:10]}" if r["lo"] else ""
        print(f"{r['source']:12s} {r['n']:9,d} {(r['b'] or 0)/1048576:9.1f}M  {span}")
    print("-" * 62)
    print(f"{'TOTAL':12s} {tot[0]:9,d} {(tot[1] or 0)/1048576:9.1f}M"
          f"   db {config.DB.stat().st_size/1048576:.1f} MB")
    return 0


def _search_grouped(a, con) -> int:
    """`search --grouped`: one ranking per source, kept apart."""
    try:
        groups = db.search_grouped(con, a.query, a.source, a.since, a.until,
                                   a.limit, visibility=a.visibility,
                                   inherit_sources=_inherit_sources(a.visibility))
    except sqlite3.OperationalError as e:
        sys.exit(f"query error: {e}")

    if a.json:
        out = []
        for name, rows, effective, relaxed_from in groups:
            hits = []
            for r in rows:
                d = dict(r)
                d["meta"] = json.loads(d["meta"] or "{}")
                if not a.full:
                    d.pop("body", None)
                hits.append(d)
            out.append({"source": name, "effective_query": effective,
                        "relaxed_from": relaxed_from, "hits": hits})
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    if not groups:
        print("no matches in any source")
        return 0
    for name, rows, effective, relaxed_from in groups:
        note = f"  (searched as {effective})" if relaxed_from else ""
        print(f"\n[{name}]{note}")
        for i, r in enumerate(rows, 1):
            print(f"  {i}. {r['title']}")
            print(f"     {(r['date'] or '')[:10]}  {r['path']}")
    return 0


def _inherit_sources(want: str | None) -> tuple[str, ...]:
    """Sources whose OWN visibility is `want`, so their NULL rows match it.

    A row's effective visibility is its own value or its source's. The source's
    value is in the manifest, which `db` deliberately knows nothing about, so
    the resolution happens here and the names go into the SQL.

    A manifest that will not load returns nothing rather than raising: the
    filter then matches only rows carrying an explicit value, which is narrower
    than asked for and never wider. Failing towards fewer results is the safe
    direction for a filter whose whole job is to keep gated material out of an
    answer.
    """
    if not want:
        return ()
    try:
        return tuple(n for n, s in manifest.load().items()
                     if s.visibility == want)
    except Exception:
        return ()


def cmd_search(a) -> int:
    con = db.connect(readonly=True)
    if a.grouped:
        return _search_grouped(a, con)
    try:
        rows = db.search(con, a.query, a.source, a.since, a.until, a.limit,
                         visibility=a.visibility,
                         inherit_sources=_inherit_sources(a.visibility))
    except sqlite3.OperationalError as e:
        sys.exit(f"query error: {e}")

    if a.json:
        out = []
        for r in rows:
            d = dict(r)
            d["meta"] = json.loads(d["meta"] or "{}")
            if not a.full:
                d.pop("body", None)
            out.append(d)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    if not rows:
        print("no matches")
        return 0
    for i, r in enumerate(rows, 1):
        print(f"\n{i}. [{r['source']}] {r['title']}")
        print(f"   {(r['date'] or '')[:10]}  {r['path']}")
        text = r["body"] if a.full else r["snip"]
        for line in str(text).strip().splitlines()[: (200 if a.full else 4)]:
            print(f"   {line}")
    print(f"\n{len(rows)} result(s)")
    return 0


def cmd_files(a) -> int:
    """Dump the files a source DECLARES, or the files it actually INDEXED.

    This exists for an external ledger. `lookup`'s inclusion principle
    guarantees nothing forbidden gets in; it guarantees nothing about anything
    wanted being left out, and the three ways a file disappears in silence — it
    matches no include glob, its extraction yields an empty body, or it is the
    eleventh problem in a list that caps at ten — are all invisible from inside.
    A checker outside the pipeline needs exactly two lists to prove otherwise,
    and neither was obtainable before this command:

    * ``--declared`` walks the manifest's include globs. Filesystem only, so it
      runs on the host and needs no database.
    * ``--indexed`` reads `docs`, one row per distinct source file, carrying
      `src_sha256` so the join can be on CONTENT rather than on filename. Needs
      the database: ``uv run lookup files --indexed``.

    Output is TSV on stdout with a header, so the caller can `cut` it. The
    header is the only thing written to stdout that is not a row; everything
    else goes to stderr, which keeps the stream pipeable.

    ⚠️ A NULL digest is printed as an empty field and means "not hashed", never
    "no hash exists" — rows predating the column and rows whose file has since
    moved stay NULL. The count is reported on stderr so a consumer can state
    its coverage instead of reading NULL as a mismatch.
    """
    names = list(a.source or [])
    if a.declared:
        srcs = manifest.load()
        unknown = [n for n in names if n not in srcs]
        if unknown:
            sys.exit(f"unknown source(s): {', '.join(unknown)}\nknown: {', '.join(srcs)}")
        chosen = [srcs[n] for n in (names or list(srcs))]
        print("source\tpath")
        n = 0
        for s in chosen:
            if not s.exists():
                print(f"  {s.name}: root not present: {s.root}", file=sys.stderr)
                continue
            for p in s.files():
                rel = p.relative_to(s.root) if a.relative else p
                print(f"{s.name}\t{rel}")
                n += 1
        print(f"declared: {n} file(s)", file=sys.stderr)
        return 0

    con = db.connect(readonly=True)
    where, params = "", []
    if names:
        where = f" WHERE source IN ({','.join('?' * len(names))})"
        params = names
    # GROUP BY (source, path): one file can produce many documents — 5,660
    # vcards came from one .vcf — and the ledger accounts for FILES. `docs` is
    # the count, so a caller can still see the fan-out.
    rows = con.execute(
        "SELECT source, path,"
        # MAX() rather than a bare column: every document from one file carries
        # that file's digest, so they agree, and MAX skips a NULL from a row
        # written before the column while the others carry one.
        " MAX(src_sha256) AS sha, COUNT(*) AS docs"
        f" FROM docs{where} GROUP BY source, path ORDER BY source, path",
        params).fetchall()
    if a.verify:
        return _verify_indexed(con, rows)

    print("source\tpath\tsha256\tdocs")
    nul = 0
    for r in rows:
        if not r["sha"]:
            nul += 1
        print(f"{r['source']}\t{r['path'] or ''}\t{r['sha'] or ''}\t{r['docs']}")
    print(f"indexed: {len(rows)} file(s), {nul} without a digest", file=sys.stderr)
    con.close()
    return 0


def _verify_indexed(con, rows) -> int:
    """Re-hash each indexed file and report where the index is behind it.

    ⚠️ This exists because nothing in `ingest`'s own output can reveal that a
    row disagrees with its file. `src_files` skips a file whose mtime AND size
    both match what it recorded — the fast path that makes a network share
    affordable — so a pass can report `N unchanged` and be right about the
    files it read and wrong about the index.

    **Two different faults produce the same digest mismatch, and they need
    opposite fixes, which is why this reports them separately.** Both were met
    on 2026-09-09, the day it was written:

    * **DIGEST WRONG.** All 25 mismatches across the live index were this: the
      indexed text was CURRENT and only the recorded digest was one revision
      behind, because `_SHA_CACHE` was process-lifetime while the resident
      server re-ingests every 300 s in one process. Search answered correctly;
      an external ledger joining on content did not. Fixed at the cause. A
      re-ingest would have repaired nothing, because nothing was stale.
    * **INDEX BEHIND.** The text itself is older than the file. This is what a
      write preserving both mtime and size would produce — a `git checkout`
      between branches restores content with its old mtime, which a
      docs-branch workflow does constantly. The ledger cannot detect it by
      construction. **No instance has actually been observed**, and saying so
      matters: the first version of this note used the theoretical case as the
      explanation for the 25 real ones and concluded a periodic full `--rehash`
      of the whole index was needed. It is not.

    So: **verify periodically, and rehash only when this says INDEX BEHIND.**
    The verify is a read and costs nothing to be wrong about; the rehash is the
    expensive remedy and should follow evidence rather than precede it.

    `lake-ledger.sh reconcile` makes the same comparison for a corpus that has a
    ledger. Most sources have none, which is why it has to be reachable here.

    Exit 3 for INDEX BEHIND only, so a script stops on a wrong answer and not on
    bookkeeping.
    """
    behind, wrongsha, missing, nodigest = [], [], [], 0
    for r in rows:
        if not r["sha"]:
            nodigest += 1
            continue
        # cached=False is load-bearing: the point is to re-read the file.
        actual = db.file_sha256(r["path"], cached=False)
        if actual is None:
            missing.append(r["path"])
            continue
        if actual == r["sha"]:
            continue
        # ⚠️ A digest mismatch is TWO different faults and they need opposite
        # fixes, so the body is compared as well rather than assuming the worse
        # one. Found on 2026-09-09: of 24 mismatches across the live index,
        # every single one had a body identical to the file — the stored digest
        # was wrong, the indexed text was current, and reporting them as a
        # stale index would have sent someone to re-ingest 4,059 documents to
        # fix nothing.
        print(f"    checking body: {r['path']}", file=sys.stderr)
        if r["docs"] == 1 and _body_matches(con, r["path"]):
            wrongsha.append(r["path"])
        else:
            behind.append(r["path"])
    n = len(rows)
    for p in behind:
        print(f"INDEX BEHIND  {p}")
    for p in wrongsha:
        print(f"DIGEST WRONG  {p}")
    for p in missing:
        print(f"UNREADABLE    {p}")
    print(f"\nverified {n} indexed file(s): {len(behind)} indexed at stale "
          f"content, {len(wrongsha)} carrying a wrong digest over current "
          f"content, {len(missing)} unreadable, {nodigest} without a digest")
    if behind:
        print("INDEX BEHIND: the indexed text is older than the file. Fix: "
              "`ingest --source ... --rehash` — a plain ingest skips these, "
              "because their mtime and size still match the ledger.")
    if wrongsha:
        print("DIGEST WRONG: the indexed text is CURRENT and only the recorded "
              "digest disagrees, so search answers correctly and any ledger "
              "joining on content does not. Fix: `backfill-hashes --all`. "
              "Re-ingesting fixes nothing here, because there is nothing stale "
              "about the text.")
    if not behind and not wrongsha and not missing:
        print("Every indexed file matches the content on disk.")
    if nodigest:
        print(f"{nodigest} row(s) carry no digest, so they are unknown rather "
              f"than verified: `backfill-hashes` fills what it can.")
    con.close()
    # Only a stale body is a wrong answer to a search. A wrong digest is a
    # bookkeeping fault, reported and not fatal, or every ledger run would
    # fail on something no reader would ever notice.
    return 3 if behind else 0


def _body_matches(con, path: str) -> bool:
    """Is the indexed body identical to the file's text as the extractor reads it?

    Compared through `extract.common.read_text`, the same function the
    extractors use, so a decoding or newline difference cannot masquerade as a
    content difference.
    """
    from .extract.common import read_text
    row = con.execute("SELECT body FROM docs WHERE path=? LIMIT 1", (path,)).fetchone()
    if row is None:
        return False
    try:
        return row["body"] == read_text(pathlib.Path(path))
    except OSError:
        return False


def cmd_backfill_hashes(a) -> int:
    """Fill src_sha256 for rows written before the column existed.

    Only rows whose source file is still readable at the recorded path can be
    filled; anything moved, deleted or unmounted stays NULL and is counted, not
    hidden. Re-runs are cheap — filled rows are skipped unless --all is given.
    """
    con = db.connect()
    applied = db.migrate(con)
    if applied:
        print(f"  migrated: {', '.join(applied)}")

    where = "" if a.all else " WHERE src_sha256 IS NULL"
    rows = con.execute(f"SELECT id, path FROM docs{where} ORDER BY path").fetchall()
    total = len(rows)
    if not total:
        print("nothing to backfill — every row already carries a digest")
        con.close()
        return 0

    filled = unreadable = nopath = 0
    cur = con.cursor()
    for i, r in enumerate(rows, 1):
        if not r["path"]:
            nopath += 1
            continue
        digest = db.file_sha256(r["path"])
        if digest is None:
            unreadable += 1
            continue
        cur.execute("UPDATE docs SET src_sha256=? WHERE id=?", (digest, r["id"]))
        filled += 1
        if i % 2000 == 0:
            con.commit()
            print(f"    {i}/{total} seen, {filled} filled ({unreadable} unreadable)",
                  flush=True)
    con.commit()

    still = con.execute("SELECT COUNT(*) FROM docs WHERE src_sha256 IS NULL").fetchone()[0]
    n = con.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    print(f"\nbackfill: {filled} filled, {unreadable} source file unreadable, "
          f"{nopath} row(s) with no path")
    print(f"coverage: {n - still}/{n} rows carry a digest "
          f"({100 * (n - still) / n:.1f}%); {still} remain NULL")
    if still:
        print("  NULL is 'not hashed', not 'no hash exists' — a consumer must "
              "report it as unknown coverage, never as a mismatch.")
    con.close()
    return 0


def cmd_prune(a) -> int:
    """Drop a source's rows on purpose.

    The escape hatch for a source that is declared but unreachable: `ingest`
    keeps those rows deliberately, so that an offline machine still searches
    what it indexed at home, which means there has to be a way to say "no,
    really, forget it". Undeclaring the source in the manifest does the same
    thing automatically.
    """
    con = db.connect()
    cur = con.cursor()
    held = set(db.sources_in_index(con))
    names = a.source or sorted(held)
    unknown = [n for n in names if n not in held]
    if unknown:
        sys.exit(f"the index holds no rows for: {', '.join(unknown)}\n"
                 f"it holds: {', '.join(sorted(held)) or '(nothing)'}")
    total = 0
    for n in names:
        before = con.execute("SELECT COUNT(*) FROM docs WHERE source=?",
                             (n,)).fetchone()[0]
        if not a.yes:
            print(f"would drop {before:,} row(s) from `{n}`")
            continue
        total += db.prune_source(cur, n, None)
        print(f"dropped {before:,} row(s) from `{n}`")
    if a.yes:
        con.commit()
        con.execute("INSERT INTO fts(fts) VALUES('optimize')")
        con.commit()
        print(f"\ntotal {total:,} row(s) dropped; "
              f"{con.execute('SELECT COUNT(*) FROM docs').fetchone()[0]:,} remain")
    else:
        print("\nnothing changed — pass --yes to apply")
    con.close()
    return 0


def cmd_mcp(a) -> int:
    """Serve MCP on stdio, or over loopback HTTP for the resident daemon.

    stdio is one server process per client, which is fine for a harness that
    spawns its own. A supervised deployment uses --http: one process outlives
    every client by design, and has no reaping problem to solve. There is no
    --host: the bind is 127.0.0.1, always.
    """
    from .mcp import serve, serve_http
    if a.http:
        return serve_http(a.port, a.reingest_interval, not a.no_ingest_on_start)
    return serve()


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="lookup",
                                 description="Search over personal archives.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("sources", help="list the inclusion manifest")
    p.add_argument("--count", action="store_true", help="count matching files (slower)")
    p.set_defaults(fn=cmd_sources)

    p = sub.add_parser("ingest", help="build or update the index")
    p.add_argument("--source", nargs="*", default=[])
    p.add_argument("--all", action="store_true")
    p.add_argument("--rehash", action="store_true",
                   help="read and hash every file even when mtime and size are "
                        "unchanged; the ledger's mtime+size check can be fooled "
                        "by a write that preserves both")
    p.add_argument("--optimize", action="store_true")
    p.set_defaults(fn=cmd_ingest)

    p = sub.add_parser("search", help="query the index")
    p.add_argument("query")
    p.add_argument("--source", nargs="*")
    p.add_argument("--since"); p.add_argument("--until")
    p.add_argument("--limit", type=int, default=15)
    p.add_argument("--json", action="store_true")
    p.add_argument("--full", action="store_true")
    # The MCP surface groups by source by default, because a merged ranking
    # across corpora of very different sizes reports the biggest corpus rather
    # than the right one. Here it is opt-in, so existing callers and the ops
    # scripts keep the flat output they parse.
    p.add_argument("--visibility", choices=("open", "gated", "unstated"),
                   help="only results whose effective visibility is this: a "
                        "document's own value, or its source's where the "
                        "document declares none. `--visibility open` is how to "
                        "ask for an answer drawn only from material that is "
                        "free to reuse.")
    p.add_argument("--grouped", action="store_true",
                   help="one ranking per source instead of one merged ranking; "
                        "--limit becomes per-source")
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("files",
                       help="dump the files a source declares, or the files it "
                            "indexed, as TSV for an external ledger")
    p.add_argument("--source", nargs="*",
                   help="source name(s); default is every source")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--indexed", action="store_true", default=True,
                   help="one row per source FILE in the index, with its digest "
                        "(default; needs the database)")
    g.add_argument("--declared", action="store_true",
                   help="the files the manifest's include globs select, from "
                        "the filesystem (needs no database)")
    p.add_argument("--relative", action="store_true",
                   help="with --declared, print paths relative to the root")
    p.add_argument("--verify", action="store_true",
                   help="with --indexed, re-hash every indexed file and report "
                        "where the index is behind it; exits 3 if any is. A "
                        "plain ingest skips a file whose mtime and size are "
                        "unchanged, and a git checkout restores content with "
                        "its old mtime — so run this periodically, not only "
                        "when you happen to suspect something")
    p.set_defaults(fn=cmd_files)

    p = sub.add_parser("backfill-hashes",
                       help="fill src_sha256 for rows that predate the column")
    p.add_argument("--all", action="store_true",
                   help="re-hash every row, not only those missing a digest")
    p.set_defaults(fn=cmd_backfill_hashes)

    sub.add_parser("stats", help="what is indexed").set_defaults(fn=cmd_stats)
    p = sub.add_parser("prune",
                       help="drop a source's rows from the index, on purpose")
    p.add_argument("--source", nargs="*",
                   help="source name(s); default is every source in the index")
    p.add_argument("--yes", action="store_true",
                   help="actually delete; without it this is a dry run")
    p.set_defaults(fn=cmd_prune)

    p = sub.add_parser("mcp", help="run the MCP server (stdio, or --http)")
    p.add_argument("--http", action="store_true",
                   help="serve Streamable HTTP on 127.0.0.1 instead of stdio, for "
                        "a resident deployment (POST /mcp)")
    p.add_argument("--port", type=int, default=8100,
                   help="port for --http (default 8100)")
    p.add_argument("--reingest-interval", type=int, default=300,
                   help="seconds between background re-ingests under --http; "
                        "0 disables the timer (default 300)")
    p.add_argument("--no-ingest-on-start", action="store_true",
                   help="skip the ingest at startup under --http")
    p.set_defaults(fn=cmd_mcp)
    return ap


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    if a.cmd == "ingest" and not a.all and not a.source:
        sys.exit("give --source NAME... or --all (see `sources`)")
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
