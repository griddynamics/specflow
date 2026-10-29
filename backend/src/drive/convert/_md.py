"""Shared Markdown helpers for the Google file converters (pure, deterministic)."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

# DATA-GDUI-0001: images never leave the converter; these markers must not appear in output.
_SURROGATES = re.compile("[\ud800-\udfff]")
_BLANKS = re.compile(r"\n{3,}")
MAX_DEPTH = 24
_SAFE_SCHEMES = ("http://", "https://", "mailto:")


class ConversionError(ValueError):
    """Fixed-message conversion failure; the import maps it to ``convert_failed``."""


def clean(text: str) -> str:
    """Strip NUL (Postgres ``text`` rejects it) and lone surrogates (not UTF-8 encodable)."""
    return _SURROGATES.sub("\ufffd", text.replace("\x00", ""))


def as_map(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def is_mapping(value: object) -> bool:
    return isinstance(value, Mapping)


def as_list(value: Any) -> Sequence[Any]:
    return value if isinstance(value, list) else []


def one_line(text: str) -> str:
    return " ".join(clean(text).split())


_INLINE = re.compile(r"[\\`*\[\]<]|(?<![A-Za-z0-9])_|_(?![A-Za-z0-9])")
_LEADING = re.compile(r"^(\s*)(?:([#>+\-])|(\d+)([.)]))")
_UNSAFE_URL = re.compile(r"[\x00-\x20\x7f<>\[\]\\]")
_IMAGE_MARKERS = re.compile(r"googleusercontent|contenturi", re.IGNORECASE)


def escape_inline(text: str) -> str:
    """Backslash-escape characters that would change meaning inside a Markdown paragraph."""
    return _INLINE.sub(lambda m: "&lt;" if m.group() == "<" else "\\" + m.group(), text)


def escape_leading(text: str) -> str:
    """Escape a leading block marker (heading, quote, list, ordered list) of a paragraph."""

    def fix(m: re.Match[str]) -> str:
        if m.group(2):
            return m.group(1) + "\\" + m.group(2)
        return m.group(1) + m.group(3) + "\\" + m.group(4)

    return _LEADING.sub(fix, text, count=1)


def escape_text(text: str) -> str:
    """Escape plain text used as a whole paragraph (inline and leading markers)."""
    return escape_leading(escape_inline(text))


def title_of(raw: Any) -> str:
    title = one_line(raw) if isinstance(raw, str) else ""
    return escape_inline(title) if title else "Untitled"


def link_target(url: Any) -> str | None:
    """A link URL safe to write, or None. Image hosts, odd schemes and unsafe characters are dropped."""
    if not isinstance(url, str):
        return None
    cleaned: str = url.strip()
    low = cleaned.lower()
    if not low.startswith(_SAFE_SCHEMES) or _UNSAFE_URL.search(cleaned) or _IMAGE_MARKERS.search(cleaned):
        return None
    return cleaned.replace("(", "%28").replace(")", "%29")


def link(text: str, url: Any) -> str:
    """Escaped run text, wrapped as a Markdown link when the URL is safe."""
    target = link_target(url)
    if target is None or not text.strip():
        return escape_inline(text)
    lead = text[: len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()) :]
    return f"{lead}[{escape_inline(text.strip())}]({target}){trail}"


def cell_text(text: str, *, escape: bool = False) -> str:
    text = clean(text)
    if escape:
        text = escape_inline(text)
    return text.replace("|", "\\|").replace("\r", "").replace("\n", "<br>").strip()


def table_md(rows: Sequence[Sequence[str]], *, escape: bool = False) -> str:
    """Markdown table; the first row is the header. Rows are padded to the widest row.

    ``escape`` is for raw cell text (Sheets); Docs and Slides cells arrive already escaped.
    """
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    if width == 0:
        return ""
    grid = [[cell_text(c, escape=escape) for c in r] + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(grid[0]) + " |", "|" + " --- |" * width]
    lines.extend("| " + " | ".join(r) + " |" for r in grid[1:])
    return "\n".join(lines)


def grid_from_cells(rows: Sequence[Sequence[tuple[str, int, int]]]) -> list[list[str]]:
    """Place ``(text, row_span, col_span)`` cells on a grid honouring merges.

    A cell sits on the next free column; the squares it covers stay empty. A span of 0
    marks a cell merged into another one and is skipped.
    """
    covered: set[tuple[int, int]] = set()
    out: list[list[str]] = []
    for r, row in enumerate(rows):
        line: list[str] = []
        c = 0
        for text, rspan, cspan in row:
            if rspan < 1 or cspan < 1:
                continue
            while (r, c) in covered:
                line.append("")
                c += 1
            line.append(text)
            for dr in range(rspan):
                for dc in range(cspan):
                    if dr or dc:
                        covered.add((r + dr, c + dc))
            c += 1
            for _ in range(cspan - 1):
                line.append("")
                c += 1
        while (r, c) in covered:
            line.append("")
            c += 1
        out.append(line)
    return out


def join_blocks(blocks: Sequence[str]) -> str:
    text = "\n\n".join(b for b in blocks if b and b.strip())
    text = _IMAGE_MARKERS.sub("[image]", clean(text))  # invariant: no image link in any output
    return _BLANKS.sub("\n\n", text).strip("\n") + "\n"
