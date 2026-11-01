"""Shared parsing helpers.

Stdlib only — core has no bs4/lxml, and keeping it that way is what lets the
arm64 image cross-build with no compiled dependencies.
"""
from __future__ import annotations

import datetime as dt
import html
import re
from pathlib import Path

_TAG = re.compile(r"<[^>]+>")
_SCRIPT = re.compile(r"<(script|style)\b.*?</\1>", re.S | re.I)
_WS = re.compile(r"[ \t\r\f\v]+")
_NL = re.compile(r"\n{3,}")
_GOOGLE_TS = re.compile(r"^([A-Z][a-z]{2} \d{1,2}, \d{4}, \d{1,2}:\d{2}:\d{2}\s*[AP]M)")


def html_to_text(raw: str) -> str:
    """Collapse HTML to readable plain text without a parser dependency."""
    raw = _SCRIPT.sub(" ", raw)
    raw = re.sub(r"<br\s*/?>", "\n", raw, flags=re.I)
    raw = re.sub(r"</(p|div|li|tr|h[1-6])>", "\n", raw, flags=re.I)
    raw = _TAG.sub(" ", raw)
    raw = html.unescape(raw)
    raw = _WS.sub(" ", raw)
    return _NL.sub("\n\n", raw).strip()


def read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def mtime(p: Path) -> str:
    return dt.datetime.fromtimestamp(p.stat().st_mtime).isoformat()


def google_ts(line: str) -> str | None:
    """Parse Google's 'Jul 30, 2026, 2:19:08 PM EEST' timestamps to ISO."""
    m = _GOOGLE_TS.match(line.strip())
    if not m:
        return None
    try:
        return dt.datetime.strptime(m.group(1).strip(), "%b %d, %Y, %I:%M:%S %p").isoformat()
    except ValueError:
        return None


def doc(source: str, path: Path, title: str, body: str,
        date: str | None = None, meta: dict | None = None,
        key: str | None = None, visibility: str | None = None) -> dict:
    """Build one index document. `key`, when given, is a stable identity
    (mail uses Message-ID); otherwise the pipeline assigns a positional one.

    `visibility` is the document's OWN declared value, or None to inherit the
    source's. Only extractors over authored text supply it; everything else
    leaves it None, which is the honest answer for a corpus with nowhere to
    write one.
    """
    return {
        "source": source,
        "path": str(path),
        "title": (title or "").strip()[:300] or path.name,
        "date": date,
        "body": body,
        "meta": meta or {},
        "key": key,
        "visibility": visibility,
    }


# `visibility:` in YAML front matter, and nothing else from it. A full YAML
# parse is deliberately not used: this runs over every markdown file in every
# corpus, the only key that matters is one bare scalar, and a parser would make
# an unrelated syntax error elsewhere in the block fail the whole file.
#
# ⚠️ Front matter is a LEADING `---` fence only. A `---` further down a document
# is a horizontal rule, and treating one as a fence would read prose as
# metadata.
_FRONT_MATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
_VISIBILITY_KEY = re.compile(
    r"^visibility:[ \t]*[\"']?([A-Za-z]+)[\"']?[ \t]*$", re.M)
_DOC_VISIBILITIES = ("open", "gated", "unstated")


def front_matter_visibility(text: str) -> str | None:
    """The `visibility:` value from a document's own front matter, or None.

    An unrecognised value returns None rather than raising. The manifest
    validates a SOURCE's value loudly because a typo there mislabels a whole
    corpus; a typo in one file's front matter must not stop that file being
    indexed. None means inherit, which can never be wider than the source, so
    the failure direction is the safe one.

    `mixed` is not accepted here: it describes a corpus, and a document
    claiming it would be answering a question about itself that it cannot.
    """
    m = _FRONT_MATTER.match(text)
    if not m:
        return None
    v = _VISIBILITY_KEY.search(m.group(1))
    if not v:
        return None
    value = v.group(1).lower()
    return value if value in _DOC_VISIBILITIES else None
