"""Dropbox links (docs/dropbox/source.md "Links"): what a member may paste, and the link the app builds back.

Accepted (decision DB7): https on ``www.dropbox.com`` or ``dropbox.com``, no user name, password or port in the
address: a shared link ``/scl/fo/...`` (folder), ``/scl/fi/...`` (file), the legacy ``/s/...`` and ``/sh/...``, and the
web path ``/home/<path>`` of the member's own Dropbox (with ``?preview=<file>``: the file ``<path>/<file>``). Any
other host or path, ``/home`` alone (the Dropbox root) and an address that is not a link are ``dropbox_link_invalid``.
A shared link is a bearer secret: it lives only in ``DropboxTarget.shared_link`` for one request or one import run and
is never stored, logged or put in a ``repr`` (decision DB8).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal, override
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit

from dropbox.codes import DropboxCode, DropboxError

WEB_URL: Final = "https://www.dropbox.com"
HOME_PREFIX: Final = "/home"
HOSTS: Final = frozenset({"www.dropbox.com", "dropbox.com"})

_SHARED_PREFIXES: Final = frozenset({"scl", "s", "sh"})  # /scl/fo|fi/..., /s/..., /sh/...
_DROPPED_PARAMS: Final = frozenset({"dl", "st", "raw"})  # the download flag and the share-token of a link, not its key
_TAGS: Final = re.compile(r"[A-Za-z0-9_-]+")
MAX_PATH_CHARS: Final = 1024
PREVIEW_PARAM: Final = "preview"  # /home/<folder>?preview=<file>: the file open in its folder
_DOT_SEGMENTS: Final = frozenset({".", ".."})

TargetKind = Literal["shared", "path"]


@dataclass(frozen=True)
class DropboxTarget:
    """A parsed link: a shared link (``shared_link``, the pasted https address with its ``rlkey``; the folder or file it
    names is read with ``sharing/get_shared_link_metadata``) or a web path of the member's Dropbox (``path``, decoded,
    starting with ``/``; read with ``files/get_metadata``)."""

    kind: TargetKind
    shared_link: str | None = None
    path: str | None = None

    @override
    def __repr__(self) -> str:
        return f"DropboxTarget(kind={self.kind!r})"  # never the shared link or the path


def _invalid() -> DropboxError:
    return DropboxError(DropboxCode.LINK_INVALID)


def _shared(segments: list[str], query: str) -> DropboxTarget:
    """A shared link: ``/scl/fo|fi/<id>/<name or key>``, ``/s/<key>[/<name>]`` or ``/sh/<key>[/<id>]``."""

    match segments:
        case ["scl", "fo" | "fi", key, _, *_] if _TAGS.fullmatch(key):
            pass
        case ["s" | "sh", key, *_] if _TAGS.fullmatch(key):
            pass
        case _:
            raise _invalid()
    pairs = [(key, value) for key, value in parse_qsl(query, keep_blank_values=True) if key not in _DROPPED_PARAMS]
    rest = f"?{urlencode(pairs)}" if pairs else ""
    return DropboxTarget("shared", shared_link=f"{WEB_URL}/{'/'.join(segments)}{rest}")


def _home(raw_path: str, query: str) -> DropboxTarget:
    """A web path of the member's Dropbox (``/home/<path>``), decoded; ``/home`` alone is the Dropbox root. A
    ``?preview=<file>`` (Dropbox's address of a file open in its folder) names the file ``<path>/<file>``. A ``.`` or
    ``..`` segment, or a ``/`` inside the preview name, is refused (no path traversal)."""

    parts = [part for part in unquote(raw_path).split("/")[2:] if part]
    preview = next((value for key, value in parse_qsl(query) if key == PREVIEW_PARAM), None)
    if preview is not None:
        if "/" in preview:
            raise _invalid()
        parts.append(preview)
    path = "/" + "/".join(parts)
    if (
        path == "/"
        or any(part in _DOT_SEGMENTS for part in parts)
        or len(path) > MAX_PATH_CHARS
        or any(not char.isprintable() for char in path)
    ):
        raise _invalid()
    return DropboxTarget("path", path=path)


def parse_link(link: str) -> DropboxTarget:
    """The target of a pasted link (see the module docstring); ``DropboxError(dropbox_link_invalid)`` for anything
    else. Pure: no network, no log. A shared link keeps its query (``rlkey`` is needed to read it) without ``dl``,
    ``st`` and the fragment; a web path is URL-decoded, and its ``preview`` names a file in it."""

    text = link.strip()
    if not text or any(char.isspace() or not char.isprintable() for char in text):
        raise _invalid()
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        raise _invalid() from None
    host = (parts.hostname or "").lower()
    if parts.scheme.lower() != "https" or "@" in parts.netloc or port is not None or host not in HOSTS:
        raise _invalid()
    segments = [segment for segment in parts.path.split("/") if segment]
    if not segments:
        raise _invalid()
    if segments[0] in _SHARED_PREFIXES:
        return _shared(segments, parts.query)
    if segments[0] == HOME_PREFIX.lstrip("/"):
        return _home("/" + "/".join(segments), parts.query)
    raise _invalid()


def canonical_link(path_display: str) -> str:
    """The address stored as ``connections.link`` of a source: ``https://www.dropbox.com/home<path_display>`` of the
    resolved target, URL-encoded (``/`` kept). It opens only for people who can see the item; it is never the shared
    link (decision DB8)."""

    path = path_display if path_display.startswith("/") else f"/{path_display}"
    return f"{WEB_URL}{HOME_PREFIX}{quote(path, safe='/')}"
