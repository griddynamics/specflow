"""Google Slides (presentations.get) to Markdown: one section per slide."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from drive.convert import _md
from drive.convert._md import as_list, as_map

_TITLES = {"TITLE", "CENTERED_TITLE"}


def _paragraphs(text: Mapping[str, Any]) -> list[tuple[int | None, str]]:
    """(bullet level or None, text) per paragraph of a Slides ``TextContent``."""
    out: list[tuple[int | None, list[str]]] = []
    for raw_element in as_list(text.get("textElements")):
        element = as_map(raw_element)
        if "paragraphMarker" in element:
            bullet = as_map(as_map(element["paragraphMarker"]).get("bullet"))
            level = bullet.get("nestingLevel", 0) if bullet else None
            out.append(
                (
                    level if bullet and isinstance(level, int) else (0 if bullet else None),
                    [],
                )
            )
        elif "textRun" in element:
            run = as_map(element["textRun"])
            content = run.get("content")
            if not isinstance(content, str):
                continue
            if not out:
                out.append((None, []))
            content = content.rstrip("\n").replace("\x0b", "  \n")
            out[-1][1].append(_md.link(content, as_map(as_map(run.get("style")).get("link")).get("url")))
    return [(lvl, "".join(parts).strip()) for lvl, parts in out if "".join(parts).strip()]


def _text_blocks(text: Mapping[str, Any]) -> list[str]:
    blocks: list[str] = []
    items: list[str] = []
    for level, para in _paragraphs(text):
        if level is None:
            if items:
                blocks.append("\n".join(items))
                items = []
            blocks.append(_md.escape_leading(para))
        else:
            items.append("    " * min(level, 8) + "- " + para.replace("  \n", " "))
    if items:
        blocks.append("\n".join(items))
    return blocks


def _plain(text: Mapping[str, Any]) -> str:
    return " ".join(" ".join(p for _, p in _paragraphs(text)).split())


def _table(table: Mapping[str, Any]) -> str:
    rows = []
    for row in as_list(table.get("tableRows")):
        cells = []
        for raw_cell in as_list(as_map(row).get("tableCells")):
            cell = as_map(raw_cell)
            text = "\n".join(p for _, p in _paragraphs(as_map(cell.get("text"))))
            cells.append((text, _span(cell.get("rowSpan")), _span(cell.get("columnSpan"))))
        rows.append(cells)
    return _md.table_md(_md.grid_from_cells(rows))


def _span(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 1


def _elements(elements: Sequence[Any], depth: int, title: list[str], blocks: list[str]) -> None:
    if depth > _md.MAX_DEPTH:
        raise _md.ConversionError("presentation conversion failed")
    for raw_element in elements:
        element = as_map(raw_element)
        shape = as_map(element.get("shape"))
        if shape:
            text = as_map(shape.get("text"))
            if as_map(shape.get("placeholder")).get("type") in _TITLES and not title:
                title.append(_plain(text))
                continue
            blocks.extend(_text_blocks(text))
        elif "table" in element:
            blocks.append(_table(as_map(element["table"])))
        elif "elementGroup" in element:
            _elements(
                as_list(as_map(element["elementGroup"]).get("children")),
                depth + 1,
                title,
                blocks,
            )
        # images, videos, charts, lines: dropped (no image link is ever written)


def _notes(slide: Mapping[str, Any]) -> list[str]:
    page = as_map(as_map(slide.get("slideProperties")).get("notesPage"))
    wanted = as_map(page.get("notesProperties")).get("speakerNotesObjectId")
    for raw_element in as_list(page.get("pageElements")):
        element = as_map(raw_element)
        if wanted and element.get("objectId") == wanted:
            return _text_blocks(as_map(as_map(element.get("shape")).get("text")))
    return []


def presentation_to_markdown(presentation: Mapping[str, Any]) -> str:
    """FR-GDUI-0004: slide text, tables and speaker notes; images dropped."""
    if not _md.is_mapping(presentation) or ("slides" not in presentation and "title" not in presentation):
        raise _md.ConversionError("presentation conversion failed")
    slides = presentation.get("slides")
    if slides is not None and not isinstance(slides, list):
        raise _md.ConversionError("presentation conversion failed")
    blocks = ["# " + _md.title_of(presentation.get("title"))]
    try:
        for number, slide in enumerate(slides or [], start=1):
            title: list[str] = []
            body: list[str] = []
            _elements(as_list(as_map(slide).get("pageElements")), 0, title, body)
            heading = f"## Slide {number}"
            if title and title[0]:
                heading += ": " + title[0]
            blocks.append(heading)
            blocks.extend(body)
            notes = _notes(as_map(slide))
            if notes:
                blocks.append("### Speaker notes")
                blocks.extend(notes)
    except _md.ConversionError:
        raise
    except (TypeError, AttributeError, KeyError, ValueError, RecursionError):
        raise _md.ConversionError("presentation conversion failed") from None
    return _md.join_blocks(blocks)
