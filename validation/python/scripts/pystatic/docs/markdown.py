"""Thin adapter over `markdown-it-py`'s syntax tree (GFM-like preset): headings, tables, fences
and links with 1-based line numbers. Shape only, no meaning."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from markdown_it import MarkdownIt
from markdown_it.tree import SyntaxTreeNode

_MD = MarkdownIt("gfm-like")
_ANCHOR_PUNCTUATION_RE = re.compile(r"[^\w\s-]")
# One hyphen per whitespace character (GitHub heading-anchor parity), never a collapsed run.
_ANCHOR_WHITESPACE_RE = re.compile(r"\s")
_NUMBERED_HEADING_RE = re.compile(r"^\s*(?:\d+[\.\)]|\d+(?:\.\d+)+[\.\)]?)\s*")


@dataclass
class Heading:
    level: int
    title: str
    line: int
    end: int  # exclusive: the next heading at a level <= this one, or len(lines) + 1


@dataclass
class Table:
    header: list[str]
    rows: list[list[str]]
    row_lines: list[int]
    line: int


@dataclass
class Fence:
    lang: str
    start: int
    end: int


@dataclass
class Link:
    label: str
    target: str
    line: int


@lru_cache(maxsize=64)
def _tree_for_text(text: str) -> SyntaxTreeNode:
    return SyntaxTreeNode(_MD.parse(text))


def _tree(lines: list[str]) -> SyntaxTreeNode:
    return _tree_for_text("\n".join(lines))


def parse_headings(lines: list[str]) -> list[Heading]:
    raw = [
        (int(n.tag[1]), n.map[0] + 1, (n.children[0].content if n.children else "").strip())
        for n in _tree(lines).walk(include_self=False)
        if n.type == "heading" and n.map is not None
    ]
    heads = [Heading(level=lvl, title=title, line=ln, end=len(lines) + 1) for lvl, ln, title in raw]
    for idx, head in enumerate(heads):
        nxt = next((n for n in heads[idx + 1 :] if n.level <= head.level), None)
        if nxt:
            head.end = nxt.line
    return heads


def heading_anchor(title: str) -> str:
    """GitHub-style heading slug: lowercase, punctuation dropped, one hyphen per space."""
    text = _ANCHOR_PUNCTUATION_RE.sub("", title.lower())
    return _ANCHOR_WHITESPACE_RE.sub("-", text.strip())


def is_heading_numbered(title: str) -> bool:
    """Whether `title` begins with a numbering pattern ('1. ', '1) ', '1.1 ')."""
    return bool(_NUMBERED_HEADING_RE.match(title))


def find_tables(lines: list[str]) -> list[Table]:
    out: list[Table] = []
    for node in _tree(lines).walk(include_self=False):
        if node.type != "table" or node.map is None:
            continue
        table_line = node.map[0] + 1
        thead = next((c for c in node.children if c.type == "thead"), None)
        tbody = next((c for c in node.children if c.type == "tbody"), None)
        header = [cell.children[0].content for cell in thead.children[0].children] if thead else []
        rows, row_lines = [], []
        for tr in tbody.children if tbody else []:
            rows.append([cell.children[0].content if cell.children else "" for cell in tr.children])
            row_lines.append(tr.map[0] + 1 if tr.map else table_line)
        out.append(Table(header=header, rows=rows, row_lines=row_lines, line=table_line))
    return out


def find_fences(lines: list[str]) -> list[Fence]:
    return [
        Fence(lang=(n.info or "").strip(), start=n.map[0] + 1, end=n.map[1])
        for n in _tree(lines).walk(include_self=False)
        if n.type == "fence" and n.map is not None
    ]


def find_links(lines: list[str]) -> list[Link]:
    out: list[Link] = []
    for inline in _tree(lines).walk(include_self=False):
        if inline.type != "inline" or inline.map is None:
            continue
        line = inline.map[0] + 1
        for child in inline.children:
            if child.type in ("softbreak", "hardbreak"):
                line += 1
            elif child.type == "link":
                label = "".join(
                    g.content for g in child.children if g.type in ("text", "code_inline")
                )
                href = (child.attrs or {}).get("href")
                out.append(Link(label=label, target=str(href) if href else "", line=line))
    return out


def links_under_heading(lines: list[str], heading: Heading) -> list[Link]:
    """The links inside `heading`'s section."""
    return [link for link in find_links(lines) if heading.line <= link.line < heading.end]
