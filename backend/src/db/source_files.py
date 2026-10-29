"""Read model of the sources: file states, source summaries and the paged file list.

The state of a file is computed here, in SQL, from the item, its document and the
source (DATA-SRC-0001). The twelve rules below are applied in order and the first
match wins. ``file_rule`` / ``file_state`` are the same rules as a pure function:
the offline tests pin them, the live test asserts that SQL equals the mirror.

Same rules as ``db.sources``: every function runs inside the caller's session and
transaction and never commits; one SQL statement per read. File names live in the
database and in API bodies only; nothing here logs.

Requirements: DATA-SRC-0001, INT-SRC-0001, INT-SRC-0002, NFR-PERF-0002. The rules,
in order (``holds`` = ``doc.source_id = item.connection_id``):

1. Drive source failed (stored, or stale) and rules 2-12 say waiting or
   processing: failed / ``source_failed``. Never for an upload batch.
2. holds, document ``uploaded`` and fresh: waiting.
3. holds, document ``processing`` and fresh: processing.
4. holds, document parsed, ingested or partially ingested: done.
5. holds, document ``failed``, or ``processing`` / ``uploaded`` and stale: failed.
6. item ``duplicate``: already_in_project (the holder is the document's source).
7. item ``pending``: waiting.   8. ``processing``: processing.   9. ``processed``: done.
10. ``stored``: not_read.   11. ``skipped``: skipped.   12. ``error``: failed.
"""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, NamedTuple

from sqlalchemy import (
    ARRAY,
    Select,
    and_,
    bindparam,
    case,
    func,
    literal,
    null,
    or_,
    select,
    text,
    tuple_,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from db.models import Connection, ConnectionItem, Document, User
from db.query_files import read_query
from db.sources import (
    DRIVE_IMPORT_EXECUTION_TIMEOUT,
    STALE_FAILURE_REASON,
    UPLOAD_STALE_AFTER,
)

# An upload batch is open until it ends, or this long after its last activity
# (INT-SRC-0004).
UPLOAD_OPEN_FOR: Final = timedelta(minutes=15)
PAGE_LIMIT: Final = 200
MAX_CURSOR_NAME: Final = 1_000
_FIRST_PRINTABLE: Final = 32
_DELETE_CHAR: Final = 127

# The numbered rules of DATA-SRC-0001 that the code names.
RULE_DOC_FAILED: Final = 5
RULE_DUPLICATE: Final = 6
RULE_STORED: Final = 10
RULE_SKIPPED: Final = 11
RULE_ERROR: Final = 12

# The shared contract (``api/fixtures/sources-contract.json``): the web app and the
# stub API check their own copies against the same file.
FILE_STATES: Final = (
    "waiting",
    "processing",
    "done",
    "not_read",
    "skipped",
    "failed",
    "already_in_project",
)
REASON_CODES: Final = (
    "source_failed",
    "read_failed",
    "unknown",
    "already_in_project",
    "not_readable",
    "not_imported",
    "access_lost",
    "drive_unavailable",
    "store_failed",
)
# Display order of the groups in the modal; states inside one group sort by name.
GROUP_ORDER: Final = (
    ("not_read", "skipped", "failed", "already_in_project"),  # problems
    ("processing",),
    ("waiting",),
    ("done",),
)
UPLOAD_OUTCOMES: Final = ("added", "already_in_project", "read_again", "duplicate")
API_ERROR_CODES: Final = (
    "source_not_found",
    "batch_ended",
    "removal_unavailable",
    "project_not_found",
    "validation_failed",
    "missing_filename",
    "empty_file",
    "unsupported_format",
    "database_unavailable",
)
SOURCE_FIELDS: Final = (
    "id",
    "kind",
    "name",
    "name_suffix",
    "added_by",
    "created_at",
    "listing_finished",
    "open",
    "total",
    "counts",
    "failure_reason",
    "folder_id",
    "link",
)
FILE_FIELDS: Final = ("id", "name", "category", "state", "reason", "holder")
HOLDER_FIELDS: Final = ("id", "kind", "name", "name_suffix", "created_at")

_RANK: Final = {state: rank for rank, states in enumerate(GROUP_ORDER) for state in states}
# (state, reason) of every rule. Rule 5 and rule 12 pick the reason from the data.
RULE_STATE: Final = {
    1: ("failed", "source_failed"),
    2: ("waiting", None),
    3: ("processing", None),
    4: ("done", None),
    5: ("failed", "unknown"),
    6: ("already_in_project", "already_in_project"),
    7: ("waiting", None),
    8: ("processing", None),
    9: ("done", None),
    10: ("not_read", "not_readable"),
    11: ("skipped", "not_imported"),
    12: ("failed", "unknown"),
}
PARSED_STATUSES: Final = ("parsed", "ingested", "partially_ingested")
WAITING_OR_PROCESSING_RULES: Final = (2, 3, 7, 8)
READ_FAILED_DOCUMENT_ERROR: Final = "pdf_parse_failed"
# Reason of a Drive file in ``error`` by its stored file error; anything else is ``unknown``.
ERROR_REASON_CODE: Final = {
    "no_access": "access_lost",
    "drive_unavailable": "drive_unavailable",
    "convert_failed": "read_failed",
    "parse_failed": "read_failed",
    "store_failed": "store_failed",
    "import_failed": "source_failed",
}
_ITEM_STATE_RULE: Final = {
    "duplicate": 6,
    "pending": 7,
    "processing": 8,
    "processed": 9,
    "stored": 10,
    "skipped": 11,
    "error": 12,
}

# The mirror ----------------------------------------------------------------------


class FileOutcome(NamedTuple):
    state: str
    reason: str | None


def file_rule(
    *,
    source_failed: bool,
    item_state: str,
    holds: bool,
    doc_status: str | None,
    doc_age: timedelta | None,
) -> int:
    """The number (1-12) of the first rule that matches.

    ``source_failed``: a Drive source that is failed or stale (False for a batch).
    ``doc_age``: ``now - documents.updated_at``; ``None`` without a document.
    A document ``processing`` or ``uploaded`` for exactly ``UPLOAD_STALE_AFTER`` is still fresh.
    """

    if item_state not in _ITEM_STATE_RULE:
        raise ValueError(f"unknown item state {item_state!r}")
    fresh = doc_age is not None and doc_age <= UPLOAD_STALE_AFTER
    rule = _ITEM_STATE_RULE[item_state]
    if holds and doc_status == "uploaded" and fresh:
        rule = 2
    elif holds and doc_status == "processing" and fresh:
        rule = 3
    elif holds and doc_status in PARSED_STATUSES:
        rule = 4
    elif holds and doc_status in ("failed", "processing", "uploaded"):
        rule = 5  # failed, or processing / uploaded and not fresh
    if source_failed and rule in WAITING_OR_PROCESSING_RULES:
        return 1
    return rule


def file_state(
    *,
    source_failed: bool,
    item_state: str,
    item_error_reason: str | None,
    holds: bool,
    doc_status: str | None,
    doc_error: str | None,
    doc_age: timedelta | None,
) -> FileOutcome:
    """State and reason code of one file (DATA-SRC-0001)."""

    rule = file_rule(
        source_failed=source_failed,
        item_state=item_state,
        holds=holds,
        doc_status=doc_status,
        doc_age=doc_age,
    )
    state, reason = RULE_STATE[rule]
    if rule == RULE_DOC_FAILED and doc_error == READ_FAILED_DOCUMENT_ERROR:
        reason = "read_failed"
    elif rule == RULE_ERROR:
        reason = ERROR_REASON_CODE.get(item_error_reason or "", "unknown")
    return FileOutcome(state, reason)


def state_rank(state: str) -> int:
    """Group rank: problems 0, processing 1, waiting 2, done 3."""

    return _RANK[state]


# Cursor --------------------------------------------------------------------------


class InvalidCursorError(ValueError):
    """The cursor is not one this module issued (the API answers 422)."""


@dataclass(frozen=True)
class FileCursor:
    rank: int
    lower_name: str
    id: uuid.UUID


def encode_file_cursor(rank: int, lower_name: str, file_id: uuid.UUID) -> str:
    raw = json.dumps(
        {"r": rank, "n": lower_name, "i": str(file_id)},
        separators=(",", ":"),
        sort_keys=True,
    )
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_file_cursor(cursor: str) -> FileCursor:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        body = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        rank, name, file_id = body["r"], body["n"], body["i"]
        valid = (
            set(body) == {"r", "n", "i"}
            and isinstance(rank, int)
            and not isinstance(rank, bool)
            and 0 <= rank < len(GROUP_ORDER)
            and isinstance(name, str)
            and len(name) <= MAX_CURSOR_NAME
            and not any(ord(c) < _FIRST_PRINTABLE or ord(c) == _DELETE_CHAR for c in name)  # Postgres refuses NUL
            and isinstance(file_id, str)
        )
        parsed_id = uuid.UUID(file_id) if valid else None
    except (
        binascii.Error,
        UnicodeError,
        ValueError,
        KeyError,
        TypeError,
    ) as error:  # json.JSONDecodeError is a ValueError
        raise InvalidCursorError("bad cursor") from error
    if parsed_id is None:
        raise InvalidCursorError("bad cursor")
    return FileCursor(rank, name, parsed_id)


# Results -------------------------------------------------------------------------


@dataclass(frozen=True)
class HolderRef:
    id: uuid.UUID
    kind: str
    name: str
    name_suffix: int
    created_at: datetime


@dataclass(frozen=True)
class FileRow:
    id: uuid.UUID
    name: str
    category: str
    state: str
    reason: str | None
    holder: HolderRef | None


@dataclass(frozen=True)
class FilePage:
    items: tuple[FileRow, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class SourceSummary:
    """One card. ``sum(counts.values()) == total`` always holds."""

    id: uuid.UUID
    kind: str
    name: str
    name_suffix: int
    added_by_id: uuid.UUID
    added_by_name: str  # users.display_name: fine in API bodies, never in a log
    created_at: datetime
    listing_finished: bool
    open: bool
    total: int
    counts: dict[str, int]
    failure_reason: str | None
    folder_id: str | None
    link: str | None


# Statements ----------------------------------------------------------------------


def _drive_failed() -> Any:
    """A Drive source that is failed, or still running past its execution timeout."""

    stale = and_(
        Connection.state.in_(("listing", "importing")),
        Connection.created_at < func.now() - DRIVE_IMPORT_EXECUTION_TIMEOUT,
    )
    return and_(Connection.kind == "gdrive", or_(Connection.state == "failed", stale))


def _files_cte(project_id: uuid.UUID, source_id: uuid.UUID | None = None) -> Any:
    """Every item with its rule, state and reason (three layers, one statement)."""

    d = aliased(Document, name="d")
    i = ConnectionItem
    holds = d.source_id == i.connection_id
    fresh = d.updated_at >= func.now() - UPLOAD_STALE_AFTER
    base_rule = case(
        (and_(holds, d.status == "uploaded", fresh), 2),
        (and_(holds, d.status == "processing", fresh), 3),
        (and_(holds, d.status.in_(PARSED_STATUSES)), 4),
        (
            and_(
                holds,
                d.status.in_(("failed", "processing", "uploaded")),
            ),
            5,
        ),
        *[(i.state == name, rule) for name, rule in _ITEM_STATE_RULE.items()],
        else_=12,  # unreachable: the CHECK on connection_items.state
    )
    where = [i.project_id == project_id]
    if source_id is not None:
        where.append(i.connection_id == source_id)
    base = (
        select(
            i.id.label("id"),
            i.connection_id.label("connection_id"),
            i.name.label("name"),
            i.category.label("category"),
            i.created_at.label("created_at"),
            i.error_reason.label("error_reason"),
            d.source_id.label("doc_source_id"),
            d.error.label("doc_error"),
            _drive_failed().label("drive_failed"),
            base_rule.label("base_rule"),
        )
        .select_from(i)
        .join(Connection, Connection.id == i.connection_id)
        .outerjoin(d, and_(d.id == i.document_id, d.project_id == i.project_id))
        .where(*where)
        .cte("base")
    )
    rule = case(
        (
            and_(
                base.c.drive_failed,
                base.c.base_rule.in_(WAITING_OR_PROCESSING_RULES),
            ),
            1,
        ),
        else_=base.c.base_rule,
    )
    rules = select(*base.c, rule.label("rule")).cte("rules")
    r = rules.c
    state = case(
        *[(r.rule == n, s) for n, (s, _) in RULE_STATE.items()],
        else_="failed",
    )
    reason = case(
        (r.rule == 1, "source_failed"),
        (
            r.rule == RULE_DOC_FAILED,
            case(
                (r.doc_error == READ_FAILED_DOCUMENT_ERROR, "read_failed"),
                else_="unknown",
            ),
        ),
        (r.rule == RULE_DUPLICATE, "already_in_project"),
        (r.rule == RULE_STORED, "not_readable"),
        (r.rule == RULE_SKIPPED, "not_imported"),
        (
            r.rule == RULE_ERROR,
            case(
                *[(r.error_reason == e, c) for e, c in ERROR_REASON_CODE.items()],
                else_="unknown",
            ),
        ),
        else_=null(),
    )
    return select(*rules.c, state.label("state"), reason.label("reason")).cte("files")


def _summaries_statement(project_id: uuid.UUID, source_id: uuid.UUID | None = None) -> Select[Any]:
    files = _files_cte(project_id, source_id)
    counts = (
        select(
            files.c.connection_id.label("connection_id"),
            func.count().label("item_count"),
            func.max(files.c.created_at).label("last_item_at"),
            *[func.count().filter(files.c.state == s).label(s) for s in FILE_STATES],
        )
        .group_by(files.c.connection_id)
        .cte("counts")
    )
    c = Connection
    items = func.coalesce(counts.c.item_count, 0)
    drive_stale = and_(
        c.kind == "gdrive",
        c.state.in_(("listing", "importing")),
        c.created_at < func.now() - DRIVE_IMPORT_EXECUTION_TIMEOUT,
    )
    effective_state = case((drive_stale, "failed"), else_=c.state)
    is_open = and_(
        c.kind == "upload",
        c.ended_at.is_(None),
        func.greatest(c.created_at, counts.c.last_item_at) > func.now() - UPLOAD_OPEN_FOR,
    )
    # Files an open batch still expects count as waiting.
    to_come = case(
        (is_open, func.greatest(func.coalesce(c.expected_files, 0) - items, 0)),
        else_=0,
    )
    count_columns = [
        (
            func.coalesce(getattr(counts.c, s), 0) + to_come
            if s == "waiting"
            else func.coalesce(getattr(counts.c, s), 0)
        ).label(s)
        for s in FILE_STATES
    ]
    statement = (
        select(
            c.id,
            c.kind,
            c.name,
            c.name_suffix,
            c.connected_by.label("added_by_id"),
            User.display_name.label("added_by_name"),
            c.created_at,
            or_(c.kind == "upload", effective_state != "listing").label("listing_finished"),
            is_open.label("is_open"),
            (items + to_come).label("total"),
            case(
                (drive_stale, STALE_FAILURE_REASON),
                (c.kind == "gdrive", c.failure_reason),
                else_=null(),
            ).label("failure_reason"),
            case((c.kind == "gdrive", c.external_id), else_=null()).label("folder_id"),
            c.link,
            *count_columns,
        )
        .select_from(c)
        .join(User, User.id == c.connected_by)
        .outerjoin(counts, counts.c.connection_id == c.id)
        .where(c.project_id == project_id)
        # Hidden: a finished upload batch without a file (never a Drive source).
        .where(~and_(c.kind == "upload", ~is_open, items == 0))
        .order_by(c.created_at, c.id)
    )
    if source_id is not None:
        statement = statement.where(c.id == source_id)
    return statement


def _summary(row: Any) -> SourceSummary:
    return SourceSummary(
        id=row.id,
        kind=row.kind,
        name=row.name,
        name_suffix=row.name_suffix,
        added_by_id=row.added_by_id,
        added_by_name=row.added_by_name,
        created_at=row.created_at,
        listing_finished=bool(row.listing_finished),
        open=bool(row.is_open),
        total=int(row.total),
        counts={s: int(getattr(row, s)) for s in FILE_STATES},
        failure_reason=row.failure_reason,
        folder_id=row.folder_id,
        link=row.link,
    )


async def source_summaries(session: AsyncSession, project_id: uuid.UUID) -> list[SourceSummary]:
    """Every source of the project except hidden batches, oldest first (INT-SRC-0001)."""

    result = await session.execute(_summaries_statement(project_id))
    return [_summary(row) for row in result.all()]


async def source_summary(session: AsyncSession, project_id: uuid.UUID, source_id: uuid.UUID) -> SourceSummary | None:
    """One source, ``None`` when it is absent, in another project or hidden."""

    result = await session.execute(_summaries_statement(project_id, source_id))
    row = result.first()
    return None if row is None else _summary(row)


def _rank_expression(state: Any) -> Any:
    return case(
        *[(state.in_(states), rank) for rank, states in enumerate(GROUP_ORDER)],
        else_=len(GROUP_ORDER) - 1,
    )


def _page_statement(
    project_id: uuid.UUID,
    source_id: uuid.UUID,
    cursor: FileCursor | None,
    limit: int,
) -> Select[Any]:
    files = _files_cte(project_id, source_id)
    holder = aliased(Connection, name="holder")
    rank = _rank_expression(files.c.state)
    lower_name = func.lower(files.c.name)
    statement = (
        select(
            files.c.id,
            files.c.name,
            files.c.category,
            files.c.state,
            files.c.reason,
            rank.label("rank"),
            lower_name.label("lower_name"),
            holder.id.label("holder_id"),
            holder.kind.label("holder_kind"),
            holder.name.label("holder_name"),
            holder.name_suffix.label("holder_name_suffix"),
            holder.created_at.label("holder_created_at"),
        )
        .select_from(files)
        .outerjoin(
            holder,
            and_(
                files.c.rule == RULE_DUPLICATE,
                holder.id == files.c.doc_source_id,
                holder.project_id == project_id,
            ),
        )
        .order_by(rank, lower_name, files.c.id)
        .limit(limit + 1)
    )
    if cursor is not None:
        statement = statement.where(
            tuple_(rank, lower_name, files.c.id)
            > tuple_(
                literal(cursor.rank),
                literal(cursor.lower_name),
                literal(cursor.id, PG_UUID(as_uuid=True)),
            )
        )
    return statement


async def file_page(
    session: AsyncSession,
    project_id: uuid.UUID,
    source_id: uuid.UUID,
    cursor: FileCursor | None,
    limit: int = PAGE_LIMIT,
) -> FilePage:
    """The next ``limit`` files of one source by (group rank, lower name, id).

    The caller checks the source with ``source_summary`` first. The key includes the
    state rank, so a file that changes group between two page reads can be skipped or
    repeated on that pass; the next refresh converges.
    """

    if not 1 <= limit <= PAGE_LIMIT:
        raise ValueError(f"limit must be 1..{PAGE_LIMIT}")
    result = await session.execute(_page_statement(project_id, source_id, cursor, limit))
    rows = list(result.all())
    more = len(rows) > limit
    if more:
        rows.pop()  # the probe row that only told us another page exists
    page = rows
    items = tuple(
        FileRow(
            id=row.id,
            name=row.name,
            category=row.category,
            state=row.state,
            reason=row.reason,
            holder=(
                None
                if row.holder_id is None
                else HolderRef(
                    id=row.holder_id,
                    kind=row.holder_kind,
                    name=row.holder_name,
                    name_suffix=row.holder_name_suffix,
                    created_at=row.holder_created_at,
                )
            ),
        )
        for row in page
    )
    next_cursor = None
    if more:
        last = page[-1]
        next_cursor = encode_file_cursor(last.rank, last.lower_name, last.id)
    return FilePage(items, next_cursor)


_SWEEP: Final = text(read_query("source_files/sweep_candidates.sql")).bindparams(
    bindparam("document_ids", type_=ARRAY(PG_UUID(as_uuid=True)))
)


async def sweep_candidates(
    session: AsyncSession,
    project_id: uuid.UUID,
    source_id: uuid.UUID,
    document_ids: Sequence[uuid.UUID],
) -> tuple[bool, list[uuid.UUID]]:
    """``(source_present, ids with no document row)`` for the cleanup sweeps.

    Only an id without any row, in any project, may lose its objects: a document
    that was handed over or never removed keeps them.
    """

    row = (
        await session.execute(
            _SWEEP,
            {
                "project_id": project_id,
                "source_id": source_id,
                "document_ids": list(document_ids),
            },
        )
    ).one()
    return bool(row.present), list(row.missing)
