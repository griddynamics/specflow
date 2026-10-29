"""Tier 1 (no database): the source tables of migration 0001_initial, the source models and the repository rules.

Requirements: INT-GDUI-0003, INT-GDUI-0004, DATA-GDUI-0001.
"""

from __future__ import annotations

import re
import uuid
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import CheckConstraint, Table, UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from db import models, sources
from db.models import Connection, ConnectionItem, Document
from db.sources import DocumentFile
from db_recorder_support import Recorder

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"
SCOPE = "NULLIF(current_setting('app.project_id', true), '')::uuid"
HEAD = "0001_initial"
CONNECTIONS: Table = Connection.__table__
ITEMS: Table = ConnectionItem.__table__


def offline(target: str, *, down: bool = False) -> str:
    buffer = StringIO()
    cfg = Config(str(ALEMBIC_INI), output_buffer=buffer)
    cfg.attributes["database_url"] = "postgresql+asyncpg://u:p@offline.invalid/db"
    if down:
        command.downgrade(cfg, target, sql=True)
    else:
        command.upgrade(cfg, target, sql=True)
    return buffer.getvalue()


# Migration 0001_initial: the source tables ------------------------------------


def test_the_source_tables_are_created_after_the_tables_they_reference() -> None:
    sql = offline(HEAD)

    tables = re.findall(r"CREATE TABLE (\w+)", sql)
    for parent, child in (
        ("projects", "connections"),
        ("users", "connections"),
        ("connections", "documents"),  # documents.source_id
        ("connections", "connection_items"),
        ("documents", "connection_items"),
    ):
        assert tables.index(parent) < tables.index(child), (parent, child)
    # every column and key is part of its CREATE TABLE: no later ALTER of a source table
    for table in ("connections", "connection_items", "documents"):
        assert f"ALTER TABLE {table} ADD" not in sql
        assert f"ALTER TABLE {table} ALTER" not in sql
    assert "INSERT INTO" not in sql.replace("INSERT INTO alembic_version", "")  # no backfill
    assert "UPDATE " not in sql
    assert "INSERT INTO alembic_version (version_num) VALUES ('0001_initial')" in sql


def test_the_source_tables_name_the_unique_constraints_and_keys() -> None:
    sql = offline(HEAD)

    for fragment in (
        "CONSTRAINT uq_connections_project_kind_external UNIQUE (project_id, kind, external_id)",
        "CONSTRAINT uq_connection_items_connection_external UNIQUE (connection_id, external_id)",
        "CONSTRAINT fk_connection_items_document_id_documents FOREIGN KEY(document_id) REFERENCES documents (id)",
        (
            "CONSTRAINT fk_connection_items_connection_id_connections FOREIGN KEY(connection_id) "
            "REFERENCES connections (id)"
        ),
        "CONSTRAINT fk_connections_connected_by_users FOREIGN KEY(connected_by) REFERENCES users (id)",
        "CREATE INDEX ix_connection_items_connection_id_state ON connection_items (connection_id, state)",
        "CREATE INDEX ix_connections_project_id ON connections (project_id)",
        "CREATE INDEX ix_connection_items_project_id ON connection_items (project_id)",
    ):
        assert fragment in sql
    assert "state TEXT DEFAULT 'listing' NOT NULL" in sql
    assert "state TEXT DEFAULT 'pending' NOT NULL" in sql


def test_the_source_tables_get_rls_with_the_agent_policy_and_no_grant() -> None:
    sql = offline(HEAD)

    assert "-- agent role existence check skipped (offline)" in sql
    for table in ("connections", "connection_items"):
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in sql
        assert (
            f'CREATE POLICY agent_project_scope ON {table} TO "specflow_agent" '
            f"USING (project_id = {SCOPE}) WITH CHECK (project_id = {SCOPE})"
        ) in sql
    grants = [line for line in sql.splitlines() if "GRANT" in line.upper()]
    assert grants and all(" ON documents TO " in line for line in grants)  # the one grant is on documents


def test_the_downgrade_drops_each_source_table_before_what_it_references() -> None:
    sql = offline(f"{HEAD}:base", down=True)

    assert sql.index("DROP TABLE connection_items") < sql.index("DROP TABLE documents")
    assert sql.index("DROP TABLE documents") < sql.index("DROP TABLE connections")  # documents.source_id
    assert sql.index("DROP TABLE connections") < sql.index("DROP TABLE projects")
    assert "DELETE FROM alembic_version WHERE alembic_version.version_num = '0001_initial'" in sql


def test_the_source_check_constraints_equal_the_models() -> None:
    """The migration text and the models carry the same CHECKs."""

    sql = " ".join(offline(HEAD).split())
    for table in (CONNECTIONS, ITEMS):
        for constraint in table.constraints:
            if isinstance(constraint, CheckConstraint):
                expected = " ".join(str(constraint.sqltext).split())
                assert f"CHECK ({expected})" in sql, constraint.name
                assert f"CONSTRAINT {constraint.name} CHECK" in sql


# Models ------------------------------------------------------------------------


def test_documents_gets_only_the_holder_column() -> None:
    assert set(Document.__table__.columns.keys()) == {
        "id",
        "project_id",
        "content_hash",
        "original_filename",
        "media_type",
        "size_bytes",
        "origin",
        "original_key",
        "markdown_key",
        "parser",
        "version",
        "previous_revision_id",
        "status",
        "error",
        "skipped_fragments",
        "uploaded_by",
        "created_at",
        "updated_at",
        "markdown",
        "summarization",
        "summarization_status",
        "summarization_model",
        "summarization_error",
        "summarized_at",
        "source_id",
    }


def test_the_unique_constraints_are_named_and_cover_the_idempotency_keys() -> None:
    def uniques(table: Table) -> dict[str, tuple[str, ...]]:
        return {
            str(c.name): tuple(col.name for col in c.columns)
            for c in table.constraints
            if isinstance(c, UniqueConstraint)
        }

    assert uniques(CONNECTIONS) == {"uq_connections_project_kind_external": ("project_id", "kind", "external_id")}
    assert uniques(ITEMS) == {"uq_connection_items_connection_external": ("connection_id", "external_id")}


@pytest.mark.parametrize(
    ("table", "column"),
    [
        (CONNECTIONS, "project_id"),
        (CONNECTIONS, "kind"),
        (CONNECTIONS, "external_id"),
        (CONNECTIONS, "name"),
        (CONNECTIONS, "state"),
        (CONNECTIONS, "name_suffix"),
        (CONNECTIONS, "connected_by"),
        (ITEMS, "connection_id"),
        (ITEMS, "project_id"),
        (ITEMS, "external_id"),
        (ITEMS, "name"),
        (ITEMS, "mime_type"),
        (ITEMS, "category"),
        (ITEMS, "processing_class"),
        (ITEMS, "state"),
    ],
)
def test_required_columns_are_not_null(table: Table, column: str) -> None:
    assert not table.c[column].nullable


def test_optional_columns_are_nullable() -> None:
    for column in ("link", "failure_reason", "last_synced_at", "expected_files", "ended_at"):
        assert CONNECTIONS.c[column].nullable
    for column in (
        "size_bytes",
        "modified_time",
        "drive_version",
        "md5",
        "error_reason",
        "error_step",
        "document_id",
    ):
        assert ITEMS.c[column].nullable


def test_the_enum_values_equal_the_drive_package() -> None:
    categories = pytest.importorskip("drive.categories")
    reasons = pytest.importorskip("drive.reasons")

    assert tuple(c.value for c in categories.Category) == models.ITEM_CATEGORIES
    assert tuple(c.value for c in categories.ProcessingClass) == models.ITEM_PROCESSING_CLASSES
    assert tuple(r.value for r in reasons.Reason) == models.CONNECTION_FAILURE_REASONS
    assert tuple(r.value for r in reasons.FileErrorReason) == models.ITEM_ERROR_REASONS


# Repository rules (raised before any statement runs) -----------------------------


class NoSession:
    """A session stand-in: any use fails the test, so a rule must raise first."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"the repository touched the session ({name})")


NO_SESSION: AsyncSession = NoSession()
ID = uuid.uuid4()


async def test_create_connection_rejects_an_unknown_kind() -> None:
    with pytest.raises(ValueError, match="connection kind"):
        await sources.create_connection(
            NO_SESSION,
            project_id=ID,
            kind="dropbox",
            external_id="x" * 12,
            link="l",
            name="n",
            connected_by=ID,
        )


@pytest.mark.parametrize(
    ("state", "reason", "message"),
    [
        ("failed", None, "failure reason goes with"),
        ("done", "no_access", "failure reason goes with"),
        ("failed", "import_failed", "failure reason"),  # file-level reason only
        ("paused", None, "connection state"),
    ],
)
async def test_set_connection_state_rejects_bad_combinations(state: str, reason: str | None, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        await sources.set_connection_state(NO_SESSION, ID, state, failure_reason=reason)


@pytest.mark.parametrize(
    ("state", "kwargs", "message"),
    [
        ("pending", {}, "final file state"),
        ("error", {}, "error reason goes with"),
        ("processed", {"error_reason": "convert_failed"}, "error reason goes with"),
        ("error", {"error_reason": "nope"}, "file error reason"),
        (
            "error",
            {"error_reason": "convert_failed", "error_step": "x"},
            "file error step",
        ),
        ("stored", {"error_step": "store"}, "error step goes with"),
    ],
)
async def test_finish_item_rejects_bad_combinations(state: str, kwargs: dict[str, str], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        await sources.finish_item(NO_SESSION, ID, state, **kwargs)


async def test_fail_unfinished_items_rejects_an_unknown_step() -> None:
    with pytest.raises(ValueError, match="file error step"):
        await sources.fail_unfinished_items(NO_SESSION, ID, "x")


async def test_insert_connection_document_rejects_an_unknown_status() -> None:
    with pytest.raises(ValueError, match="document status"):
        await sources.insert_connection_document(
            NO_SESSION,
            file=DocumentFile(
                document_id=ID,
                content_hash="0" * 64,
                filename="a",
                size_bytes=1,
                original_key="k",
            ),
            project_id=ID,
            media_type="application/pdf",
            status="bogus",
        )


def test_item_values_categorize_by_mime_type_never_by_name() -> None:
    drive = pytest.importorskip("drive.model")
    item = drive.DriveItem(
        id="f1",
        name="report.docx",
        mime_type="application/vnd.google-apps.document",
        parent_id="p",
        size=None,
        modified_time="2026-10-19T08:30:00.000Z",
        version=7,
        md5=None,
    )

    values = sources._item_values(ID, ID, item)

    assert (values["category"], values["processing_class"]) == (
        "google_docs",
        "processed",
    )
    assert values["modified_time"].isoformat() == "2026-10-19T08:30:00+00:00"
    assert (values["external_id"], values["drive_version"], values["size_bytes"]) == (
        "f1",
        7,
        None,
    )


def test_item_values_require_a_time_zone() -> None:
    drive = pytest.importorskip("drive.model")
    naive = drive.DriveItem(
        id="f",
        name="n",
        mime_type="application/pdf",
        parent_id="p",
        size=1,
        modified_time="2026-10-19T08:30:00",
        version=1,
        md5=None,
    )

    with pytest.raises(ValueError, match="time zone"):
        sources._item_values(ID, ID, naive)
    zulu = drive.DriveItem(**{**naive.__dict__, "modified_time": "2026-10-19T08:30:00Z"})
    assert sources._item_values(ID, ID, zulu)["modified_time"].utcoffset().total_seconds() == 0


def test_item_values_refuse_a_folder() -> None:
    drive = pytest.importorskip("drive.model")
    folder = drive.DriveItem(
        id="d1",
        name="d",
        mime_type="application/vnd.google-apps.folder",
        parent_id="p",
        size=None,
        modified_time=None,
        version=None,
        md5=None,
    )

    with pytest.raises(ValueError, match="a folder has no file category"):
        sources._item_values(ID, ID, folder)


def test_the_progress_statement_is_one_statement_with_the_read_time_rules() -> None:
    statement = sources._sources_progress_statement(ID)

    sql = str(statement.compile(dialect=postgresql.asyncpg.dialect()))

    assert sql.count("SELECT") >= 4 and sql.count(";") == 0
    assert "UNION ALL" in sql and "FILTER" in sql
    assert "now() - $" in sql and "::INTERVAL" in sql  # both stale rules read the clock
    assert "documents.status IN" in sql  # processing and uploaded both count as open
    assert "documents.origin" in sql and "connections.project_id" in sql


def test_the_read_time_limits_are_the_planned_constants() -> None:
    assert sources.UPLOAD_STALE_AFTER.total_seconds() == 60 * 60
    assert sources.DRIVE_IMPORT_EXECUTION_TIMEOUT.total_seconds() == 12 * 60 * 60


# Statements and result assembly against a recording session ------------------------


def recorder(*answers: list[object]) -> tuple[Recorder, AsyncSession]:
    fake = Recorder(*answers)
    return fake, fake


def drive_item(index: int, mime: str, modified: str | None = None) -> object:
    model = pytest.importorskip("drive.model")
    return model.DriveItem(
        id=f"f{index}",
        name=f"n{index}",
        mime_type=mime,
        parent_id="p",
        size=index,
        modified_time=modified,
        version=index,
        md5=None,
    )


async def test_create_connection_inserts_with_do_nothing_then_reads_the_winner() -> None:
    winner = object()
    fake, session = recorder([], [winner])

    row, created = await sources.create_connection(
        session,
        project_id=ID,
        kind="gdrive",
        external_id="x" * 12,
        link="l",
        name="n",
        connected_by=ID,
    )

    assert (row, created) == (winner, False)
    assert "ON CONFLICT ON CONSTRAINT uq_connections_project_kind_external DO NOTHING" in fake.sql[0]
    assert fake.sql[1].lstrip().startswith("SELECT connections.id")


async def test_create_connection_returns_the_new_row() -> None:
    new = object()
    _, session = recorder([new])

    assert await sources.create_connection(
        session,
        project_id=ID,
        kind="gdrive",
        external_id="x" * 12,
        link="l",
        name="n",
        connected_by=ID,
    ) == (new, True)


async def test_create_connection_gives_up_when_the_row_keeps_vanishing() -> None:
    _, session = recorder()

    with pytest.raises(RuntimeError, match="did not settle"):
        await sources.create_connection(
            session,
            project_id=ID,
            kind="gdrive",
            external_id="x" * 12,
            link="l",
            name="n",
            connected_by=ID,
        )


async def test_delete_and_state_changes_report_whether_a_row_matched() -> None:
    fake, session = recorder([ID], [], [ID], [])

    assert await sources.delete_connection_if_empty(session, ID, ID) is True
    assert await sources.delete_connection_if_empty(session, ID, ID) is False
    assert await sources.set_connection_state(session, ID, "done") is True
    assert await sources.set_connection_state(session, ID, "importing") is False
    assert "NOT (EXISTS" in fake.sql[0]
    assert "last_synced_at=now()" in fake.sql[2] and "last_synced_at" not in fake.sql[3]


async def test_upsert_items_chunks_and_counts_inserted_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sources, "_INSERT_CHUNK", 2)
    items = [drive_item(i, "application/pdf", "2026-10-19T08:00:00Z") for i in range(5)]
    fake, session = recorder([1, 2], [3], [4, 5])

    assert await sources.upsert_items(session, ID, ID, items) == 5
    assert len(fake.sql) == 3
    assert all("DO NOTHING" in sql and "uq_connection_items_connection_external" in sql for sql in fake.sql)
    assert await sources.upsert_items(session, ID, ID, []) == 0


async def test_item_updates_only_touch_unfinished_rows() -> None:
    fake, session = recorder([ID], [], [1, 2, 3])

    assert await sources.finish_item(session, ID, "stored") is True
    assert await sources.finish_item(session, ID, "error", error_reason="no_access") is False
    assert await sources.fail_unfinished_items(session, ID, "list") == 3
    assert all("connection_items.state IN (" in sql for sql in fake.sql)


async def test_insert_connection_document_falls_back_to_the_row_that_won() -> None:
    existing = object()
    fake, session = recorder([], [existing])

    row, created = await sources.insert_connection_document(
        session,
        file=DocumentFile(
            document_id=ID,
            content_hash="0" * 64,
            filename="a",
            size_bytes=1,
            original_key="k",
        ),
        project_id=ID,
        media_type="x",
    )

    assert (row, created) == (existing, False)
    assert "ON CONFLICT DO NOTHING" in fake.sql[0]


def progress_row(kind: str, **fields: object) -> object:
    base = {
        "row_kind": kind,
        "connection_id": None,
        "state": None,
        "failure_reason": None,
        "total": 0,
        "finished": 0,
        "failed": 0,
        "created_at": None,
    }
    return SimpleNamespace(**{**base, **fields})


async def test_sources_progress_assembles_project_and_source_numbers() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    _, session = recorder(
        [
            progress_row("connection", connection_id=a, state="importing", total=23, finished=17),
            progress_row(
                "connection", connection_id=b, state="failed", failure_reason="no_access", total=10, finished=10
            ),
            progress_row("uploads", total=2, finished=2, failed=1),
        ]
    )

    result = await sources.sources_progress(session, ID)

    assert result.progress == sources.Progress(listing_finished=True, total=35, finished=29)
    assert [(c.connection_id, c.state, c.failure_reason) for c in result.connections] == [
        (a, "importing", None),
        (b, "failed", "no_access"),
    ]
    assert result.uploads == sources.UploadsProgress(sources.Progress(listing_finished=True, total=2, finished=2), 2, 1)


async def test_sources_progress_listing_is_finished_only_when_every_source_is() -> None:
    _, session = recorder(
        [
            progress_row("connection", connection_id=ID, state="listing"),
            progress_row("connection", connection_id=uuid.uuid4(), state="done", total=1, finished=1),
            progress_row("uploads"),
        ]
    )

    result = await sources.sources_progress(session, ID)

    assert result.progress == sources.Progress(listing_finished=False, total=1, finished=1)
    assert result.uploads is None  # no upload documents: no uploads card
