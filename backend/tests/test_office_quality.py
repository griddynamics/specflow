"""G11 (Office/web quality, offline) of docs/docling-pdf-parser.md, and G8 for these formats.

One small generated file per format goes through the real ``file_to_markdown`` (Docling, then
``OfficeStructure``) and is scored with the metrics of ``examples/parse_benchmark.py`` against the Markdown a
perfect parser would write (``office_support.truth_markdown``).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from docx import Document
from PIL import Image
from pptx import Presentation
from pptx.util import Inches

from file_parsers.convert import ParsedMarkdown, file_to_markdown
from file_parsers.engines.docling import engine_id
from office_support import (
    DOCX,
    PPTX,
    SPEC,
    XLSX,
    Heading,
    Paragraph,
    build_csv,
    build_docx,
    build_html,
    build_markdown,
    build_pptx,
    build_xlsx,
    truth_markdown,
)
from parse_benchmark import evaluate, headings

MARKER = re.compile(r"^<!-- source-page: (\d+) -->$", re.MULTILINE)


def convert(path: Path, media_type: str) -> ParsedMarkdown:
    return file_to_markdown(path, media_type)


def assert_quality(result: str, truth: str) -> dict[str, Any]:
    scores = evaluate(result, truth)
    assert scores["word_recall"] >= 0.98, scores
    assert scores["tables_found"] == scores["tables_expected"], scores
    assert scores["cell_recall"] >= 0.95, scores
    assert scores["headings_found"] == scores["headings_expected"], scores
    assert scores["heading_level_exact"] == scores["headings_expected"], scores
    return scores


def test_docx_headings_are_shifted_to_their_word_levels(tmp_path: Path):
    result = convert(build_docx(tmp_path / "spec.docx"), DOCX)
    assert result.parser == engine_id()
    assert_quality(result.markdown, truth_markdown(SPEC))
    assert MARKER.search(result.markdown) is None  # no pages, no marker


def test_docx_with_a_title_style_keeps_title_as_h1_and_heading_1_as_h2(tmp_path: Path):
    result = convert(build_docx(tmp_path / "titled.docx", title="Specification Title"), DOCX)
    truth = "# Specification Title\n\n" + truth_markdown(SPEC, level_offset=1)
    assert_quality(result.markdown, truth)
    assert result.markdown.startswith("# Specification Title")


def test_docx_without_any_heading_is_not_changed_by_the_shift(tmp_path: Path):
    blocks = (Paragraph("Only a paragraph of plain prose without any heading at all."),)
    result = convert(build_docx(tmp_path / "plain.docx", blocks), DOCX)
    assert result.markdown.strip() == "Only a paragraph of plain prose without any heading at all."


def test_html_levels_are_exact_and_no_marker(tmp_path: Path):
    result = convert(build_html(tmp_path / "spec.html"), "text/html")
    assert_quality(result.markdown, truth_markdown(SPEC))
    assert MARKER.search(result.markdown) is None


def test_html_without_headings_gives_plain_text(tmp_path: Path):
    path = tmp_path / "flat.html"
    path.write_text("<html><body><p>Just a paragraph of text.</p></body></html>", encoding="utf-8")
    assert convert(path, "text/html").markdown.strip() == "Just a paragraph of text."


def test_html_starting_at_h2_keeps_its_levels(tmp_path: Path):
    blocks = (Heading(2, "Second Level Start"), Paragraph("Body text of the section."), Heading(3, "Deeper Section"))
    result = convert(build_html(tmp_path / "h2.html", blocks), "text/html")
    assert [level for level, _ in headings(result.markdown)] == [2, 3]


def test_markdown_levels_are_exact_and_no_marker(tmp_path: Path):
    result = convert(build_markdown(tmp_path / "spec.md"), "text/markdown")
    assert_quality(result.markdown, truth_markdown(SPEC))
    assert MARKER.search(result.markdown) is None


def test_pptx_has_one_marker_per_slide_and_the_titles_are_h1(tmp_path: Path):
    slides = [
        ("Opening Slide", ["first point", "second point"]),
        ("Architecture", ["gateway", "workers"]),
        ("Closing", ["thanks"]),
    ]
    result = convert(build_pptx(tmp_path / "deck.pptx", slides), PPTX)
    assert [int(n) for n in MARKER.findall(result.markdown)] == [1, 2, 3]
    assert [(1, title.lower()) for title, _ in slides] == headings(result.markdown)  # headings() lower-cases
    for _, bullets in slides:
        for bullet in bullets:
            assert bullet in result.markdown
    assert result.markdown.index("<!-- source-page: 2 -->") < result.markdown.index("Architecture")


def test_pptx_slide_without_content_still_gets_its_marker(tmp_path: Path):
    path = build_pptx(tmp_path / "gap.pptx", [("One", ["a"]), ("", [""]), ("Three", ["c"])])
    assert [int(n) for n in MARKER.findall(convert(path, PPTX).markdown)] == [1, 2, 3]


def test_xlsx_has_one_marker_and_one_heading_per_sheet_and_every_cell(tmp_path: Path):
    sheets = [
        (
            "Requirements",
            [["Identifier", "Capability"], ["FR-IAM-001", "Single sign-on"], ["FR-IAM-002", "Role mapping"]],
        ),
        ("Budget", [["Quarter", "Amount"], ["Q1", 1200], ["Q2", 3400]]),
    ]
    result = convert(build_xlsx(tmp_path / "book.xlsx", sheets), XLSX)
    assert [int(n) for n in MARKER.findall(result.markdown)] == [1, 2]
    assert headings(result.markdown) == [(1, "requirements"), (1, "budget")]  # headings() lower-cases
    scores = evaluate(
        result.markdown,
        "| Identifier | Capability |\n| - | - |\n| FR-IAM-001 | Single sign-on |\n| FR-IAM-002 | Role mapping |\n\n"
        "| Quarter | Amount |\n| - | - |\n| Q1 | 1200 |\n| Q2 | 3400 |\n",
    )
    assert scores["tables_found"] == 2
    assert scores["cell_recall"] == 1.0


def test_xlsx_empty_sheet_keeps_its_marker_and_name_and_a_sheet_without_a_table_has_no_table(tmp_path: Path):
    sheets = [("Data", [["a", "b"], ["c", "d"]]), ("Blank", [])]
    markdown = convert(build_xlsx(tmp_path / "gap.xlsx", sheets), XLSX).markdown
    assert [int(n) for n in MARKER.findall(markdown)] == [1, 2]
    assert headings(markdown) == [(1, "data"), (1, "blank")]
    assert markdown.rstrip().endswith("# Blank")


def test_an_empty_workbook_is_one_empty_sheet(tmp_path: Path):
    markdown = convert(build_xlsx(tmp_path / "empty.xlsx", [("Sheet", [])]), XLSX).markdown
    assert markdown == "<!-- source-page: 1 -->\n\n# Sheet\n"


def test_csv_becomes_one_table_without_a_marker_and_keeps_quotes_commas_and_newlines(tmp_path: Path):
    path = build_csv(tmp_path / "t.csv", 'name,note\r\n"Smith, Ann","said ""hi"""\r\nBob,"two\nlines"\r\n')
    markdown = convert(path, "text/csv").markdown
    assert MARKER.search(markdown) is None
    scores = evaluate(markdown, '| name | note |\n| - | - |\n| Smith, Ann | said "hi" |\n| Bob | two lines |\n')
    assert scores["tables_found"] == 1
    assert scores["cell_recall"] >= 0.95


def test_a_utf8_markdown_file_with_a_byte_order_mark_is_accepted(tmp_path: Path):
    path = tmp_path / "bom.md"
    path.write_bytes(b"\xef\xbb\xbf# Title\n\nBody text.\n")
    assert headings(convert(path, "text/markdown").markdown) == [(1, "title")]


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda p: (build_docx(p / "a.docx"), DOCX), id="docx"),
        pytest.param(lambda p: (build_pptx(p / "a.pptx", [("T", ["x"])]), PPTX), id="pptx"),
        pytest.param(lambda p: (build_xlsx(p / "a.xlsx", [("S", [["a", "b"]])]), XLSX), id="xlsx"),
        pytest.param(lambda p: (build_html(p / "a.html"), "text/html"), id="html"),
        pytest.param(lambda p: (build_markdown(p / "a.md"), "text/markdown"), id="markdown"),
        pytest.param(lambda p: (build_csv(p / "a.csv", "a,b\n1,2\n"), "text/csv"), id="csv"),
    ],
)
def test_g8_the_same_file_twice_gives_byte_identical_markdown(build: Any, tmp_path: Path):
    path, media_type = build(tmp_path)
    first, second = convert(path, media_type), convert(path, media_type)
    assert first.markdown == second.markdown
    assert first.markdown.encode() == second.markdown.encode()
    assert first.markdown.endswith("\n")


def test_a_semicolon_csv_is_one_table(tmp_path: Path):
    markdown = convert(build_csv(tmp_path / "s.csv", "name;qty\nbolt;10\nnut;20\n"), "text/csv").markdown
    assert markdown.startswith("| name | qty |")
    assert "| nut | 20 |" in markdown


# --- pictures keep a placeholder in the PDF shape (DESIGN: a figure placeholder per picture) ------------------


@pytest.fixture
def png(tmp_path: Path) -> Path:
    path = tmp_path / "pixel.png"
    Image.new("RGB", (40, 40), "red").save(path)
    return path


def test_docx_pictures_get_unpaged_placeholders_numbered_from_zero(png: Path, tmp_path: Path):
    document = Document()
    document.add_paragraph("before")
    document.add_picture(str(png))
    document.add_paragraph("between")
    document.add_picture(str(png))
    document.save(tmp_path / "pics.docx")
    markdown = convert(tmp_path / "pics.docx", DOCX).markdown
    assert markdown == "before\n\n![fig0](images/fig0.png)\n\nbetween\n\n![fig1](images/fig1.png)\n"
    assert not (tmp_path / "images").exists()  # a placeholder only, no file is written


def test_pptx_pictures_are_numbered_per_slide_with_the_slide_number(png: Path, tmp_path: Path):
    deck = Presentation()
    for title, count in (("One", 1), ("Two", 2)):
        slide = deck.slides.add_slide(deck.slide_layouts[5])
        slide.shapes.title.text = title
        for index in range(count):
            slide.shapes.add_picture(str(png), Inches(1 + 2 * index), Inches(2))
    deck.save(tmp_path / "pics.pptx")
    markdown = convert(tmp_path / "pics.pptx", PPTX).markdown
    assert re.findall(r"!\[[^\]]*\]\([^)]*\)", markdown) == [
        "![p1.fig0](images/p1.fig0.png)",
        "![p2.fig0](images/p2.fig0.png)",
        "![p2.fig1](images/p2.fig1.png)",
    ]
    assert markdown.index("![p1.fig0]") < markdown.index("<!-- source-page: 2 -->") < markdown.index("![p2.fig0]")


def test_html_and_markdown_images_get_unpaged_placeholders(tmp_path: Path):
    html = tmp_path / "pic.html"
    html.write_text("<p>a</p><img src='x.png'><p>b</p>", encoding="utf-8")
    assert convert(html, "text/html").markdown == "a\n\n![fig0](images/fig0.png)\n\nb\n"
    md = tmp_path / "pic.md"
    md.write_text("a\n\n![alt](x.png)\n", encoding="utf-8")
    assert "![fig0](images/fig0.png)" in convert(md, "text/markdown").markdown
