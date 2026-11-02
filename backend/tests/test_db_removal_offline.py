"""Tier 1 (no database): the pure helpers of source removal and its error mapping.

Requirements: FR-SRC-0016, FR-SRC-0017, NFR-REL-0003, SD3, SD7, R01, R11, R14.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import OperationalError

from db import removal
from db.batches import LockConflictError
from db.models import Document
from db.removal import (
    LockTimeoutError,
    RevisionChainError,
    _hand_over_target,
    _needs_reread,
    _object_prefixes_for,
    _sweep_ids_for,
    drive_document_id,
    remove_source,
)

PID = uuid.UUID("11111111-1111-1111-1111-111111111111")
SID = uuid.UUID("22222222-2222-2222-2222-222222222222")
T0 = datetime(2026, 10, 21, 10, 0, tzinfo=UTC)


def doc(ext: str) -> str:
    return str(drive_document_id(SID, ext))


def prefix(doc_id: str) -> str:
    return f"projects/{PID}/documents/{doc_id}/"


def prefixes_for(items, *, deleted=(), protected=(), kind="gdrive") -> list[str]:
    return _object_prefixes_for(
        PID,
        SID,
        kind=kind,
        items=items,
        deleted_ids=deleted,
        protected=set(protected),
    )


# Hand-over target and re-read (SD7) ---------------------------------------------------


def test_the_hand_over_target_is_the_oldest_lister_then_the_smallest_id() -> None:
    a, b, c = (uuid.UUID(int=n) for n in (3, 1, 2))
    assert _hand_over_target([(a, T0 + timedelta(minutes=2)), (b, T0 + timedelta(minutes=1))]) == b
    assert _hand_over_target([(a, T0), (b, T0), (c, T0)]) == b  # same time: smallest id
    assert _hand_over_target([(a, T0)]) == a
    assert _hand_over_target([]) is None


@pytest.mark.parametrize(
    ("status", "media", "expected"),
    [
        ("parsed", "application/pdf", False),
        ("ingested", "application/pdf", False),
        ("partially_ingested", "application/pdf", False),
        ("uploaded", "application/pdf", True),
        ("processing", "application/pdf", True),
        ("failed", "application/pdf", True),
        ("failed", "text/markdown", True),  # every processed format is read again
        (
            "uploaded",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            True,
        ),
        ("parsed", "text/markdown", False),
        (
            "failed",
            "application/octet-stream",
            False,
        ),  # a stored-only file has nothing to read
    ],
)
def test_a_handed_over_document_is_read_again_only_when_not_done(status, media, expected) -> None:
    assert _needs_reread(status, media) is expected


# Object prefixes per item state (SPECS 5) ---------------------------------------------


@pytest.mark.parametrize("state", ["stored", "processing"])
def test_a_stored_or_processing_item_has_its_document_prefix(state) -> None:
    assert prefixes_for([("f1", state)]) == [prefix(doc("f1"))]


@pytest.mark.parametrize("state", ["pending", "processed", "skipped", "error", "duplicate"])
def test_other_item_states_have_no_uuid5_prefix(state) -> None:
    assert prefixes_for([("f1", state)]) == []


def test_a_deleted_document_gives_its_prefix_once_whatever_its_extension() -> None:
    own = drive_document_id(SID, "f1")
    plain = uuid.uuid4()
    assert prefixes_for([("f1", "processing")], deleted=[own, plain, plain]) == sorted(
        [prefix(str(own)), prefix(str(plain))]
    )


def test_a_prefix_ends_with_a_slash_so_one_id_never_matches_another() -> None:
    (only,) = prefixes_for([], deleted=[uuid.UUID(int=1)])
    assert only.endswith("/")
    assert not f"projects/{PID}/documents/{uuid.UUID(int=10)}/original.pdf".startswith(only)


def test_r01_a_handed_over_or_still_present_id_contributes_no_prefix() -> None:
    items = [("f1", "processing"), ("f2", "stored"), ("f3", "processing")]
    protected = {drive_document_id(SID, "f1"), drive_document_id(SID, "f2")}
    assert prefixes_for(items, protected=protected) == [prefix(doc("f3"))]


def test_r01_the_guard_does_not_hide_the_prefix_of_an_unprotected_item() -> None:
    assert prefixes_for([("f1", "processing")], protected={uuid.uuid4()}) == [prefix(doc("f1"))]


def test_an_upload_batch_never_derives_uuid5_prefixes() -> None:
    items = [("hash1", "processing"), ("hash2", "stored")]
    gone = uuid.uuid4()
    assert prefixes_for(items, kind="upload", deleted=[gone]) == [prefix(str(gone))]
    assert _sweep_ids_for(SID, kind="upload", items=items, deleted_ids=[]) == []


def test_the_sweep_ids_are_deleted_docs_plus_stored_and_processing_items() -> None:
    gone = uuid.uuid4()
    items = [("f1", "stored"), ("f2", "processing"), ("f3", "processed"), ("f4", "pending")]
    got = _sweep_ids_for(SID, kind="gdrive", items=items, deleted_ids=[gone, gone])
    assert got == [gone, drive_document_id(SID, "f1"), drive_document_id(SID, "f2")]


# The id list bind and the errors ----------------------------------------------------


def test_an_id_list_is_one_bind_parameter() -> None:
    compiled = removal._any(Document.id, [uuid.uuid4() for _ in range(40_000)]).compile(dialect=postgresql.dialect())
    assert "ANY" in str(compiled)
    assert len(compiled.params) == 1


class _OrigError(Exception):
    def __init__(self, sqlstate: str, text: str = "") -> None:
        super().__init__(text)
        self.sqlstate = sqlstate


class _Failing:
    def __init__(self, sqlstate: str, text: str = "") -> None:
        self.error = OperationalError("stmt", {}, _OrigError(sqlstate, text))

    async def execute(self, *_args, **_kwargs):
        raise self.error


@pytest.mark.parametrize(
    ("sqlstate", "text", "expected"),
    [
        ("55P03", "lock timeout", LockTimeoutError),
        ("40P01", "deadlock", LockConflictError),
        ("40001", "could not serialize", LockConflictError),
        ("23503", 'violates "fk_connection_items_document_id_documents"', LockConflictError),
        ("23503", 'violates "fk_documents_previous_revision_id_documents"', RevisionChainError),
    ],
)
async def test_database_errors_become_typed_errors(sqlstate, text, expected) -> None:
    with pytest.raises(expected):
        await remove_source(_Failing(sqlstate, text), PID, SID)


async def test_an_unknown_database_error_is_not_swallowed() -> None:
    with pytest.raises(OperationalError):
        await remove_source(_Failing("42P01"), PID, SID)
