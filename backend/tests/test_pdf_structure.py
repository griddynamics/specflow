"""PdfStructure: heading levels by effective font size and glued-word repair, on small synthetic PDFs.

Each test builds a real PDF (tests/pdf_builder_support.py) and a ``PDFDocument`` as Docling would
deliver it (every heading ``##``), runs ``PdfStructure`` and reads the Markdown back.
"""

from __future__ import annotations

from pathlib import Path

from file_parsers.pdf.schema import PDFDocument, PDFPageContent, PDFTableBox
from file_parsers.pdf.structure import PdfStructure
from pdf_builder_support import Line, write_pdf


def _document(path: Path, *markdown: str) -> PDFDocument:
    pages = [PDFPageContent(page_number=n, markdown=text, figures=[]) for n, text in enumerate(markdown, start=1)]
    return PDFDocument(source_path=path, pages=pages)


def _apply(tmp_path: Path, pdf_pages: list[list[Line]], *markdown: str) -> list[str]:
    path = write_pdf(tmp_path / "doc.pdf", pdf_pages)
    return [page.markdown for page in PdfStructure(path).apply(_document(path, *markdown)).pages]


def test_levels_are_ranked_by_font_size_across_the_whole_document(tmp_path: Path):
    pdf = [
        [Line("Report Title", 24), Line("First Chapter", 18), Line("plain body text", 12)],
        [Line("Second Chapter", 18), Line("A Small Section", 14), Line("more body text", 12)],
    ]
    first, second = _apply(
        tmp_path,
        pdf,
        "## Report Title\n\n## First Chapter\n\nplain body text",
        "## Second Chapter\n\n## A Small Section\n\nmore body text",
    )
    assert first == "# Report Title\n\n## First Chapter\n\nplain body text"
    assert second == "## Second Chapter\n\n### A Small Section\n\nmore body text"


def test_the_size_is_nominal_times_the_text_matrix_scale(tmp_path: Path):
    pdf = [[Line("Matrix Title", 20, via_matrix=True), Line("Plain Heading", 14), Line("body", 12)]]
    [page] = _apply(tmp_path, pdf, "## Plain Heading\n\n## Matrix Title\n\nbody")
    assert page == "## Plain Heading\n\n# Matrix Title\n\nbody"


def test_sizes_within_half_a_point_share_a_level_and_more_than_six_sizes_stop_at_level_six(tmp_path: Path):
    sizes = [30, 28, 26, 24, 22, 20, 18]
    pdf = [[Line(f"Heading {word}", size) for word, size in zip("abcdefg", sizes, strict=True)]]
    markdown = "\n\n".join(f"## Heading {word}" for word in "abcdefg")
    [page] = _apply(tmp_path, pdf, markdown)
    assert [line.split(" ")[0] for line in page.split("\n\n")] == [
        "#",
        "##",
        "###",
        "####",
        "#####",
        "######",
        "######",
    ]

    twins = [[Line("Alpha Heading", 18.0), Line("Beta Heading", 18.2), Line("Gamma Heading", 12)]]
    [page] = _apply(tmp_path, twins, "## Alpha Heading\n\n## Beta Heading\n\n## Gamma Heading")
    assert page == "# Alpha Heading\n\n# Beta Heading\n\n## Gamma Heading"


def test_a_heading_not_found_in_the_text_uses_the_numbering_depth(tmp_path: Path):
    pdf = [[Line("Document Title", 24), Line("1 Introduction", 18), Line("1.1 Background", 14)]]
    [page] = _apply(
        tmp_path,
        pdf,
        "## Document Title\n\n## 1 Introduction\n\n## 1.1 Background\n\n## 2 Missing Chapter\n\n"
        "## 2.1 Missing Section\n\n## 2.1.1 Missing Detail\n\n## Unnumbered Missing",
    )
    assert page.split("\n\n") == [
        "# Document Title",
        "## 1 Introduction",
        "### 1.1 Background",
        "## 2 Missing Chapter",  # the level of the measured depth-1 headings
        "### 2.1 Missing Section",  # the level of the measured depth-2 headings
        "#### 2.1.1 Missing Detail",  # depth 3 was never measured: depth + 1
        "## Unnumbered Missing",  # no number: keeps the level Docling gave it
    ]


def test_without_any_measured_heading_numbered_ones_use_depth_plus_one_and_others_keep_their_level(tmp_path: Path):
    [page] = _apply(tmp_path, [[Line("only body text here", 12)]], "### Not In Pdf\n\n### 3 Numbered\n\ntext")
    assert page == "### Not In Pdf\n\n## 3 Numbered\n\ntext"


def test_a_page_without_headings_and_a_blank_page_are_unchanged(tmp_path: Path):
    pdf = [[Line("just a paragraph of prose", 12)], []]
    assert _apply(tmp_path, pdf, "just a paragraph of prose", "") == ["just a paragraph of prose", ""]


def test_table_only_page_keeps_its_rows_and_is_not_repaired(tmp_path: Path):
    pdf = [[Line("column one column two", 12)]]
    table = "| columnone | columntwo |\n| --- | --- |\n| 1 | 2 |"
    assert _apply(tmp_path, pdf, table) == [table]


def test_glued_words_are_repaired_from_the_pdf_words(tmp_path: Path):
    pdf = [[Line("when judges move they move from the right answer", 12)]]
    [page] = _apply(tmp_path, pdf, "whenjudgesmove they move fromthe rightanswer, rarely")
    assert page == "when judges move they move from the right answer, rarely"


def test_a_token_that_is_a_word_of_the_page_or_not_made_of_its_words_is_left_alone(tmp_path: Path):
    pdf = [[Line("judges move on", 12)]]
    [page] = _apply(tmp_path, pdf, "judges moveonward unknownword judgesmoveon")
    assert page == "judges moveonward unknownword judges move on"  # three words are joined; the others stay


def test_tables_code_images_and_comments_are_not_repaired(tmp_path: Path):
    pdf = [[Line("judges move on", 12)]]
    markdown = (
        "| judgesmove |\n```\njudgesmove\n# judgesmove\n```\n"
        "![judgesmove](images/p1.fig0.png)\n<!-- judgesmove -->\njudgesmove"
    )
    [page] = _apply(tmp_path, pdf, markdown)
    assert page.split("\n")[:-1] == markdown.split("\n")[:-1]
    assert page.split("\n")[-1] == "judges move"


def test_a_hash_line_inside_a_code_fence_is_not_a_heading(tmp_path: Path):
    pdf = [[Line("Real Heading", 20), Line("comment", 12)]]
    [page] = _apply(tmp_path, pdf, "## Real Heading\n\n```\n# comment\n```")
    assert page == "# Real Heading\n\n```\n# comment\n```"


def test_glue_repair_also_fixes_heading_text_and_keeps_the_new_level(tmp_path: Path):
    pdf = [[Line("Wiggle Framework Overview", 20), Line("body", 12)]]
    [page] = _apply(tmp_path, pdf, "## WiggleFramework Overview\n\nbody")
    assert page == "# Wiggle Framework Overview\n\nbody"


def test_figures_boxes_and_page_numbers_are_kept_and_the_input_is_not_modified(tmp_path: Path):
    path = write_pdf(tmp_path / "doc.pdf", [[Line("A Title", 20), Line("body", 12)]])
    box = PDFTableBox(x0=1, y0=2, x1=3, y1=4)
    document = PDFDocument(
        source_path=path,
        pages=[PDFPageContent(page_number=1, markdown="## A Title\n\nbody", figures=[], tables=[box])],
    )
    result = PdfStructure(path).apply(document)
    assert document.pages[0].markdown == "## A Title\n\nbody"
    assert result.pages[0].markdown == "# A Title\n\nbody"
    assert (result.pages[0].page_number, result.pages[0].tables) == (1, [box])
    assert result.source_path == path


def test_a_page_number_outside_the_pdf_is_left_alone(tmp_path: Path):
    path = write_pdf(tmp_path / "doc.pdf", [[Line("A Title", 20)]])
    document = PDFDocument(
        source_path=path,
        pages=[
            PDFPageContent(page_number=1, markdown="## A Title", figures=[]),
            PDFPageContent(page_number=9, markdown="## Elsewhere", figures=[]),
        ],
    )
    assert [p.markdown for p in PdfStructure(path).apply(document).pages] == ["# A Title", "## Elsewhere"]


def test_the_result_is_deterministic(tmp_path: Path):
    pdf = [[Line("Title", 24), Line("Chapter", 18), Line("glued words here", 12)]]
    markdown = "## Title\n\n## Chapter\n\ngluedwords here"
    assert _apply(tmp_path, pdf, markdown) == _apply(tmp_path, pdf, markdown)


def test_line_break_hyphens_in_a_real_pdf_are_kept_or_restored(tmp_path: Path):
    # pdfium marks the two breaks; "database" is written whole on the page too (a soft hyphen: stays joined),
    # "timestamp" is not, but "time" and "stamp" are (a compound: the hyphen comes back); "fromthe" is a glue.
    pdf = [
        [
            Line("The service writes to the data-", 12),
            Line("base every night and the time-", 12),
            Line("stamp is kept from the start; the database is small, every stamp counts in time.", 12),
        ]
    ]
    markdown = "The service writes to the database every night and the timestamp is kept fromthe start."
    [page] = _apply(tmp_path, pdf, markdown)
    assert page == "The service writes to the database every night and the time-stamp is kept from the start."


def test_a_wrapped_requirement_id_in_a_table_cell_gets_its_hyphen_back(tmp_path: Path):
    pdf = [[Line("ID Requirement", 12), Line("FR-", 12), Line("EXT-001 The export is signed.", 12)]]
    table = "| ID | Requirement |\n| --- | --- |\n| FR- EXT-001 | The export is signed. |"
    [page] = _apply(tmp_path, pdf, table)
    assert page == "| ID | Requirement |\n| --- | --- |\n| FR-EXT-001 | The export is signed. |"
