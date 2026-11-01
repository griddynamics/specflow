"""G1 (fixture quality) and G8 (determinism) of docs/file-parsers/acceptance-gates.md, offline.

``extraction.pdf`` goes through the real ``file_to_markdown`` (Docling FAST, then PdfStructure) and is
scored against the unchanged golden file ``extraction.md`` (D6) with the structural metrics of
``examples/parse_benchmark.py``: the golden file's ``llm-*`` blocks and math spans are removed first, because
its pages 1-5 are pymupdf4llm output and pages 7-8 are LLM LaTeX. The models come from the local Hugging Face
cache or ``DOCLING_ARTIFACTS_PATH`` (tests/conftest.py keeps the hub offline).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from file_parsers.convert import ParsedMarkdown, file_to_markdown
from parse_benchmark import evaluate, headings, tables

SPECS = Path(__file__).parent / "data" / "specs"
FIXTURE = SPECS / "extraction.pdf"
GOLDEN = SPECS / "extraction.md"
PDF = "application/pdf"


@pytest.fixture(scope="module")
def runs() -> tuple[ParsedMarkdown, ParsedMarkdown]:
    return file_to_markdown(FIXTURE, PDF), file_to_markdown(FIXTURE, PDF)


@pytest.fixture(scope="module")
def scores(runs: tuple[ParsedMarkdown, ParsedMarkdown]) -> dict[str, Any]:
    return evaluate(runs[0].markdown, GOLDEN.read_text(encoding="utf-8"))


def test_g1_the_parser_is_docling_with_the_post_processing_version(runs: tuple[ParsedMarkdown, ParsedMarkdown]):
    assert runs[0].parser == "docling@2.145.0+sf1"


def test_g1_all_nine_headings_are_found_at_the_exact_level_and_in_the_right_order(scores: dict[str, Any]):
    assert (scores["headings_expected"], scores["headings_found"], scores["heading_level_exact"]) == (9, 9, 9)
    assert (scores["heading_pairs"], scores["heading_pairs_ordered"]) == (36, 36)


def test_g1_three_tables_with_every_cell_of_the_numeric_ones(scores: dict[str, Any]):
    assert (scores["tables_expected"], scores["tables_found"]) == (3, 3)
    assert scores["cell_recall"] >= 0.95


def test_g1_word_f1_without_the_llm_blocks_and_math(scores: dict[str, Any]):
    assert scores["word_f1"] >= 0.98
    assert scores["word_recall"] >= 0.98


def test_g1_figure_placeholders_are_the_golden_ones_per_page(scores: dict[str, Any]):
    assert scores["figures_per_page"] == scores["expected_figures_per_page"]
    assert sum(scores["figures_per_page"].values()) == 3


def test_g1_one_page_marker_per_page(scores: dict[str, Any], runs: tuple[ParsedMarkdown, ParsedMarkdown]):
    assert (scores["page_markers"], scores["page_markers_expected"]) == (8, 8)
    markers = [line for line in runs[0].markdown.splitlines() if line.startswith("<!-- source-page:")]
    assert markers == [f"<!-- source-page: {n} -->" for n in range(1, 9)]


def test_g1_the_heading_levels_follow_the_font_sizes_not_docling(runs: tuple[ParsedMarkdown, ParsedMarkdown]):
    levels = [level for level, _ in headings(runs[0].markdown)]
    assert len(set(levels)) > 1  # Docling alone gives every heading the same level
    assert min(levels) == 1


def test_g1_the_glued_words_docling_produces_are_repaired(runs: tuple[ParsedMarkdown, ParsedMarkdown]):
    text = runs[0].markdown
    for glued in ("judgesmove", "fromthe"):
        assert glued not in text.lower()
    assert tables(text)  # tables survived the repair


def test_g1_the_stored_markdown_has_no_html_escapes_and_tables_are_not_padded(
    runs: tuple[ParsedMarkdown, ParsedMarkdown],
):
    text = runs[0].markdown
    assert "&lt;" not in text and "&gt;" not in text and "&amp;" not in text
    separators = [line for line in text.splitlines() if line.startswith("| - |")]
    assert separators and all(set(line) <= set("| -") for line in separators)  # compact_tables: `| - | - |`


def test_g8_two_runs_give_byte_identical_markdown(runs: tuple[ParsedMarkdown, ParsedMarkdown]):
    """Two runs in one process (shared engine and loaded models); a cold start is not covered."""

    first, second = runs
    assert first.markdown == second.markdown
    assert hashlib.sha256(first.markdown.encode()).digest() == hashlib.sha256(second.markdown.encode()).digest()
    assert first.parser == second.parser


def test_the_conversion_leaves_nothing_behind(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    file_to_markdown(FIXTURE, PDF)
    # torch may create its own cache folder (torchinductor_*) in the temp dir: only ours must be gone
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith("specflow-")] == []
