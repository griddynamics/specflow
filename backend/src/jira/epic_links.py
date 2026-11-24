"""A Jira epic named by its key or by a link of the configured site (``/browse/<KEY>`` or a board link with
``selectedIssue=<KEY>``). Pure; nothing is logged or kept."""

from __future__ import annotations

import re
from typing import Final
from urllib.parse import parse_qs, unquote, urlsplit

_KEY: Final = re.compile(r"^[A-Za-z][A-Za-z0-9_]*-\d+$")
_BROWSE: Final = re.compile(r"/browse/(?P<key>[^/]+)/?$")


class EpicLinkError(Exception):
    """A refused key or link; ``site_mismatch`` when it is a well-formed link of another host."""

    def __init__(self, *, site_mismatch: bool = False) -> None:
        super().__init__("epic_site_mismatch" if site_mismatch else "epic_link_invalid")
        self.site_mismatch = site_mismatch


def parse_epic(value: str, site_host: str) -> str:
    """The upper-cased issue key of ``value`` (a key, or a link of ``site_host``); ``EpicLinkError`` otherwise."""

    text = value.strip()
    if _KEY.match(text):
        return text.upper()
    parts = urlsplit(text)
    if parts.scheme != "https" or not parts.hostname:
        raise EpicLinkError
    if parts.hostname.lower() != site_host.lower():
        raise EpicLinkError(site_mismatch=True)
    found = _BROWSE.search(unquote(parts.path))
    candidates = [found["key"]] if found else parse_qs(parts.query).get("selectedIssue", [])
    if len(candidates) == 1 and _KEY.match(candidates[0]):
        return candidates[0].upper()
    raise EpicLinkError
