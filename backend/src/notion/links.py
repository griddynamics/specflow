"""The Notion links of the source form and of the plan destination form (docs/notion/source.md "Links").

``parse_link`` takes the text a member pasted and answers the object id in it, or raises ``NotionError`` with
``notion_link_invalid``. No request is made: whether the id is a page or a database, and whether the member shared it
with the connection, is read from Notion afterwards. The id is the last 32 hex digits of the last path segment
(``/Title-<id>``, ``/<id>``, ``/p/<Title>-<id>``); a ``?v=<view id>`` marks a database link (``view`` is set). Only
``notion.so``, ``notion.com`` (and their ``www.``), ``app.notion.com`` and ``*.notion.site`` over ``https`` are links
of Notion."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final
from urllib.parse import parse_qs, urlsplit

from notion.reasons import NotionCode, NotionError

_HOSTS: Final = frozenset({"notion.so", "www.notion.so", "notion.com", "www.notion.com", "app.notion.com"})
_PLAIN_ID: Final = r"(?:^|[-/])([0-9a-fA-F]{32})$"
_DASHED_ID: Final = r"^([0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12})$"
_ID_AT_END: Final = re.compile(f"{_PLAIN_ID}|{_DASHED_ID}")
LINK_MAX: Final = 2048


@dataclass(frozen=True)
class NotionLink:
    """The object a link names: ``id`` with dashes; ``view`` is the ``?v=`` view id of a database link, else
    ``None``."""

    id: str
    view: str | None = None

    @property
    def probably_database(self) -> bool:
        return self.view is not None


def dashed(plain: str) -> str:
    """``8-4-4-4-12`` form of 32 hex digits, lower case."""

    plain = plain.replace("-", "").lower()
    return f"{plain[:8]}-{plain[8:12]}-{plain[12:16]}-{plain[16:20]}-{plain[20:]}"


def parse_link(text: str) -> NotionLink:
    """The ``NotionLink`` of ``text``; ``NotionError`` ``notion_link_invalid`` when it is not a Notion page or database
    link."""

    invalid = NotionError(NotionCode.LINK_INVALID)
    text = text.strip()
    if not text or len(text) > LINK_MAX:
        raise invalid
    try:
        parts = urlsplit(text)
    except ValueError:
        raise invalid from None
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not (host in _HOSTS or host.endswith(".notion.site")):
        raise invalid
    segment = parts.path.rstrip("/").rsplit("/", 1)[-1]
    found = _ID_AT_END.search(segment)
    if found is None:
        raise invalid
    view = parse_qs(parts.query).get("v", [None])[0]
    view_id = None if view is None or not re.fullmatch(r"[0-9a-fA-F]{32}", view) else dashed(view)
    return NotionLink(dashed(found.group(1) or found.group(2)), view_id)
