"""Parse a pasted diagrams.net link (docs/sources/importers.md): which form it is and where its diagram is.

Forms read: ``#R<payload>`` (the diagram is in the link), ``#U<url>`` and the legacy ``?url=<url>`` (the diagram is
fetched), ``#P{json}`` with a ``data`` member (the diagram is in the link), and a bare ``https`` URL of a ``.drawio``
file. A storage hash (Google Drive, GitHub, ...) is refused naming the storage. Pure and offline; no error carries the
link.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Final, Literal
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit

from file_parsers.diagrams import MAX_LINK_BYTES

DIAGRAM_HOSTS: Final = frozenset(
    {"app.diagrams.net", "viewer.diagrams.net", "embed.diagrams.net", "www.draw.io", "app.draw.io"}
)
STORAGES: Final = {
    "G": "Google Drive",
    "H": "GitHub",
    "D": "Dropbox",  # retired by diagrams.net, still named
    "W": "OneDrive",
    "A": "GitLab",
    "L": "browser storage",
    "T": "Trello",
}
HTTPS_PORT: Final = 443
_SUFFIXES: Final = frozenset({"drawio", "xml", "svg", "png"})

LinkError = Literal["invalid", "unsupported", "too_large"]
LinkKind = Literal["payload", "xml", "url"]


class DrawioLinkError(Exception):
    """A link that cannot be used. ``code`` is ``invalid``, ``unsupported`` (``storage`` names it when known) or
    ``too_large``. The message is empty: it never carries the link."""

    def __init__(self, code: LinkError, storage: str | None = None) -> None:
        super().__init__(code)
        self.code: LinkError = code
        self.storage = storage


@dataclass(frozen=True)
class DrawioLink:
    """A parsed link: ``kind`` says what ``value`` is (``payload``: the text after ``#R``; ``xml``: the diagram XML of
    ``#P``; ``url``: an ``https`` URL to fetch). ``stored`` is what the source keeps as its link: no fragment for the
    forms that hold the diagram in it (the viewer URL), ``scheme://host/path`` of the fetched URL for the others (no
    query, no fragment: either may carry a token). ``name`` is the diagram name the link gives: its ``title``
    parameter, else the file name of the fetched URL, else empty."""

    kind: LinkKind
    value: str
    stored: str
    name: str = ""


def _fetchable(url: str) -> str:
    """``url`` when it is a plain ``https`` URL without credentials, else ``invalid``."""

    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        raise DrawioLinkError("invalid") from None
    if parts.scheme != "https" or not parts.hostname or parts.username is not None or parts.password is not None:
        raise DrawioLinkError("invalid")
    if port not in (None, HTTPS_PORT):
        raise DrawioLinkError("invalid")
    return url.strip()


def _bare(url: str) -> str:
    """``scheme://host/path`` of ``url``."""

    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _file_name(url: str) -> str:
    return unquote(urlsplit(url).path.rsplit("/", 1)[-1])


def clean_name(name: str) -> str:
    """``name`` without a ``.drawio``, ``.xml``, ``.svg`` or ``.png`` suffix (also stacked, as ``a.drawio.svg``)."""

    name = " ".join(name.split())
    while True:
        stem, dot, ext = name.rpartition(".")
        if not (dot and stem and ext.lower() in _SUFFIXES):
            return name
        name = stem.strip()


def _fetched(url: str, title: str) -> DrawioLink:
    return DrawioLink("url", url, _bare(url), clean_name(title) or clean_name(_file_name(url)))


def _page_data(fragment: str) -> str:
    try:
        data = json.loads(unquote(fragment))
    except (ValueError, RecursionError):  # bad JSON, or nested deeper than the parser reads
        raise DrawioLinkError("invalid") from None
    payload = data.get("data") if isinstance(data, dict) else None
    if not isinstance(payload, str) or not payload:
        raise DrawioLinkError("invalid")
    return payload


def _from_fragment(fragment: str, stripped: str, title: str) -> DrawioLink | None:
    head, rest = fragment[:1], fragment[1:]
    if head in "RUP" and head:
        if not rest:
            raise DrawioLinkError("invalid")
        if head == "R":
            return DrawioLink("payload", rest, stripped, clean_name(title))
        if head == "U":
            return _fetched(_fetchable(unquote(rest)), title)
        return DrawioLink("xml", _page_data(rest), stripped, clean_name(title))
    if head.isalpha():
        raise DrawioLinkError("unsupported", STORAGES.get(head))
    return None


def parse_link(text: str) -> DrawioLink:
    """The form of ``text`` or ``DrawioLinkError``."""

    text = text.strip()
    if len(text.encode()) > MAX_LINK_BYTES:
        raise DrawioLinkError("too_large")
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        raise DrawioLinkError("invalid") from None
    if parts.scheme != "https" or not parts.hostname or parts.username is not None or parts.password is not None:
        raise DrawioLinkError("invalid")
    if port not in (None, HTTPS_PORT):
        raise DrawioLinkError("invalid")
    if parts.hostname.lower() not in DIAGRAM_HOSTS:
        if parts.path.lower().endswith(".drawio") and not parts.fragment:
            return _fetched(text, "")
        raise DrawioLinkError("invalid")
    stripped = urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
    query = parse_qs(parts.query)
    title = next(iter(query.get("title", [])), "")
    found = _from_fragment(parts.fragment, stripped, title)
    if found is not None:
        return found
    legacy = query.get("url")
    if legacy and legacy[0]:
        return _fetched(_fetchable(legacy[0]), title)
    if parts.path.lower().endswith(".drawio"):
        return _fetched(text, title)
    raise DrawioLinkError("invalid")
