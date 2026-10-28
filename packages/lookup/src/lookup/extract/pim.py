"""Calendar (iCalendar) and Contacts (vCard) extraction."""
from __future__ import annotations

import re
from pathlib import Path

from .common import doc, read_text

# RFC 5545/6350 fold long lines with CRLF + a leading space/tab. Unfold first or
# every long field parses truncated.
_UNFOLD = re.compile(r"\r?\n[ \t]")

# Google writes a 19700101 sentinel DTSTART on some events; it is not a date.
_EPOCH_SENTINEL = "19700101"


def _field(block: str, name: str) -> str:
    m = re.search(rf"^{name}[^:]*:(.*)$", block, re.M)
    return m.group(1).strip() if m else ""


def ical(path: Path):
    raw = _UNFOLD.sub("", read_text(path))
    calendar = path.stem
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", raw, re.S):
        summary = _field(block, "SUMMARY")
        start = _field(block, "DTSTART")
        date = None
        m = re.match(r"(\d{4})(\d{2})(\d{2})", start)
        if m and m.group(0) != _EPOCH_SENTINEL:
            date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        location = _field(block, "LOCATION")
        attendees = " ".join(re.findall(r"ATTENDEE[^:]*:(\S+)", block))
        if not summary and not date:
            continue
        body = "\n".join(filter(None, [
            f"Event: {summary}",
            f"When: {start}",
            f"Location: {location}" if location else "",
            f"Attendees: {attendees}" if attendees else "",
            _field(block, "DESCRIPTION"),
        ]))
        yield doc("calendar", path, summary or "(untitled event)", body, date,
                  {"calendar": calendar})


def vcard(path: Path):
    raw = _UNFOLD.sub("", read_text(path))
    for block in re.findall(r"BEGIN:VCARD(.*?)END:VCARD", raw, re.S):
        name = _field(block, "FN") or _field(block, "N").replace(";", " ").strip()
        if not name:
            continue
        parts = [f"Name: {name}"]
        for label, key in (("Email", "EMAIL"), ("Phone", "TEL"), ("Org", "ORG"),
                           ("Title", "TITLE"), ("Note", "NOTE")):
            vals = [v.strip() for v in re.findall(rf"^{key}[^:]*:(.*)$", block, re.M) if v.strip()]
            if vals:
                parts.append(f"{label}: {', '.join(vals)}")
        yield doc("contacts", path, name, "\n".join(parts), None, {"list": path.stem})
