"""Database access and document identity."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sqlite3
from pathlib import Path

from . import config


# Columns added to `docs` after the first release. `executescript(SCHEMA)` runs
# on every open, but `CREATE TABLE IF NOT EXISTS` is a no-op against a table
# that already exists, so a new column never reaches an existing database that
# way. These are applied explicitly, are idempotent, and only ever ADD — a
# migration that rewrites or drops does not belong in an auto-run path.
_ADD_COLUMNS = (
    ("src_sha256", "TEXT"),
    # A document's own visibility; NULL means inherit the source's value. See
    # schema.sql for why it is not derived from a path rule, and `upsert_ex`
    # for why it has to be in the change comparison.
    ("visibility", "TEXT"),
)


def migrate(con: sqlite3.Connection) -> list[str]:
    """Add any missing `docs` columns. Returns what it added.

    MUST run BEFORE the schema script, not after: the schema declares an index
    over `src_sha256`, and `CREATE INDEX ... ON docs(src_sha256)` fails outright
    against a database whose `docs` predates the column. Running the script
    first is what that ordering bug looks like — `no such column: src_sha256`
    on open, for every existing index.
    """
    # Indexed, not keyed: `connect` calls this before it sets row_factory, so
    # these rows are plain tuples. table_info columns are (cid, name, type, ...).
    have = {r[1] for r in con.execute("PRAGMA table_info(docs)")}
    if not have:
        return []          # fresh database — the schema script builds it whole
    applied = []
    for name, decl in _ADD_COLUMNS:
        if name not in have:
            con.execute(f"ALTER TABLE docs ADD COLUMN {name} {decl}")
            applied.append(f"docs.{name}")

    # The original docs_au fired on a bare UPDATE, so writing any column rewrote
    # the FTS entry. CREATE TRIGGER IF NOT EXISTS will not replace it, so drop
    # the old form here and let the schema script recreate the narrowed one.
    sql = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='docs_au'"
    ).fetchone()
    if sql and "UPDATE OF" not in (sql[0] or ""):
        con.execute("DROP TRIGGER docs_au")
        applied.append("trigger docs_au -> UPDATE OF title, body")

    if applied:
        con.commit()
    return applied


def connect(readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        if not config.DB.exists():
            raise FileNotFoundError(f"index not found at {config.DB} — run `ingest` first")
        con = sqlite3.connect(f"file:{config.DB}?mode=ro", uri=True)
    else:
        config.DB.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(config.DB)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        migrate(con)
        con.executescript(config.SCHEMA.read_text())
    con.row_factory = sqlite3.Row
    return con


# One file yields many documents (5,660 vcards from one .vcf), so the digest is
# cached per path. Bounded because an ingest walks tens of thousands of files
# and an unbounded dict would hold every path for the life of the process; the
# access pattern is one file at a time, so a small cache hits ~100%.
#
# ⚠️ IT MUST NOT SURVIVE AN INGEST RUN, and it did until 2026-09-09. The
# resident server re-ingests every 300 s in ONE long-lived process, so an entry
# taken on an earlier pass could still be here on a later one. A file edited
# between the two was then re-read for its body — correctly, mtime had changed —
# and paired with the digest of the version before the edit. The row's text was
# current and its `src_sha256` was one revision behind, which no search could
# reveal and which broke exactly the thing the digest exists for: a ledger
# joining on content. 25 rows across the live index were in that state, found by
# `files --verify` the day it was written. `clear_sha_cache()` at the top of
# every ingest is the fix.
_SHA_CACHE: dict[str, str | None] = {}
_SHA_CACHE_MAX = 64


def clear_sha_cache() -> None:
    """Drop every cached digest. Called at the start of each ingest run."""
    _SHA_CACHE.clear()


def file_sha256(path: str | Path, cached: bool = True) -> str | None:
    """sha256 of a file's bytes, or None if it cannot be read.

    None is not an error here: extractors legitimately emit documents whose
    `path` is a container entry rather than a file on disk, and a source tree
    can be mounted read-only and incomplete. The caller records the None.

    ⚠️ Pass `cached=False` from any caller whose PURPOSE is to notice that a
    file has changed. The cache exists so a file yielding 5,660 documents is
    hashed once, which is right during an ingest and wrong for a verification:
    `files --verify` first returned 0 against a deliberately corrupted file,
    because the digest it compared was the one this cache had taken moments
    earlier during the same process's ingest. A check that does not re-read is
    the failure it exists to catch.
    """
    key = str(path)
    if cached and key in _SHA_CACHE:
        return _SHA_CACHE[key]
    digest: str | None
    try:
        h = hashlib.sha256()
        with open(key, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        digest = h.hexdigest()
    except (OSError, ValueError):
        digest = None
    if len(_SHA_CACHE) >= _SHA_CACHE_MAX:
        _SHA_CACHE.clear()
    _SHA_CACHE[key] = digest
    return digest


def doc_key(d: dict, ordinal: int) -> str:
    """Stable, collision-free identity for a document.

    An extractor may supply its own `key` (mail uses Message-ID). Otherwise the
    key is source + path + the document's ordinal within that path — NOT its
    body.

    Hashing the body looked correct until two distinct contacts sharing a name
    with no other fields hashed identically and silently overwrote each other:
    5,660 vcards collapsed to 3,971 rows, reported as "updated". Ordinals cannot
    collide, and stay stable across re-runs while the source is append-only.
    """
    h = hashlib.sha1()
    h.update((d.get("source") or "").encode())
    h.update(b"|")
    h.update((d.get("path") or "").encode())
    h.update(b"|")
    h.update(str(d.get("key") or ordinal).encode())
    return h.hexdigest()


def upsert_ex(cur: sqlite3.Cursor, key: str, d: dict) -> str:
    """Insert, update, or leave alone. Returns "added", "updated" or "unchanged".

    `d["src_sha256"]` is the source file's digest when the pipeline could take
    one. On update it is written unconditionally, including as NULL: a document
    re-ingested from a file that is no longer readable must not keep claiming a
    digest that was never re-verified.

    ⚠️ **"unchanged" exists because the resident server re-ingests on a timer.**
    Writing every row on every pass rewrote `ingested` 1,199 times each run,
    which dirtied every page: one refresh took the database from 28.3 MB to
    40.8 MB, and at a five-minute tick that growth is unbounded until the
    process restarts. Skipping identical documents makes a refresh over an
    unchanged corpus a genuine no-op.

    The cost is that `ingested` now means "when this document last *changed*",
    not "when it was last seen". That is the more useful of the two, and the
    `runs` table already records when a pass happened.
    """
    body = d["body"]
    title = (d.get("title") or "")[:300]
    meta = json.dumps(d.get("meta") or {}, ensure_ascii=False)
    nbytes = len(body.encode("utf-8", "replace"))
    sha = d.get("src_sha256")
    vis = d.get("visibility")

    cur.execute("SELECT id, source, path, title, date, body, meta, bytes,"
                " src_sha256, visibility FROM docs WHERE doc_key=?", (key,))
    have = cur.fetchone()
    row = (d["source"], d.get("path"), title, d.get("date"), body, meta, nbytes,
           dt.datetime.now().isoformat(), sha, vis)
    if have is not None:
        # ⚠️ EVERY WRITTEN COLUMN BELONGS IN THIS COMPARISON, and `visibility`
        # is the one where forgetting it is dangerous rather than merely wrong.
        # This tuple decides "unchanged", and "unchanged" writes nothing: a
        # document whose front matter was edited from `open` to `gated` would
        # be read as identical, the index would keep serving the wider label,
        # and the resident server's re-ingest timer would never correct it —
        # a silent failure that looks exactly like the feature working.
        same = (have["source"] == d["source"] and have["path"] == d.get("path")
                and have["title"] == title and have["date"] == d.get("date")
                and have["body"] == body and have["meta"] == meta
                and have["bytes"] == nbytes and have["src_sha256"] == sha
                and have["visibility"] == vis)
        if same:
            return "unchanged"
        cur.execute(
            "UPDATE docs SET source=?,path=?,title=?,date=?,body=?,meta=?,bytes=?,"
            "ingested=?,src_sha256=?,visibility=? WHERE doc_key=?", row + (key,))
        return "updated"
    cur.execute(
        "INSERT INTO docs(source,path,title,date,body,meta,bytes,ingested,src_sha256,"
        "visibility,doc_key) VALUES(?,?,?,?,?,?,?,?,?,?,?)", row + (key,))
    return "added"


def upsert(cur: sqlite3.Cursor, key: str, d: dict) -> bool:
    """Back-compatible wrapper: True if newly added.

    Kept because `ops/` and several tests call it. "unchanged" reads as False
    here, which matches the old behaviour from the caller's point of view — the
    document is in the index and was not newly created.
    """
    return upsert_ex(cur, key, d) == "added"


def stats(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT source, COUNT(*) n, SUM(bytes) b, MIN(date) lo, MAX(date) hi"
        " FROM docs GROUP BY source ORDER BY n DESC").fetchall()


# FTS5's query grammar rejects most punctuation outright, and callers hand us
# natural-language questions however firmly they are told to write FTS5. Two
# failures were hit for real, both hard errors rather than poor results:
# a trailing '?' gives `fts5: syntax error near "?"`, and a hyphenated word is
# parsed as a column filter, so "off-site" gives `no such column: site` and
# "lookup" gives `no such column: index`. Ten questions out of ten
# failed this way. An error is a worse answer than a blunt one, so a failing
# query is retried in a relaxed form.
_STOPWORDS = frozenset("""
a an and any are as at be been but by can could did do does for from had has
have how i if in into is it its me my of on or should so that the their then
there these they this to was we were what when where which who why will with
would you your
""".split())

_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def relax(query: str) -> str | None:
    """Rewrite a natural-language question as a valid FTS5 query.

    Every token is quoted, so no punctuation can be read as syntax, and the
    tokens are OR-ed rather than AND-ed: a question's words rarely all appear
    in the answer, and `bm25()` already ranks a document matching more of the
    rare terms above one matching a single common term. Stopwords are dropped
    for the same reason. Returns None when nothing usable is left, so the
    caller can surface the original error instead of an empty search.
    """
    toks = [t for t in _WORD.findall(query.lower())
            if len(t) > 1 and t not in _STOPWORDS]
    # Preserve order, drop repeats.
    seen, out = set(), []
    for t in toks:
        if t not in seen:
            seen.add(t)
            out.append(t)
    if not out:
        return None
    return " OR ".join('"%s"' % t for t in out)


def _run(con, query, sources, since, until, limit, visibility=None,
         inherit_sources=()):
    where, params = ["fts MATCH ?"], [query]
    if sources:
        where.append(f"d.source IN ({','.join('?' * len(sources))})")
        params += list(sources)
    if since:
        where.append("d.date >= ?"); params.append(since)
    if until:
        where.append("d.date <= ?"); params.append(until)
    if visibility:
        # A row's effective visibility is its own value, or its source's when
        # NULL. The source's value lives in the manifest, not in the database,
        # so the caller resolves which sources carry the wanted value and
        # passes them as `inherit_sources` — `db` stays free of manifest
        # knowledge, and the filter is still applied IN SQL.
        #
        # ⚠️ In SQL rather than over the returned rows on purpose: `LIMIT`
        # applies before a post-filter would, so filtering afterwards would
        # quietly return fewer results than asked for and look like a thin
        # corpus rather than a narrowed query.
        clause = ["d.visibility = ?"]
        params.append(visibility)
        if inherit_sources:
            clause.append(
                f"(d.visibility IS NULL AND d.source IN "
                f"({','.join('?' * len(inherit_sources))}))")
            params += list(inherit_sources)
        where.append(f"({' OR '.join(clause)})")
    params.append(limit)
    return con.execute(
        "SELECT d.doc_key, d.source, d.title, d.date, d.path, d.body, d.meta,"
        " d.visibility,"
        " snippet(fts, 1, '[', ']', ' ... ', 18) AS snip"
        " FROM fts JOIN docs d ON d.id = fts.rowid"
        f" WHERE {' AND '.join(where)} ORDER BY bm25(fts) LIMIT ?", params).fetchall()


# An explicit operator means the caller wrote FTS5 on purpose, so an empty
# result is their answer and must not be second-guessed. A bare phrase is a
# question, and FTS5 AND-s bare terms — which is why "staging rule when
# committing" matched nothing at all in a corpus that plainly holds the answer.
_FTS5_OPERATORS = re.compile(r'"|\*|:|\bAND\b|\bOR\b|\bNOT\b|\bNEAR\b')


def looks_like_fts5(query: str) -> bool:
    """True when the query uses FTS5 syntax deliberately."""
    return bool(_FTS5_OPERATORS.search(query))


def search_ex(con: sqlite3.Connection, query: str, sources=None, since=None,
              until=None, limit: int = 15, relaxed: bool = True,
              visibility=None, inherit_sources=()):
    """Search, falling back to a relaxed query when the input does not work.

    Two failures get the fallback, and they are different:

    * **FTS5 rejects the query.** A trailing '?' or a hyphen raises
      `OperationalError`, so there is no result at all.
    * **FTS5 accepts it and matches nothing.** Bare terms are AND-ed, so a
      plain phrase demands every word appear in one document. Only retried
      when the query carries no explicit operator — a caller who wrote
      `"passphrase" AND backup` and got nothing has their answer.

    Returns (rows, effective_query, relaxed_from). `relaxed_from` is None when
    the query ran as given, and otherwise carries the original, so a caller can
    tell the user what was actually searched.
    """
    vis = (visibility, inherit_sources)
    try:
        rows = _run(con, query, sources, since, until, limit, *vis)
    except sqlite3.OperationalError:
        if not relaxed:
            raise
        alt = relax(query)
        if alt is None:
            raise
        return _run(con, alt, sources, since, until, limit, *vis), alt, query

    if rows or not relaxed or looks_like_fts5(query):
        return rows, query, None
    alt = relax(query)
    if alt is None or alt == query:
        return rows, query, None
    return _run(con, alt, sources, since, until, limit, *vis), alt, query


def search(con: sqlite3.Connection, query: str, sources=None, since=None,
           until=None, limit: int = 15, visibility=None,
           inherit_sources=()) -> list[sqlite3.Row]:
    return search_ex(con, query, sources, since, until, limit,
                     visibility=visibility,
                     inherit_sources=inherit_sources)[0]


def source_names(con: sqlite3.Connection) -> list[str]:
    """Every source present in the index, largest first."""
    return [r["source"] for r in con.execute(
        "SELECT source FROM docs GROUP BY source ORDER BY COUNT(*) DESC")]


def search_grouped(con: sqlite3.Connection, query: str, sources=None,
                   since=None, until=None, per_source: int = 3,
                   visibility=None, inherit_sources=()):
    """One search per source, results kept apart rather than merged.

    A single ranking over sources of wildly different sizes returns whichever
    corpus is largest, not whichever is right. Measured over eleven questions
    on this corpus: searching per source answered ten within the top three,
    while one global ranking answered six. The cause is that `bm25()` is
    computed over the whole index, so a long document from a big corpus
    matching many of a question's words outranks a short precise one from a
    small corpus matching few — and `.claude/memory/`, at 41 short files, is
    where a large share of the answers live.

    Returns a list of (source, rows, effective_query, relaxed_from), skipping
    sources with no match, in the order the sources were given.
    """
    names = list(sources) if sources else source_names(con)
    out = []
    for name in names:
        try:
            rows, effective, relaxed_from = search_ex(
                con, query, [name], since, until, per_source,
                visibility=visibility, inherit_sources=inherit_sources)
        except sqlite3.OperationalError:
            # A query FTS5 rejects and relax() cannot rescue fails for every
            # source alike; let the caller see it rather than returning a
            # silently empty result for each one in turn.
            raise
        if rows:
            out.append((name, rows, effective, relaxed_from))
    return out


def prune_source(cur: sqlite3.Cursor, source: str, keep: set[str] | None = None) -> int:
    """Delete rows for `source` that no longer correspond to a live document.

    `keep` is the set of doc_keys an ingest pass actually saw; rows outside it
    have lost their file. `keep=None` means the source itself is gone — a
    missing root, or a source no longer in the manifest — so every row goes.

    ⚠️ **Ingestion used to be purely additive, and that was a real defect.**
    Removing the archive mirror left its 530 rows in the index: `sources`
    correctly reported `archive MISSING`, a refresh reported `8 source(s),
    673 unchanged`, and `archive_stats` still claimed 530 documents while
    searches returned paths that were GONE. An index whose residency follows
    its files has to be able to forget.

    Returns the number of rows deleted. The FTS entry goes with the row via
    the `docs_ad` trigger.
    """
    if keep is None:
        cur.execute("DELETE FROM src_files WHERE source=?", (source,))
        cur.execute("DELETE FROM docs WHERE source=?", (source,))
        return cur.rowcount
    cur.execute("SELECT doc_key FROM docs WHERE source=?", (source,))
    gone = [r[0] if not isinstance(r, sqlite3.Row) else r["doc_key"]
            for r in cur.fetchall()]
    gone = [k for k in gone if k not in keep]
    if not gone:
        return 0
    # Chunked: SQLite's default parameter limit is 999 and a source can hold
    # tens of thousands of rows.
    for i in range(0, len(gone), 500):
        batch = gone[i:i + 500]
        cur.execute(
            f"DELETE FROM docs WHERE doc_key IN ({','.join('?' * len(batch))})",
            batch)
    return len(gone)


def sources_in_index(con: sqlite3.Connection) -> list[str]:
    """Every distinct source name the index holds rows for."""
    return [r[0] if not isinstance(r, sqlite3.Row) else r["source"]
            for r in con.execute("SELECT DISTINCT source FROM docs")]


def file_state(cur: sqlite3.Cursor, source: str, path: str):
    """(mtime, size, [doc_keys]) recorded for this file, or None."""
    cur.execute("SELECT mtime, size, keys FROM src_files WHERE source=? AND path=?",
                (source, path))
    r = cur.fetchone()
    if r is None:
        return None
    mtime = r["mtime"] if isinstance(r, sqlite3.Row) else r[0]
    size = r["size"] if isinstance(r, sqlite3.Row) else r[1]
    keys = r["keys"] if isinstance(r, sqlite3.Row) else r[2]
    try:
        return mtime, size, json.loads(keys)
    except (TypeError, json.JSONDecodeError):
        return None


def record_file(cur: sqlite3.Cursor, source: str, path: str, mtime: float,
                size: int, keys: list[str]) -> None:
    """Record what a file looked like and which documents it produced."""
    cur.execute(
        "INSERT INTO src_files(source, path, mtime, size, keys, seen)"
        " VALUES(?,?,?,?,?,?)"
        " ON CONFLICT(source, path) DO UPDATE SET"
        " mtime=excluded.mtime, size=excluded.size, keys=excluded.keys,"
        " seen=excluded.seen",
        (source, path, float(mtime), int(size),
         json.dumps(sorted(keys)), dt.datetime.now().isoformat()))


def forget_files(cur: sqlite3.Cursor, source: str, keep_paths: set[str] | None = None) -> int:
    """Drop ledger rows for files that are gone, so the ledger cannot outlive
    the documents it describes. `keep_paths=None` drops the whole source."""
    if keep_paths is None:
        cur.execute("DELETE FROM src_files WHERE source=?", (source,))
        return cur.rowcount
    cur.execute("SELECT path FROM src_files WHERE source=?", (source,))
    rows = [r["path"] if isinstance(r, sqlite3.Row) else r[0] for r in cur.fetchall()]
    gone = [p for p in rows if p not in keep_paths]
    for i in range(0, len(gone), 500):
        batch = gone[i:i + 500]
        cur.execute(
            "DELETE FROM src_files WHERE source=? AND path IN (%s)"
            % ",".join("?" * len(batch)), [source, *batch])
    return len(gone)
