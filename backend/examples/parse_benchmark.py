"""Benchmark of the PDF parser (gates G2, G4, G5 of docs/docling-pdf-parser.md).

Converts one PDF with the production path (``file_to_markdown``: Docling in 10-page ranges, then
``PdfStructure``) and prints the wall time, the peak RSS and, when a ground-truth Markdown is given,
the quality numbers: word recall/precision/F1, headings found and at the exact level, heading-pair
order, tables and table-cell recall. The ground truth may be a golden file with ``llm-*`` blocks
and LaTeX (they are removed) or the plain source Markdown the PDF was rendered from.

    uv run python examples/parse_benchmark.py <pdf> [ground-truth.md] [--output result.md]

Pin the cores (``taskset -c 0,1``) and set ``MALLOC_ARENA_MAX=2`` to reproduce the worker-parse
sizing. The same ``evaluate`` function backs the G1 test (tests/test_pdf_quality.py).
"""

from __future__ import annotations

import argparse
import difflib
import re
import resource
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

from file_parsers.convert import file_to_markdown
from file_parsers.formats import PDF_MEDIA_TYPE

_PAGE = re.compile(r"<!-- source-page: (\d+) -->")
_HEADING = re.compile(r"^(#{1,6})[ \t]+(\S.*?)[ \t]*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_WORD = re.compile(r"[A-Za-z]{2,}")
_MATH_BLOCK = re.compile(r"\$\$.*?\$\$", re.DOTALL)
_MATH_INLINE = re.compile(r"\$[^$\n]+\$")
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)|<!-- image -->")
_TAG = re.compile(r"<[^>]+>")
_CELL_MARKUP = re.compile(r"<[^>]+>|[*_`\\]")
_TABLE_ROW = re.compile(r"^\|.*\|\s*$")
_SEPARATOR_ROW = re.compile(r"^\|(\s*:?-+:?\s*\|)+\s*$")
_PROVENANCE = re.compile(r"<!--(?! source-page).*?-->", re.DOTALL)
_CLOSE_MATCH = 0.85
_ORDER_MAX_WORDS = 20_000

Table = list[list[str]]


def strip_llm(golden: str) -> str:
    """Remove the LLM-written blocks (descriptions, Mermaid) and the provenance comments of a golden file."""

    blocks = golden.split("\n\n")
    kept: list[str] = []
    index = 0
    while index < len(blocks):
        head = blocks[index].strip()
        if head.startswith("<!-- llm-description"):
            index += 2  # the comment, then the one-sentence summary
            if index < len(blocks) and blocks[index].strip().startswith("- "):
                index += 1
            continue
        if head.startswith("<!-- llm-mermaid"):
            index += 1
            if index < len(blocks) and blocks[index].strip().startswith("```mermaid"):
                index += 1
            continue
        kept.append(blocks[index])
        index += 1
    return _PROVENANCE.sub("", "\n\n".join(kept))


def pages(markdown: str) -> dict[int, str]:
    parts = _PAGE.split(markdown)
    return {int(parts[i]): parts[i + 1] for i in range(1, len(parts), 2)}


def words(markdown: str) -> list[str]:
    """Lowercase alphabetic tokens without math, comments, images and HTML tags."""

    for pattern in (_MATH_BLOCK, _MATH_INLINE, _COMMENT, _IMAGE, _TAG):
        markdown = pattern.sub(" ", markdown)
    return [word.lower() for word in _WORD.findall(markdown)]


def headings(markdown: str) -> list[tuple[int, str]]:
    """``(level, normalised text)`` of every heading line outside code fences."""

    found: list[tuple[int, str]] = []
    in_fence = False
    for line in markdown.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
        elif not in_fence and (match := _HEADING.match(line)):
            text = " ".join(_WORD.findall(re.sub(r"[*_`]", "", match[2]))).lower()
            found.append((len(match[1]), text))
    return found


def _cell_text(cell: str) -> str:
    """A table cell without HTML tags, emphasis, code ticks and backslashes, lowercase, spaces collapsed."""

    return " ".join(_CELL_MARKUP.sub("", cell).lower().split())


def tables(markdown: str) -> list[Table]:
    found: list[Table] = []
    current: Table = []
    for line in [*markdown.splitlines(), ""]:
        row = line.strip()
        if _TABLE_ROW.match(row):
            if not _SEPARATOR_ROW.match(row):
                current.append([_cell_text(cell) for cell in row.strip("|").split("|")])
        elif current:
            found.append(current)
            current = []
    return found


def _cells(found: list[Table]) -> Counter[str]:
    return Counter(cell for table in found for row in table for cell in row if cell)


def _word_scores(truth: str, result: str) -> dict[str, float]:
    expected, actual = Counter(words(truth)), Counter(words(result))
    overlap = sum((expected & actual).values())
    recall = overlap / max(1, sum(expected.values()))
    precision = overlap / max(1, sum(actual.values()))
    return {
        "word_recall": recall,
        "word_precision": precision,
        "word_f1": 2 * recall * precision / max(1e-9, recall + precision),
    }


def _heading_scores(truth: str, result: str) -> dict[str, Any]:
    expected, actual = headings(truth), headings(result)
    level_of: dict[str, int] = {}
    for level, text in actual:
        level_of.setdefault(text, level)

    def match(text: str) -> str | None:
        if text in level_of:
            return text
        close = difflib.get_close_matches(text, list(level_of), n=1, cutoff=_CLOSE_MATCH)
        return close[0] if close else None

    pairs = [(level, level_of[m] if (m := match(text)) else None) for level, text in expected]
    found = sum(got is not None for _, got in pairs)
    exact = sum(want == got for want, got in pairs)
    compared = agreed = 0
    for a, (want_a, got_a) in enumerate(pairs):
        for want_b, got_b in pairs[a + 1 :]:
            if got_a is None or got_b is None:
                continue
            compared += 1
            agreed += (want_a > want_b) - (want_a < want_b) == (got_a > got_b) - (got_a < got_b)
    return {
        "headings_expected": len(expected),
        "headings_found": found,
        "heading_level_exact": exact,
        "heading_pairs": compared,
        "heading_pairs_ordered": agreed,
    }


def evaluate(result: str, truth: str) -> dict[str, Any]:
    """Quality of ``result`` against ``truth`` (a golden file with LLM blocks or a plain Markdown source).

    Tables whose cells hold math (LaTeX written by an LLM) are not counted in the cell recall.
    """

    truth = strip_llm(truth)
    truth_tables, result_tables = tables(truth), tables(result)
    plain = [table for table in truth_tables if not any("$" in cell for row in table for cell in row)]
    expected_cells, actual_cells = _cells(plain), _cells(result_tables)
    truth_pages, result_pages = pages(truth), pages(result)
    truth_words, result_words = words(truth), words(result)
    scores: dict[str, Any] = {**_word_scores(truth, result), **_heading_scores(truth, result)}
    scores.update(
        {
            # quadratic: only for small documents
            "order_similarity": difflib.SequenceMatcher(None, truth_words, result_words, autojunk=False).ratio()
            if len(truth_words) < _ORDER_MAX_WORDS
            else None,
            "tables_expected": len(truth_tables),
            "tables_found": len(result_tables),
            "cell_recall": sum((expected_cells & actual_cells).values()) / max(1, sum(expected_cells.values())),
            "figures_per_page": {n: len(_IMAGE.findall(result_pages.get(n, ""))) for n in truth_pages},
            "expected_figures_per_page": {n: len(_IMAGE.findall(text)) for n, text in truth_pages.items()},
            "page_markers": len(result_pages),
            "page_markers_expected": len(truth_pages),
        }
    )
    return scores


def _peak_rss_mb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 * 1024 if sys.platform == "darwin" else 1024)  # bytes on macOS, KiB on Linux


def main() -> int:
    cli = argparse.ArgumentParser(description="Time and quality of the PDF parser on one PDF.")
    cli.add_argument("pdf", type=Path)
    cli.add_argument("truth", nargs="?", type=Path, help="ground-truth Markdown (golden file or source)")
    cli.add_argument("--output", type=Path, help="write the converted Markdown here")
    args = cli.parse_args()

    started = time.perf_counter()
    result = file_to_markdown(args.pdf, PDF_MEDIA_TYPE)
    elapsed = time.perf_counter() - started
    page_count = len(pages(result.markdown))
    print(f"parser: {result.parser}")
    print(f"pages: {page_count}")
    print(f"time_s: {elapsed:.1f} ({elapsed / max(1, page_count):.2f} s/page, models load included)")
    print(f"peak_rss_mb: {_peak_rss_mb():.0f}")
    print(f"markdown_chars: {len(result.markdown)}")
    if args.output:
        args.output.write_text(result.markdown, encoding="utf-8")
    if args.truth:
        for key, value in evaluate(result.markdown, args.truth.read_text(encoding="utf-8")).items():
            print(f"{key}: {round(value, 3) if isinstance(value, float) else value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
