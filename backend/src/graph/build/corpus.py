"""Corpus text rules of the Plan step: token estimate, masked lines, segmentation (build.md Plan 1-3).

Pure functions over markdown; no I/O. Line numbers are 1-based and count every line of the revision
(``markdown.split("\\n")``, a trailing ``\\r`` dropped), masked lines included (tech-specs 5.4).

Segmentation walks the heading tree: a heading subtree within ``fragment_tokens.target`` is one
fragment; a larger one is split into its own text and its children, and pieces below
``fragment_tokens.min`` merge with their neighbours under the same parent while the result stays within
``fragment_tokens.max``. A section whose own text is over ``max`` is cut at the paragraph boundary whose
content hash is lowest within the allowed window, so the cut depends on the content around it and an
edit elsewhere does not move it. Fenced code blocks are never split.
"""

from __future__ import annotations

import bisect
import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

from graph.build.models import FragmentRec
from graph.build.options import BuildOptions
from graph.build.ports import CorpusRevision

# ceil(len / 3.6) in integer arithmetic: len / 3.6 == len * 5 / 18.
_TOKEN_NUMERATOR: Final = 5
_TOKEN_DENOMINATOR: Final = 18
ID_HEX_CHARS: Final = 8
SLUG_PART_MAX_CHARS: Final = 48
EMPTY_PATH_SLUG: Final = "_"
EMPTY_PART_SLUG: Final = "section"

_FENCE_OPEN: Final = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_FENCE_CLOSE: Final = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*$")
_HEADING: Final = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*$")
_CLOSING_HASHES: Final = re.compile(r"(?:^|[ \t]+)#+$")
_SLUG_SEPARATORS: Final = re.compile(r"[\W_]+")
_COMMENT_OPEN: Final = "<!--"
_COMMENT_CLOSE: Final = "-->"


def estimate_tokens(text: str) -> int:
    """Estimated tokens of ``text``: ``ceil(len(text) / 3.6)`` (tech-specs 5.4)."""

    return -(-len(text) * _TOKEN_NUMERATOR // _TOKEN_DENOMINATOR)


def split_lines(markdown: str) -> list[str]:
    """The lines of a revision as numbered by the build (index ``i`` is line ``i + 1``)."""

    return [line.removesuffix("\r") for line in markdown.split("\n")]


def masked_lines(markdown: str) -> frozenset[int]:
    """Line numbers (1-based) of ``<!-- ... -->`` blocks: parser comment lines (build.md Plan 1).

    A comment block opens only where a line outside fenced code starts with ``<!--`` after optional
    indentation, and closes at the first ``-->`` after it. A ``<!--`` mid-line (inline code
    included) never opens one. Every line of the block is masked except a closing line with text
    after ``-->``, which is kept whole. An unclosed ``<!--`` is not a comment. Lines of fenced code
    blocks are code, never masked.
    """

    return _Layout.of(split_lines(markdown)).masked


def first_heading(markdown: str) -> str | None:
    """The text of the first heading outside code blocks and comments, or None."""

    layout = _Layout.of(split_lines(markdown))
    for number in sorted(layout.headings):
        title = layout.headings[number][1]
        if title:
            return title
    return None


@dataclass(frozen=True, slots=True)
class Segmentation:
    """The fragments of one revision and how many of its lines were masked as comments."""

    fragments: list[FragmentRec]
    masked_lines: int


def segment(revision: CorpusRevision, options: BuildOptions) -> list[FragmentRec]:
    """Fragments of one revision in document order (build.md Plan 3; data-model.md "Fragment").

    Deterministic: the same markdown and options give the same fragments and ids. A revision without
    unmasked content gives no fragment.
    """

    return segment_revision(revision, options).fragments


def segment_revision(revision: CorpusRevision, options: BuildOptions) -> Segmentation:
    """``segment`` plus the number of masked comment lines of the revision."""

    lines = split_lines(revision.markdown)
    layout = _Layout.of(lines)
    root = _section_tree(layout)
    packer = _Packer(layout, options)
    pieces = packer.pack(root)
    paths = _line_paths(root, len(lines))
    fragments: list[FragmentRec] = []
    seen: dict[str, int] = {}
    for piece_start, piece_end in pieces:
        trimmed = layout.trim(piece_start, piece_end)
        if trimmed is None:
            continue
        start, end = trimmed
        heading_path = _common_prefix([paths[number] for number in range(start, end + 1)])
        base = "#".join((revision.lineage_id, _path_slug(heading_path), layout.anchor_hex(start, end)))
        seen[base] = seen.get(base, 0) + 1
        fragment_id = base if seen[base] == 1 else f"{base}~{seen[base]}"
        text = "\n".join(layout.visible(number) for number in range(start, end + 1))
        fragments.append(
            FragmentRec(
                fragment_id=fragment_id,
                document_id=revision.lineage_id,
                heading_path=heading_path,
                ordinal=len(fragments),
                line_start=start,
                line_end=end,
                text=text,
                content_hash=layout.content_hash(start, end),
                tokens=estimate_tokens(text),
            )
        )
    return Segmentation(fragments=fragments, masked_lines=len(layout.masked))


# Layout: fences, comments, headings and line sizes of one revision


def _hex(text: str, chars: int = ID_HEX_CHARS) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:chars]


@dataclass(frozen=True)
class _Layout:
    lines: list[str]
    # Line number -> (start, end) of the fenced block holding it (delimiters included).
    fence_of: dict[int, tuple[int, int]]
    masked: frozenset[int]
    # Line number -> (level, text) of an ATX heading.
    headings: dict[int, tuple[int, str]]
    # cumulative[i]: characters of lines 1..i as text, each line + 1 newline, masked lines empty.
    cumulative: list[int]

    @classmethod
    def of(cls, lines: list[str]) -> _Layout:
        fence_of = _fences(lines)
        masked = _comment_lines(lines, fence_of)
        headings: dict[int, tuple[int, str]] = {}
        for number, line in enumerate(lines, start=1):
            if number in fence_of or number in masked:
                continue
            match = _HEADING.match(line)
            if match:
                title = _CLOSING_HASHES.sub("", (match.group(2) or "").strip()).strip()
                headings[number] = (len(match.group(1)), title)
        cumulative = [0]
        for number, line in enumerate(lines, start=1):
            cumulative.append(cumulative[-1] + (0 if number in masked else len(line)) + 1)
        return cls(lines, fence_of, masked, headings, cumulative)

    def visible(self, number: int) -> str:
        return "" if number in self.masked else self.lines[number - 1]

    def has_content(self, number: int) -> bool:
        return number not in self.masked and self.lines[number - 1].strip() != ""

    def tokens(self, start: int, end: int) -> int:
        """Estimated tokens of the text of lines ``start..end`` (masked lines blank)."""

        if end < start:
            return 0
        chars = self.cumulative[end] - self.cumulative[start - 1] - 1
        return -(-chars * _TOKEN_NUMERATOR // _TOKEN_DENOMINATOR)

    def trim(self, start: int, end: int) -> tuple[int, int] | None:
        """``start..end`` without blank or masked lines at either end; None when nothing is left."""

        while start <= end and not self.has_content(start):
            start += 1
        while end >= start and not self.has_content(end):
            end -= 1
        return (start, end) if start <= end else None

    def splits_fence(self, number: int) -> bool:
        """A cut before line ``number`` would split a fenced code block."""

        block = self.fence_of.get(number)
        return block is not None and block[0] < number

    def anchor_hex(self, start: int, end: int) -> str:
        """8 hex of the first unmasked body line (else the first unmasked line) of ``start..end``."""

        first_content = None
        for number in range(start, end + 1):
            if not self.has_content(number):
                continue
            if first_content is None:
                first_content = number
            if number not in self.headings:
                return _hex(self.lines[number - 1].strip())
        return _hex(self.lines[first_content - 1].strip() if first_content is not None else "")

    def content_hash(self, start: int, end: int) -> str:
        """sha256 of the non-blank unmasked lines of ``start..end`` (trailing whitespace dropped): masked
        and blank lines are left out, so moving a page marker or a blank line changes nothing."""

        kept = [self.lines[number - 1].rstrip() for number in range(start, end + 1) if self.has_content(number)]
        return hashlib.sha256("\n".join(kept).encode("utf-8")).hexdigest()


def _fences(lines: Sequence[str]) -> dict[int, tuple[int, int]]:
    blocks: list[tuple[int, int]] = []
    number = 1
    while number <= len(lines):
        match = _FENCE_OPEN.match(lines[number - 1])
        if match is None or (match.group(1)[0] == "`" and "`" in match.group(2)):
            number += 1
            continue
        marker = match.group(1)
        close = len(lines)
        for candidate in range(number + 1, len(lines) + 1):
            closing = _FENCE_CLOSE.match(lines[candidate - 1])
            if closing and closing.group(1)[0] == marker[0] and len(closing.group(1)) >= len(marker):
                close = candidate
                break
        blocks.append((number, close))
        number = close + 1
    return {line: block for block in blocks for line in range(block[0], block[1] + 1)}


def _comment_lines(lines: Sequence[str], fence_of: dict[int, tuple[int, int]]) -> frozenset[int]:
    masked: set[int] = set()
    count = len(lines)
    number = 1
    while number <= count:
        line = lines[number - 1]
        body = line.lstrip()
        if number in fence_of or not body.startswith(_COMMENT_OPEN):
            number += 1
            continue
        close_line = number
        close_at = line.find(_COMMENT_CLOSE, len(line) - len(body) + len(_COMMENT_OPEN))
        while close_at == -1 and close_line < count:
            close_line += 1
            close_at = lines[close_line - 1].find(_COMMENT_CLOSE)
        if close_at == -1:
            break  # unclosed: no ``-->`` follows, so no later opening closes either
        tail_blank = not lines[close_line - 1][close_at + len(_COMMENT_CLOSE) :].strip()
        last = close_line if tail_blank else close_line - 1
        masked.update(masked_number for masked_number in range(number, last + 1) if masked_number not in fence_of)
        number = close_line + 1
    return frozenset(masked)


# Heading tree


@dataclass
class _Section:
    level: int
    path: tuple[str, ...]
    start: int
    end: int
    children: list[_Section] = field(default_factory=list)

    @property
    def own_end(self) -> int:
        """Last line of the section's own text: the line before its first child's heading."""

        return self.children[0].start - 1 if self.children else self.end


def _section_tree(layout: _Layout) -> _Section:
    root = _Section(level=0, path=(), start=1, end=len(layout.lines))
    stack = [root]
    for number in sorted(layout.headings):
        level, title = layout.headings[number]
        while stack[-1].level >= level:
            closed = stack.pop()
            closed.end = number - 1
        parent = stack[-1]
        section = _Section(level=level, path=(*parent.path, title), start=number, end=len(layout.lines))
        parent.children.append(section)
        stack.append(section)
    return root


def _line_paths(root: _Section, count: int) -> list[tuple[str, ...]]:
    """Heading path of every line (index = line number; index 0 unused)."""

    paths: list[tuple[str, ...]] = [()] * (count + 1)

    def visit(section: _Section) -> None:
        for number in range(section.start, section.own_end + 1):
            paths[number] = section.path
        for child in section.children:
            visit(child)

    visit(root)
    return paths


def _common_prefix(paths: Sequence[tuple[str, ...]]) -> tuple[str, ...]:
    if not paths:
        return ()
    prefix = paths[0]
    for path in paths[1:]:
        size = 0
        while size < min(len(prefix), len(path)) and prefix[size] == path[size]:
            size += 1
        prefix = prefix[:size]
    return prefix


def _path_slug(path: tuple[str, ...]) -> str:
    if not path:
        return EMPTY_PATH_SLUG
    parts = []
    for part in path:
        slug = _SLUG_SEPARATORS.sub("-", part.casefold()).strip("-")[:SLUG_PART_MAX_CHARS].strip("-")
        parts.append(slug or EMPTY_PART_SLUG)
    return "/".join(parts)


# Packing


Piece = tuple[int, int]


class _Packer:
    def __init__(self, layout: _Layout, options: BuildOptions) -> None:
        self._layout = layout
        self._min = options.fragment_tokens.min
        self._target = options.fragment_tokens.target
        self._max = options.fragment_tokens.max

    def _tokens(self, piece: Piece) -> int:
        return self._layout.tokens(*piece)

    def pack(self, section: _Section) -> list[Piece]:
        whole = (section.start, section.end)
        if section.end < section.start:
            return []
        total = self._tokens(whole)
        if total <= self._target or (not section.children and total <= self._max):
            return [whole]
        pieces: list[Piece] = []
        own = (section.start, section.own_end)
        if own[1] >= own[0]:
            pieces.extend(self.cut(own) if self._tokens(own) > self._max else [own])
        for child in section.children:
            pieces.extend(self.pack(child))
        return self.merge_small(pieces)

    def merge_small(self, pieces: Sequence[Piece]) -> list[Piece]:
        """Merge a piece below ``min`` into the next one (the last one into the previous) within ``max``."""

        merged: list[Piece] = []
        for piece in pieces:
            if merged and self._tokens(merged[-1]) < self._min:
                candidate = (merged[-1][0], piece[1])
                if self._tokens(candidate) <= self._max:
                    merged[-1] = candidate
                    continue
            merged.append(piece)
        if len(merged) > 1 and self._tokens(merged[-1]) < self._min:
            candidate = (merged[-2][0], merged[-1][1])
            if self._tokens(candidate) <= self._max:
                merged[-2:] = [candidate]
        return merged

    def cut(self, piece: Piece) -> list[Piece]:
        """Cut a long section's own text at content-chosen boundaries (tech-specs 5.4 "Fragment cut")."""

        start, end = piece
        out: list[Piece] = []
        # A paragraph's start and hash depend only on the lines from it to ``end``, so both lists are
        # computed once for the piece and narrowed to the lines after ``start`` at each cut.
        all_paragraphs = self._paragraph_starts(start, end)
        paragraph_numbers = [number for number, _ in all_paragraphs]
        all_lines: list[tuple[int, str]] | None = None
        while self._tokens((start, end)) > self._max:
            paragraphs = all_paragraphs[bisect.bisect_right(paragraph_numbers, start) :]
            boundary = self._boundary(start, end, paragraphs)
            if boundary is None:
                if all_lines is None:
                    all_lines = self._line_starts(start, end)
                lines = all_lines[bisect.bisect_right(all_lines, (start, "\uffff")) :]
                boundary = self._boundary(start, end, lines)
            if boundary is None:
                # Only an unsplittable block (a long code block) is over max: end the piece right after it.
                boundary = next(
                    (number for number, _ in paragraphs if self._tokens((start, number - 1)) > self._max), None
                )
            if boundary is None:
                break
            out.append((start, boundary - 1))
            start = boundary
        out.append((start, end))
        return out

    def _boundary(self, start: int, end: int, candidates: Sequence[tuple[int, str]]) -> int | None:
        """The candidate with the lowest hash among those leaving both sides within range."""

        allowed = [
            (digest, number)
            for number, digest in candidates
            if self._min <= self._tokens((start, number - 1)) <= self._max and self._tokens((number, end)) >= self._min
        ]
        return min(allowed)[1] if allowed else None

    def _paragraph_starts(self, start: int, end: int) -> list[tuple[int, str]]:
        layout = self._layout
        found = []
        for number in range(start + 1, end + 1):
            if layout.has_content(number) and not layout.has_content(number - 1) and not layout.splits_fence(number):
                paragraph = []
                for line in range(number, end + 1):
                    if not layout.has_content(line) and not layout.splits_fence(line):
                        break
                    paragraph.append(layout.visible(line))
                found.append((number, _hex("\n".join(paragraph))))
        return found

    def _line_starts(self, start: int, end: int) -> list[tuple[int, str]]:
        layout = self._layout
        return [
            (number, _hex(layout.lines[number - 1]))
            for number in range(start + 1, end + 1)
            if layout.has_content(number) and not layout.splits_fence(number)
        ]
