"""The ``refers_to`` entries of ``add_finding``, checked by type before any write (tools.md "add_finding").

A reference is stored in the development gap's first event, never fetched or opened, and no error names its value:
a commit is 7 to 64 hex digits (stored lower case), a ``pr`` is a pull request number or an ``http(s)`` URL, a ``file``
is a relative path inside the repository, a ``url`` an ``http(s)`` URL with a host and no user info. Repeats are kept
once; at most 10.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Final
from urllib.parse import urlsplit

from graph.gap import SourceRef

REFS_LIMIT: Final = 10
FILE_LIMIT: Final = 500
URL_LIMIT: Final = 2000
COMMIT_PATTERN: Final = re.compile(r"^[0-9a-fA-F]{7,64}$")
PR_NUMBER_PATTERN: Final = re.compile(r"^[0-9]{1,9}$")
_DRIVE_PREFIX: Final = re.compile(r"^[A-Za-z]:")
_SCHEMES: Final = frozenset({"http", "https"})
REF_TYPES: Final = frozenset({"pr", "commit", "file", "url"})


class InvalidRefError(ValueError):
    """A ``refers_to`` entry that is not valid for its type (the route answers ``invalid_arguments``)."""


def checked_refs(entries: Iterable[tuple[str, str]]) -> tuple[SourceRef, ...]:
    """The distinct, checked references; ``InvalidRefError`` for too many or a bad one."""

    found: dict[tuple[str, str], SourceRef] = {}
    listed = list(entries)
    if len(listed) > REFS_LIMIT:
        raise InvalidRefError("too_many")
    for kind, value in listed:
        stored = reference_value(kind, value)
        found.setdefault((kind, stored), SourceRef.model_validate({"type": kind, "value": stored}))
    return tuple(found.values())


def reference_value(kind: str, value: str) -> str:
    """The stored form of a reference value."""

    if kind not in REF_TYPES:
        raise InvalidRefError("type")
    if kind == "commit":
        if not COMMIT_PATTERN.fullmatch(value):
            raise InvalidRefError("commit")
        return value.lower()
    if kind == "file":
        _check_file(value)
    elif kind == "url" or not PR_NUMBER_PATTERN.fullmatch(value):
        _check_url(value)
    return value


def _check_file(path: str) -> None:
    segments = path.split("/")
    if (
        not path
        or len(path) > FILE_LIMIT
        or not path.isprintable()
        or path.startswith("/")
        or "\\" in path
        or ".." in segments
        or _DRIVE_PREFIX.match(path)
    ):
        raise InvalidRefError("file")


def _check_url(url: str) -> None:
    """An ``http`` or ``https`` URL with a host and without user info, never fetched."""

    if not url or len(url) > URL_LIMIT or not url.isprintable() or any(ch.isspace() for ch in url):
        raise InvalidRefError("url")
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        raise InvalidRefError("url") from None
    if parts.scheme not in _SCHEMES or not host or "@" in parts.netloc:
        raise InvalidRefError("url")
