"""Box links (docs/box/source.md "Links"): what a member may paste, and the links the app builds back.

Accepted: https on ``app.box.com`` or ``<name>.app.box.com`` (no user name or password in the address): a folder
``/folder/<id>``, a file ``/file/<id>`` and a shared link ``/s/<token>`` or ``/v/<vanity>``. Folder ``0`` (All Files),
a web link, any other host or path, and an address that is not a link are ``box_link_invalid``. A shared link is a
bearer secret: it lives only in ``BoxTarget.shared_link`` for one request or one import run and is never stored,
logged or put in a ``repr`` (decision B8).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal, override
from urllib.parse import urlsplit

from box.codes import BoxCode, BoxError

WEB_URL: Final = "https://app.box.com"
_HOST: Final = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)?app\.box\.com")
_ID: Final = re.compile(r"[0-9]{1,30}")
_TOKEN: Final = re.compile(r"[A-Za-z0-9]{1,128}")  # the part of a ``/s/`` link
_VANITY: Final = re.compile(r"[A-Za-z0-9_-]{1,128}")  # the part of a ``/v/`` link

TargetKind = Literal["folder", "file", "shared"]


@dataclass(frozen=True)
class BoxTarget:
    """A parsed link: a folder or file by ``id``, or a shared item by ``shared_link`` (the pasted address, https)."""

    kind: TargetKind
    id: str | None = None
    shared_link: str | None = None

    @override
    def __repr__(self) -> str:
        return f"BoxTarget(kind={self.kind!r}, id={self.id!r})"  # never the shared link


def _invalid() -> BoxError:
    return BoxError(BoxCode.LINK_INVALID)


def parse_link(link: str) -> BoxTarget:
    """The target of a pasted link (see the module docstring); ``BoxError(box_link_invalid)`` for anything else.

    Pure: no network, no log. The query and the fragment are dropped (a shared link keeps its scheme, host and path)."""

    text = link.strip()
    if not text or any(char.isspace() or not char.isprintable() for char in text):
        raise _invalid()
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        raise _invalid() from None
    host = (parts.hostname or "").lower()
    if parts.scheme.lower() != "https" or "@" in parts.netloc or port is not None or not _HOST.fullmatch(host):
        raise _invalid()
    segments = parts.path.strip("/").split("/")
    match segments:
        case [kind, value]:
            pass
        case _:
            raise _invalid()
    if kind in ("folder", "file") and _ID.fullmatch(value):
        if int(value) == 0:  # All Files
            raise _invalid()
        return BoxTarget("folder", id=value) if kind == "folder" else BoxTarget("file", id=value)
    if (kind == "s" and _TOKEN.fullmatch(value)) or (kind == "v" and _VANITY.fullmatch(value)):
        return BoxTarget("shared", shared_link=f"https://{host}/{kind}/{value}")
    raise _invalid()


def canonical_link(kind: Literal["folder", "file"], item_id: str) -> str:
    """The address stored as ``connections.link`` of a source: ``https://app.box.com/folder/<id>`` or ``/file/<id>``
    of the resolved item (never the shared link; decision B8)."""

    return f"{WEB_URL}/{kind}/{item_id}"


def file_link(file_id: str) -> str:
    """The link back to one imported file (decision B15): Box checks the viewer's access when it opens."""

    return canonical_link("file", file_id)
