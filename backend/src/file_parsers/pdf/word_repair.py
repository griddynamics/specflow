"""The word repairs of Docling's PDF Markdown, checked against the pdfium text layer of the same page.

Docling handles a word broken across lines in two ways, and sometimes glues words. pdfium marks a hyphen at a
line break with U+FFFE (or keeps ``-`` and the line break), so the page text tells which words were broken, and
how many times: each break of a page is evidence for one repair only (a page with one ``pre-`` break joins one
``pre- and``, not every one).

1. Hyphen and space. Docling writes ``FR- EXT-001``, ``read- only`` for ``FR-EXT-001``, ``read-only`` (almost
   always in a table cell whose text wraps). A ``left- right`` whose two sides meet at a line-break hyphen on
   the page becomes ``left-right``, in prose, headings and table rows alike, never inside a backtick span or an
   indented code line.
2. Joined compounds. Docling sometimes joins the two sides and drops the hyphen (``thirdparty``,
   ``URLencoded``). A token that is exactly such a join gets the hyphen back only when both parts are words
   written whole elsewhere in the document's text layer, each at least 3 letters long (identifier-like parts,
   all capitals such as ``FR``/``GRA`` or a mixed-case token such as ``URLencoded``, may be shorter), and the
   joined word itself is never written whole. Anything else was a syllable break and stays joined
   (``display`` from ``dis-play``, ``database`` with ``database`` elsewhere, ``approximation``).
3. Glued words. Any other prose token that is not a word of the page, but equals two or three consecutive
   words of the page, gets the spaced form (``judgesmove`` -> ``judges move``).

Only repair 1 touches table rows; code, comments and image lines are left alone; headings are prose.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass

_TOKEN = re.compile(r"[A-Za-z]{4,}")
_WORD = re.compile(r"[A-Za-z]+")
_FENCE = re.compile(r"^\s*(```|~~~)")
_MIN_JOINED_WORDS, _MAX_JOINED_WORDS = 2, 3
# A hyphen at a line break: pdfium writes "-" and a line break, or its own marker U+FFFE (a soft hyphen, U+00AD,
# also counts).
_HYPHEN_BREAK = re.compile(r"-[ \t]*[\r\n]+\s*|[\ufffe\u00ad][ \t]*[\r\n]*[ \t]*")
_BROKEN_WORD = re.compile(rf"[A-Za-z0-9]*(?:{_HYPHEN_BREAK.pattern})[A-Za-z0-9]*")
_RUN_BEFORE = re.compile(r"[A-Za-z0-9]+$")
_RUN_AFTER = re.compile(r"[A-Za-z0-9]+")
_RUN_LOOKBACK = 64
_HYPHEN_SPACE = re.compile(r"\b([A-Za-z0-9]+)- (?=([A-Za-z0-9]+))")  # right side not consumed: FR- TODO- 001
_TABLE_ROW = "|"
_CODE_SPAN = re.compile(r"(`+)(?:(?!\1).)+?\1")
_INDENTED_CODE = re.compile(r"^(?: {4}|\t)(?![ \t]*(?:[-*+]|\d+[.)])[ \t])")  # not an indented list item
_MIN_PART_LETTERS = 3
_NOT_PROSE = ("![", "<!--")


@dataclass(slots=True)
class WordRepairs:
    """How many of each repair one page (or a document) got."""

    glue: int = 0
    hyphen_spaces: int = 0
    hyphens_restored: int = 0
    soft_hyphens_kept: int = 0

    def add(self, other: WordRepairs) -> None:
        self.glue += other.glue
        self.hyphen_spaces += other.hyphen_spaces
        self.hyphens_restored += other.hyphens_restored
        self.soft_hyphens_kept += other.soft_hyphens_kept


def document_words(texts: Iterable[str]) -> frozenset[str]:
    """The lower-case words of the text layer written whole: the two sides of a line-break hyphen are left out."""

    return frozenset(word.lower() for text in texts for word in _WORD.findall(_BROKEN_WORD.sub(" ", text)))


def line_break_hyphens(text: str) -> Counter[tuple[str, str]]:
    """How many times each ``(left, right)`` pair of lower-case runs meets at a line-break hyphen in ``text``."""

    pairs: Counter[tuple[str, str]] = Counter()
    for match in _HYPHEN_BREAK.finditer(text):
        left = _RUN_BEFORE.search(text, max(0, match.start() - _RUN_LOOKBACK), match.start())
        right = _RUN_AFTER.match(text, match.end())
        if left is not None and right is not None:
            pairs[(left[0].lower(), right[0].lower())] += 1
    return pairs


def _outside_code_spans(line: str, repair: Callable[[str], str]) -> str:
    """``repair`` applied to the parts of ``line`` outside backtick code spans."""

    parts: list[str] = []
    end = 0
    for span in _CODE_SPAN.finditer(line):
        parts += [repair(line[end : span.start()]), span[0]]
        end = span.end()
    return "".join([*parts, repair(line[end:])])


def repair_words(lines: list[str], page_text: str, words_of_document: frozenset[str]) -> WordRepairs:
    """Apply the three repairs to the prose lines of one page (repair 1 to its table rows too), in place."""

    page = _PageRepair(page_text, words_of_document)
    in_fence = False
    for number, line in enumerate(lines):
        if _FENCE.match(line):
            in_fence = not in_fence
        elif in_fence:
            continue
        elif line.startswith(_TABLE_ROW):  # a wrapped cell: Docling joins its lines with a space
            lines[number] = page.hyphen_spaces(line)
        elif _INDENTED_CODE.match(line):
            lines[number] = _TOKEN.sub(page.token, line)
        elif not line.startswith(_NOT_PROSE):
            lines[number] = _TOKEN.sub(page.token, page.hyphen_spaces(line))
    return page.counts


class _PageRepair:
    """What one page's text layer says: its line-break hyphens (each usable once), its words, its 2-3 word runs."""

    def __init__(self, page_text: str, words_of_document: frozenset[str]) -> None:
        self.counts = WordRepairs()
        self._document = words_of_document
        self._breaks = line_break_hyphens(page_text)
        self._joins = {
            left + right: (left, right) for left, right in self._breaks if left.isalpha() and right.isalpha()
        }
        words = _WORD.findall(page_text)
        self._vocabulary = {word.lower() for word in words}
        self._glued: dict[str, str] = {}
        for size in range(_MIN_JOINED_WORDS, _MAX_JOINED_WORDS + 1):
            for start in range(len(words) - size + 1):
                run = words[start : start + size]
                self._glued.setdefault("".join(run).lower(), " ".join(run))

    def hyphen_spaces(self, line: str) -> str:
        return _outside_code_spans(line, lambda part: _HYPHEN_SPACE.sub(self._hyphen_space, part))

    def _take(self, pair: tuple[str, str]) -> bool:
        """Use one line-break hyphen of ``pair``; ``False`` when the page has none left."""

        if self._breaks[pair] <= 0:
            return False
        self._breaks[pair] -= 1
        return True

    def _hyphen_space(self, match: re.Match[str]) -> str:
        if not self._take((match[1].lower(), match[2].lower())):
            return match[0]
        self.counts.hyphen_spaces += 1
        return f"{match[1]}-"

    def token(self, match: re.Match[str]) -> str:
        text = match[0]
        lower = text.lower()
        pair = self._joins.get(lower)
        if pair is not None and self._take(pair):
            if self._is_compound(text, pair):
                self.counts.hyphens_restored += 1
                split = len(pair[0])
                return f"{text[:split]}-{text[split:]}"
            self.counts.soft_hyphens_kept += 1
            return text
        if lower in self._vocabulary or lower in self._joins:
            return text
        spaced = self._glued.get(lower)
        if spaced is not None and spaced.replace(" ", "") == text:
            self.counts.glue += 1
            return spaced
        return text

    def _is_compound(self, text: str, pair: tuple[str, str]) -> bool:
        """Both parts are whole words of the document, long enough, and the joined word never is."""

        left, right = pair
        if text.lower() in self._document or left not in self._document or right not in self._document:
            return False
        head, tail = text[: len(left)], text[len(left) :]
        identifier = (head.isupper() and tail.isupper()) or not (text.islower() or text.istitle() or text.isupper())
        return identifier or min(len(left), len(right)) >= _MIN_PART_LETTERS
