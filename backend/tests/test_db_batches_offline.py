"""Tier 1 (no database): batch write helpers, the outcome table and ``mark_processing``.

Requirements: INT-SRC-0004, FR-SRC-0020, FR-SRC-0021, FR-SRC-0022, DATA-SRC-0003.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import DBAPIError, OperationalError

from db import batches, removal
from db import repositories as repo
from db.batches import AddOutcome, decide_outcome
from db.sources import DocumentFile


class _OrigError(Exception):
    def __init__(self, sqlstate: str | None, text: str = "") -> None:
        super().__init__(text)
        self.sqlstate = sqlstate


def db_error(sqlstate: str | None, text: str = "") -> DBAPIError:
    return OperationalError("statement", {}, _OrigError(sqlstate, text))


# Outcome table (SPECS 4.4 rule 4) -----------------------------------------------------

UPLOAD, DRIVE = "upload", "gdrive"


@pytest.mark.parametrize(
    ("is_batch", "kind", "status", "stale", "expected"),
    [
        (True, UPLOAD, "parsed", False, "duplicate"),
        (True, UPLOAD, "failed", True, "duplicate"),  # this batch holds it: nothing to move
        (False, UPLOAD, "failed", False, "read_again"),
        (False, UPLOAD, "processing", True, "read_again"),  # R10: stale processing = failed
        (False, UPLOAD, "uploaded", True, "read_again"),  # M1: stale uploaded = failed
        (False, UPLOAD, "processing", False, "already_in_project"),
        (False, UPLOAD, "uploaded", False, "already_in_project"),
        (False, UPLOAD, "parsed", False, "already_in_project"),
        (False, UPLOAD, "ingested", True, "already_in_project"),
        (False, UPLOAD, "partially_ingested", False, "already_in_project"),
        (False, DRIVE, "failed", False, "already_in_project"),  # SD6
        (False, DRIVE, "processing", True, "already_in_project"),
        (False, None, "failed", False, "already_in_project"),  # no holder (R15)
        (False, None, "uploaded", True, "already_in_project"),
    ],
)
def test_the_outcome_table(is_batch, kind, status, stale, expected) -> None:
    assert decide_outcome(holder_is_batch=is_batch, holder_kind=kind, doc_status=status, doc_stale=stale) == expected


def test_only_an_added_file_keeps_the_uploaded_object() -> None:
    for outcome, discard in (
        ("added", False),
        ("read_again", True),
        ("already_in_project", True),
        ("duplicate", True),
    ):
        assert AddOutcome(outcome, None, None, None, None).discard_upload is discard


# Errors ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", ["40P01", "40001"])
def test_deadlock_and_serialization_failures_are_a_lock_conflict(code) -> None:
    assert isinstance(batches.conflict_from(db_error(code)), batches.LockConflictError)


@pytest.mark.parametrize("code", [None, "23505", "55P03", "42P01"])
def test_other_database_errors_are_not_translated(code) -> None:
    assert batches.conflict_from(db_error(code)) is None


def test_a_lock_wait_over_the_limit_is_a_lock_timeout() -> None:
    assert isinstance(batches.timeout_from(db_error("55P03")), batches.LockTimeoutError)
    assert batches.timeout_from(db_error("40P01")) is None
    assert batches.LockTimeoutError is removal.LockTimeoutError


class _Failing:
    async def execute(self, *_a, **_k):
        raise db_error("55P03")


async def test_add_start_and_legacy_map_a_lock_wait_timeout() -> None:
    pid = uuid.uuid4()
    with pytest.raises(batches.LockTimeoutError):
        await batches.start_batch(_Failing(), pid, pid, 1)
    with pytest.raises(batches.LockTimeoutError):
        await batches.add_batch_file(
            _Failing(),
            pid,
            pid,
            file=DocumentFile(
                document_id=pid,
                content_hash="h",
                filename="f",
                size_bytes=1,
                original_key="k",
            ),
            user_id=pid,
        )
    with pytest.raises(batches.LockTimeoutError):
        await batches.legacy_day_batch(_Failing(), pid, pid, datetime.now(UTC))


def test_sqlstate_reads_the_driver_cause_too() -> None:
    outer = Exception("wrapper")
    outer.__cause__ = _OrigError("40P01")
    assert batches.sqlstate(OperationalError("s", {}, outer)) == "40P01"


async def test_start_batch_refuses_an_expected_count_outside_the_database_check() -> None:
    for bad in (0, -1, batches.MAX_EXPECTED_FILES + 1):
        with pytest.raises(ValueError, match="expected_files must be 1"):
            await batches.start_batch(None, uuid.uuid4(), uuid.uuid4(), bad)


# mark_processing (R10) -------------------------------------------------------------


class _Capture:
    def __init__(self) -> None:
        self.sql: list[str] = []

    async def scalars(self, statement):
        self.sql.append(str(statement.compile(dialect=postgresql.dialect())))

        class _Rows:
            def one_or_none(self):
                return None

        return _Rows()


async def _mark_sql(**kwargs) -> str:
    capture = _Capture()
    await repo.mark_processing(capture, uuid.uuid4(), uuid.uuid4(), **kwargs)
    return " ".join(capture.sql[0].split())


async def test_mark_processing_keeps_todays_statement_by_default() -> None:
    where = (await _mark_sql()).split("WHERE", 1)[1]
    assert " OR " not in where
    assert "updated_at <" not in where


async def test_mark_processing_stale_branch_adds_processing_and_uploaded_older_than_60_min() -> None:
    where = (await _mark_sql(include_stale=True)).split("WHERE", 1)[1]
    assert " OR " in where
    assert "documents.updated_at < now() -" in where
    assert repo.UPLOAD_STALE_AFTER.total_seconds() == 3600
