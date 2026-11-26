"""Offline tests of the diagrams.net Markdown converter: golden files, escaping, order and determinism."""

from __future__ import annotations

import os
import re
from dataclasses import replace
from pathlib import Path

import pytest

from file_parsers.diagrams import (
    MAX_MARKDOWN_BYTES,
    Arrow,
    Diagram,
    DiagramDecodeError,
    Page,
    Role,
    Shape,
    decode,
    file_markdown,
    page_title,
    to_markdown,
)
from file_parsers.diagrams.fake import fake_diagram

FIXTURES = Path(__file__).parent / "fixtures" / "drawio"


def _golden(name: str, actual: str) -> None:
    path = FIXTURES / name
    if os.environ.get("UPDATE_GOLDEN"):
        path.write_text(actual)
    assert actual == path.read_text()


@pytest.mark.parametrize("name", ["flow", "structure"])
def test_golden_page_and_file(name: str) -> None:
    diagram = decode((FIXTURES / f"{name}.drawio").read_bytes(), hint=f"{name}.drawio")
    _golden(f"{name}.page1.md", to_markdown(diagram.pages[0], diagram))
    _golden(f"{name}.file.md", file_markdown(diagram))


def test_second_page_numbering() -> None:
    diagram = decode((FIXTURES / "structure.drawio").read_bytes(), hint="structure.drawio")
    text = to_markdown(diagram.pages[1], diagram)
    assert text.startswith("# Second page\nSource: diagrams.net · page 2 of 2\n")
    assert "- **Only** (step)" in text


def _page(shapes: tuple[Shape, ...] = (), arrows: tuple[Arrow, ...] = ()) -> Page:
    return Page(id="p", name="P", shapes=shapes, arrows=arrows)


def test_title_with_diagram_name_and_empty_page() -> None:
    page = _page()
    diagram = Diagram("My flow", (page,))
    assert to_markdown(page, diagram) == "# My flow · P\nSource: diagrams.net · page 1 of 1\n"
    assert file_markdown(diagram).startswith("# My flow\nSource: diagrams.net · 1 page\n\n## P\n")


def test_empty_sections_left_out_and_one_trailing_newline() -> None:
    page = _page((Shape("a", "A", Role.STEP, None, None, 0, 0),))
    text = to_markdown(page, Diagram("", (page,)))
    assert "## Arrows" not in text and "## Notes" not in text
    assert text.endswith("\n") and not text.endswith("\n\n")


def test_labels_cannot_inject_markdown() -> None:
    props = (("link", "https://e.x/a_b?q=[1]"), ("k", "line\nbreak"))
    nasty = (
        Shape("a", "# Heading", Role.STEP, None, None, 0, 0),
        Shape("b", "[x](javascript:alert(1)) ![i](u) `code` <script>", Role.STEP, None, None, 0, 1),
        Shape("c", "1. item", Role.NOTE, None, None, 0, 2),
        Shape("d", "- bullet · &amp; &copy; a_b _c_ **b**", Role.STEP, None, None, 0, 3),
        Shape("e", "back\\slash", Role.STEP, None, None, 0, 4, props),
    )
    page = _page(nasty, (Arrow("a", "b", "> quote [l](u)"),))
    text = to_markdown(page, Diagram("# D", (page,)))
    lines = text.splitlines()
    assert lines[0] == "# \\# D · P"
    assert [ln for ln in lines if ln.startswith("#")] == ["# \\# D · P", "## Shapes", "## Arrows", "## Notes"]
    assert "[x](" not in text and "![" not in text and "`code`" not in text and "<script>" not in text
    assert "\\# Heading" in text and "1\\. item" in text and "\\- bullet" in text
    assert "\\&amp; \\&copy;" in text
    assert "a_b" in text and "\\_c\\_" in text and "\\*\\*b\\*\\*" in text
    assert "  - link: https://e.x/a_b?q=\\[1\\]" in text
    assert "  - k: line break" in text
    assert "\\> quote \\[l\\](u)" in text
    for ln in lines:
        assert not ln.startswith(("[", "<", ">", "1."))


def test_link_property_is_plain_text() -> None:
    shape = Shape("a", "A", Role.STEP, None, None, 0, 0, (("link", "https://example.com/x"), ("empty", "")))
    page = _page((shape,))
    text = to_markdown(page, Diagram("", (page,)))
    assert "  - link: https://example.com/x" in text
    assert "](" not in text and "empty" not in text


def test_unlabelled_ends() -> None:
    page = _page((Shape("a", "A", Role.STEP, None, None, 0, 0),), (Arrow(None, "a", ""), Arrow("a", None, "go")))
    text = to_markdown(page, Diagram("", (page,)))
    assert "- (unlabelled shape) → A\n- A → (unlabelled shape): go\n" in text


def test_arrow_names_use_the_first_line_of_the_label() -> None:
    shapes = (
        Shape("a", "Plan · planner (opus) · PLAN.md", Role.STEP, None, None, 0, 0),
        Shape("b", "B", Role.STEP, None, None, 0, 1),
    )
    page = _page(shapes, (Arrow("a", "b", ""),))
    text = to_markdown(page, Diagram("", (page,)))
    assert "- **Plan** (step): planner (opus) · PLAN.md" in text
    assert "- Plan → B\n" in text


def test_nested_group_headings_and_group_props() -> None:
    shapes = (
        Shape("g", "Outer", Role.GROUP, None, None, 0, 0, (("owner", "me"),)),
        Shape("h", "Inner", Role.GROUP, "g", None, 0, 1),
        Shape("a", "A", Role.STEP, "h", None, 0, 2),
        Shape("n", "note", Role.NOTE, "g", None, 0, 3),
    )
    page = _page(shapes)
    text = to_markdown(page, Diagram("", (page,)))
    assert "### Group: Outer\n- owner: me\n### Group: Outer \u203a Inner\n- **A** (step)\n" in text
    assert "## Notes\n- note\n" in text


def test_determinism_byte_equal() -> None:
    data = (FIXTURES / "structure.drawio").read_bytes()
    a, b = decode(data, hint="a.drawio"), decode(data, hint="a.drawio")
    assert file_markdown(a).encode() == file_markdown(b).encode()
    assert to_markdown(a.pages[0], a).encode() == to_markdown(b.pages[0], b).encode()


def _nested(depth: int) -> Page:
    shapes = tuple(
        Shape(f"g{i}", f"Lane {i} " + "x" * 500, Role.GROUP, f"g{i - 1}" if i else None, None, 0, i)
        for i in range(depth)
    )
    return _page(shapes)


def test_a_thousand_nested_swimlanes_render_three_levels_with_capped_labels() -> None:
    page = _nested(1000)

    text = to_markdown(page, Diagram("D", (page,)))

    titles = [ln for ln in text.splitlines() if "Group:" in ln]
    assert len(titles) == 1000 and len(text) < 1_500_000
    assert titles[-1].startswith("### Group: … \u203a Lane 997 ") and titles[-1].count(" \u203a ") == 3
    assert titles[1].count("\u203a") == 1 and not titles[0].count("\u203a")
    assert all(len(t) < 3 * 305 + 40 for t in titles)


def test_a_three_level_path_has_no_ellipsis() -> None:
    page = _nested(3)

    titles = [ln for ln in to_markdown(page, Diagram("D", (page,))).splitlines() if "Group:" in ln]

    assert titles[2].count("…") == 3 and "Group: Lane 0" in titles[2] and "Group: … " not in titles[2]


def test_every_label_is_cut_at_300_characters() -> None:
    long = "w" * 900
    shape = Shape("a", long, Role.STEP, None, None, 0, 0, (("k", long),))
    other = Shape("b", "B", Role.STEP, None, None, 0, 1)
    page = _page((shape, other), (Arrow("a", "b", long),))

    text = to_markdown(page, Diagram("n" * 900, (page,)))

    runs = [len(run) for run in re.findall(r"[wn]+", text)]
    assert max(runs) == 299 and text.count("…") == 5  # title, shape, property, arrow end, arrow label


def test_a_page_over_the_markdown_cap_is_too_large() -> None:
    shapes = tuple(Shape(f"s{i}", "x" * 299, Role.STEP, None, None, 0, i, (("k", "v" * 299),)) for i in range(5000))
    page = _page(shapes)

    with pytest.raises(DiagramDecodeError) as caught:
        to_markdown(page, Diagram("D", (page,)))

    assert caught.value.code == "too_large"


def test_a_file_over_the_markdown_file_cap_is_too_large_while_each_page_fits() -> None:
    shapes = tuple(Shape(f"s{i}", "x" * 299, Role.STEP, None, None, 0, i, (("k", "v" * 299),)) for i in range(2500))
    pages = tuple(Page(f"p{i}", f"P{i}", shapes, ()) for i in range(10))
    diagram = Diagram("D", pages)

    assert all(len(to_markdown(page, diagram).encode()) < MAX_MARKDOWN_BYTES for page in pages)
    with pytest.raises(DiagramDecodeError) as caught:
        file_markdown(diagram)

    assert caught.value.code == "too_large"


def test_arrows_to_equal_names_say_which_one() -> None:
    shapes = (
        Shape("g1", "Design", Role.GROUP, None, None, 0, 0),
        Shape("g2", "Build", Role.GROUP, None, None, 0, 5),
        Shape("r1", "Review", Role.STEP, "g1", None, 0, 1),
        Shape("r2", "Review", Role.STEP, "g2", None, 0, 6),
        Shape("t1", "Test", Role.STEP, "g2", None, 0, 7),
        Shape("t2", "Test", Role.STEP, "g2", None, 0, 8),
        Shape("x", "Ship", Role.STEP, None, None, 0, 9),
        Shape("s1", "Solo", Role.STEP, None, None, 0, 10),
        Shape("s2", "Solo", Role.STEP, None, None, 0, 11),
    )
    arrows = (
        Arrow("r1", "r2", ""),
        Arrow("t1", "t2", "retry"),
        Arrow("s1", "s2", ""),
        Arrow("x", "r1", ""),
        Arrow(None, "x", ""),
    )
    page = _page(shapes, arrows)

    lines = to_markdown(page, Diagram("D", (page,))).split("## Arrows\n")[1].splitlines()[:5]

    assert lines == [
        "- Review (in Design) → Review (in Build)",
        "- Test (in Build) #1 → Test (in Build) #2: retry",
        "- Solo #1 → Solo #2",
        "- Ship → Review (in Design)",
        "- (unlabelled shape) → Ship",
    ]


def test_the_page_title_leaves_out_an_empty_or_repeated_name() -> None:
    page = fake_diagram().pages[0]
    assert page_title(replace(fake_diagram(), name=page.name), page) == page.name
    assert page_title(replace(fake_diagram(), name=""), page) == page.name
    assert page_title(replace(fake_diagram(), name="D"), page) == f"D · {page.name}"
