"""Identity rules: project ids, identity keys, ``spec_id`` slugs and uuids (docs/graph/data-model.md "Identity").

An identity key decides whether two extracted nodes are the same thing; a ``spec_id`` is the stable
external id given once and never reused for another identity key; uuids are storage keys derived from
the identity key at creation.
"""

from __future__ import annotations

import hashlib
import re
import string
import unicodedata
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Final, Literal, Protocol

from graph.core.errors import ConfigurationError

PROJECT_ID_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
NAMESPACE: Final = uuid.UUID("36012a50-eff4-4a22-8410-fad8c8a2474a")

IdentityKind = Literal["id", "span", "surface"]
SpecIdState = Literal["active", "alias", "retired"]

SLUG_WORDS: Final = 5
SLUG_MAX_CHARS: Final = 80
STOP_WORDS: Final = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "can",
        "for",
        "from",
        "has",
        "have",
        "in",
        "into",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "their",
        "this",
        "to",
        "was",
        "were",
        "will",
        "with",
        "within",
    ]
)

_ID_SEPARATORS = re.compile(r"[\s_.]+")
_WHITESPACE = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^A-Z0-9]+")
_EDGE_PUNCTUATION = string.punctuation + "\u201c\u201d\u2018\u2019\u00ab\u00bb\u2026\u2013\u2014"
_BRACKETS: Final = {"(": ")", "[": "]", "{": "}"}
_CLOSING: Final = {closing: opening for opening, closing in _BRACKETS.items()}


def validate_project_id(project_id: object) -> str:
    """Return ``project_id`` when it matches the partition pattern, else raise ``ConfigurationError``."""

    if not isinstance(project_id, str) or not PROJECT_ID_PATTERN.fullmatch(project_id):
        raise ConfigurationError("invalid_project_id")
    return project_id


def normalise_id(text: str) -> str:
    """Normalise an explicit id: upper case, separators (``_``, space, ``.``) as ``-``; digits unchanged."""

    return _ID_SEPARATORS.sub("-", text.strip()).upper().strip("-")


def explicit_id_pattern(families: Sequence[str]) -> re.Pattern[str] | None:
    """The explicit-id grammar of data-model.md "Identity" for ``families`` (None without families).

    An id is a family exactly as written (upper case), a separator (``-``, ``_``, or a space directly
    before the digits), optional upper-case letter segments each ending in ``-`` or ``_``, then digits
    (``.``, ``-`` or ``_`` may join more digit groups). It stands alone: no word character or ``-``
    right before it, and no word character (or ``-`` and one) right after it. The same grammar as the
    build's id finder.
    """

    return _explicit_id_pattern(tuple(sorted(set(families), key=lambda family: (-len(family), family))))


@lru_cache(maxsize=16)
def _explicit_id_pattern(families: tuple[str, ...]) -> re.Pattern[str] | None:
    if not families:
        return None
    alternatives = "|".join(re.escape(family) for family in families)
    return re.compile(rf"(?<![\w-])(?:{alternatives})(?:[-_](?:[A-Z]{{1,10}}[-_])*| )\d+(?:[-_.]\d+)*(?!\w|-\w)")


def normalise_surface(text: str) -> str:
    """Normalise a name or line: NFKC, case-folded, single spaces, no punctuation at either end.

    A bracket at an end is kept when its partner is inside the text ("erp (external)").
    """

    value = _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", text).casefold()).strip()
    start, end = 0, len(value)
    while start < end and value[start] in _EDGE_PUNCTUATION:
        partner = _BRACKETS.get(value[start])
        if partner is not None and partner in value[start + 1 : end]:
            break
        start += 1
    while end > start and value[end - 1] in _EDGE_PUNCTUATION:
        partner = _CLOSING.get(value[end - 1])
        if partner is not None and partner in value[start : end - 1]:
            break
        end -= 1
    return value[start:end].strip()


def identity_key(scope: str, kind: IdentityKind, value: str) -> str:
    """``<scope>|<kind>|<value>``; ``value`` must already be normalised for its kind."""

    return f"{scope}|{kind}|{value}"


KEY_VALUE_MAX_CHARS: Final = 500
"""Longest identity key value kept verbatim (``bounded_key_value``)."""


def bounded_key_value(value: str, limit: int = KEY_VALUE_MAX_CHARS) -> str:
    """``value`` when it has at most ``limit`` characters, else its first ``limit`` characters, ``#`` and
    the first 12 hex digits of the sha256 of the whole value (so two long values stay distinct)."""

    if len(value) <= limit:
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"{value[:limit]}#{digest}"


def slug(name: str, explicit_id: str | None = None) -> str:
    """The ``spec_id`` SLUG: upper case, non-alphanumerics as ``_``, stop words dropped, first 5 words.

    An explicit id leads the slug as one word. The result has at most 80 characters and is never empty.
    """

    words = [
        word
        for word in _NON_ALNUM.sub(" ", unicodedata.normalize("NFKC", name).upper()).split()
        if word.casefold() not in STOP_WORDS
    ]
    if explicit_id:
        lead = _NON_ALNUM.sub("_", normalise_id(explicit_id)).strip("_")
        words = [lead, *_without_sequence(words, lead.split("_"))]
    text = "_".join(words[:SLUG_WORDS])[:SLUG_MAX_CHARS].strip("_")
    return text or "NODE"


def _without_sequence(words: list[str], sequence: list[str]) -> list[str]:
    """``words`` with every run equal to ``sequence`` removed (the id already leads the slug)."""

    size = len(sequence)
    result: list[str] = []
    index = 0
    while index < len(words):
        if size and words[index : index + size] == sequence:
            index += size
            continue
        result.append(words[index])
        index += 1
    return result


def node_uuid(project_id: str, key: str) -> str:
    """Storage uuid of a node: uuid5 over ``<project_id>|node|<identity key at creation>``."""

    return str(uuid.uuid5(NAMESPACE, f"{project_id}|node|{key}"))


def edge_uuid(project_id: str, edge_key: str) -> str:
    """Storage uuid of a domain edge: uuid5 over ``<project_id>|edge|<edge key at creation>``."""

    return str(uuid.uuid5(NAMESPACE, f"{project_id}|edge|{edge_key}"))


@dataclass(frozen=True, slots=True)
class SpecIdRow:
    """One issued ``spec_id``: the identity key it belongs to, its node uuid and its state."""

    spec_id: str
    identity_key: str
    uuid: str
    state: SpecIdState


def allocate_spec_id(prefix: str, slug_text: str, key: str, taken: Mapping[str, SpecIdRow]) -> str:
    """Return ``<prefix><slug>``, suffixed ``_2``, ``_3``... when taken by another identity key.

    A ``spec_id`` issued for the same identity key that is still ``active`` or ``retired`` is returned
    again; one issued for a different key, or kept as an alias of a merged node, is never reused.
    """

    candidate = f"{prefix}{slug_text}"
    number = 1
    while True:
        row = taken.get(candidate)
        if row is None or (row.identity_key == key and row.state != "alias"):
            return candidate
        number += 1
        candidate = f"{prefix}{slug_text}_{number}"


class SpecIdRegistry(Protocol):
    """Port: every ``spec_id`` issued in a project and its state (docs/graph/api.md "Ports")."""

    async def taken(self, project_id: str) -> dict[str, SpecIdRow]:
        """All issued ``spec_id``s of the project, by ``spec_id``."""
        ...

    async def record(self, project_id: str, build_id: str, rows: Sequence[SpecIdRow]) -> None:
        """Insert or update rows by ``spec_id``; ``build_id`` names the build that changed them."""
        ...
