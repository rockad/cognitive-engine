"""Plain document extraction: markdown, HTML, JSON and tabular files."""
from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path

from .common import (doc, front_matter_visibility, html_to_text, mtime,
                     read_text)

_H1 = re.compile(r"^#\s+(.+)$", re.M)


def _source_for(path: Path) -> str:
    """Documents are labelled by which declared root they came from.

    The pipeline overrides this with the manifest's source name, so this is only
    a sensible default when an extractor is called directly (e.g. in tests).
    """
    return "documents"


def markdown(path: Path):
    text = read_text(path)
    if not text.strip():
        return
    m = _H1.search(text)
    yield doc(_source_for(path), path, m.group(1) if m else path.stem,
              text, mtime(path), {"ext": path.suffix},
              visibility=front_matter_visibility(text))


def html_or_text(path: Path):
    raw = read_text(path)
    text = html_to_text(raw) if path.suffix.lower() in (".html", ".htm") else raw
    if not text.strip():
        return
    # Front matter is read from the RAW text, not the HTML-stripped text: the
    # `---` fence survives stripping but the tag remover can reflow around it.
    yield doc(_source_for(path), path, path.stem, text, mtime(path),
              {"ext": path.suffix},
              visibility=front_matter_visibility(raw))


def json_blob(path: Path):
    """Flatten arbitrary JSON to searchable text.

    Keep/Tasks/Maps each use a different shape, and matching every schema buys
    nothing for a keyword index — the values are what get searched.
    """
    try:
        data = json.loads(read_text(path))
    except (json.JSONDecodeError, OSError):
        return

    def walk(o, depth=0):
        if depth > 6:
            return []
        if isinstance(o, dict):
            return [f"{k}: {' '.join(walk(v, depth + 1))}" for k, v in o.items()
                    if walk(v, depth + 1)]
        if isinstance(o, list):
            return [s for i in o for s in walk(i, depth + 1)]
        if isinstance(o, (str, int, float)) and str(o).strip():
            return [str(o)]
        return []

    body = "\n".join(walk(data))
    if not body.strip():
        return
    title = path.stem
    if isinstance(data, dict):
        title = str(data.get("title") or data.get("name") or title)
    yield doc(_source_for(path), path, title, body, None, {"ext": ".json"})


def tabular(path: Path):
    """CSV rows, or JSON via json_blob — Maps exports mix both."""
    if path.suffix.lower() == ".json":
        yield from json_blob(path)
        return
    try:
        rows = list(csv.reader(io.StringIO(read_text(path))))
    except (csv.Error, OSError):
        return
    body = "\n".join(", ".join(c for c in r if c) for r in rows)
    if not body.strip():
        return
    yield doc(_source_for(path), path, path.stem, body, None, {"rows": len(rows)})
