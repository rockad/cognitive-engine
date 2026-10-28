"""MCP server for Lookup: FastMCP over stdio or loopback Streamable HTTP.

**The tool handlers are the product; the transport is FastMCP's.** `HANDLERS`
below takes a plain dict and returns text, and `build_server()` registers each
under the name and description in `TOOLS`. Nothing else in this package imports
FastMCP, so the engine (db, manifest, pipeline, extractors) stays stdlib-only.

**HTTP is the deployment**: one resident process on `127.0.0.1:8100/mcp`,
stateless, so every session shares one index and one re-ingest timer. The bind
address is not a parameter — loopback only, because the tools return the text of
documents classified `gated` and there is no authentication in front of them.

**stdio is kept for development and for harnesses that spawn their server.**
One process per session is the shape that leaked containers on the old Docker
deployment (`--rm` does not fire when a client dies without closing stdin), so
anything serving real traffic should be the single HTTP process.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from typing import Annotated, Literal

from pydantic import Field

from . import db

from . import __version__ as VERSION

HOST = "127.0.0.1"
PORT = 8100
PATH = "/mcp"


TOOLS = [
    {
        "name": "search_archive",
        "description": (
            "Full-text search across user indexed corpora — Obsidian "
            "vault, durable memories, session handoff files, repository "
            "documentation, shared knowledge base, skills, and "
            "transcripts. Use it for questions about user history, "
            "decisions, past work, correspondence or documents.\n\n"
            "Ask a plain English question. Punctuation and hyphens are safe, and "
            "so is a phrase whose words do not all appear together: a query that "
            "FTS5 rejects, or that it accepts and matches nothing, is retried "
            "with every token quoted and OR-ed, and the reply says what was "
            "actually searched. Deliberate FTS5 syntax (OR, NOT, \"quoted "
            "phrases\", trailing *) is honoured verbatim and an empty result is "
            "left as the answer.\n\n"
            "⚠️ A result is marked REMOTE when the index reads that source over "
            "a network mount, which the machine you are on very likely does not "
            "have: the path is real to the index and not to you. It is marked NOT "
            "REACHABLE when even the index cannot resolve it. Either way, search "
            "still answers from the index's own copy of the text, and "
            "get_document returns the full body without touching the filesystem "
            "— so read a marked result with get_document rather than by opening "
            "its path.\n\n"
            "⚠️ Every result carries a `visibility`, inherited from the system "
            "the material came from. `open` — a company-wide channel, an "
            "unrestricted page — is free to reuse. `gated` — a DM, a private "
            "channel, a permission-restricted page — is usable only after the "
            "fact is verified against its source and the user has explicitly "
            "signed it off. `unstated` means the source declares nothing, so "
            "the provenance is unknown and unknown is handled as gated. Nothing "
            "is banned: provenance decides the ceremony, not whether the "
            "material may be used, so bring a gated fact to the user rather than "
            "discarding it.\n\n"
            "Results come back GROUPED BY SOURCE, one small ranking per corpus, "
            "because a single ranking across corpora of very different sizes "
            "returns whichever corpus is largest rather than whichever is right — "
            "measured over eleven questions, per-source search answered ten within "
            "the top three and one global ranking answered six. Read each group. "
            "Pass `source` only to narrow the search deliberately; call "
            "archive_stats for the list of source names."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "a plain question, or FTS5 syntax"},
                "source": {"type": "array", "items": {"type": "string"},
                           "description": "narrow to these sources; omit to search "
                                          "them all, grouped. See archive_stats."},
                "since": {"type": "string", "description": "ISO date lower bound"},
                "until": {"type": "string", "description": "ISO date upper bound"},
                "limit": {"type": "integer", "default": 3, "maximum": 10,
                          "description": "results per source, not in total"},
                "full": {"type": "boolean", "default": False,
                         "description": "whole documents instead of snippets; only "
                                        "honoured when `source` names one source"},
                "visibility": {"type": "string",
                               "enum": ["open", "gated", "unstated"],
                               "description": "only results with this effective "
                                              "visibility — a document's own value, "
                                              "or its source's where it declares "
                                              "none. Pass `open` when you need an "
                                              "answer you can reuse without asking "
                                              "the user first."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_document",
        "description": "Fetch one indexed document in full by the doc_key returned "
                       "in search_archive results.",
        "inputSchema": {"type": "object",
                        "properties": {"doc_key": {"type": "string"}},
                        "required": ["doc_key"]},
    },
    {
        "name": "refresh_index",
        "description": (
            "Re-ingest every declared source, so a note written moments ago is "
            "findable now. Takes about 1.5 s over 1,197 documents. The resident "
            "server also re-ingests on a timer, so this is for when you cannot "
            "wait for the next tick — after writing a note you then want to "
            "search, most obviously. Safe to call repeatedly: ingestion is "
            "idempotent, keyed on source|path|ordinal."),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "archive_stats",
        "description": "What is in the index: document counts, text volume and date "
                       "range per source. Check coverage here before concluding "
                       "something is absent.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


_REMOTE_SOURCES: set[str] | None = None


def remote_sources() -> set[str]:
    """Sources the index reaches over a network mount the CALLER may not share.

    ⚠️ This exists because the obvious implementation of "tell the caller the
    path will not open" does not work, and shipping it proved that.
    `_reachable()` runs inside the server, and the server is a container whose
    own cifs volume mounts `/Volumes/knowledge-archive` regardless of what the
    host has mounted. So when the host's SMB mount was removed, every archive
    result was still reported as reachable — correctly, from where the check
    ran, and uselessly for the caller who asked.

    The server cannot know what the caller has mounted. What it does know is
    which sources it reaches over a network volume, because the manifest says
    so, and a path under one of those is a path the caller has no reason to be
    able to open. That is the honest signal: not "this is broken" but "this is
    not yours to open".
    """
    global _REMOTE_SOURCES
    if _REMOTE_SOURCES is None:
        try:
            from . import manifest
            _REMOTE_SOURCES = {n for n, src in manifest.load().items() if src.volume}
        except Exception:
            _REMOTE_SOURCES = set()
    return _REMOTE_SOURCES


_VISIBILITY: dict[str, str] | None = None


def source_visibility() -> dict[str, str]:
    """Each source's declared visibility, read from the manifest.

    A fact inherits the visibility of the system it came from: material from a
    company-wide channel or an unrestricted page is free to reuse, material
    from a DM, a private channel or a restricted page needs verifying against
    its source and the user's sign-off first. Nothing is refused outright.

    The label has to travel with the result, because the agent reading a hit is
    the one about to reuse the fact and it cannot see the manifest. A source
    that declares nothing is reported as `unstated` and handled as gated —
    unknown provenance resolves that way rather than to `open`.

    Cached like `remote_sources()`, and for the same reason: the manifest does
    not change under a running server, and a failure to load it must not take
    search down with it.
    """
    global _VISIBILITY
    if _VISIBILITY is None:
        try:
            from . import manifest
            _VISIBILITY = {n: s.visibility for n, s in manifest.load().items()}
        except Exception:
            _VISIBILITY = {}
    return _VISIBILITY


def _visibility_of(source: str | None) -> str:
    """The label for one SOURCE. An unknown source is unstated, not open.

    ⚠️ Not the label for a HIT. A document carries its own value, so a result
    line goes through `_visibility_row`; this answers only what the manifest
    says about the corpus, which for a `mixed` source is "ask the document".
    """
    from . import manifest
    return source_visibility().get(source or "", manifest.VISIBILITY_UNSTATED)


def _inherit_sources(want: str | None) -> tuple[str, ...]:
    """Sources whose OWN visibility is `want`, so their NULL rows match it.

    `db` knows nothing about the manifest by design, so a filter on effective
    visibility has to be told which sources' documents inherit the wanted
    value. A manifest that would not load leaves `source_visibility()` empty,
    which matches only rows carrying an explicit value — narrower than was
    asked for, never wider, which is the safe direction for a filter whose job
    is to keep gated material out of an answer.
    """
    if not want:
        return ()
    # ⚠️ Through `effective_visibility`, the same function that labels a hit, so
    # the filter and the label cannot disagree. Comparing the raw source value
    # missed one case: a document that states nothing inside a `mixed` source is
    # LABELLED `unstated` but sat in a source whose value is `mixed`, so
    # `visibility="unstated"` never returned it. (`open` was never affected: a
    # `mixed` source resolves to `unstated`, never to `open`.)
    from . import manifest
    return tuple(n for n, v in source_visibility().items()
                 if manifest.effective_visibility(None, v) == want)


def _visibility_row(row) -> str:
    """The label for one HIT: its own value, falling back to its source's.

    Every result line must use this. A `mixed` source with a document that
    declared nothing resolves to `unstated`, never to `mixed` — `mixed`
    describes a corpus and says nothing about the document in hand, while
    `unstated` is both true and handled as gated.
    """
    from . import manifest
    try:
        own = row["visibility"]
    except (KeyError, IndexError):
        own = None          # a row selected without the column
    return manifest.effective_visibility(own, _visibility_of(row["source"]))


def _visibility_legend(names, rows=None) -> str:
    """One line naming what in this reply is not free to reuse.

    Added only when something actually is not, so an all-`open` reply carries no
    ceremony at all. The per-hit lines carry the bare label; this is where it is
    said what the label obliges the reader to do.

    `rows`, when given, is the reply's actual hits, and it is what makes a
    `mixed` source reportable: naming the source would be useless there, since
    the answer differs per document, so those hits are counted instead.
    """
    from . import manifest
    vis = {n: _visibility_of(n) for n in set(names)}
    gated = sorted(n for n, v in vis.items() if v == manifest.VISIBILITY_GATED)
    unstated = sorted(n for n, v in vis.items()
                      if v == manifest.VISIBILITY_UNSTATED)
    # A hit inside an `open` or `mixed` source can still be gated on its own
    # account, and that is invisible from the source list alone.
    narrowed = 0
    for r in (rows or ()):
        if (_visibility_of(r["source"]) not in (manifest.VISIBILITY_GATED,
                                                manifest.VISIBILITY_UNSTATED)
                and _visibility_row(r) != manifest.VISIBILITY_OPEN):
            narrowed += 1
    if not gated and not unstated and not narrowed:
        return ""
    out = ["⚠️ **Not every result below is free to reuse.** A fact inherits the "
           "visibility of the system it came from."]
    if gated:
        out.append(f"`{'`, `'.join(gated)}` "
                   f"{'is' if len(gated) == 1 else 'are'} **gated**: verify the "
                   f"fact against its source and get the user's explicit "
                   f"sign-off before reusing it anywhere it was not already "
                   f"put.")
    if unstated:
        out.append(f"`{'`, `'.join(unstated)}` "
                   f"{'declares' if len(unstated) == 1 else 'declare'} no "
                   f"visibility (**unstated**), so the provenance is unknown "
                   f"and unknown is handled as gated — same verification and "
                   f"sign-off.")
    if narrowed:
        out.append(f"{narrowed} result{'' if narrowed == 1 else 's'} below "
                   f"{'carries' if narrowed == 1 else 'carry'} a visibility of "
                   f"{'its' if narrowed == 1 else 'their'} own that is narrower "
                   f"than the source's — read the `visibility:` on each line "
                   f"rather than the source's name, and treat anything that is "
                   f"not `open` as needing the same sign-off.")
    out.append("Nothing here is banned; provenance decides the ceremony, not "
               "whether the material may be used.")
    return " ".join(out) + "\n"


def _reachable(path: str | None) -> bool:
    """Does this document's file still resolve on the machine serving the query?

    A source can be indexed and then become unreachable — the knowledge archive
    is on a network share, and the share is routinely unmounted. Its documents
    stay searchable on purpose, but the path in the result no longer opens, and
    a caller that is told nothing will simply try to read it and get a
    confusing failure.

    Decision 2026-09-07: *"if the path no longer resolved - a notification
    message, but allow search and fail on navigating to the note."* So the
    search still answers from the index's own copy of the text; only the
    invitation to open the file is withdrawn.
    """
    if not path:
        return False
    try:
        return os.path.exists(path)
    except OSError:
        return False


def _hit(r, text: str) -> str:
    if not _reachable(r["path"]):
        # Not reachable by the INDEX either: its source is gone or unmounted
        # on the server side, so the row is a survivor of a corpus that moved.
        gone = ("  ⚠️ NOT REACHABLE even by the index — its source is not "
                "mounted here; the text below is the index's own copy")
    elif r["source"] in remote_sources():
        gone = ("  ⚠️ REMOTE — the index reads this over a network mount, so "
                "this path very likely does not resolve on your machine; use "
                "get_document with the doc_key rather than opening it")
    else:
        gone = ""
    # `visibility` rides on the same line as `date` and `doc_key` because the
    # reader about to reuse this fact is the one who needs it, and it cannot
    # see the manifest. `open` is free to reuse; `gated` and `unstated` need
    # verification against the source and the user's sign-off — the legend at
    # the head of the reply says so in full.
    return (f"### {r['title']}\n"
            f"date: {(r['date'] or 'unknown')[:10]}  |  doc_key: {r['doc_key']}"
            f"  |  visibility: {_visibility_row(r)}\n"
            f"path: {r['path']}{gone}\n\n{text}")


def t_search(a: dict) -> str:
    """Search, grouped by source unless exactly one source is named.

    The grouping is the default rather than an option because the failure it
    prevents is the caller forgetting to scope: a merged ranking over corpora
    of very different sizes reports the biggest corpus, not the right one. A
    caller who names one source has scoped deliberately and gets a single
    ranking, with `full` bodies if asked.
    """
    con = db.connect(readonly=True)
    sources = a.get("source") or None
    per_source = max(1, min(int(a.get("limit", 3)), 10))
    # An unrecognised value is dropped rather than refused: a filter is a
    # narrowing, and a caller who mistypes one should get the unfiltered
    # answer with the label on every hit, not an error instead of results.
    want_vis = a.get("visibility")
    if want_vis not in ("open", "gated", "unstated"):
        want_vis = None

    if sources and len(sources) == 1:
        rows, effective, relaxed_from = db.search_ex(
            con, a["query"], sources, a.get("since"), a.get("until"),
            per_source, visibility=want_vis,
            inherit_sources=_inherit_sources(want_vis))
        if not rows:
            return (f"No matches in `{sources[0]}`. Try broader terms, drop "
                    f"`source` to search every corpus, or call archive_stats.")
        full = bool(a.get("full"))
        head = [f"## [{sources[0]}] {len(rows)} result(s)"]
        if relaxed_from:
            head.append(f"_searched as_ `{effective}` — the query as written "
                        f"did not work (FTS5 either rejected it or matched "
                        f"nothing), so every token was quoted and OR-ed.")
        n_gone = sum(1 for r in rows if not _reachable(r["path"]))
        if n_gone:
            head.append(f"⚠️ {n_gone} of {len(rows)} name a path the index cannot "
                        f"resolve either — that source is not mounted on the "
                        f"server. The text is the index's own copy.")
        elif sources[0] in remote_sources():
            head.append(f"⚠️ `{sources[0]}` is read over a **network mount**, so "
                        f"these paths very likely do not resolve on your machine. "
                        f"**Use `get_document` with the doc_key rather than opening "
                        f"the path.**")
        legend = _visibility_legend([sources[0]], rows)
        if legend:
            head.append(legend)
        body = []
        for r in rows:
            text = r["body"] if full else r["snip"]
            if full and len(text) > 20000:
                text = text[:20000] + "\n... [truncated]"
            body.append(_hit(r, text))
        return "\n".join(head) + "\n\n" + "\n\n---\n\n".join(body)

    groups = db.search_grouped(con, a["query"], sources, a.get("since"),
                               a.get("until"), per_source,
                               visibility=want_vis,
                               inherit_sources=_inherit_sources(want_vis))
    if not groups:
        searched = ", ".join(sources) if sources else "every corpus"
        return (f"No matches in {searched}. Try broader terms, or call "
                f"archive_stats to check coverage.")
    note = groups[0][3]
    out = []
    if note:
        out.append(f"_searched as_ `{groups[0][2]}` — the query as written did "
                   f"not work (FTS5 either rejected it or matched nothing), so "
                   f"every token was quoted and OR-ed.\n")
    out.append("Grouped by source; each group is ranked on its own, so a hit in a "
               "small corpus is not buried by a large one.\n")
    remote = remote_sources()
    unreachable = sum(1 for _n, rows, _e, _r in groups
                      for r in rows if not _reachable(r["path"]))
    n_remote = sum(1 for name, rows, _e, _r in groups
                   for r in rows if name in remote and _reachable(r["path"]))
    if unreachable:
        out.append(f"⚠️ {unreachable} result(s) name a path the INDEX cannot resolve "
                   f"either — that source is not mounted on the server. The text is "
                   f"still the index's own copy and is answerable.\n")
    if n_remote:
        out.append(f"⚠️ {n_remote} result(s) come from a source the index reads over a "
                   f"**network mount** ({', '.join(sorted(remote))}), so those paths "
                   f"very likely do not resolve on your machine — the index has the "
                   f"share mounted, you may not. **Use `get_document` with the "
                   f"doc_key rather than opening the path**; it returns the full body "
                   f"without touching the filesystem.\n")
    legend = _visibility_legend([name for name, _rows, _e, _r in groups],
                               [r for _n, rs, _e, _r in groups for r in rs])
    if legend:
        out.append(legend)
    for name, rows, _effective, _relaxed in groups:
        out.append(f"## [{name}]")
        out.append("\n\n".join(_hit(r, r["snip"]) for r in rows))
    return "\n\n".join(out)


def t_get(a: dict) -> str:
    con = db.connect(readonly=True)
    r = con.execute("SELECT source,title,date,path,body,meta,visibility"
                    " FROM docs WHERE doc_key=?",
                    (a["doc_key"],)).fetchone()
    if not r:
        return f"No document with doc_key {a['doc_key']}"
    if not _reachable(r["path"]):
        note = ("\n⚠️ that path does not resolve for the index either — its source "
                "is not mounted on the server. The body below is the index's own "
                "copy, which is why this tool works when opening the file would not.")
    elif r["source"] in remote_sources():
        note = ("\n⚠️ that path is read over a network mount, so it very likely does "
                "not resolve on your machine. The body below is the index's own copy, "
                "which is why this tool works when opening the file would not.")
    else:
        note = ""
    legend = _visibility_legend([r["source"]], [r])
    return (f"# {r['title']}\nsource: {r['source']}  date: {r['date']}  "
            f"visibility: {_visibility_row(r)}\n"
            f"path: {r['path']}{note}\nmeta: {r['meta']}\n"
            + (f"\n{legend}" if legend else "")
            + f"\n{r['body']}")


def t_stats(_a: dict) -> str:
    con = db.connect(readonly=True)
    rows = db.stats(con)
    tot = con.execute("SELECT COUNT(*), SUM(bytes) FROM docs").fetchone()
    # `visibility` is here as well as on every hit, so a caller choosing which
    # source to search can see up front which corpora will need sign-off.
    lines = ["| source | docs | text | date range | visibility |",
             "|---|---:|---:|---|---|"]
    for r in rows:
        span = f"{(r['lo'] or '')[:10]} to {(r['hi'] or '')[:10]}" if r["lo"] else "-"
        lines.append(f"| {r['source']} | {r['n']:,} | {(r['b'] or 0)/1048576:.1f} MB "
                     f"| {span} | {_visibility_of(r['source'])} |")
    lines.append(f"| **total** | **{tot[0]:,}** | **{(tot[1] or 0)/1048576:.1f} MB** | | |")
    # Aliases, so a reader can tell that several addresses are ONE source
    # rather than wondering which of them the index used.
    try:
        from . import manifest
        aliased = {n: s.addresses() for n, s in manifest.load().items()
                   if s.addresses()}
    except Exception:
        aliased = {}
    if aliased:
        lines.append("")
        lines.append("Sources reachable at more than one address — the same "
                     "location, not copies:")
        for name, addrs in sorted(aliased.items()):
            lines.append(f"- **{name}**: {', '.join(addrs)}")
    return "\n".join(lines)


# One ingest at a time. The timer thread and a `refresh_index` call can
# otherwise overlap, and two writers on one SQLite file is a lock error rather
# than a merge.
_INGEST_LOCK = threading.Lock()


def t_refresh(_a: dict) -> str:
    from . import pipeline
    if not _INGEST_LOCK.acquire(blocking=False):
        return "A refresh is already running; the index will be current shortly."
    try:
        t0 = time.time()
        r = pipeline.ingest_all(optimize=True)
    finally:
        _INGEST_LOCK.release()
    out = [f"Re-ingested {r['sources']} source(s) in {time.time() - t0:.1f} s — "
           f"{r['added']} added, {r['updated']} changed, {r['unchanged']} "
           f"unchanged, {r['pruned']} pruned, {r['errors']} error(s), "
           f"{r['refused']} refused. {r['skipped_files']:,} file(s) skipped "
           f"unread on an unchanged mtime and size. Index now "
           f"{r['docs']:,} documents, {r['db_bytes'] / 1048576:.1f} MB."]
    if r["dropped"]:
        # Named, not just counted: a document leaving the index is the one
        # outcome a caller cannot discover by searching for it.
        out.append("Dropped (file or source gone): "
                   + ", ".join(f"{k} {v:,}" for k, v in sorted(r["dropped"].items())))
    if r["undeclared"]:
        out.append("Removed from the manifest, so their rows went: "
                   + ", ".join(r["undeclared"]))
    if r["reverify"]:
        out.append("Unreachable, so their file ledger was dropped — the next "
                   "pass that finds them re-reads and re-hashes every file "
                   "rather than trusting a pre-gap mtime: "
                   + ", ".join(r["reverify"]))
    if r["stale"]:
        # Kept deliberately: an unreachable root is not the same as a deleted
        # one, and an offline machine should keep searching what it indexed at
        # home. `lookup prune --source NAME` drops them on purpose.
        out.append("⚠️ Declared but unreachable, rows KEPT and now stale: "
                   + ", ".join(r["stale"]))
    if r["skipped"]:
        out.append("Skipped, root absent: " + ", ".join(r["skipped"]))
    if r["unmounted"]:
        out.append("⚠️ Declare a network `volume` that native Lookup does not mount: "
                   + ", ".join(r["unmounted"])
                   + ". Their roots must already resolve on this machine — mount the "
                     "share on the host and point `root` at it. Until then they are "
                     "not indexed, which is not the same as empty.")
    return "\n".join(out)


HANDLERS = {"search_archive": t_search, "get_document": t_get,
            "archive_stats": t_stats, "refresh_index": t_refresh}



def _refresher(interval: int) -> None:
    """Re-ingest on a timer, forever.

    A timer rather than a file watcher, deliberately: a watcher can look correct
    and silently miss changes (inotify does not cross every filesystem boundary),
    which is the worst available failure mode. A timer cannot miss anything; it
    can only be late, and an ingest costs ~1.5 s, so a 5-minute tick is well
    under 1% duty cycle. `refresh_index` exists for when a caller cannot wait.
    """
    from . import pipeline
    while True:
        time.sleep(interval)
        if not _INGEST_LOCK.acquire(blocking=False):
            continue
        try:
            t0 = time.time()
            r = pipeline.ingest_all(optimize=True)
            sys.stderr.write(
                "refresh: %d source(s), +%d added, %d changed, %d unchanged, "
                "%d pruned%s, %d total, %.1fs\n"
                % (r["sources"], r["added"], r["updated"], r["unchanged"],
                   r["pruned"],
                   (" [" + ", ".join(f"{k} {v}" for k, v in sorted(r["dropped"].items())) + "]")
                   if r["dropped"] else "",
                   r["docs"], time.time() - t0))
            sys.stderr.flush()
        except Exception as e:
            # A refresh failure must not kill the server: the existing index is
            # still serving, and the next tick may well succeed.
            sys.stderr.write("refresh FAILED: %s: %s\n" % (type(e).__name__, e))
            sys.stderr.flush()
        finally:
            _INGEST_LOCK.release()


def _startup_ingest() -> None:
    """Index once before serving, so the first search is not 'index not found'."""
    from . import pipeline
    with _INGEST_LOCK:
        try:
            t0 = time.time()
            r = pipeline.ingest_all(optimize=True, vacuum=True)
            dropped = ("  dropped: " + ", ".join(
                f"{k} {v}" for k, v in sorted(r["dropped"].items()))
            ) if r["dropped"] else ""
            stale = ("  stale (rows kept): " + ", ".join(r["stale"])) if r["stale"] else ""
            unmounted = ("  UNMOUNTED network volume (not indexed): "
                         + ", ".join(r["unmounted"])) if r["unmounted"] else ""
            sys.stderr.write("startup ingest: %d docs, %.1f MB, %.1fs%s%s%s\n"
                             % (r["docs"], r["db_bytes"] / 1048576,
                                time.time() - t0, dropped, stale, unmounted))
        except Exception as e:
            sys.stderr.write("startup ingest FAILED: %s: %s\n" % (type(e).__name__, e))
        sys.stderr.flush()


def build_server():
    """A FastMCP server exposing the four Lookup tools.

    Each tool is a thin typed shim over `HANDLERS`, which keep taking the plain
    argument dict they always did, so the engine's behaviour is pinned by the same
    tests whichever transport carries it. Descriptions come from `TOOLS`.
    """
    from fastmcp import FastMCP

    desc = {t["name"]: t["description"] for t in TOOLS}
    server = FastMCP("lookup", version=VERSION)

    @server.tool(name="search_archive", description=desc["search_archive"])
    def search_archive(
        query: Annotated[str, Field(description="a plain question, or FTS5 syntax")],
        source: Annotated[list[str] | None, Field(
            description="narrow to these sources; omit to search them all, grouped. "
                        "See archive_stats.")] = None,
        since: Annotated[str | None, Field(description="ISO date lower bound")] = None,
        until: Annotated[str | None, Field(description="ISO date upper bound")] = None,
        limit: Annotated[int, Field(ge=1, le=10,
                                    description="results per source, not in total")] = 3,
        full: Annotated[bool, Field(
            description="whole documents instead of snippets; only honoured when "
                        "`source` names one source")] = False,
        visibility: Annotated[Literal["open", "gated", "unstated"] | None, Field(
            description="only results with this effective visibility — a document's "
                        "own value, or its source's where it declares none. Pass "
                        "`open` when you need an answer you can reuse without asking "
                        "the user first.")] = None,
    ) -> str:
        return t_search({"query": query, "source": source, "since": since,
                         "until": until, "limit": limit, "full": full,
                         "visibility": visibility})

    @server.tool(name="get_document", description=desc["get_document"])
    def get_document(doc_key: str) -> str:
        return t_get({"doc_key": doc_key})

    @server.tool(name="refresh_index", description=desc["refresh_index"])
    def refresh_index() -> str:
        return t_refresh({})

    @server.tool(name="archive_stats", description=desc["archive_stats"])
    def archive_stats() -> str:
        return t_stats({})

    return server


def serve() -> int:
    """MCP over stdio. stdout is the protocol channel; logs go to stderr."""
    build_server().run(transport="stdio", show_banner=False)
    return 0


def serve_http(port: int = PORT, reingest_interval: int = 300,
               ingest_on_start: bool = True) -> int:
    """MCP over Streamable HTTP on loopback, stateless, plus a re-ingest timer."""
    if ingest_on_start:
        _startup_ingest()
    if reingest_interval > 0:
        threading.Thread(target=_refresher, args=(reingest_interval,),
                         daemon=True, name="reingest").start()
    sys.stderr.write(f"lookup {VERSION} MCP starting on http://{HOST}:{port}{PATH}\n")
    sys.stderr.flush()
    build_server().run(transport="http", host=HOST, port=port, path=PATH,
                       stateless_http=True, show_banner=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(serve())
