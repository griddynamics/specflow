"""``PdfStructure``: the two SpecFlow repairs of Docling's PDF Markdown (DESIGN 'Post-processing').

Docling gives every heading the same level and sometimes glues words (``judgesmove``). Both are
repaired against the text layer of the PDF read with pdfium:

1. Heading levels. Each heading's text is looked up in the page's pdfium text and its effective font
   size is read (nominal size x text-matrix scale). The distinct sizes of the whole document, in
   0.5 pt buckets, are ranked: the largest is ``#``, the next ``##``, down to ``######``. A heading
   whose text is not found takes the level of the numbered headings of the same depth, else
   ``depth + 1`` (``1 Intro`` is ``##``); an unnumbered one keeps the level Docling gave it.
2. Glued words. A prose token that is not a word of the page, but equals two or three consecutive
   words of the page, gets the spaced form. Tables, code, comments and image lines are left alone.

Ranking needs every page, so ``apply`` runs once on the whole document, after the last range. It is
deterministic and uses no model (about 2 s for 216 pages). Bump ``POST_PROCESSING_VERSION``
(``file_parsers.engines.docling``) when a change here can change the output.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from file_parsers.pdf.pdfium_typed import PageText, PdfiumDocument, open_document
from file_parsers.pdf.schema import PDFDocument
from logger.logger import INFO, get_logger, log_event

logger = get_logger("file_parsers.pdf.structure")

MAX_LEVEL = 6
_BUCKET = 2  # sizes are rounded to 1/_BUCKET pt
_HEADING = re.compile(r"^(?P<hashes>#{1,6})[ \t]+(?P<text>\S.*?)[ \t]*$")
_NUMBERING = re.compile(r"^(?:(?:appendix|section|chapter)\s+)?(?P<number>\d+(?:\.\d+)*)[.)]?(?:\s|$)", re.IGNORECASE)
_FENCE = re.compile(r"^\s*(```|~~~)")
_TOKEN = re.compile(r"[A-Za-z]{4,}")
_WORD = re.compile(r"[A-Za-z]+")
_MARKUP = re.compile(r"[*_`]")
_NEEDLE_MIN, _NEEDLE_MAX = 8, 40
_MIN_JOINED_WORDS, _MAX_JOINED_WORDS = 2, 3


@dataclass(slots=True)
class _Heading:
    """A heading line of one page: its line index, Docling's level and the measured size."""

    line: int
    level: int
    text: str
    size: float | None = None


@dataclass(slots=True)
class _Page:
    lines: list[str]
    headings: list[_Heading] = field(default_factory=list)


def _compact(text: str) -> tuple[str, list[int]]:
    """Lowercase alphanumerics of ``text`` and the index of each in ``text``."""

    chars: list[str] = []
    positions: list[int] = []
    for position, char in enumerate(text):
        if char.isalnum():
            chars.append(char.lower())
            positions.append(position)
    return "".join(chars), positions


class _PageIndex:
    """The compacted pdfium text of one page, searched for heading texts."""

    def __init__(self, page: PageText) -> None:
        self.sizes = page.sizes
        self.haystack, self.positions = _compact(page.text)

    def heading_size(self, heading_text: str) -> float | None:
        needle, _ = _compact(_MARKUP.sub("", heading_text))
        if not needle or not self.sizes:
            return None
        start = self.haystack.find(needle[: max(_NEEDLE_MIN, min(len(needle), _NEEDLE_MAX))])
        if start < 0:
            return None
        end = min(start + len(needle), len(self.positions))
        measured = [self.sizes[self.positions[j]] for j in range(start, end) if self.positions[j] < len(self.sizes)]
        return max(measured) if measured else None


def _scan(markdown: str) -> _Page:
    """Split a page into lines and find its heading lines (not those inside code fences)."""

    page = _Page(lines=markdown.split("\n"))
    in_fence = False
    for number, line in enumerate(page.lines):
        if _FENCE.match(line):
            in_fence = not in_fence
        elif not in_fence and (match := _HEADING.match(line)):
            page.headings.append(_Heading(number, len(match["hashes"]), match["text"]))
    return page


def _bucket(size: float) -> float:
    return round(size * _BUCKET) / _BUCKET


def _numbering_depth(text: str) -> int | None:
    match = _NUMBERING.match(text)
    return match["number"].count(".") + 1 if match else None


def _repair_glued(lines: list[str], page: PageText) -> int:
    """Insert the missing spaces in prose lines, in place; return the number of repairs."""

    words = _WORD.findall(page.text)
    vocabulary = {word.lower() for word in words}
    joined: dict[str, str] = {}
    for size in range(_MIN_JOINED_WORDS, _MAX_JOINED_WORDS + 1):
        for start in range(len(words) - size + 1):
            run = words[start : start + size]
            joined.setdefault("".join(run).lower(), " ".join(run))
    repairs = 0

    def fix(match: re.Match[str]) -> str:
        nonlocal repairs
        token = match[0]
        if token.lower() in vocabulary:
            return token
        spaced = joined.get(token.lower())
        if spaced is not None and spaced.replace(" ", "") == token:
            repairs += 1
            return spaced
        return token

    in_fence = False
    for number, line in enumerate(lines):
        if _FENCE.match(line):
            in_fence = not in_fence
        elif not in_fence and not line.startswith(("|", "![", "<!--")):
            lines[number] = _TOKEN.sub(fix, line)
    return repairs


class PdfStructure:
    """Repairs the heading levels and the glued words of a parsed PDF, using the PDF's own text layer."""

    def __init__(self, pdf_path: Path) -> None:
        self.pdf_path = pdf_path

    def apply(self, document: PDFDocument) -> PDFDocument:
        """Return ``document`` with repaired page Markdown (figures, boxes and numbers unchanged)."""

        scanned: list[_Page] = []
        repairs = 0
        with open_document(self.pdf_path) as pdf:
            for page in document.pages:
                parsed = _scan(page.markdown)
                text = self._page_text(pdf, page.page_number, with_sizes=bool(parsed.headings))
                index = _PageIndex(text)
                for heading in parsed.headings:
                    heading.size = index.heading_size(heading.text)
                repairs += _repair_glued(parsed.lines, text)
                scanned.append(parsed)
        ranking = _Ranking(scanned)
        pages = []
        for page, parsed in zip(document.pages, scanned, strict=True):
            for heading in parsed.headings:
                heading.level = ranking.level(heading)
                parsed.lines[heading.line] = _rewrite(parsed.lines[heading.line], heading.level)
            pages.append(page.model_copy(update={"markdown": "\n".join(parsed.lines)}))
        headings = [heading for parsed in scanned for heading in parsed.headings]
        log_event(
            logger,
            INFO,
            "pdf.structure.done",
            pages=len(pages),
            headings=len(headings),
            headings_by_size=sum(heading.size is not None for heading in headings),
            glue_repairs=repairs,
        )
        return document.model_copy(update={"pages": pages})

    @staticmethod
    def _page_text(pdf: PdfiumDocument, page_number: int, *, with_sizes: bool) -> PageText:
        if page_number < 1 or page_number > len(pdf):
            return PageText("", [])
        return pdf.page_text(page_number - 1, with_sizes=with_sizes)


class _Ranking:
    """Maps a heading to its level from the sizes of the whole document."""

    def __init__(self, pages: list[_Page]) -> None:
        headings = [heading for page in pages for heading in page.headings]
        ranked = sorted({_bucket(h.size) for h in headings if h.size}, reverse=True)
        self._level_of_size = {size: min(MAX_LEVEL, rank + 1) for rank, size in enumerate(ranked)}
        self._by_depth: dict[int, Counter[int]] = {}
        for heading in headings:
            depth = _numbering_depth(heading.text)
            if heading.size and depth is not None:
                self._by_depth.setdefault(depth, Counter())[self._level_of_size[_bucket(heading.size)]] += 1

    def level(self, heading: _Heading) -> int:
        if heading.size:
            return self._level_of_size[_bucket(heading.size)]
        depth = _numbering_depth(heading.text)
        if depth is None:
            return heading.level
        observed = self._by_depth.get(depth)
        if observed:
            return observed.most_common(1)[0][0]
        return min(MAX_LEVEL, depth + 1)


def _rewrite(line: str, level: int) -> str:
    match = _HEADING.match(line)
    return line if match is None else f"{'#' * level} {match['text']}"
