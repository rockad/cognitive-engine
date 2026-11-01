"""Extractor registry.

`sources.toml` refers to extractors by `module.function`. Resolution goes
through this explicit table rather than `importlib` on an arbitrary string, so
a typo in the manifest fails loudly at load time and the manifest cannot name
arbitrary code.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

from . import docs, google, mail, pim

Extractor = Callable[[Path], Iterator[dict]]

REGISTRY: dict[str, Extractor] = {
    "google.gemini": google.gemini,
    "google.activity": google.activity,
    "google.bookmarks": google.bookmarks,
    "mail.mbox": mail.mbox,
    "pim.ical": pim.ical,
    "pim.vcard": pim.vcard,
    "docs.markdown": docs.markdown,
    "docs.html_or_text": docs.html_or_text,
    "docs.json_blob": docs.json_blob,
    "docs.tabular": docs.tabular,
}


def resolve(name: str) -> Extractor:
    try:
        return REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"unknown extractor {name!r}; known: {', '.join(sorted(REGISTRY))}") from None
