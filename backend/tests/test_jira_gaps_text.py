"""The words of the Jira gap channel (``api.jira_gaps.text``): the numbered answers of an area sub-task, the hint and
its dedupe by mention, and the reply parser the channels share (``api.gap.replies``)."""

from __future__ import annotations

import pytest

from api.gap.replies import parse_reply
from api.jira_gaps import text


@pytest.mark.parametrize(
    ("comment", "expected"),
    [
        ("1: B", {1: "B"}),
        ("1: B\n2) own words here\n3. A", {1: "B", 2: "own words here", 3: "A"}),
        ("intro\n1: first\nand it goes on\n2: B", {1: "first and it goes on", 2: "B"}),
        ("1: A\n1: B", {1: "A"}),  # a number given twice keeps its first answer
        ("1: A\n1: B\nmore", {1: "A"}),
        ("12:\n", {}),  # a number with no answer
        ("no numbers at all", {}),
        ("  7 :  C  ", {7: "C"}),
    ],
)
def test_numbered_lines_are_answers_by_question_number(comment: str, expected: dict[int, str]) -> None:
    assert text.numbered_answers(comment) == expected


def test_the_hint_mentions_the_person_and_is_found_again_by_that_mention_only() -> None:
    doc = text.hint_doc("acct-1")

    assert text.mentions(doc) == {"acct-1"}
    assert text.is_hint_for(doc, "acct-1") and not text.is_hint_for(doc, "acct-2")
    assert not text.is_hint_for(text.recorded_doc("acct-1", ["recorded A"]), "acct-1")  # a confirmation is no hint


def test_mentions_of_a_malformed_document_are_empty() -> None:
    assert (
        text.mentions(None) == set() and text.mentions({"content": [{"type": "mention", "attrs": {"id": 5}}]}) == set()
    )


def test_long_titles_are_cut_for_the_jira_summary_limit() -> None:
    assert len(text.story_summary("x" * 500)) <= 255 and len(text.area_summary("y" * 500)) <= 255
    assert text.cut("a  b\n c", 100) == "a b c" and text.cut("abcdef", 4) == "abc…"


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("B", (2, None)),
        (" b. ", (2, None)),
        ("E", (None, "E")),  # a letter the gap does not offer is words
        ("We keep  one cart", (None, "We keep one cart")),
        ("", (None, None)),
        ("   ", (None, None)),
        ("x" * 50, (None, "x" * 16)),  # cut at the channel's own limit
    ],
)
def test_a_reply_is_an_option_letter_or_the_persons_own_words(
    reply: str, expected: tuple[int | None, str | None]
) -> None:
    assert parse_reply(reply, {"A": 1, "B": 2}, 16) == expected
    assert text.mentions({"content": 5}) == set()  # content that is not a list is ignored
    assert text.mentions({"content": "x", "type": "doc"}) == set()
    assert text.mentions({"content": [{"type": "mention", "attrs": {"id": "a"}}, 7]}) == {"a"}
