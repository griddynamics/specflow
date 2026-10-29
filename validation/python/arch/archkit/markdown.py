"""Read Markdown tables for the catalogue build step (`archkit build-catalogue`).

Headings and tables come from `markdown-it-py` tokens, never from a `#` or `|` regex."""

from __future__ import annotations

import re

from markdown_it import MarkdownIt
from markdown_it.tree import SyntaxTreeNode

_MD = MarkdownIt("commonmark").enable("table")
_FRONTMATTER_RE = re.compile(r"\A---\n(.*?\n)---\n", re.DOTALL)


def blank_frontmatter(text: str) -> str:
    """Blank a leading `---` front-matter block, keeping the line count."""
    matched = _FRONTMATTER_RE.match(text)
    if not matched:
        return text
    return "\n" * text.count("\n", 0, matched.end()) + text[matched.end() :]


def _tree(text: str) -> SyntaxTreeNode:
    return SyntaxTreeNode(_MD.parse(blank_frontmatter(text)))


def _inline_text(node: SyntaxTreeNode) -> str:
    """The visible text of a node: its text and code-span contents, in order."""
    return "".join(
        child.content
        for child in node.walk(include_self=False)
        if child.type in ("text", "code_inline")
    ).strip()


def tables_under_heading(text: str, heading: str) -> list[list[list[str]]]:
    """Every table in the section titled `heading`, as rows of cell texts (header row first).

    The section ends at the next heading of the same or a higher level."""
    tables: list[list[list[str]]] = []
    level: int | None = None
    for node in _tree(text).children:
        if node.type == "heading":
            depth = int(node.tag[1:])
            if level is not None and depth <= level:
                level = None
            if level is None and _inline_text(node) == heading:
                level = depth
        elif node.type == "table" and level is not None:
            rows = [
                [_inline_text(cell) for cell in row.children]
                for row in node.walk(include_self=False)
                if row.type == "tr"
            ]
            tables.append(rows)
    return tables


def table_records(
    text: str,
    heading: str,
    columns: dict[str, str],
    *,
    integers: frozenset[str] = frozenset(),
    lists: frozenset[str] = frozenset(),
) -> list[dict[str, object]]:
    """Rows of every table under `heading` that has all `columns` (`key -> header text`).

    A key in `integers` is converted with `int`; a key in `lists` is split on commas."""
    records: list[dict[str, object]] = []
    for rows in tables_under_heading(text, heading):
        header = rows[0] if rows else []
        if not all(title in header for title in columns.values()):
            continue
        index = {key: header.index(title) for key, title in columns.items()}
        for row in rows[1:]:
            record: dict[str, object] = {}
            for key, position in index.items():
                cell = row[position] if position < len(row) else ""
                if key in integers:
                    record[key] = int(cell)
                elif key in lists:
                    parts = (part.strip().strip("`") for part in cell.split(","))
                    record[key] = [part for part in parts if part]
                else:
                    record[key] = cell
            records.append(record)
    return records
