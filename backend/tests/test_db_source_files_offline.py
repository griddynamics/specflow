"""Tier 1 (no database): the file-state mirror, the cursor, the read-model statements
and the shared contract file.

Requirements: DATA-SRC-0001, INT-SRC-0001, INT-SRC-0002.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import re
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy.dialects import postgresql

from db import source_files as sf
from db.source_files import (
    FileCursor,
    InvalidCursorError,
    decode_file_cursor,
    encode_file_cursor,
    file_rule,
    file_state,
)
from db.sources import UPLOAD_STALE_AFTER

CONTRACT = Path(sf.__file__).resolve().parents[1] / "api" / "fixtures" / "sources-contract.json"
ID = uuid.uuid4()
FRESH = timedelta(minutes=5)
STALE = UPLOAD_STALE_AFTER + timedelta(seconds=1)


def facts(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "source_failed": False,
        "item_state": "pending",
        "item_error_reason": None,
        "holds": False,
        "doc_status": None,
        "doc_error": None,
        "doc_age": None,
    }
    base.update(over)
    return base


# The mirror: 12 rules, first match wins ------------------------------------------

RULE_CASES = [
    # rule 1: a failed Drive source turns waiting and processing into failed
    (1, facts(source_failed=True, item_state="pending"), ("failed", "source_failed")),
    (1, facts(source_failed=True, item_state="processing"), ("failed", "source_failed")),
    (1, facts(source_failed=True, holds=True, doc_status="uploaded", doc_age=FRESH), ("failed", "source_failed")),
    (1, facts(source_failed=True, holds=True, doc_status="processing", doc_age=FRESH), ("failed", "source_failed")),
    # ... and leaves every other state alone
    (4, facts(source_failed=True, holds=True, doc_status="parsed"), ("done", None)),
    (
        5,
        facts(source_failed=True, holds=True, doc_status="failed", doc_error="pdf_parse_failed"),
        ("failed", "read_failed"),
    ),
    (6, facts(source_failed=True, item_state="duplicate"), ("already_in_project", "already_in_project")),
    (9, facts(source_failed=True, item_state="processed"), ("done", None)),
    (10, facts(source_failed=True, item_state="stored"), ("not_read", "not_readable")),
    (11, facts(source_failed=True, item_state="skipped"), ("skipped", "not_imported")),
    (12, facts(source_failed=True, item_state="error", item_error_reason="no_access"), ("failed", "access_lost")),
    # rule 2
    (2, facts(holds=True, doc_status="uploaded", doc_age=FRESH), ("waiting", None)),
    (2, facts(holds=True, doc_status="uploaded", doc_age=timedelta(minutes=59)), ("waiting", None)),
    (2, facts(holds=True, doc_status="uploaded", doc_age=UPLOAD_STALE_AFTER), ("waiting", None)),
    (2, facts(item_state="processed", holds=True, doc_status="uploaded", doc_age=FRESH), ("waiting", None)),
    # a stale ``uploaded`` document (the parse never started) is failed, like stale processing
    (5, facts(holds=True, doc_status="uploaded", doc_age=timedelta(minutes=61)), ("failed", "unknown")),
    (
        5,
        facts(holds=True, doc_status="uploaded", doc_age=STALE, doc_error="pdf_parse_failed"),
        ("failed", "read_failed"),
    ),
    (5, facts(holds=True, doc_status="uploaded", doc_age=None), ("failed", "unknown")),
    (5, facts(item_state="processed", holds=True, doc_status="uploaded", doc_age=STALE), ("failed", "unknown")),
    # rule 3, with the edge of the stale limit
    (3, facts(holds=True, doc_status="processing", doc_age=FRESH), ("processing", None)),
    (3, facts(holds=True, doc_status="processing", doc_age=UPLOAD_STALE_AFTER), ("processing", None)),
    # rule 4
    (4, facts(holds=True, doc_status="parsed"), ("done", None)),
    (4, facts(holds=True, doc_status="ingested"), ("done", None)),
    (4, facts(holds=True, doc_status="partially_ingested"), ("done", None)),
    (4, facts(item_state="error", item_error_reason="parse_failed", holds=True, doc_status="parsed"), ("done", None)),
    # rule 5: failed or stale processing; the reason comes from the document error
    (5, facts(holds=True, doc_status="failed"), ("failed", "unknown")),
    (5, facts(holds=True, doc_status="failed", doc_error="workflow_start_failed"), ("failed", "unknown")),
    (5, facts(holds=True, doc_status="failed", doc_error="pdf_parse_failed"), ("failed", "read_failed")),
    (5, facts(holds=True, doc_status="processing", doc_age=STALE), ("failed", "unknown")),
    (
        5,
        facts(holds=True, doc_status="processing", doc_age=STALE, doc_error="pdf_parse_failed"),
        ("failed", "read_failed"),
    ),
    (5, facts(holds=True, doc_status="processing", doc_age=None), ("failed", "unknown")),
    # rule 6: a duplicate is never held here, whatever the document says
    (6, facts(item_state="duplicate"), ("already_in_project", "already_in_project")),
    (6, facts(item_state="duplicate", doc_status="failed"), ("already_in_project", "already_in_project")),
    (
        6,
        facts(item_state="duplicate", doc_status="parsed", doc_age=FRESH),
        ("already_in_project", "already_in_project"),
    ),
    # rules 7 to 11
    (7, facts(item_state="pending"), ("waiting", None)),
    (7, facts(item_state="pending", doc_status="uploaded"), ("waiting", None)),  # not the holder
    (8, facts(item_state="processing"), ("processing", None)),
    (9, facts(item_state="processed"), ("done", None)),
    (9, facts(item_state="processed", doc_status="failed"), ("done", None)),  # not the holder
    (10, facts(item_state="stored"), ("not_read", "not_readable")),
    (11, facts(item_state="skipped"), ("skipped", "not_imported")),
    # rule 12: the closed reason map, anything else is unknown
    (12, facts(item_state="error", item_error_reason="no_access"), ("failed", "access_lost")),
    (12, facts(item_state="error", item_error_reason="drive_unavailable"), ("failed", "drive_unavailable")),
    (12, facts(item_state="error", item_error_reason="convert_failed"), ("failed", "read_failed")),
    (12, facts(item_state="error", item_error_reason="parse_failed"), ("failed", "read_failed")),
    (12, facts(item_state="error", item_error_reason="store_failed"), ("failed", "store_failed")),
    (12, facts(item_state="error", item_error_reason="import_failed"), ("failed", "source_failed")),
    (12, facts(item_state="error", item_error_reason="unknown_error"), ("failed", "unknown")),
    (12, facts(item_state="error", item_error_reason=None), ("failed", "unknown")),
]


@pytest.mark.parametrize(("rule", "given", "expected"), RULE_CASES)
def test_the_mirror_applies_the_first_matching_rule(
    rule: int, given: dict[str, object], expected: tuple[str, str | None]
) -> None:
    rule_args = {k: v for k, v in given.items() if k not in {"item_error_reason", "doc_error"}}

    assert file_rule(**rule_args) == rule
    assert file_state(**given) == expected


def test_every_one_of_the_twelve_rules_is_covered_by_a_case() -> None:
    assert {rule for rule, _, _ in RULE_CASES} == set(range(1, 13))


def test_a_holder_with_a_null_holder_document_is_still_already_in_project() -> None:
    # Rule 6 never needs the holder: the API then sends ``holder: null`` (R15).
    assert file_state(**facts(item_state="duplicate")) == (
        "already_in_project",
        "already_in_project",
    )


def test_an_unknown_item_state_is_refused() -> None:
    with pytest.raises(ValueError, match="item state"):
        file_state(**facts(item_state="bogus"))


def test_the_rule_table_only_names_known_states_and_reasons() -> None:
    for state, reason in sf.RULE_STATE.values():
        assert state in sf.FILE_STATES
        assert reason is None or reason in sf.REASON_CODES
    assert set(sf.ERROR_REASON_CODE.values()) <= set(sf.REASON_CODES)
    assert timedelta(minutes=15) == sf.UPLOAD_OPEN_FOR


def test_the_group_rank_puts_problems_first_and_done_last() -> None:
    ranks = {state: sf.state_rank(state) for state in sf.FILE_STATES}

    assert ranks == {
        "not_read": 0,
        "skipped": 0,
        "failed": 0,
        "already_in_project": 0,
        "processing": 1,
        "waiting": 2,
        "done": 3,
    }


# Cursor ------------------------------------------------------------------------------


def test_the_cursor_round_trips_and_is_stable_and_url_safe() -> None:
    cursor = encode_file_cursor(2, "ünï/ça?=.pdf", ID)

    assert cursor == encode_file_cursor(2, "ünï/ça?=.pdf", ID)
    assert set(cursor) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    assert decode_file_cursor(cursor) == FileCursor(2, "ünï/ça?=.pdf", ID)
    assert decode_file_cursor(encode_file_cursor(0, "a\u00a0b", ID)).lower_name == "a\u00a0b"
    assert decode_file_cursor(encode_file_cursor(0, "", ID)) == FileCursor(0, "", ID)


def _raw(body: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip("=")


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "not base64 !!",
        "e30",  # {}
        _raw([1, 2, 3]),
        _raw({"r": 0, "n": "a"}),
        _raw({"r": 0, "n": "a", "i": "not-a-uuid"}),
        _raw({"r": "0", "n": "a", "i": str(ID)}),
        _raw({"r": True, "n": "a", "i": str(ID)}),
        _raw({"r": 4, "n": "a", "i": str(ID)}),
        _raw({"r": -1, "n": "a", "i": str(ID)}),
        _raw({"r": 0, "n": 5, "i": str(ID)}),
        _raw({"r": 0, "n": "a\u0000b", "i": str(ID)}),
        _raw({"r": 0, "n": "a\nb", "i": str(ID)}),
        _raw({"r": 0, "n": "x" * 1001, "i": str(ID)}),
        _raw({"r": 0, "n": "a", "i": str(ID), "x": 1}),
        base64.urlsafe_b64encode(b"\xff\xfe").decode(),
        base64.urlsafe_b64encode(b"{not json").decode(),
    ],
)
def test_a_bad_cursor_raises_invalid_cursor(bad: str) -> None:
    with pytest.raises(InvalidCursorError):
        decode_file_cursor(bad)


def test_invalid_cursor_is_a_value_error_for_the_422_mapping() -> None:
    assert issubclass(InvalidCursorError, ValueError)


# Compiled SQL smoke tests ----------------------------------------------------------------


def compiled(statement: object) -> str:
    return str(statement.compile(dialect=postgresql.asyncpg.dialect()))


def test_the_summaries_statement_is_one_read_with_every_rule_input() -> None:
    sql = compiled(sf._summaries_statement(ID))

    assert sql.count(";") == 0 and sql.startswith("WITH base AS")
    for fragment in (
        "FROM connection_items",
        "JOIN connections",
        "LEFT OUTER JOIN documents AS d",
        "d.source_id = connection_items.connection_id",
        "d.status",
        "d.updated_at",
        "now() - $",
        "::INTERVAL",
        "FILTER (WHERE files.state =",
        "GROUP BY files.connection_id",
        "greatest(",
        "JOIN users",
        "ORDER BY connections.created_at, connections.id",
        "connection_items.project_id = $",
        "connections.project_id = $",
    ):
        assert fragment in sql, fragment
    assert "connections.id = $" not in sql  # no source scope without a source id
    # a column named like a ColumnCollection method (``items``) would bind a method
    assert "coalesce(counts.item_count" in sql and "counts.waiting" in sql


def test_a_source_scoped_statement_filters_the_items_and_the_source() -> None:
    sql = compiled(sf._summaries_statement(ID, ID))

    assert "connection_items.connection_id = $" in sql
    assert "connections.id = $" in sql


def test_the_drive_stale_rule_is_gated_by_the_drive_kind_in_both_places() -> None:
    sql = compiled(sf._summaries_statement(ID))

    # file rule 1 (the base CTE) and the card's state / failure reason
    assert sql.count("connections.kind = $") >= 4
    assert sql.count("connections.created_at < now() - $") >= 2


def test_hidden_batches_are_left_out_for_uploads_only() -> None:
    sql = compiled(sf._summaries_statement(ID))

    assert "NOT (connections.kind = $" in sql  # hidden = upload AND not open AND 0 items
    assert "connections.ended_at IS NULL" in sql


def test_the_page_statement_orders_by_the_keyset_and_fetches_one_extra_row() -> None:
    first = compiled(sf._page_statement(ID, ID, None, 200))
    later = compiled(sf._page_statement(ID, ID, FileCursor(1, "a", ID), 200))

    assert "lower(files.name)" in first and "ORDER BY CASE" in first
    assert re.search(r"LIMIT \$\d+::INTEGER\s*$", first)  # a bound limit
    assert ") > (" not in first
    assert ") > (" in later  # the row comparison of the keyset
    assert "LEFT OUTER JOIN connections AS holder" in first and "files.rule" in first


def test_the_page_statement_binds_the_limit_plus_one() -> None:
    params = sf._page_statement(ID, ID, None, 7).compile(dialect=postgresql.asyncpg.dialect()).params

    assert 8 in params.values()


async def test_file_page_refuses_a_limit_outside_1_to_200() -> None:
    for bad in (0, 201, -1):
        with pytest.raises(ValueError, match="limit"):
            await sf.file_page(None, ID, ID, None, bad)


def test_the_sweep_is_one_statement_scoped_to_the_project() -> None:
    sql = " ".join(str(sf._SWEEP).split())

    assert ";" not in sql and "project_id = :project_id" in sql
    # A candidate is missing only when no document row has its id, in any project.
    assert "NOT EXISTS ( SELECT 1 FROM documents AS d WHERE d.id = candidate.document_id )" in sql


# The shared contract file ---------------------------------------------------------------


def test_the_contract_json_equals_the_python_constants() -> None:
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))

    assert contract == {
        "file_states": list(sf.FILE_STATES),
        "reason_codes": list(sf.REASON_CODES),
        "group_order": [list(group) for group in sf.GROUP_ORDER],
        "page_limit": sf.PAGE_LIMIT,
        "outcomes": list(sf.UPLOAD_OUTCOMES),
        "error_codes": list(sf.API_ERROR_CODES),
        "source_fields": list(sf.SOURCE_FIELDS),
        "file_fields": list(sf.FILE_FIELDS),
        "holder_fields": list(sf.HOLDER_FIELDS),
    }


def test_the_contract_states_are_the_states_of_the_rules_and_the_groups() -> None:
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))

    assert {s for group in contract["group_order"] for s in group} == set(contract["file_states"])
    assert {state for state, _ in sf.RULE_STATE.values()} == set(contract["file_states"])
    assert contract["page_limit"] == 200


def test_the_summary_fields_of_the_contract_match_the_dataclasses() -> None:
    summary = {f.name for f in dataclasses.fields(sf.SourceSummary)}
    holder = {f.name for f in dataclasses.fields(sf.HolderRef)}
    file_row = {f.name for f in dataclasses.fields(sf.FileRow)}

    assert set(sf.HOLDER_FIELDS) == holder
    assert set(sf.FILE_FIELDS) == file_row
    # the API nests ``added_by_id`` / ``added_by_name`` as ``added_by``
    assert set(sf.SOURCE_FIELDS) - {"added_by"} <= summary
    assert summary - set(sf.SOURCE_FIELDS) == {"added_by_id", "added_by_name"}
