"""``OfficeStructure``: heading shift, slide and sheet markers, sheet-name headings (no Docling needed)."""

from __future__ import annotations

import pytest

from file_parsers.formats import DocumentFormat
from file_parsers.office.model import OfficeContent, OfficeSection
from file_parsers.office.structure import OfficeStructure, shift_headings


def render(document_format: DocumentFormat, *sections: OfficeSection) -> str:
    return OfficeStructure().render(OfficeContent(document_format, sections))


def one(markdown: str) -> OfficeSection:
    return OfficeSection(number=1, markdown=markdown)


def test_docx_without_a_h1_moves_every_heading_up_one_level():
    markdown = "## Chapter\n\ntext\n\n### Section\n\n#### Detail"
    assert render(DocumentFormat.DOCX, one(markdown)) == "# Chapter\n\ntext\n\n## Section\n\n### Detail\n"


def test_docx_with_a_title_keeps_its_levels():
    markdown = "# Title\n\n## Chapter\n\n### Section"
    assert render(DocumentFormat.DOCX, one(markdown)) == markdown + "\n"


def test_the_shift_leaves_text_that_only_looks_like_a_heading_alone():
    markdown = "## Real\n\nA line with #hashtag and ## inside.\n\n#nospace\n\n####### seven"
    assert shift_headings(markdown) == "# Real\n\nA line with #hashtag and ## inside.\n\n#nospace\n\n####### seven"


def test_the_shift_skips_fenced_code_and_a_fenced_h1_does_not_block_it():
    markdown = "## Real\n\n```\n# comment in code\n## also code\n```\n\n~~~\n# tilde\n~~~\n\n### Sub"
    shifted = shift_headings(markdown)
    assert shifted.startswith("# Real")
    assert "```\n# comment in code\n## also code\n```" in shifted
    assert "~~~\n# tilde\n~~~" in shifted
    assert shifted.endswith("## Sub")


def test_text_without_headings_is_unchanged():
    assert shift_headings("just text\n\nmore") == "just text\n\nmore"


def test_html_and_markdown_are_never_shifted():
    for document_format in (DocumentFormat.HTML, DocumentFormat.MARKDOWN):
        assert render(document_format, one("## Starts at two\n\n### three")) == "## Starts at two\n\n### three\n"


def test_csv_is_one_table_without_markers():
    assert render(DocumentFormat.CSV, one("| a |\n| - |\n| 1 |")) == "| a |\n| - |\n| 1 |\n"


def test_pptx_gets_one_marker_per_slide_including_an_empty_one():
    sections = (
        OfficeSection(1, "# One\n\n- a"),
        OfficeSection(2, ""),
        OfficeSection(3, "# Three"),
    )
    assert render(DocumentFormat.PPTX, *sections) == (
        "<!-- source-page: 1 -->\n\n# One\n\n- a\n\n<!-- source-page: 2 -->\n\n<!-- source-page: 3 -->\n\n# Three\n"
    )


def test_xlsx_gets_a_marker_and_the_sheet_name_as_a_heading():
    sections = (OfficeSection(1, "| a |\n| - |\n| 1 |", name="Budget 2026"), OfficeSection(2, "", name="Empty"))
    assert render(DocumentFormat.XLSX, *sections) == (
        "<!-- source-page: 1 -->\n\n# Budget 2026\n\n| a |\n| - |\n| 1 |\n\n<!-- source-page: 2 -->\n\n# Empty\n"
    )


def test_a_sheet_name_with_line_breaks_stays_one_heading_and_a_missing_name_is_numbered():
    sections = (OfficeSection(1, "", name="Two\nLines\t here"), OfficeSection(2, "", name=None))
    markdown = render(DocumentFormat.XLSX, *sections)
    assert "# Two Lines here\n" in markdown
    assert "# Sheet 2\n" in markdown


def test_an_empty_document_renders_as_empty_text_not_a_newline():
    assert render(DocumentFormat.CSV, one("")) == ""
    assert render(DocumentFormat.DOCX, one("")) == ""


@pytest.mark.parametrize("document_format", [DocumentFormat.PPTX, DocumentFormat.XLSX])
def test_rendering_is_deterministic(document_format: DocumentFormat):
    sections = (OfficeSection(1, "x", name="A"), OfficeSection(2, "y", name="B"))
    assert render(document_format, *sections) == render(document_format, *sections)
