"""Tier 1 (no database): the batch schema of migration 0001_initial, the repair statements and the new source writes.

Requirements: DATA-SRC-0001, DATA-SRC-0002, DATA-SRC-0003, INT-SRC-0001, FR-SRC-0021.
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
from sqlalchemy.dialects import postgresql

from db import models, source_repair, sources
from db.sources import DocumentFile
from db_recorder_support import Recorder

BACKEND = Path(__file__).resolve().parents[1]
ALEMBIC_INI = BACKEND / "alembic.ini"
MIGRATION = BACKEND / "migrations" / "versions" / "0001_initial.py"
HEAD = "0001_initial"
ID = uuid.uuid4()


def offline(target: str, *, down: bool = False) -> str:
    buffer = StringIO()
    cfg = Config(str(ALEMBIC_INI), output_buffer=buffer)
    cfg.attributes["database_url"] = "postgresql+asyncpg://u:p@offline.invalid/db"
    if down:
        command.downgrade(cfg, target, sql=True)
    else:
        command.upgrade(cfg, target, sql=True)
    return buffer.getvalue()


def squash(sql: str) -> str:
    return " ".join(sql.split())


def unscoped_repair() -> str:
    return "\n".join(source_repair.repair_statements(project_scoped=False))


# Migration 0001_initial: the final batch shape -----------------------------------------


def test_the_initial_revision_renders_the_batch_ddl_with_the_planned_names() -> None:
    sql = squash(offline(HEAD))

    for fragment in (
        "CONSTRAINT ck_connections_kind CHECK (kind IN ('gdrive', 'upload'))",
        "link TEXT, name TEXT NOT NULL",  # nullable: a Local files batch has no link
        "CONSTRAINT ck_connections_link_for_drive CHECK (kind <> 'gdrive' OR link IS NOT NULL)",
        "expected_files INTEGER, ended_at TIMESTAMP WITH TIME ZONE, name_suffix INTEGER DEFAULT 1 NOT NULL",
        (
            "CONSTRAINT ck_connections_expected_files CHECK "
            "(expected_files IS NULL OR expected_files BETWEEN 1 AND 10000)"
        ),
        "CONSTRAINT ck_connections_name_suffix CHECK (name_suffix >= 1)",
        (
            "CONSTRAINT ck_connection_items_state CHECK "
            "(state IN ('pending', 'processing', 'processed', 'stored', 'skipped', "
            "'error', 'duplicate'))"
        ),
        (
            "CONSTRAINT ck_connection_items_duplicate_has_document CHECK "
            "(state <> 'duplicate' OR document_id IS NOT NULL)"
        ),
        "CREATE INDEX ix_connection_items_document_id ON connection_items (document_id)",
        "source_id UUID, CONSTRAINT pk_documents PRIMARY KEY (id)",
        "CONSTRAINT fk_documents_source_id_connections FOREIGN KEY(source_id) REFERENCES connections (id)",
        "CREATE INDEX ix_documents_source_id ON documents (source_id)",
        (
            "CREATE INDEX ix_documents_unheld ON documents (project_id) WHERE source_id IS "
            "NULL AND origin IN ('upload', 'connection')"
        ),
        "removed_files INTEGER DEFAULT 0 NOT NULL",
        "INSERT INTO alembic_version (version_num) VALUES ('0001_initial')",
    ):
        assert fragment in sql, fragment
    assert "ON DELETE" not in sql  # the holder FK has no cascade
    assert "link TEXT NOT NULL" not in sql


def test_each_batch_check_is_declared_once_in_its_create_table() -> None:
    sql = offline(HEAD)

    assert "DROP CONSTRAINT" not in sql and "ADD CONSTRAINT" not in sql
    for name in ("ck_connections_kind", "ck_connection_items_state"):
        assert sql.count(f"CONSTRAINT {name} CHECK") == 1


def test_the_initial_revision_has_no_backfill() -> None:
    sql = offline(HEAD)

    assert "INSERT INTO connections" not in sql
    assert "INSERT INTO connection_items" not in sql
    assert "UPDATE " not in sql
    assert "legacy-" not in sql


def test_the_repair_runs_b1_to_b5_in_order() -> None:
    sql = unscoped_repair()

    positions = [
        sql.index("INSERT INTO connections ("),
        sql.index("INSERT INTO connection_items ("),
        sql.index("UPDATE documents d SET source_id = c.id"),
        sql.index("UPDATE documents d SET source_id = h.connection_id"),
        sql.index("UPDATE connection_items i\nSET state = 'duplicate'"),
    ]
    assert positions == sorted(positions)
    assert sql.count("INSERT INTO connections (") == 1


def test_the_repair_is_idempotent_by_construction() -> None:
    sql = squash(unscoped_repair())

    assert "ON CONFLICT (project_id, kind, external_id) DO NOTHING" in sql
    assert "ON CONFLICT (connection_id, external_id) DO NOTHING" in sql
    assert sql.count("d.source_id IS NULL") >= 4  # B1, B2, B3, B4
    # B5 only moves a linker once: the state filter excludes ``duplicate``
    assert "i.state IN ('processed', 'error')" in sql


def test_the_repair_day_key_is_the_utc_day_and_never_the_generated_id() -> None:
    sql = squash(unscoped_repair())

    day = "to_char((d.created_at AT TIME ZONE 'UTC')::date, 'YYYYMMDD')"
    assert sql.count(day) == 3  # the B1 select list, the B2 join and the B3 join
    assert "'legacy-' || f.day_key" in sql
    # items join the batch by external_id, not by a generated value
    assert "c.external_id = 'legacy-' || " + day in sql
    assert "COALESCE(f.uploaded_by, p.created_by)" in sql  # connected_by is NOT NULL


def test_the_holder_column_stays_out_of_the_agent_grant() -> None:
    sql = offline(HEAD)

    grants = [line for line in sql.splitlines() if "GRANT" in line.upper()]
    assert len(grants) == 1 and "source_id" not in grants[0]
    repair = unscoped_repair().upper()
    assert "GRANT" not in repair and "POLICY" not in repair and "PG_ROLES" not in repair


def test_the_batch_check_constraints_and_indexes_equal_the_models() -> None:
    sql = squash(offline(HEAD))
    for table in (models.Connection.__table__, models.ConnectionItem.__table__):
        for constraint in table.constraints:
            if constraint.__class__.__name__ == "CheckConstraint":
                assert f"CONSTRAINT {constraint.name} CHECK" in sql, constraint.name
    new_indexes = {
        "ix_connection_items_document_id",
        "ix_documents_source_id",
        "ix_documents_unheld",
    }
    model_indexes = {
        str(index.name)
        for table in (models.Document.__table__, models.ConnectionItem.__table__)
        for index in table.indexes
    }
    assert new_indexes <= model_indexes
    for name in new_indexes:
        assert f"CREATE INDEX {name} ON" in sql, name


# Migration 0001_initial, downgrade -------------------------------------------------------


def test_the_downgrade_only_drops_the_tables() -> None:
    sql = offline(f"{HEAD}:base", down=True)

    assert re.findall(r"DROP TABLE (\w+)", sql) == [
        "connection_items",
        "documents",
        "connections",
        "project_members",
        "projects",
        "users",
    ]
    # no data step and no piecewise undo: dropping a table drops its keys, indexes and policy
    for absent in ("UPDATE ", "DROP COLUMN", "DROP CONSTRAINT", "DROP INDEX", "DROP POLICY"):
        assert absent not in sql, absent
    assert "DELETE FROM" not in sql.replace("DELETE FROM alembic_version", "")


def test_the_migration_never_imports_the_source_tree() -> None:
    text = MIGRATION.read_text(encoding="utf-8")

    imports = re.findall(r"^\s*(?:from|import)\s+([\w.]+)", text, flags=re.MULTILINE)
    assert not [m for m in imports if m.split(".")[0] in {"db", "api", "src", "temporal"}]


# The repair statements: one text, with or without a project scope ---------------------------


def test_the_unscoped_statements_are_the_scoped_ones_without_the_project_filter() -> None:
    unscoped = source_repair.repair_statements(project_scoped=False)
    scoped = source_repair.repair_statements(project_scoped=True)

    unfiltered = [s.replace("AND d.project_id = :pid", "").replace("AND i.project_id = :pid", "") for s in scoped]
    assert [squash(s) for s in unscoped] == [squash(s) for s in unfiltered]
    assert len(unscoped) == len(source_repair.STEPS) == 5
    assert all(":pid" not in s for s in unscoped)


def test_the_scoped_statements_filter_the_project_everywhere_it_matters() -> None:
    scoped = [squash(s) for s in source_repair.repair_statements(project_scoped=True)]

    assert all(":pid" in s for s in scoped)
    assert scoped[0].count("d.project_id = :pid") == 1  # the first upload per day
    assert scoped[1].count("d.project_id = :pid") == 1
    assert scoped[2].count("d.project_id = :pid") == 1
    assert "i.project_id = :pid" in scoped[3] and "d.project_id = :pid" in scoped[3]
    assert "i.project_id = :pid" in scoped[4]


class Result:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class RecordingSession:
    def __init__(self, *rowcounts: int) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self._rowcounts = list(rowcounts)

    async def execute(self, statement: object, params: dict[str, object]) -> Result:
        self.calls.append((str(statement), params))
        return Result(self._rowcounts.pop(0) if self._rowcounts else 0)


async def test_adopt_unheld_runs_b1_to_b5_for_one_project_and_counts_the_holders() -> None:
    session = RecordingSession(0, 2, 5, 3, 4, 1)  # the first call is the project lock

    adopted = await source_repair.adopt_unheld(session, ID)

    assert adopted == 7  # B3 + B4 set documents.source_id
    assert len(session.calls) == 6
    assert all(params == {"pid": ID} for _, params in session.calls)
    lock, *steps = [squash(sql) for sql, _ in session.calls]
    assert lock == "SELECT 1 FROM projects WHERE id = :pid FOR NO KEY UPDATE"
    assert steps == [squash(s) for s in source_repair.repair_statements(project_scoped=True)]


async def test_adopt_unheld_without_a_project_runs_the_unscoped_statements() -> None:
    session = RecordingSession()

    assert await source_repair.adopt_unheld(session, None) == 0

    assert len(session.calls) == 5  # no project lock for the global run
    assert all(params == {} and ":pid" not in sql for sql, params in session.calls)


# db.sources: the new worker-facing writes ---------------------------------------------------


def test_the_vocabularies_know_the_new_kind_and_states() -> None:
    assert models.CONNECTION_KINDS == ("gdrive", "upload")
    assert {"processing", "duplicate"} <= set(models.ITEM_STATES)
    assert {
        "processed",
        "stored",
        "skipped",
        "error",
        "duplicate",
    } == sources.FINAL_ITEM_STATES
    assert "processing" not in sources.FINAL_ITEM_STATES
    assert "pending" not in sources.FINAL_ITEM_STATES


async def test_lock_importing_connection_takes_a_share_lock_on_an_importing_source() -> None:
    row = object()
    fake = Recorder([row])

    got = await sources.lock_importing_connection(fake, ID, ID)

    assert got is row
    assert fake.sql[0].rstrip().endswith("FOR SHARE")
    assert "connections.state = $" in fake.sql[0] and "connections.project_id = $" in fake.sql[0]


async def test_claim_item_locks_the_source_then_claims_pending_or_processing() -> None:
    item = object()
    fake = Recorder([object()], [item])

    assert await sources.claim_item(fake, ID, ID, ID) is item

    lock, claim = fake.sql
    assert "FOR SHARE" in lock
    assert claim.lstrip().startswith("UPDATE connection_items")
    assert "connection_items.state IN (" in claim
    assert "connection_items.connection_id = $" in claim and "RETURNING" in claim
    assert "connection_items.project_id = $" in claim


async def test_claim_item_refreshes_a_row_the_session_already_holds() -> None:
    seen: list[object] = []

    class Spy(Recorder):
        async def scalar(self, statement: object) -> object | None:
            seen.append(statement)
            return await super().scalar(statement)

    await sources.claim_item(Spy([object()], [object()]), ID, ID, ID)

    update = seen[-1]
    assert update.get_execution_options()["populate_existing"] is True


async def test_claim_item_does_nothing_when_the_source_is_gone_or_not_importing() -> None:
    fake = Recorder([])

    assert await sources.claim_item(fake, ID, ID, ID) is None

    assert len(fake.sql) == 1  # no UPDATE was sent


async def test_finish_item_accepts_duplicate_only_with_its_document() -> None:
    with pytest.raises(ValueError, match="needs the document"):
        await sources.finish_item(Recorder(), ID, "duplicate")
    fake = Recorder([ID])

    assert await sources.finish_item(fake, ID, "duplicate", document_id=ID) is True
    assert "connection_items.state IN (" in fake.sql[0]


async def test_fail_unfinished_items_ends_pending_and_processing_files() -> None:
    fake = Recorder([1, 2])

    assert await sources.fail_unfinished_items(fake, ID) == 2

    assert "connection_items.state IN (" in fake.sql[0]


async def test_list_connections_filters_by_kind_only_when_asked() -> None:
    plain, filtered = Recorder(), Recorder()

    await sources.list_connections(plain, ID)
    await sources.list_connections(filtered, ID, "upload")
    with pytest.raises(ValueError, match="connection kind"):
        await sources.list_connections(Recorder(), ID, "dropbox")

    assert "connections.kind = $" not in plain.sql[0]
    assert "connections.kind = $" in filtered.sql[0]


async def test_insert_connection_document_writes_the_holder() -> None:
    fake = Recorder([object()])

    await sources.insert_connection_document(
        fake,
        file=DocumentFile(
            document_id=ID,
            content_hash="0" * 64,
            filename="a",
            size_bytes=1,
            original_key="k",
        ),
        project_id=ID,
        media_type="x",
        source_id=ID,
    )

    assert "source_id" in fake.sql[0]


def test_the_legacy_progress_statement_reads_drive_sources_and_counts_the_final_set() -> None:
    sql = str(sources._sources_progress_statement(ID).compile(dialect=postgresql.asyncpg.dialect()))

    assert "connections.kind = $" in sql  # gdrive only: the 12 h rule is Drive-only
    assert "connection_items.state IN (" in sql  # finished = the final set
    assert "connection_items.state != $" not in sql
    assert "documents.origin" in sql  # the uploads aggregate stays for old tabs


def test_a_finished_drive_file_is_one_in_a_final_state() -> None:
    row = SimpleNamespace(state="duplicate")

    assert row.state in sources.FINAL_ITEM_STATES
