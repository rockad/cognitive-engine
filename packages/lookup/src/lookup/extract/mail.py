"""Gmail mbox extraction."""
from __future__ import annotations

import email
import email.policy
import email.utils
from pathlib import Path

from .. import config
from .common import doc, html_to_text


def _parse(chunk: list[str], path: Path) -> dict | None:
    try:
        msg = email.message_from_string("".join(chunk), policy=email.policy.default)
    except Exception:
        return None

    subject = str(msg.get("Subject", "") or "")
    frm = str(msg.get("From", "") or "")
    to = str(msg.get("To", "") or "")

    date = None
    if msg.get("Date"):
        try:
            date = email.utils.parsedate_to_datetime(str(msg.get("Date"))).isoformat()
        except Exception:
            date = None

    try:
        part = msg.get_body(preferencelist=("plain", "html"))
        text = part.get_content() if part is not None else ""
        if part is not None and part.get_content_type() == "text/html":
            text = html_to_text(text)
    except Exception:
        text = ""
    if not text or not text.strip():
        return None

    body = f"From: {frm}\nTo: {to}\nSubject: {subject}\n\n{text[:config.MAIL_MAX_BODY]}"
    msgid = str(msg.get("Message-ID", "") or "").strip()
    return doc("mail", path, subject or "(no subject)", body, date,
               {"from": frm[:200], "to": to[:200], "msgid": msgid[:200]},
               key=msgid or None)


def mbox(path: Path):
    """Stream an mbox, yielding one document per message.

    Streamed rather than via `mailbox.mbox` on purpose: that builds a full table
    of contents up front, and this archive is 3.5 GB on a box whose swap is
    already fully consumed. Messages are split on the `From ` line at column 0,
    which is the mbox record separator.
    """
    buf: list[str] = []
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith("From ") and buf:
                d = _parse(buf, path)
                if d:
                    yield d
                buf = [line]
            else:
                buf.append(line)
    d = _parse(buf, path)
    if d:
        yield d
