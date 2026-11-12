"""Argument checks of the findings calls and the dedupe tuple; each check raises ``ConfigurationError`` before any
query ([gap.md](gap.md) "Rules"). Values are never fetched or opened, and no message carries the value checked."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable, Sequence
from typing import Final, cast
from urllib.parse import urlsplit

from graph.core.errors import ConfigurationError
from graph.gap.models import FindingRef
from graph.gap.names import (
    ABOUT_LIMIT,
    AUTHOR_LIMIT,
    FILE_LIMIT,
    KINDS,
    NOTE_LIMIT,
    PAGE_LIMITS,
    REF_TYPES,
    REFS_LIMIT,
    STORY_LIMIT,
    TEXT_LIMIT,
    URL_LIMIT,
    RefType,
)

COMMIT_PATTERN: Final = re.compile(r"^[0-9a-fA-F]{7,64}$")
PR_NUMBER_PATTERN: Final = re.compile(r"^[0-9]{1,9}$")
_DRIVE_PREFIX: Final = re.compile(r"^[A-Za-z]:")
_WHITESPACE: Final = re.compile(r"\s+")
_SCHEMES: Final = frozenset({"http", "https"})
ID_LIMIT: Final = 200
PAIR_SIZE: Final = 2


def check_kind(kind: str, *, blocking: bool = False) -> None:
    """A known kind (``invalid_kind``); a blocking finding is never a ``note``."""

    if kind not in KINDS or (blocking and kind == "note"):
        raise ConfigurationError("invalid_kind")


def check_text(text: str) -> None:
    """1-2000 characters with something besides whitespace (``invalid_text``)."""

    if not text.strip() or len(text) > TEXT_LIMIT:
        raise ConfigurationError("invalid_text")


def check_author(value: str) -> None:
    """A user id: 1-200 printable characters (``invalid_author``), as the plan's ``claimed_by``."""

    if not value or len(value) > AUTHOR_LIMIT or not value.isprintable():
        raise ConfigurationError("invalid_author")


def check_story(story_id: str | None, *, blocking: bool = False) -> None:
    """A story id of 1-200 printable characters; a blocking finding needs one (``invalid_story``)."""

    if story_id is None:
        if blocking:
            raise ConfigurationError("invalid_story")
        return
    if not story_id or len(story_id) > STORY_LIMIT or not story_id.isprintable():
        raise ConfigurationError("invalid_story")


def check_note(note: str | None) -> str | None:
    """A note of at most 500 characters; an empty one is none (``invalid_note``)."""

    if note is None:
        return None
    if len(note) > NOTE_LIMIT:
        raise ConfigurationError("invalid_note")
    return note or None


def check_limit(limit: int) -> None:
    """A page size of 1-200 (``invalid_limit``)."""

    low, high = PAGE_LIMITS
    if isinstance(limit, bool) or not low <= limit <= high:
        raise ConfigurationError("invalid_limit")


def about_ids(about: Iterable[str]) -> tuple[str, ...]:
    """The distinct ``about`` ids in the order given: 1-20 of them, each 1-200 printable characters
    (``invalid_about``)."""

    if isinstance(about, str):
        raise ConfigurationError("invalid_about")
    ids = tuple(dict.fromkeys(about))
    if not 1 <= len(ids) <= ABOUT_LIMIT:
        raise ConfigurationError("invalid_about")
    for spec_id in ids:
        if not spec_id or len(spec_id) > ID_LIMIT or not spec_id.isprintable():
            raise ConfigurationError("invalid_about")
    return ids


def spec_id_filter(spec_ids: Iterable[str]) -> tuple[str, ...]:
    """The distinct ids of a ``findings`` filter: at most 200, each 1-200 printable characters (``invalid_about``)."""

    if isinstance(spec_ids, str):
        raise ConfigurationError("invalid_about")
    ids = tuple(dict.fromkeys(spec_ids))
    if len(ids) > PAGE_LIMITS[1] or any(
        not spec_id or len(spec_id) > ID_LIMIT or not spec_id.isprintable() for spec_id in ids
    ):
        raise ConfigurationError("invalid_about")
    return ids


def references(
    refers_to: Iterable[FindingRef | tuple[str, str]],
) -> tuple[FindingRef, ...]:
    """The distinct, checked ``refers_to`` entries: at most 10, each of a known type and a valid value
    (``invalid_ref``)."""

    entries = tuple(refers_to)
    if len(entries) > REFS_LIMIT:
        raise ConfigurationError("invalid_ref")
    found: dict[tuple[str, str], FindingRef] = {}
    for entry in entries:
        kind, value = (entry.type, entry.value) if isinstance(entry, FindingRef) else _pair(entry)
        reference = FindingRef(type=cast(RefType, kind), value=reference_value(kind, value))
        found.setdefault((reference.type, reference.value), reference)
    return tuple(found.values())


def _pair(entry: object) -> tuple[str, str]:
    if not isinstance(entry, Sequence) or isinstance(entry, str) or len(entry) != PAIR_SIZE:
        raise ConfigurationError("invalid_ref")
    kind, value = entry
    if not isinstance(kind, str) or not isinstance(value, str) or kind not in REF_TYPES:
        raise ConfigurationError("invalid_ref")
    return kind, value


def reference_value(kind: str, value: str) -> str:
    """The stored form of a reference value, or ``ConfigurationError("invalid_ref")``."""

    if kind not in REF_TYPES:
        raise ConfigurationError("invalid_ref")
    if kind == "commit":
        if not COMMIT_PATTERN.fullmatch(value):
            raise ConfigurationError("invalid_ref")
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
        raise ConfigurationError("invalid_ref")


def _check_url(url: str) -> None:
    """An ``http`` or ``https`` URL with a host and without user info, never fetched."""

    if not url or len(url) > URL_LIMIT or not url.isprintable() or any(ch.isspace() for ch in url):
        raise ConfigurationError("invalid_ref")
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        raise ConfigurationError("invalid_ref") from None
    if parts.scheme not in _SCHEMES or not host or "@" in parts.netloc:
        raise ConfigurationError("invalid_ref")


def normalised_text(text: str) -> str:
    """NFKC, case-folded, whitespace runs as one space, trimmed."""

    return _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", text).casefold()).strip()


def dedupe_key(created_by: str, kind: str, about: Iterable[str], text: str) -> str:
    """sha256 hex of the dedupe tuple: author, kind, sorted current ``about`` ids, normalised text."""

    payload = json.dumps([created_by, kind, sorted(set(about)), normalised_text(text)], ensure_ascii=True)
    return hashlib.sha256(payload.encode("ascii")).hexdigest()
