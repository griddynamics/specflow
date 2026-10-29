"""Plan step text rules: tokens, masking, segmentation, units, windows, change set (plan 2.T2; FR-BLD-04).

Pure functions over markdown and hand-made records; no I/O except reading the shop fixture.
"""

from __future__ import annotations

import hashlib
import itertools
import re
from collections.abc import Sequence
from pathlib import Path

import pytest

from graph.build.changes import change_set, dirty_windows, plan_units, window_tokens
from graph.build.corpus import estimate_tokens, first_heading, masked_lines, segment, segment_revision
from graph.build.models import ChangeSet, DocumentRec, FragmentRec, Unit
from graph.build.options import BuildOptions, FragmentTokens
from graph.build.ports import CorpusRevision
from runtime_defaults_support import RUNTIME_DEFAULTS

SHOP_1 = Path(__file__).parent / "fixtures" / "graph" / "shop" / "1"
ID_PATTERN = re.compile(r"^(?P<doc>[^#]+)#(?P<slug>[^#]+)#(?P<hex>[0-9a-f]{8})(?:~(?P<n>\d+))?$")


def revision(markdown: str, lineage_id: str = "doc.md") -> CorpusRevision:
    return CorpusRevision(
        lineage_id=lineage_id,
        revision_id=hashlib.sha256(markdown.encode()).hexdigest(),
        version=1,
        path=lineage_id,
        title=lineage_id,
        markdown=markdown,
    )


def options(
    *,
    low: int | None = None,
    target: int | None = None,
    high: int = 3000,
    unit: int | None = None,
    windows: int | None = None,
) -> BuildOptions:
    return BuildOptions(
        fragment_tokens=FragmentTokens(
            min=min(300, high) if low is None else low, target=min(2000, high) if target is None else target, max=high
        ),
        unit_tokens=max(high, unit or RUNTIME_DEFAULTS["UNIT_TOKENS"]),
        max_node_windows=RUNTIME_DEFAULTS["MAX_NODE_WINDOWS"] if windows is None else windows,
    )


SMALL = options(low=10, target=60, high=120, unit=400)


def shop(name: str) -> CorpusRevision:
    return revision((SHOP_1 / name).read_text(encoding="utf-8"), name)


def shop_names() -> list[str]:
    return sorted(path.name for path in SHOP_1.glob("*.md"))


def paragraphs(count: int, *, prefix: str = "P", sentences: int = 4) -> list[str]:
    """Distinct paragraphs of about ``sentences * 14`` tokens each."""

    return [
        " ".join(
            f"{prefix}{index} sentence {number} says that the system keeps things in order."
            for number in range(sentences)
        )
        for index in range(count)
    ]


def long_section(count: int, *, prefix: str = "P") -> str:
    return "# Doc\n\n## Long\n\n" + "\n\n".join(paragraphs(count, prefix=prefix)) + "\n"


def frag(document: str, index: int, tokens: int, path: tuple[str, ...] = ("H",)) -> FragmentRec:
    return FragmentRec(
        fragment_id=f"{document}#{index:03d}",
        document_id=document,
        heading_path=path,
        ordinal=index,
        line_start=index * 10 + 1,
        line_end=index * 10 + 9,
        text="x",
        content_hash=f"hash-{document}-{index}",
        tokens=tokens,
    )


# estimate_tokens


@pytest.mark.parametrize(
    ("length", "expected"),
    [(0, 0), (1, 1), (3, 1), (4, 2), (36, 10), (37, 11), (360, 100), (361, 101)],
)
def test_estimate_tokens_is_ceil_of_chars_over_3_6(length: int, expected: int) -> None:
    assert estimate_tokens("a" * length) == expected


def test_estimate_tokens_counts_characters_not_bytes() -> None:
    assert estimate_tokens("é" * 36) == estimate_tokens("e" * 36) == 10


# Masked lines


def test_comment_line_is_masked_with_its_line_number() -> None:
    assert masked_lines("a\n<!-- source-page: 2 -->\nb") == {2}


def test_indented_comment_line_is_masked() -> None:
    assert masked_lines("a\n   <!-- note -->\nb") == {2}


def test_multi_line_comment_masks_every_line_up_to_the_close() -> None:
    assert masked_lines("a\n<!-- one\ntwo\nthree -->\nb") == {2, 3, 4}


def test_several_comments_are_masked_independently() -> None:
    assert masked_lines("<!-- a -->\nx\n<!-- b\nc -->\ny") == {1, 3, 4}


def test_closing_line_with_text_after_the_close_is_kept() -> None:
    assert masked_lines("<!-- one\ntwo --> tail text\nb") == {1}


def test_one_line_comment_followed_by_text_is_not_masked() -> None:
    assert masked_lines("<!-- a --> kept text\nb") == frozenset()


def test_inline_comment_marker_is_not_a_comment() -> None:
    markdown = "text <!-- inline --> more\nuse `<!--` to open one\nb"
    assert masked_lines(markdown) == frozenset()


def test_unclosed_comment_is_not_a_comment() -> None:
    assert masked_lines("a\n<!-- never closed\nb\nc") == frozenset()


def test_unclosed_comment_also_leaves_later_openings_unmasked() -> None:
    assert masked_lines("<!-- open\nb\n<!-- another\nc") == frozenset()


def test_comment_inside_a_fenced_block_is_code_not_a_comment() -> None:
    markdown = "```html\n<!-- inside the fence -->\n```\n<!-- outside -->"
    assert masked_lines(markdown) == {4}


def test_tilde_fenced_block_also_protects_comments() -> None:
    assert masked_lines("~~~\n<!-- code -->\n~~~") == frozenset()


def test_carriage_returns_do_not_change_line_numbers_or_masking() -> None:
    assert masked_lines("a\r\n<!-- c -->\r\nb\r\n") == {2}


def test_first_heading_skips_comments_and_code() -> None:
    markdown = "<!-- # not this -->\n```\n# nor this\n```\n## Real title ##\n# Later"
    assert first_heading(markdown) == "Real title"
    assert first_heading("no heading here") is None


# segment on the shop corpus


@pytest.mark.parametrize("name", shop_names())
def test_segment_is_deterministic(name: str) -> None:
    assert segment(shop(name), BuildOptions()) == segment(shop(name), BuildOptions())
    assert segment(shop(name), SMALL) == segment(shop(name), SMALL)


@pytest.mark.parametrize("name", shop_names())
@pytest.mark.parametrize("opts", [BuildOptions(), SMALL], ids=["default", "small"])
def test_fragments_follow_the_document_in_order_without_overlap(name: str, opts: BuildOptions) -> None:
    fragments = segment(shop(name), opts)
    lines = (SHOP_1 / name).read_text(encoding="utf-8").split("\n")

    assert fragments
    assert [fragment.ordinal for fragment in fragments] == list(range(len(fragments)))
    assert len({fragment.fragment_id for fragment in fragments}) == len(fragments)
    for previous, current in itertools.pairwise(fragments):
        assert previous.line_end < current.line_start
    for fragment in fragments:
        assert fragment.document_id == name
        assert 1 <= fragment.line_start <= fragment.line_end <= len(lines)
        assert fragment.text.split("\n") == [
            "" if "<!--" in line else line for line in lines[fragment.line_start - 1 : fragment.line_end]
        ]
        assert fragment.tokens == estimate_tokens(fragment.text)


def test_page_markers_are_masked_in_text_but_keep_their_line_numbers() -> None:
    result = segment_revision(shop("checkout.md"), BuildOptions())
    lines = (SHOP_1 / "checkout.md").read_text(encoding="utf-8").split("\n")
    marker_lines = [number for number, line in enumerate(lines, start=1) if line.startswith("<!-- source-page")]
    (fragment,) = result.fragments

    assert len(marker_lines) == 2
    assert result.masked_lines == 2
    assert "source-page" not in fragment.text
    assert (fragment.line_start, fragment.line_end) == (1, len(lines) - 1 if lines[-1] == "" else len(lines))
    for number in marker_lines:
        assert fragment.text.split("\n")[number - fragment.line_start] == ""


def test_masked_comments_never_reach_fragment_text_of_any_shop_document() -> None:
    for name in shop_names():
        for fragment in segment(shop(name), SMALL):
            assert "<!--" not in fragment.text


def test_a_long_section_is_split_inside_the_section() -> None:
    opts = BuildOptions()
    fragments = segment(shop("orders.md"), opts)
    export = [fragment for fragment in fragments if fragment.heading_path[-1:] == ("Export details",)]

    assert len(export) >= 2
    assert all(fragment.tokens <= opts.fragment_tokens.max for fragment in fragments)
    assert all(fragment.tokens >= opts.fragment_tokens.min for fragment in export)
    assert sum(fragment.tokens for fragment in export) > opts.fragment_tokens.max
    # No line of the section is lost between its pieces apart from blank separator lines.
    assert export[0].line_end + 2 >= export[1].line_start


def test_small_documents_are_one_fragment_with_the_title_path() -> None:
    (fragment,) = segment(shop("admin.md"), BuildOptions())
    assert fragment.heading_path == ("Admin console Requirements",)
    assert fragment.line_start == 1


def test_fragment_ids_have_the_documented_format() -> None:
    for name in shop_names():
        for fragment in segment(shop(name), SMALL):
            match = ID_PATTERN.match(fragment.fragment_id)
            assert match, fragment.fragment_id
            assert match["doc"] == fragment.document_id


def test_fragment_id_hex_is_taken_from_the_first_body_line() -> None:
    markdown = "# Title\n\n## Section\n\n  The first body line.  \n\nSecond.\n"
    (fragment,) = segment(revision(markdown, "a/b.md"), BuildOptions())
    digest = hashlib.sha256(b"The first body line.").hexdigest()[:8]
    # The heading path of a fragment is the common path of its lines.
    assert fragment.fragment_id == f"a/b.md#title#{digest}"


def test_heading_path_slug_is_lowercase_and_dash_separated() -> None:
    markdown = "# Orders & Payments (v2)\n\n## ERP delivery\n\nBody text.\n"
    fragments = segment(revision(markdown), options(low=1, target=5, high=20, unit=100))
    assert any("/erp-delivery#" in fragment.fragment_id for fragment in fragments)
    assert any(fragment.fragment_id.startswith("doc.md#orders-payments-v2") for fragment in fragments)


def test_repeated_heading_path_and_first_line_get_a_tilde_suffix() -> None:
    fragments = segment(shop("checkout.md"), SMALL)
    notes = [fragment for fragment in fragments if fragment.heading_path[-1:] == ("Notes",)]

    assert len(notes) == 2
    assert notes[0].fragment_id.count("~") == 0
    assert notes[1].fragment_id == f"{notes[0].fragment_id}~2"


def test_three_identical_sections_get_tilde_2_and_3() -> None:
    section = "## Notes\n\nSame first line.\n\nDifferent tail {n}.\n\n"
    markdown = "# Doc\n\n" + "".join(section.replace("{n}", str(number)) for number in range(3))
    fragments = segment(revision(markdown), options(low=1, target=5, high=30, unit=100))
    notes = [fragment.fragment_id for fragment in fragments if "/notes#" in fragment.fragment_id]

    assert len(notes) == 3
    assert "~" not in notes[0]
    assert notes[1:] == [f"{notes[0]}~2", f"{notes[0]}~3"]
    assert len({fragment.fragment_id for fragment in fragments}) == len(fragments)


def test_unchanged_sections_keep_their_ids_when_another_section_is_edited() -> None:
    before_text = (SHOP_1 / "checkout.md").read_text(encoding="utf-8")
    edited = before_text.replace("show every basket line", "list every basket line")
    assert edited != before_text
    before = segment(revision(before_text), SMALL)
    after = segment(revision(edited), SMALL)

    assert len(before) == len(after)
    changed = [pair for pair in zip(before, after, strict=True) if pair[0].content_hash != pair[1].content_hash]
    assert len(changed) == 1
    for old, new in zip(before, after, strict=True):
        if old.content_hash == new.content_hash:
            assert old.fragment_id == new.fragment_id
    # The edited line is in the section's body, not its first line: even its own id is stable.
    assert changed[0][0].fragment_id == changed[0][1].fragment_id


def test_unchanged_sections_keep_their_ids_when_a_paragraph_is_inserted() -> None:
    before_text = (SHOP_1 / "checkout.md").read_text(encoding="utf-8")
    inserted = before_text.replace(
        "## Integrations\n", "## Integrations\n\nA new paragraph about integrations that was not there.\n", 1
    )
    before = {fragment.content_hash: fragment for fragment in segment(revision(before_text), SMALL)}
    after = segment(revision(inserted), SMALL)

    unchanged = [fragment for fragment in after if fragment.content_hash in before]
    # The inserted paragraph changes the section it lands in and at most one neighbour.
    assert len(unchanged) >= len(before) - 2
    for fragment in unchanged:
        assert fragment.fragment_id == before[fragment.content_hash].fragment_id
        assert fragment.line_start >= before[fragment.content_hash].line_start  # shifted down, same id


def test_an_edit_inside_a_long_section_changes_at_most_one_fragment_and_its_neighbour() -> None:
    base = long_section(24)
    edited = base.replace("P12 sentence 1 says", "P12 sentence 1 now says")
    opts = options(low=60, target=200, high=400, unit=2000)
    before = segment(revision(base), opts)
    after = segment(revision(edited), opts)

    assert len(before) >= 4
    old_ids = {fragment.fragment_id for fragment in before}
    new_ids = {fragment.fragment_id for fragment in after}
    assert len(old_ids - new_ids) <= 2
    assert len(new_ids - old_ids) <= 2
    assert len(old_ids & new_ids) >= len(old_ids) - 2


def test_a_paragraph_inserted_into_a_long_section_leaves_distant_fragments_untouched() -> None:
    base_paragraphs = paragraphs(24)
    head = "# Doc\n\n## Long\n\n"
    base = head + "\n\n".join(base_paragraphs) + "\n"
    extra = "An inserted paragraph that adds a few more words to the section."
    changed = head + "\n\n".join([*base_paragraphs[:12], extra, *base_paragraphs[12:]]) + "\n"
    opts = options(low=60, target=200, high=400, unit=2000)
    before = segment(revision(base), opts)
    after = segment(revision(changed), opts)
    kept = {fragment.fragment_id for fragment in before} & {fragment.fragment_id for fragment in after}

    assert len(before) >= 4
    assert len(kept) >= len(before) - 3


def test_content_hash_ignores_masked_and_blank_lines() -> None:
    plain = "# T\n\n## S\n\nLine one.\nLine two.\n"
    noisy = "# T\n\n\n<!-- source-page: 9 -->\n## S\n\nLine one.   \n\n<!-- more\nmasked -->\nLine two.\n\n\n"
    plain_fragment = segment(revision(plain), BuildOptions())[0]
    noisy_fragment = segment(revision(noisy), BuildOptions())[0]

    assert plain_fragment.content_hash == noisy_fragment.content_hash
    assert plain_fragment.fragment_id == noisy_fragment.fragment_id


def test_content_hash_changes_with_the_text() -> None:
    first = segment(revision("# T\n\nLine one.\n"), BuildOptions())[0]
    second = segment(revision("# T\n\nLine one!\n"), BuildOptions())[0]
    assert first.content_hash != second.content_hash


def test_masked_line_count_is_reported_per_revision() -> None:
    result = segment_revision(shop("notes.md"), BuildOptions())
    assert result.masked_lines == 4
    assert result.fragments


@pytest.mark.parametrize(
    "markdown",
    ["", "\n\n", "   \n\t\n", "<!-- source-page: 1 -->", "<!-- a -->\n\n<!-- b\nc -->\n"],
    ids=["empty", "blank", "whitespace", "one-comment", "only-comments"],
)
def test_a_document_without_unmasked_content_has_no_fragments(markdown: str) -> None:
    assert segment(revision(markdown), BuildOptions()) == []


def test_a_document_of_only_headings_is_still_content() -> None:
    assert len(segment(revision("# Title\n"), BuildOptions())) == 1


def test_a_masked_block_between_sections_does_not_become_a_fragment() -> None:
    markdown = "# A\n\nText a.\n\n<!-- page -->\n\n## B\n\nText b.\n"
    fragments = segment(revision(markdown), options(low=1, target=5, high=40, unit=100))
    assert all("page" not in fragment.text for fragment in fragments)
    assert all(fragment.text.strip() for fragment in fragments)


def test_fenced_code_block_is_never_split_even_with_blank_lines_inside() -> None:
    code = ["```text", *(f"line {n}" if n % 3 else "" for n in range(40)), "```"]
    markdown = (
        "# Doc\n\n## Code\n\n"
        + "\n".join(paragraphs(1))
        + "\n\n"
        + "\n".join(code)
        + "\n\n"
        + "\n".join(paragraphs(1, prefix="Q"))
        + "\n"
    )
    lines = markdown.split("\n")
    fence = [number for number, line in enumerate(lines, start=1) if line.startswith("```")]
    fragments = segment(revision(markdown), options(low=10, target=40, high=100, unit=1000))

    assert len(fragments) >= 2
    holder = [fragment for fragment in fragments if fragment.line_start <= fence[0] <= fragment.line_end]
    assert len(holder) == 1
    assert holder[0].line_start <= fence[0] and holder[0].line_end >= fence[1]


def test_a_code_block_longer_than_the_maximum_stays_whole() -> None:
    code = "```\n" + "\n".join(f"x = {number}  # padding for the block" for number in range(60)) + "\n```"
    markdown = "# Doc\n\n" + paragraphs(1)[0] + "\n\n" + code + "\n\n" + paragraphs(1, prefix="Q")[0] + "\n"
    opts = options(low=10, target=40, high=100, unit=1000)
    lines = markdown.split("\n")
    fence = [number for number, line in enumerate(lines, start=1) if line == "```"]
    fragments = segment(revision(markdown), opts)
    holder = [fragment for fragment in fragments if fragment.line_start <= fence[0] <= fragment.line_end]

    assert len(holder) == 1
    assert holder[0].line_end >= fence[1]
    assert max(fragment.tokens for fragment in fragments) > opts.fragment_tokens.max


def test_headings_inside_a_fence_do_not_start_sections() -> None:
    markdown = "# Doc\n\n```\n# not a heading\n## nor this\n```\n\nBody.\n"
    fragments = segment(revision(markdown), options(low=1, target=3, high=50, unit=100))
    assert all(fragment.heading_path == ("Doc",) for fragment in fragments)


def test_crlf_line_endings_give_the_same_fragments_as_lf() -> None:
    lf = (SHOP_1 / "checkout.md").read_text(encoding="utf-8")
    crlf = lf.replace("\n", "\r\n")
    lf_fragments = segment(revision(lf), SMALL)
    crlf_fragments = segment(revision(crlf), SMALL)

    assert [(f.fragment_id, f.line_start, f.line_end, f.content_hash) for f in lf_fragments] == [
        (f.fragment_id, f.line_start, f.line_end, f.content_hash) for f in crlf_fragments
    ]


# plan_units


def test_a_document_within_unit_tokens_is_one_unit() -> None:
    fragments = [frag("a.md", index, 100) for index in range(5)]
    (unit,) = plan_units(fragments, options(high=300, unit=1000))

    assert unit.fragment_ids == tuple(fragment.fragment_id for fragment in fragments)
    assert unit.tokens == 500
    assert unit.document_id == "a.md"


def test_a_unit_never_spans_documents() -> None:
    fragments = [
        frag("a.md", 0, 100),
        frag("a.md", 1, 100),
        frag("b.md", 0, 100),
        frag("c.md", 0, 100),
        frag("c.md", 1, 100),
    ]
    units = plan_units(fragments, options(high=300, unit=100000))

    assert [unit.document_id for unit in units] == ["a.md", "b.md", "c.md"]
    assert [len(unit.fragment_ids) for unit in units] == [2, 1, 2]


def test_no_fragments_give_no_units() -> None:
    assert plan_units([], BuildOptions()) == []


def test_units_are_deterministic_and_have_stable_ids() -> None:
    fragments = [frag("a.md", index, 150) for index in range(8)]
    first = plan_units(fragments, options(high=300, unit=600))
    second = plan_units(fragments, options(high=300, unit=600))

    assert first == second
    assert all(unit.unit_id.startswith("unit:") for unit in first)
    assert len({unit.unit_id for unit in first}) == len(first)


def test_unit_content_hash_follows_the_fragment_content_hashes() -> None:
    fragments = [frag("a.md", 0, 100), frag("a.md", 1, 100)]
    (before,) = plan_units(fragments, options(high=300, unit=1000))
    edited = [fragments[0], fragments[1].model_copy(update={"content_hash": "other"})]
    (after,) = plan_units(edited, options(high=300, unit=1000))

    assert before.content_hash != after.content_hash
    assert before.unit_id == after.unit_id


def test_a_long_document_is_cut_into_balanced_parts_within_unit_tokens() -> None:
    fragments = [frag("a.md", index, 100) for index in range(12)]
    units = plan_units(fragments, options(high=300, unit=500))

    assert len(units) == 3
    assert [unit.tokens for unit in units] == [400, 400, 400]
    assert all(unit.tokens <= 500 for unit in units)
    assert [fid for unit in units for fid in unit.fragment_ids] == [fragment.fragment_id for fragment in fragments]


def test_uneven_fragments_still_fit_every_unit_and_stay_contiguous() -> None:
    sizes = [300, 50, 250, 120, 10, 290, 40, 200, 150, 60]
    fragments = [frag("a.md", index, size) for index, size in enumerate(sizes)]
    units = plan_units(fragments, options(high=300, unit=600))

    assert all(unit.tokens <= 600 for unit in units)
    assert sum(unit.tokens for unit in units) == sum(sizes)
    assert [fid for unit in units for fid in unit.fragment_ids] == [fragment.fragment_id for fragment in fragments]
    assert len(units) == 3  # the fewest parts: ceil(1470 / 600)


def test_a_fragment_over_unit_tokens_is_a_unit_of_its_own() -> None:
    fragments = [
        frag("a.md", 0, 200),
        frag("a.md", 1, 200),
        frag("a.md", 2, 5000),
        frag("a.md", 3, 200),
        frag("a.md", 4, 200),
    ]
    units = plan_units(fragments, options(high=3000, unit=3000))

    assert [unit.fragment_ids for unit in units] == [
        ("a.md#000", "a.md#001"),
        ("a.md#002",),
        ("a.md#003", "a.md#004"),
    ]
    assert units[1].tokens == 5000
    assert units[1].windows == ((0, 1),)


def test_balanced_parts_prefer_a_top_level_heading_boundary() -> None:
    paths = [("A",), ("A",), ("B", "y"), ("B", "y"), ("B", "z"), ("B", "z")]
    sizes = [100, 100, 100, 100, 100, 100]
    fragments = [frag("a.md", index, size, path) for index, (size, path) in enumerate(zip(sizes, paths, strict=True))]
    units = plan_units(fragments, options(low=1, target=100, high=400, unit=400))

    # A perfectly balanced cut (3 and 3) would fall inside section B; the cut at the A/B boundary wins.
    assert [len(unit.fragment_ids) for unit in units] == [2, 4]


def test_without_a_heading_preference_the_cut_is_the_balanced_one() -> None:
    paths = [("A",), ("A",), ("A",), ("B",), ("B",), ("B",)]
    fragments = [frag("a.md", index, 100, path) for index, path in enumerate(paths)]
    units = plan_units(fragments, options(low=1, target=100, high=400, unit=400))

    assert [len(unit.fragment_ids) for unit in units] == [3, 3]


@pytest.mark.parametrize("max_windows", [1, 2, 3, 5, 8])
def test_windows_are_contiguous_cover_the_unit_and_respect_the_cap(max_windows: int) -> None:
    fragments = [frag("a.md", index, 1000) for index in range(20)]
    opts = options(high=3000, windows=max_windows)
    (unit,) = plan_units(fragments, opts)
    expected = min(max_windows, max(1, -(-20000 // window_tokens(opts))))

    assert unit.tokens == 20000
    assert len(unit.windows) == expected <= max_windows
    assert unit.windows[0][0] == 0
    assert unit.windows[-1][1] == 20
    for (_, end), (start, _) in zip(unit.windows, unit.windows[1:], strict=False):
        assert end == start
    assert all(start < end for start, end in unit.windows)


def test_a_small_unit_has_a_single_window_and_never_more_windows_than_fragments() -> None:
    (small,) = plan_units([frag("a.md", 0, 50), frag("a.md", 1, 50)], BuildOptions())
    (few,) = plan_units([frag("a.md", 0, 2900), frag("a.md", 1, 2900)], options(high=3000, unit=6000, windows=8))

    assert small.windows == ((0, 2),)
    assert len(few.windows) == 2  # 5800 tokens would allow 2 windows of 3000; never more than fragments


def test_window_tokens_is_unit_tokens_over_the_window_cap() -> None:
    assert window_tokens(BuildOptions()) == 3000
    assert window_tokens(options(high=3000, unit=10000, windows=3)) == 3334


def test_shop_orders_document_plans_two_windows_over_its_three_fragments() -> None:
    opts = BuildOptions()
    fragments = segment(shop("orders.md"), opts)
    (unit,) = plan_units(fragments, opts)

    assert len(fragments) == 3
    assert unit.tokens == sum(fragment.tokens for fragment in fragments)
    assert 1 <= len(unit.windows) <= opts.max_node_windows
    assert unit.windows[0][0] == 0
    assert unit.windows[-1][1] == len(fragments)


def test_shop_units_never_span_documents_with_small_options() -> None:
    fragments = [fragment for name in shop_names() for fragment in segment(shop(name), SMALL)]
    units = plan_units(fragments, SMALL)
    by_id = {fragment.fragment_id: fragment for fragment in fragments}

    assert units
    for unit in units:
        assert {by_id[fid].document_id for fid in unit.fragment_ids} == {unit.document_id}
        assert unit.tokens <= SMALL.unit_tokens
        assert len(unit.windows) <= SMALL.max_node_windows
    assert sorted(fid for unit in units for fid in unit.fragment_ids) == sorted(by_id)


# dirty_windows


def make_unit(count: int, windows: Sequence[tuple[int, int]]) -> Unit:
    return Unit(
        unit_id="unit:u",
        document_id="a.md",
        fragment_ids=tuple(f"f{index}" for index in range(count)),
        tokens=count * 10,
        content_hash="h",
        windows=tuple(windows),
    )


def ten(count: int) -> dict[str, int]:
    return {f"f{index}": 10 for index in range(count)}


def test_nothing_dirty_gives_no_windows() -> None:
    assert dirty_windows(make_unit(5, [(0, 5)]), set(), max_windows=8, tokens=ten(5)) == []


def test_one_window_for_one_dirty_run() -> None:
    unit = make_unit(10, [(0, 10)])
    assert dirty_windows(unit, {"f2", "f3", "f4"}, max_windows=8, tokens=ten(10)) == [(2, 5)]


def test_one_window_per_maximal_run_of_dirty_fragments() -> None:
    unit = make_unit(10, [(0, 10)])
    windows = dirty_windows(unit, {"f0", "f1", "f4", "f7", "f8", "f9"}, max_windows=8, tokens=ten(10))
    assert windows == [(0, 2), (4, 5), (7, 10)]


def test_ids_that_are_not_in_the_unit_are_ignored() -> None:
    unit = make_unit(4, [(0, 4)])
    assert dirty_windows(unit, {"other", "f1"}, max_windows=8, tokens=ten(4)) == [(1, 2)]


def test_a_run_is_cut_where_a_planned_window_starts() -> None:
    unit = make_unit(10, [(0, 5), (5, 10)])
    assert dirty_windows(unit, {"f4", "f5"}, max_windows=8, tokens=ten(10)) == [(4, 5), (5, 6)]


def test_a_fully_dirty_unit_keeps_its_planned_windows() -> None:
    unit = make_unit(10, [(0, 4), (4, 7), (7, 10)])
    windows = dirty_windows(unit, {f"f{index}" for index in range(10)}, max_windows=8, tokens=ten(10))
    assert windows == [(0, 4), (4, 7), (7, 10)]


def test_runs_merge_down_to_the_cap_starting_with_the_cheapest_pair() -> None:
    unit = make_unit(10, [(0, 10)])
    dirty = {"f0", "f2", "f4", "f6", "f8"}
    windows = dirty_windows(unit, dirty, max_windows=2, tokens=ten(10))

    assert windows == [(0, 3), (4, 9)]
    assert len(windows) <= 2


def test_merging_prefers_the_pair_with_the_fewest_tokens_between_and_in_them() -> None:
    unit = make_unit(9, [(0, 9)])
    tokens = {f"f{index}": 10 for index in range(9)} | {"f1": 500}
    windows = dirty_windows(unit, {"f0", "f2", "f4", "f6", "f8"}, max_windows=4, tokens=tokens)

    # f0..f2 holds the 500-token fragment f1, so the first merge is elsewhere: (2,3)+(4,5) -> (2,5).
    assert windows == [(0, 1), (2, 5), (6, 7), (8, 9)]


def test_a_merged_window_covers_the_clean_fragments_between_its_runs() -> None:
    unit = make_unit(8, [(0, 8)])
    windows = dirty_windows(unit, {"f0", "f7"}, max_windows=1, tokens=ten(8))
    assert windows == [(0, 8)]


def test_the_cap_is_never_exceeded_whatever_the_dirty_pattern() -> None:
    unit = make_unit(30, [(0, 30)])
    dirty = {f"f{index}" for index in range(0, 30, 2)}
    for cap in (1, 2, 3, 7, 15, 20):
        windows = dirty_windows(unit, dirty, max_windows=cap, tokens=ten(30))
        assert 1 <= len(windows) <= cap
        covered = {index for start, end in windows for index in range(start, end)}
        assert {int(name[1:]) for name in dirty} <= covered
        for (_, end), (start, _) in itertools.pairwise(windows):
            assert end <= start


def test_dirty_windows_follow_a_planned_unit_from_plan_units() -> None:
    fragments = [frag("a.md", index, 1000) for index in range(10)]
    opts = options(high=3000, unit=24000, windows=4)
    (unit,) = plan_units(fragments, opts)
    dirty = {unit.fragment_ids[index] for index in (1, 2, len(fragments) - 2)}
    windows = dirty_windows(
        unit, dirty, max_windows=opts.max_node_windows, tokens={f.fragment_id: f.tokens for f in fragments}
    )

    assert windows[0][0] == 1
    assert windows[-1][1] == 9
    assert len(windows) <= 4


# change_set (build.md Plan 2)


def persisted(document_id: str, revision_id: str, status: str = "active") -> DocumentRec:
    return DocumentRec.model_validate(
        {
            "document_id": document_id,
            "revision_id": revision_id,
            "version": 1,
            "path": document_id,
            "title": document_id,
            "status": status,
        }
    )


def snapshot(*pairs: tuple[str, str]) -> list[CorpusRevision]:
    return [
        CorpusRevision(lineage_id=lineage, revision_id=rev, version=1, path=lineage, title=lineage, markdown="# t\n")
        for lineage, rev in pairs
    ]


def test_everything_is_new_without_persisted_documents() -> None:
    result = change_set(snapshot(("a.md", "r1"), ("b.md", "r1")), [])
    assert result == ChangeSet(new=("a.md", "b.md"))


def test_a_different_revision_is_revised_and_the_same_one_unchanged() -> None:
    result = change_set(
        snapshot(("a.md", "r2"), ("b.md", "r1")),
        [persisted("a.md", "r1"), persisted("b.md", "r1")],
    )
    assert result == ChangeSet(revised=("a.md",), unchanged=("b.md",))


def test_an_active_document_missing_from_the_snapshot_is_removed() -> None:
    result = change_set(snapshot(("a.md", "r1")), [persisted("a.md", "r1"), persisted("gone.md", "r1")])
    assert result == ChangeSet(unchanged=("a.md",), removed=("gone.md",))


def test_a_lineage_with_only_a_removed_document_is_new_again() -> None:
    result = change_set(snapshot(("a.md", "r1")), [persisted("a.md", "r1", status="removed")])
    assert result.new == ("a.md",)
    assert result.unchanged == ()


def test_an_already_removed_document_is_not_removed_again() -> None:
    result = change_set(
        snapshot(("a.md", "r1")), [persisted("a.md", "r1"), persisted("old.md", "r1", status="removed")]
    )
    assert result.removed == ()


def test_retry_lists_requested_lineages_that_are_unchanged() -> None:
    result = change_set(
        snapshot(("a.md", "r1"), ("b.md", "r1")),
        [persisted("a.md", "r1"), persisted("b.md", "r1")],
        retry_lineages=["b.md"],
    )
    assert result.retried == ("b.md",)
    assert result.unchanged == ("a.md", "b.md")


def test_retry_of_a_new_or_revised_lineage_is_extracted_anyway_and_not_listed() -> None:
    result = change_set(
        snapshot(("new.md", "r1"), ("rev.md", "r2")),
        [persisted("rev.md", "r1")],
        retry_lineages=["new.md", "rev.md"],
    )
    assert result.retried == ()
    assert result.new == ("new.md",)
    assert result.revised == ("rev.md",)


def test_retry_of_a_lineage_outside_the_snapshot_is_ignored() -> None:
    result = change_set(snapshot(("a.md", "r1")), [persisted("a.md", "r1")], retry_lineages=["ghost.md"])
    assert result.retried == ()
    assert result.removed == ()


def test_lists_follow_snapshot_order_and_removed_follows_persisted_order() -> None:
    result = change_set(
        snapshot(("z.md", "r1"), ("a.md", "r1"), ("m.md", "r1")),
        [persisted("q.md", "r1"), persisted("c.md", "r1"), persisted("m.md", "r1")],
        retry_lineages=["m.md"],
    )
    assert result.new == ("z.md", "a.md")
    assert result.unchanged == ("m.md",)
    assert result.removed == ("q.md", "c.md")


def test_the_five_cases_together() -> None:
    result = change_set(
        snapshot(("new.md", "r1"), ("rev.md", "r2"), ("same.md", "r1"), ("retry.md", "r1")),
        [
            persisted("rev.md", "r1"),
            persisted("same.md", "r1"),
            persisted("retry.md", "r1"),
            persisted("gone.md", "r1"),
        ],
        retry_lineages=("retry.md",),
    )
    assert result == ChangeSet(
        new=("new.md",),
        revised=("rev.md",),
        unchanged=("same.md", "retry.md"),
        removed=("gone.md",),
        retried=("retry.md",),
    )


def test_an_empty_snapshot_removes_every_active_document() -> None:
    result = change_set([], [persisted("a.md", "r1"), persisted("b.md", "r1", status="removed")])
    assert result == ChangeSet(removed=("a.md",))
