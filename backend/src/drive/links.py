"""Accepted Drive folder links (DATA-GDUI-0002).

The rules equal ``examples/gdrive_explore.parse_link`` (case-sensitive host,
a ``?`` tail and a trailing ``/`` are fine, any other host, scheme or path is
rejected). A parity test keeps both in step.
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Final

ID_PATTERN: Final = r"[A-Za-z0-9_-]{10,}"
_ID: Final = re.compile(ID_PATTERN)
_LINK_FORMS: Final = (
    re.compile(rf"https://drive\.google\.com/drive/folders/({ID_PATTERN})/?"),
    re.compile(rf"https://drive\.google\.com/drive/u/[0-9]+/folders/({ID_PATTERN})/?"),
)
_OPEN: Final = "https://drive.google.com/open"
CANONICAL_PREFIX: Final = "https://drive.google.com/drive/folders/"


def is_drive_id(value: str) -> bool:
    """True when ``value`` is safe to put inside a Drive ``q`` string."""

    return _ID.fullmatch(value) is not None


def parse_folder_link(link: str) -> str | None:
    """The folder id of an accepted link or bare id, else None (DATA-GDUI-0002)."""

    link = link.strip()
    if _ID.fullmatch(link):
        return link
    base, _, query = link.partition("?")
    for form in _LINK_FORMS:
        match = form.fullmatch(base)
        if match:
            return match.group(1)
    if base == _OPEN:
        ids = urllib.parse.parse_qs(query).get("id", [])
        if ids and _ID.fullmatch(ids[0]):
            return ids[0]
    return None


def canonical_link(folder_id: str) -> str:
    """The stored form of a folder link; never the text the user pasted."""

    if not is_drive_id(folder_id):
        raise ValueError("not a Drive id")
    return f"{CANONICAL_PREFIX}{folder_id}"
