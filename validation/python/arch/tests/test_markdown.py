"""Self-tests of `archkit.markdown`: Markdown tables and the catalogue build step."""

from __future__ import annotations

import json
from pathlib import Path

from archkit.cli import main
from archkit.markdown import blank_frontmatter, table_records, tables_under_heading

EXAMPLE_DOCS = Path(__file__).resolve().parents[1] / "examples" / "shop" / "docs"

_DOC = """---
title: a document
---
# Service

## Error codes

| Code | HTTP | Note |
|---|---|---|
| `NOT_FOUND` | 404 | missing |
| `CONFLICT` | 409 | stale |

### Detail

| Code | HTTP |
|---|---|
| `GONE` | 410 |

## Data model

| Column | Values |
|---|---|
| `orders.status` | `NEW`, `PAID` |
"""


def test_frontmatter_is_blanked_keeping_the_line_count() -> None:
    blanked = blank_frontmatter(_DOC)
    assert blanked.count("\n") == _DOC.count("\n")
    assert "title: a document" not in blanked
    assert blank_frontmatter("# No front matter\n") == "# No front matter\n"


def test_a_section_includes_its_subsections_and_ends_at_a_sibling() -> None:
    tables = tables_under_heading(_DOC, "Error codes")
    assert [rows[0] for rows in tables] == [["Code", "HTTP", "Note"], ["Code", "HTTP"]]


def test_table_records_convert_and_split_cells() -> None:
    codes = table_records(
        _DOC, "Error codes", {"code": "Code", "http": "HTTP"}, integers=frozenset({"http"})
    )
    assert codes == [
        {"code": "NOT_FOUND", "http": 404},
        {"code": "CONFLICT", "http": 409},
        {"code": "GONE", "http": 410},
    ]
    columns = table_records(
        _DOC, "Data model", {"column": "Column", "values": "Values"}, lists=frozenset({"values"})
    )
    assert columns == [{"column": "orders.status", "values": ["NEW", "PAID"]}]


def test_build_catalogue_writes_the_example_error_codes(tmp_path: Path) -> None:
    out = tmp_path / "error-codes.json"
    status = main(
        [
            "build-catalogue",
            "--doc",
            str(EXAMPLE_DOCS / "architecture.md"),
            "--heading",
            "Error envelope",
            "--column",
            "code=Code",
            "--column",
            "http=HTTP",
            "--int",
            "http",
            "--out",
            str(out),
        ]
    )
    assert status == 0
    records = json.loads(out.read_text(encoding="utf-8"))
    assert {"code": "INTERNAL_ERROR", "http": 500} in records
    assert len(records) == 7
