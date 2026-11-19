"""Signatures and ids of gaps and clarifications (docs/graph/gap/data-model.md "Identity")."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta, timezone

import pytest

from graph.gap.identity import (
    canonical_json,
    clr_id,
    development_signature,
    gap_id,
    gap_signature,
    normalise_question,
    normalise_quote,
    witness_entry,
)

FRAGMENT = "FRG::orders::place-order"
ANSWERED_AT = datetime(2026, 11, 16, 9, 30, tzinfo=UTC)


def test_quotes_are_normalised_for_quote_marks_and_whitespace() -> None:
    left, right, open_single, close_single = chr(0x201C), chr(0x201D), chr(0x2018), chr(0x2019)
    text = f"  {left}Can a customer\n\tcancel a {open_single}paid{close_single} order?{right} "
    assert normalise_quote(text) == ("\"Can a customer cancel a 'paid' order?\"")
    assert normalise_question(" Can a Customer  CANCEL? ") == "can a customer cancel?"


def test_canonical_json_sorts_keys_and_keeps_text() -> None:
    assert canonical_json({"b": 1, "a": "Zürich"}) == '{"a":"Zürich","b":1}'


def test_the_signature_is_sha256_of_detector_sorted_about_and_sorted_witness() -> None:
    witness = [
        witness_entry(fragment_id=FRAGMENT, quote="Can a customer cancel a paid order?"),
        witness_entry(fact="b"),
    ]
    signature = gap_signature("OQ-STATED", ["REQ::FR::PLACE_ORDER", "QST::CANCEL_PAID_ORDER"], witness)
    expected = hashlib.sha256(
        json.dumps(
            {
                "about": ["QST::CANCEL_PAID_ORDER", "REQ::FR::PLACE_ORDER"],
                "detector": "OQ-STATED",
                "witness": sorted(witness, key=canonical_json),
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()

    assert signature == expected
    # Order of ids and witness entries, duplicate ids and quote formatting do not change the signature.
    assert signature == gap_signature(
        "OQ-STATED",
        ["QST::CANCEL_PAID_ORDER", "REQ::FR::PLACE_ORDER", "QST::CANCEL_PAID_ORDER"],
        [
            witness_entry(fact="b"),
            witness_entry(fragment_id=FRAGMENT, quote=" Can a customer  cancel a paid order? "),
        ],
    )
    assert signature != gap_signature("OQ-TBD", ["QST::CANCEL_PAID_ORDER", "REQ::FR::PLACE_ORDER"], witness)


def test_a_witness_entry_is_a_quote_or_a_fact() -> None:
    assert witness_entry(fact=" no  provider ") == {"fact": "no provider"}
    for bad in (
        {},
        {"fragment_id": FRAGMENT},
        {"fragment_id": FRAGMENT, "quote": "a", "fact": "b"},
    ):
        with pytest.raises(ValueError, match="witness entry"):
            witness_entry(**bad)


def test_a_development_signature_uses_the_task_ids_and_normalised_question() -> None:
    first = development_signature("TASK::A", ["REQ::B", "REQ::A"], "Is  the refund automatic?")

    assert first == development_signature("TASK::A", ["REQ::A", "REQ::B"], "is the refund automatic?")
    assert first != development_signature("TASK::B", ["REQ::A", "REQ::B"], "is the refund automatic?")
    assert first != gap_signature("development", ["REQ::A", "REQ::B"], [])


def test_the_gap_id_is_the_first_12_hex_digits_extended_on_a_collision() -> None:
    signature = "7c2a91e04b3d" + "a" * 52
    other = "7c2a91e04b3d" + "b" * 52

    assert gap_id(signature) == "GAP::7C2A91E04B3D"
    assert re.fullmatch(r"GAP::[0-9A-F]{12}", gap_id(gap_signature("MS-REF", [], [])))
    assert gap_id(signature, {"GAP::7C2A91E04B3D": signature}) == "GAP::7C2A91E04B3D"
    assert gap_id(signature, {"GAP::7C2A91E04B3D": other}) == "GAP::7C2A91E04B3DAAAA"
    assert gap_id(signature, {"GAP::7C2A91E04B3D": other, "GAP::7C2A91E04B3DAAAA": other}) == (
        "GAP::7C2A91E04B3DAAAAAAAA"
    )


def test_the_clarification_id_hashes_the_gap_text_and_instant() -> None:
    first = clr_id("GAP::7C2A91E04B3D", "Within 30 minutes.", ANSWERED_AT)

    assert re.fullmatch(r"CLR::[0-9A-F]{12}", first)
    # The same instant in another zone is the same id; another instant or text is another id.
    assert first == clr_id(
        "GAP::7C2A91E04B3D",
        "Within 30 minutes.",
        ANSWERED_AT.astimezone(timezone(timedelta(hours=2))),
    )
    assert first != clr_id("GAP::7C2A91E04B3D", "Within 30 minutes.", ANSWERED_AT + timedelta(seconds=1))
    assert first != clr_id("GAP::7C2A91E04B3D", "Never.", ANSWERED_AT)
    with pytest.raises(ValueError, match="timezone-aware"):
        clr_id("GAP::7C2A91E04B3D", "x", datetime.fromisoformat("2026-01-01T00:00:00"))
