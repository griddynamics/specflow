"""Google Sheets (spreadsheets.get + values.batchGet) to Markdown: one table per tab."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from drive.convert import _md
from drive.convert._md import as_list, as_map


def _rows(value_range: Mapping[str, Any]) -> list[list[str]]:
    rows = [["" if c is None else str(c) for c in as_list(r)] for r in as_list(value_range.get("values"))]
    while rows and not any(c.strip() for c in rows[-1]):
        rows.pop()  # Sheets pads with empty rows; they carry nothing
    return rows


def spreadsheet_to_markdown(spreadsheet: Mapping[str, Any]) -> str:
    """FR-GDUI-0004: input ``{"spreadsheet": ..., "valueRanges": [...]}``, one table per tab."""
    if not _md.is_mapping(spreadsheet) or not _md.is_mapping(spreadsheet.get("spreadsheet")):
        raise _md.ConversionError("spreadsheet conversion failed")
    meta = spreadsheet["spreadsheet"]
    sheets = as_list(meta.get("sheets"))
    ranges = as_list(spreadsheet.get("valueRanges"))
    blocks = ["# " + _md.title_of(as_map(meta.get("properties")).get("title"))]
    try:
        pairs = []
        for position, sheet in enumerate(sheets):
            props = as_map(as_map(sheet).get("properties"))
            index = props.get("index")
            pairs.append(
                (
                    index if isinstance(index, int) else position,
                    position,
                    _md.title_of(props.get("title")),
                    as_map(ranges[position]) if position < len(ranges) else {},
                )
            )
        for _, _, title, value_range in sorted(pairs, key=lambda p: (p[0], p[1])):
            blocks.append("## " + title)
            rows = _rows(value_range)
            blocks.append(_md.table_md(rows, escape=True) if rows else "_Empty sheet._")
    except (TypeError, AttributeError, KeyError, ValueError, RecursionError):
        raise _md.ConversionError("spreadsheet conversion failed") from None
    return _md.join_blocks(blocks)
