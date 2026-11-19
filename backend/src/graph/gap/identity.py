"""Identity of gaps and clarifications: signatures and ids (docs/graph/gap/data-model.md "Identity").

A signature is the SHA-256 of canonical JSON (sorted keys, no spaces, UTF-8). Quotes and questions are normalised
before they are hashed: quote marks are made plain, runs of whitespace become one space and the ends are trimmed;
a development gap's question is also case-folded. ``gap_id`` is ``GAP::`` and the first 12 hex digits of the
signature, extended by 4 hex digits at a time when that id is taken by a gap with another signature.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any, Final

from graph.gap.names import CLR_PREFIX, GAP_PREFIX

DEVELOPMENT_DETECTOR: Final = "development"
PERSON_DETECTOR: Final = "person"
ID_HEX: Final = 12
ID_EXTENSION_HEX: Final = 4
_SINGLE_QUOTES: Final = (0x2018, 0x2019, 0x201A, 0x201B, 0x2032)
_DOUBLE_QUOTES: Final = (0x201C, 0x201D, 0x201E, 0x201F, 0x2033, 0x00AB, 0x00BB)
_QUOTE_MARKS: Final = {**dict.fromkeys(_SINGLE_QUOTES, "'"), **dict.fromkeys(_DOUBLE_QUOTES, '"')}
_WHITESPACE: Final = re.compile(r"\s+")


def normalise_quote(text: str) -> str:
    """``text`` with plain quote marks, single spaces and no surrounding whitespace."""

    return _WHITESPACE.sub(" ", text.translate(_QUOTE_MARKS)).strip()


def normalise_question(text: str) -> str:
    """A question normalised like a quote and case-folded."""

    return normalise_quote(text).casefold()


def canonical_json(value: Any) -> str:
    """The canonical JSON of ``value``: sorted keys, no spaces, non-ASCII kept."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(value: Any) -> str:
    """SHA-256 (hex) of the canonical JSON of ``value``."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def witness_entry(
    *, fragment_id: str | None = None, quote: str | None = None, fact: str | None = None
) -> dict[str, str]:
    """One witness entry of a signature: a fragment id with its normalised quote, or a graph fact as text."""

    if fact is not None and fragment_id is None and quote is None:
        return {"fact": normalise_quote(fact)}
    if fragment_id is not None and quote is not None and fact is None:
        return {"fragment_id": fragment_id, "quote": normalise_quote(quote)}
    raise ValueError("a witness entry is a fragment id with a quote, or a fact")


def gap_signature(detector: str, about: Iterable[str], witness: Iterable[Mapping[str, str]]) -> str:
    """The signature of an analysis gap: detector code, sorted ``ABOUT`` spec ids and sorted witness entries
    (``witness_entry``)."""

    entries = sorted((dict(entry) for entry in witness), key=canonical_json)
    return sha256_hex({"detector": detector, "about": sorted(set(about)), "witness": entries})


def development_signature(task_id: str, spec_ids: Iterable[str], question: str) -> str:
    """The signature of a development gap: ``development``, the reporting task id, sorted spec ids and the
    normalised question."""

    return sha256_hex(
        {
            "detector": DEVELOPMENT_DETECTOR,
            "task_id": task_id,
            "about": sorted(set(spec_ids)),
            "question": normalise_question(question),
        }
    )


def person_signature(spec_ids: Iterable[str], question: str) -> str:
    """The signature of a gap a person raised: ``person``, sorted spec ids and the normalised question (the
    development signature without a task)."""

    return sha256_hex(
        {"detector": PERSON_DETECTOR, "about": sorted(set(spec_ids)), "question": normalise_question(question)}
    )


def gap_id(signature: str, existing: Mapping[str, str] | None = None) -> str:
    """``GAP::`` and the first 12 hex digits of ``signature`` (upper case). ``existing`` maps the gap ids already in
    the project to their signatures; an id taken by another signature is extended by the next 4 hex digits."""

    taken = existing or {}
    digest = signature.upper()
    width = ID_HEX
    while True:
        candidate = f"{GAP_PREFIX}{digest[:width]}"
        holder = taken.get(candidate)
        if holder is None or holder.upper() == digest or width >= len(digest):
            return candidate
        width += ID_EXTENSION_HEX


def clr_id(gap: str, text: str, answered_at: datetime) -> str:
    """``CLR::`` and the first 12 hex digits of SHA-256 of the gap id, the answer text and the answered-at instant
    (UTC, ISO 8601)."""

    if answered_at.tzinfo is None:
        raise ValueError("answered_at must be timezone-aware")
    instant = answered_at.astimezone(UTC).isoformat()
    return f"{CLR_PREFIX}{sha256_hex([gap, text, instant])[:ID_HEX].upper()}"
