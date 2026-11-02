"""``pdf.word_repair``: line-break hyphens Docling turned into ``word- word`` or dropped, and glued words.

The page text is written the way pdfium returns it: a hyphen at a line break is U+FFFE (or ``-`` and a line
break), a plain hyphen inside a line stays ``-``.
"""

from __future__ import annotations

import pytest

from file_parsers.pdf.word_repair import WordRepairs, document_words, line_break_hyphens, repair_words

BREAK = "\ufffe"


def repaired(markdown: str, page_text: str, words: frozenset[str] = frozenset()) -> tuple[str, WordRepairs]:
    lines = markdown.split("\n")
    counts = repair_words(lines, page_text, words or document_words([page_text]))
    return "\n".join(lines), counts


def test_the_pairs_are_the_runs_on_both_sides_of_a_line_break_hyphen():
    text = f"see FR{BREAK}EXT-001 and read-\r\nonly, plus a-b inline and NFR{BREAK}\r\nPERF"
    assert line_break_hyphens(text) == {("fr", "ext"): 1, ("read", "only"): 1, ("nfr", "perf"): 1}


@pytest.mark.parametrize(
    ("markdown", "expected"),
    [
        ("Covered by FR- EXT-001 today.", "Covered by FR-EXT-001 today."),
        ("| FR- TODO- 001 | text |", "| FR-TODO-001 | text |"),  # both breaks, a table row
        ("## The read- only Mode", "## The read-only Mode"),  # a heading
        ("A fine- grained check.", "A fine-grained check."),
    ],
)
def test_hyphen_space_at_a_line_break_becomes_a_hyphen(markdown: str, expected: str):
    page = f"FR{BREAK}EXT-001 FR{BREAK}TODO{BREAK}001 read{BREAK}only fine-\r\ngrained"
    result, counts = repaired(markdown, page)
    assert result == expected
    assert counts.hyphen_spaces >= 1


def test_hyphen_space_that_is_not_a_line_break_is_kept():
    # "AC- plus" and "security- or privacy-" are written that way on the page.
    result, counts = repaired("a label made of AC- plus, security- or privacy-related", "AC- plus security- or")
    assert result == "a label made of AC- plus, security- or privacy-related"
    assert counts == WordRepairs()


def test_a_compound_docling_joined_gets_its_hyphen_back():
    page = f"without third{BREAK}party analytics, the URL{BREAK}encoded id and the X{BREAK}Request-Id header"
    words = document_words([page, "a third party sends an encoded Request to the URL of X"])  # both parts are words
    result, counts = repaired("without thirdparty analytics, the URLencoded id and the XRequest-Id header", page, words)
    assert result == "without third-party analytics, the URL-encoded id and the X-Request-Id header"
    assert counts.hyphens_restored == 3


def test_a_soft_hyphen_word_written_whole_elsewhere_stays_joined():
    page = f"writes to the data{BREAK}base every night"
    words = document_words([page, "the database is small"])  # another page writes "database"
    result, counts = repaired("writes to the database every night", page, words)
    assert result == "writes to the database every night"
    assert (counts.soft_hyphens_kept, counts.hyphens_restored, counts.glue) == (1, 0, 0)


def test_a_syllable_break_stays_joined_when_its_second_part_is_no_word_of_the_document():
    page = f"a behavioral ap{BREAK}proximation of the model; opera{BREAK}tionally simple"
    result, counts = repaired("a behavioral approximation of the model; operationally simple", page)
    assert result == "a behavioral approximation of the model; operationally simple"
    assert (counts.soft_hyphens_kept, counts.hyphens_restored) == (2, 0)


def test_the_two_sides_of_a_break_are_not_words_of_the_document():
    assert document_words([f"third{BREAK}party and data-\r\nbase, a database"]) == {"and", "a", "database"}


def test_glued_words_are_still_repaired_and_a_hyphen_inside_a_line_is_not_a_break():
    result, counts = repaired(
        "the database and the timestamp, judgesmove", "the data-base and the time stamp judges move"
    )
    assert result == "the data base and the time stamp, judges move"
    assert counts.glue == 3


def test_code_comments_and_images_are_untouched_and_tables_only_get_the_hyphen_rule():
    page = f"FR{BREAK}EXT third{BREAK}party judges move"
    markdown = (
        "```\nFR- EXT thirdparty judgesmove\n```\n<!-- FR- EXT -->\n![FR- EXT](images/p1.fig0.png)\n"
        "| FR- EXT | thirdparty judgesmove |"
    )
    result, counts = repaired(markdown, page)
    assert result.split("\n")[:5] == markdown.split("\n")[:5]
    assert result.split("\n")[5] == "| FR-EXT | thirdparty judgesmove |"
    assert counts == WordRepairs(hyphen_spaces=1)


def test_the_counts_add_up():
    total = WordRepairs()
    total.add(WordRepairs(1, 2, 3, 4))
    total.add(WordRepairs(1, 1, 1, 1))
    assert total == WordRepairs(2, 3, 4, 5)


@pytest.mark.parametrize(("word", "split"), [("display", 3), ("perform", 3), ("program", 3), ("context", 3)])
def test_a_syllable_break_stays_joined_when_only_its_second_part_is_a_word(word: str, split: int):
    # Review case "dis-play": "play" is a whole word of the document, "dis" is not, so it is a syllable break.
    page = f"the {word[:split]}{BREAK}{word[split:]} of it"
    words = document_words([page, f"we {word[split:]} it"])  # only the second part is written whole
    result, counts = repaired(f"the {word} of it", page, words)
    assert result == f"the {word} of it"
    assert (counts.soft_hyphens_kept, counts.hyphens_restored) == (1, 0)


def test_a_short_lower_case_part_is_never_restored_even_when_both_parts_are_words():
    page = f"they re{BREAK}enter and go on{BREAK}line"
    words = document_words([page, "re enter on line"])
    result, counts = repaired("they reenter and go online", page, words)
    assert result == "they reenter and go online"
    assert counts.hyphens_restored == 0


def test_identifier_parts_may_be_short():
    page = f"the FR{BREAK}GRA rules and the X{BREAK}Request-Id header"
    words = document_words([page, "FR GRA X Request"])
    result, counts = repaired("the FRGRA rules and the XRequest-Id header", page, words)
    assert result == "the FR-GRA rules and the X-Request-Id header"
    assert counts.hyphens_restored == 2


def test_one_break_joins_one_hyphen_space_only():
    page = "a pre-\r\nand post-processing step, and a pre- and b"
    result, counts = repaired("a pre- and post-processing step, and a pre- and b", page)
    assert result == "a pre-and post-processing step, and a pre- and b"
    assert counts.hyphen_spaces == 1


def test_one_break_restores_or_keeps_one_join_only_and_is_counted_once():
    page = f"writes to the data{BREAK}base"
    words = document_words([page, "the database"])
    result, counts = repaired("the database\nthe database again\ndatabase", page, words)
    assert result == "the database\nthe database again\ndatabase"
    assert counts == WordRepairs(soft_hyphens_kept=1)


def test_hyphen_space_inside_backticks_or_an_indented_code_line_is_kept():
    page = f"FR{BREAK}EXT FR{BREAK}EXT FR{BREAK}EXT"
    markdown = "see `FR- EXT` and FR- EXT\n\n    FR- EXT in code\n    - FR- EXT in a nested list"
    result, counts = repaired(markdown, page)
    assert result == "see `FR- EXT` and FR-EXT\n\n    FR- EXT in code\n    - FR-EXT in a nested list"
    assert counts.hyphen_spaces == 2
