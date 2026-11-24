"""Confluence links: what a link the member pastes points to, and the pages and Jira issues a page links to.

A page URL (``/wiki/spaces/<KEY>/pages/<id>/<title>`` or ``/wiki/pages/viewpage.action?pageId=<id>``) and a space URL
(``/wiki/spaces/<KEY>``, ``.../overview``) of the configured site are understood. A short link (``/wiki/x/<tiny id>``)
is refused: Confluence documents no tiny-id algorithm and the only call that resolves one (the v1 content API) needs a
scope the site admin did not grant, so ``confluence_short_link_unsupported``. Nothing here logs or keeps a link.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final
from urllib.parse import parse_qs, unquote, urlsplit

from jira.storage_markdown import parse_storage

_PAGE_PATH: Final = re.compile(r"^/wiki/spaces/(?P<key>[^/]+)/pages/(?P<id>\d+)(?:/|$)")
_SPACE_PATH: Final = re.compile(r"^/wiki/spaces/(?P<key>[^/]+)(?:/(?:overview|pages|blog)?/?)?$")
_VIEW_PAGE: Final = "/wiki/pages/viewpage.action"
_SHORT_PATH: Final = re.compile(r"^/wiki/x/")
_BROWSE: Final = re.compile(r"^/browse/(?P<key>[A-Z][A-Z0-9_]*-\d+)/?$")
_ISSUE_KEY: Final = re.compile(r"^[A-Z][A-Z0-9_]*-\d+$")
_SPACE_KEY: Final = re.compile(r"^[A-Za-z0-9~_-]+$")


class LinkCode(StrEnum):
    """Why a pasted link is refused (closed; the route turns each into its own API code)."""

    INVALID = "confluence_link_invalid"
    SHORT = "confluence_short_link_unsupported"
    SITE_MISMATCH = "atlassian_site_mismatch"


class LinkError(Exception):
    """A refused link; ``code`` is the whole message."""

    def __init__(self, code: LinkCode) -> None:
        super().__init__(code.value)
        self.code = code


@dataclass(frozen=True)
class PageTarget:
    page_id: str


@dataclass(frozen=True)
class SpaceTarget:
    key: str


@dataclass(frozen=True)
class TitledPage:
    """A page named by title (and space key when the link gives one), as the ``ri:page`` of an ``ac:link`` does."""

    title: str
    space_key: str | None


@dataclass(frozen=True)
class References:
    """What one page links to: pages by id, pages by title and Jira issue keys, in order of appearance, once each."""

    page_ids: list[str] = field(default_factory=list)
    titled: list[TitledPage] = field(default_factory=list)
    issue_keys: list[str] = field(default_factory=list)


def parse_link(link: str, site_host: str) -> PageTarget | SpaceTarget:
    """The page or space a link of ``site_host`` points to; ``LinkError`` for anything else."""

    parts = urlsplit(link.strip())
    if parts.scheme != "https" or not parts.hostname:
        raise LinkError(LinkCode.INVALID)
    if parts.hostname.lower() != site_host.lower():
        raise LinkError(LinkCode.SITE_MISMATCH)
    path = unquote(parts.path)
    if _SHORT_PATH.match(path):
        raise LinkError(LinkCode.SHORT)
    page = _PAGE_PATH.match(path)
    if page:
        return PageTarget(page["id"])
    if path == _VIEW_PAGE:
        ids = parse_qs(parts.query).get("pageId", [])
        if len(ids) == 1 and ids[0].isdigit():
            return PageTarget(ids[0])
        raise LinkError(LinkCode.INVALID)
    space = _SPACE_PATH.match(path)
    if space and _SPACE_KEY.match(space["key"]):
        return SpaceTarget(space["key"])
    raise LinkError(LinkCode.INVALID)


def _href_reference(href: str, site_host: str) -> tuple[str, str] | None:
    """``("page", id)`` or ``("issue", key)`` for a same-site link, else ``None``."""

    parts = urlsplit(href)
    if parts.scheme != "https" or (parts.hostname or "").lower() != site_host.lower():
        return None
    path = unquote(parts.path)
    page = _PAGE_PATH.match(path)
    if page:
        return "page", page["id"]
    issue = _BROWSE.match(path)
    if issue:
        return "issue", issue["key"]
    if path == _VIEW_PAGE:
        ids = parse_qs(parts.query).get("pageId", [])
        if len(ids) == 1 and ids[0].isdigit():
            return "page", ids[0]
    return None


def references(storage: str, site_host: str) -> References:
    """The same-site pages and the Jira issues a storage-format body links to (links to other sites are ignored)."""

    found = References()
    for node in parse_storage(storage).elements():
        if node.tag == "a":
            target = _href_reference(node.attrs.get("href", ""), site_host)
            if target is not None:
                _add(found.page_ids if target[0] == "page" else found.issue_keys, target[1])
        elif node.tag == "ri:page" and node.attrs.get("ri:content-title"):
            titled = TitledPage(node.attrs["ri:content-title"], node.attrs.get("ri:space-key") or None)
            _add(found.titled, titled)
        elif node.tag == "ac:structured-macro" and node.attrs.get("ac:name") == "jira":
            for child in node.children:
                if not isinstance(child, str) and child.tag == "ac:parameter" and child.attrs.get("ac:name") == "key":
                    key = child.text().strip()
                    if _ISSUE_KEY.match(key):
                        _add(found.issue_keys, key)
    return found


def _add[T](items: list[T], item: T) -> None:
    if item not in items:
        items.append(item)
