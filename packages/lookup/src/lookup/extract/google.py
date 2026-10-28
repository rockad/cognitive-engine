"""Google Takeout HTML exports: My Activity, Gemini Apps, Chrome bookmarks."""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

from .common import doc, google_ts, html_to_text, read_text

# Each activity entry is one `outer-cell`; the text sits in the body-1 content
# cell. Terminate on the next content-cell so the trailing "Products / Why is
# this here?" cells are not swallowed into the entry.
_CONTENT = re.compile(
    r'content-cell[^"]*mdl-typography--body-1">(.*?)'
    r'(?=<div class="content-cell|</div></div></div>)', re.S)
_FOOTER = re.compile(r"Why is this here\?")


def _cells(path: Path):
    for chunk in re.split(r'<div class="outer-cell', read_text(path))[1:]:
        m = _CONTENT.search(chunk)
        if not m:
            continue
        lines = [l.strip() for l in html_to_text(m.group(1)).split("\n") if l.strip()]
        if lines:
            yield lines


def gemini(path: Path):
    """One document per prompt+response pair.

    Both halves are kept: the prompt is the user's own writing, the response is
    the knowledge being researched. (The separate *voice* corpus keeps prompts
    only — model prose would contaminate a voice profile.)
    """
    for lines in _cells(path):
        if not lines[0].startswith("Prompted"):
            continue
        prompt = lines[0][len("Prompted"):].strip()
        if not prompt:
            continue
        date = google_ts(lines[1]) if len(lines) > 1 else None
        response = _FOOTER.split("\n".join(lines[2:]))[0].strip() if len(lines) > 2 else ""
        body = f"PROMPT:\n{prompt}" + (f"\n\nRESPONSE:\n{response}" if response else "")
        yield doc("gemini", path, prompt[:150], body, date,
                  {"product": "Gemini Apps", "prompt_chars": len(prompt),
                   "response_chars": len(response)})


def activity(path: Path):
    """One document per activity entry, for every non-Gemini product."""
    product = path.parent.name
    for lines in _cells(path):
        date = next((d for d in (google_ts(l) for l in lines[1:3]) if d), None)
        body = _FOOTER.split("\n".join(lines))[0].strip()
        if body:
            yield doc("activity", path, f"[{product}] {lines[0][:130]}", body, date,
                      {"product": product})


def bookmarks(path: Path):
    """One document per Chrome bookmark."""
    for m in re.finditer(r'<A HREF="([^"]+)"([^>]*)>(.*?)</A>', read_text(path), re.S | re.I):
        url, attrs, label = m.group(1), m.group(2), html_to_text(m.group(3))
        date = None
        d = re.search(r'ADD_DATE="(\d+)"', attrs)
        if d:
            try:
                date = dt.datetime.fromtimestamp(int(d.group(1))).isoformat()
            except (ValueError, OSError, OverflowError):
                pass
        yield doc("bookmarks", path, label or url, f"{label}\n{url}", date,
                  {"url": url[:500]})
