"""Google Docs (documents.get with includeTabsContent) to Markdown."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from drive.convert import _md
from drive.convert._md import as_list, as_map

_ORDERED = {"DECIMAL", "ZERO_DECIMAL", "ALPHA", "UPPER_ALPHA", "ROMAN", "UPPER_ROMAN"}
_HEADING = {f"HEADING_{n}": n for n in range(1, 7)}


def _paragraph_text(paragraph: Mapping[str, Any], *, in_cell: bool) -> str:
    out: list[str] = []
    for element in as_list(paragraph.get("elements")):
        run = as_map(as_map(element).get("textRun"))
        content = run.get("content")
        if not isinstance(content, str):
            continue  # images (inlineObjectElement), page breaks, footnote marks: dropped
        content = content.rstrip("\n").replace("\x0b", " " if in_cell else "  \n")
        out.append(_md.link(content, as_map(as_map(run.get("textStyle")).get("link")).get("url")))
    return "".join(out)


def _flatten(content: Sequence[Any], depth: int) -> list[str]:
    """Plain text lines of structural elements (used inside table cells)."""
    if depth > _md.MAX_DEPTH:
        raise _md.ConversionError("document conversion failed")
    lines: list[str] = []
    for raw_element in content:
        element = as_map(raw_element)
        if "paragraph" in element:
            text = _paragraph_text(as_map(element["paragraph"]), in_cell=True).strip()
            if text:
                lines.append(text)
        elif "table" in element:
            for row in as_list(as_map(element["table"]).get("tableRows")):
                for cell in as_list(as_map(row).get("tableCells")):
                    lines.extend(_flatten(as_list(as_map(cell).get("content")), depth + 1))
        elif "tableOfContents" in element:
            continue
    return lines


def _table(table: Mapping[str, Any]) -> str:
    rows = []
    for row in as_list(table.get("tableRows")):
        cells = []
        for raw_cell in as_list(as_map(row).get("tableCells")):
            cell = as_map(raw_cell)
            style = as_map(cell.get("tableCellStyle"))
            text = "\n".join(_flatten(as_list(cell.get("content")), 1))
            cells.append((text, _span(style.get("rowSpan")), _span(style.get("columnSpan"))))
        rows.append(cells)
    return _md.table_md(_md.grid_from_cells(rows))


def _span(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 1


class _Lists:
    """Bullet / numbering state for one tab."""

    def __init__(self, lists: Mapping[str, Any]) -> None:
        self._lists = lists
        self._counters: dict[str, list[int]] = {}

    def _ordered(self, list_id: str, level: int) -> bool:
        levels = as_list(as_map(as_map(self._lists.get(list_id)).get("listProperties")).get("nestingLevels"))
        if level < len(levels):
            return as_map(levels[level]).get("glyphType") in _ORDERED
        return False

    def marker(self, list_id: str, level: int) -> str:
        level = max(0, min(level, 8))
        counters = self._counters.setdefault(list_id, [])
        del counters[level + 1 :]
        counters.extend([0] * (level + 1 - len(counters)))
        counters[level] += 1
        mark = f"{counters[level]}." if self._ordered(list_id, level) else "-"
        return "    " * level + mark + " "

    def reset(self) -> None:
        self._counters.clear()


def _body(content: Sequence[Any], lists: Mapping[str, Any], base: int) -> list[str]:
    """Markdown blocks of one tab body. ``base`` is the heading level of the tab itself."""
    state = _Lists(lists)
    blocks: list[str] = []
    run: list[str] = []  # consecutive list items form one block

    def flush() -> None:
        if run:
            blocks.append("\n".join(run))
            run.clear()

    for raw_element in content:
        element = as_map(raw_element)
        if "paragraph" in element:
            paragraph = as_map(element["paragraph"])
            text = _paragraph_text(paragraph, in_cell=False).strip()
            bullet = as_map(paragraph.get("bullet"))
            if bullet and isinstance(bullet.get("listId"), str):
                if text:
                    text = text.replace("  \n", " ")  # a line break inside an item stays one line
                    level = bullet.get("nestingLevel")
                    run.append(state.marker(bullet["listId"], level if isinstance(level, int) else 0) + text)
                continue
            flush()
            if not text:
                continue
            state.reset()
            style = as_map(paragraph.get("paragraphStyle")).get("namedStyleType")
            if style in _HEADING:
                blocks.append("#" * min(6, base + _HEADING[style]) + " " + " ".join(text.split()))
            else:
                blocks.append(_md.escape_leading(text))
        elif "table" in element:
            flush()
            state.reset()
            blocks.append(_table(as_map(element["table"])))
    flush()
    return blocks


def _walk(tabs: Sequence[Any], depth: int, *, multi: bool, blocks: list[str]) -> None:
    if depth > _md.MAX_DEPTH:
        raise _md.ConversionError("document conversion failed")
    for raw_tab in tabs:
        tab = as_map(raw_tab)
        level = 2 + depth
        if multi:
            title = _md.title_of(as_map(tab.get("tabProperties")).get("title"))
            blocks.append("#" * min(6, level) + " " + title)
        document_tab = as_map(tab.get("documentTab"))
        blocks.extend(
            _body(
                as_list(as_map(document_tab.get("body")).get("content")),
                as_map(document_tab.get("lists")),
                level if multi else 1,
            )
        )
        _walk(as_list(tab.get("childTabs")), depth + 1, multi=multi, blocks=blocks)


def _count(tabs: Sequence[Any], depth: int = 0) -> int:
    if depth > _md.MAX_DEPTH:
        raise _md.ConversionError("document conversion failed")
    return sum(1 + _count(as_list(as_map(t).get("childTabs")), depth + 1) for t in tabs)


def document_to_markdown(document: Mapping[str, Any]) -> str:
    """FR-GDUI-0004: tabs (with child tabs), headings, lists, tables and links as Markdown."""
    if not isinstance(document, Mapping):
        raise _md.ConversionError("document conversion failed")
    tabs = document.get("tabs")
    if tabs is None and isinstance(document.get("body"), Mapping):
        # documents.get without includeTabsContent: one implicit tab at the top level
        tabs = [{"documentTab": {"body": document["body"], "lists": document.get("lists")}}]
    if tabs is None and "title" not in document:
        raise _md.ConversionError("document conversion failed")
    if tabs is not None and not isinstance(tabs, list):
        raise _md.ConversionError("document conversion failed")
    tabs = tabs or []
    blocks = ["# " + _md.title_of(document.get("title"))]
    try:
        _walk(tabs, 0, multi=_count(tabs) > 1, blocks=blocks)
    except _md.ConversionError:
        raise
    except (TypeError, AttributeError, KeyError, ValueError, RecursionError):
        raise _md.ConversionError("document conversion failed") from None
    return _md.join_blocks(blocks)
