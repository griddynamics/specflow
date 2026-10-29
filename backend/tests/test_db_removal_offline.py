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
    _object_keys_for,
    _sweep_ids_for,
    drive_document_id,
    remove_source,
)
from storage import keys

PID = uuid.UUID("11111111-1111-1111-1111-111111111111")
SID = uuid.UUID("22222222-2222-2222-2222-222222222222")
T0 = datetime(2026, 10, 21, 10, 0, tzinfo=UTC)


def doc(ext: str) -> str:
    return str(drive_document_id(SID, ext))


def variants(ext: str) -> set[str]:
    return {
        keys.document_original(str(PID), doc(ext)),
        keys.document_original_json(str(PID), doc(ext)),
        keys.document_original_bin(str(PID), doc(ext)),
    }


def keys_for(items, *, deleted=(), protected=(), kind="gdrive") -> list[str]:
    return _object_keys_for(
        PID,
        SID,
        kind=kind,
        items=items,
        deleted_original_keys=deleted,
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
        ("failed", "text/markdown", False),  # only a PDF is read again
        ("uploaded", "text/markdown", False),
    ],
)
def test_a_handed_over_pdf_is_read_again_only_when_not_done(status, media, expected) -> None:
    assert _needs_reread(status, media) is expected


# Object keys per item state (SPECS 5) -------------------------------------------------


def test_a_stored_item_has_only_the_bin_key() -> None:
    assert keys_for([("f1", "stored")]) == [keys.document_original_bin(str(PID), doc("f1"))]


def test_a_processing_item_has_all_three_variants() -> None:
    assert set(keys_for([("f1", "processing")])) == variants("f1")


@pytest.mark.parametrize("state", ["pending", "processed", "skipped", "error", "duplicate"])
def test_other_item_states_have_no_uuid5_key(state) -> None:
    assert keys_for([("f1", state)]) == []


def test_a_deleted_document_gives_its_original_key_once() -> None:
    own = keys.document_original(str(PID), doc("f1"))
    assert keys_for([("f1", "processing")], deleted=[own]) == sorted(variants("f1"))
    assert keys_for([], deleted=["a/b", "a/b"]) == ["a/b"]


def test_r01_a_handed_over_or_still_present_id_contributes_no_key() -> None:
    items = [("f1", "processing"), ("f2", "stored"), ("f3", "processing")]
    protected = {drive_document_id(SID, "f1"), drive_document_id(SID, "f2")}
    assert set(keys_for(items, protected=protected)) == variants("f3")


def test_r01_the_guard_does_not_hide_the_key_of_an_unprotected_item() -> None:
    assert set(keys_for([("f1", "processing")], protected={uuid.uuid4()})) == variants("f1")


def test_an_upload_batch_never_derives_uuid5_keys() -> None:
    items = [("hash1", "processing"), ("hash2", "stored")]
    assert keys_for(items, kind="upload", deleted=["x/original.pdf"]) == ["x/original.pdf"]
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
