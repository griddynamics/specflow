"""Tier 1: the ORM models match SPECS section 6 / AN section 3.5 (no database)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import CheckConstraint, Table, UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from db.models import Document, Project, ProjectMember, User


def ddl(table: Table) -> str:
    return str(CreateTable(table).compile(dialect=postgresql.dialect()))


def checks(table: Table) -> dict[str, str]:
    return {str(c.name): str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)}


def index_ddl(table: Table) -> set[str]:
    return {str(CreateIndex(index).compile(dialect=postgresql.dialect())) for index in table.indexes}


DOCUMENTS: Table = Document.__table__


def test_documents_has_the_spec_columns_plus_the_hello_stack_columns() -> None:
    expected = {
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

    assert set(DOCUMENTS.columns.keys()) == expected
    assert "connection_id" not in DOCUMENTS.columns  # DEV-13
    assert "source_item_id" not in DOCUMENTS.columns


@pytest.mark.parametrize(
    ("column", "default"),
    [
        ("status", "'uploaded'"),
        ("summarization_status", "'none'"),
        ("version", "1"),
        ("skipped_fragments", "0"),
    ],
)
def test_documents_server_defaults(column: str, default: str) -> None:
    assert str(DOCUMENTS.c[column].server_default.arg) == default


def test_documents_check_constraints_list_the_allowed_values() -> None:
    found = checks(DOCUMENTS)

    assert found["ck_documents_status"] == (
        "status IN ('uploaded', 'processing', 'parsed', 'failed', 'ingested', 'partially_ingested')"
    )
    assert found["ck_documents_summarization_status"] == (
        "summarization_status IN ('none', 'running', 'succeeded', 'failed')"
    )
    assert "ck_documents_origin" in found
    assert found["ck_documents_content_hash_sha256"] == "content_hash ~ '^[0-9a-f]{64}$'"


def test_documents_are_unique_per_project_and_content_hash() -> None:
    uniques = [c for c in DOCUMENTS.constraints if isinstance(c, UniqueConstraint)]

    assert [(u.name, [c.name for c in u.columns]) for u in uniques] == [
        ("uq_documents_project_id_content_hash", ["project_id", "content_hash"])
    ]


def test_documents_indexes() -> None:
    assert index_ddl(DOCUMENTS) == {
        "CREATE INDEX ix_documents_project_id_created_at ON documents (project_id, created_at DESC)",
        "CREATE INDEX ix_documents_project_id_status ON documents (project_id, status)",
        "CREATE INDEX ix_documents_source_id ON documents (source_id)",
        (
            "CREATE INDEX ix_documents_unheld ON documents (project_id) "
            "WHERE source_id IS NULL AND origin IN ('upload', 'connection')"
        ),
    }


def test_primary_keys_are_uuids_generated_in_python() -> None:
    for model in (User, Project, Document):
        column = model.__table__.c.id
        assert column.server_default is None
        assert isinstance(column.default.arg(None), uuid.UUID)


def test_users_auth_subject_is_unique_where_present_and_actor_id_unique() -> None:
    table: Table = User.__table__

    assert "CONSTRAINT uq_users_actor_id UNIQUE (actor_id)" in ddl(table)
    assert index_ddl(table) == {
        "CREATE UNIQUE INDEX ix_users_auth_subject ON users (auth_subject) WHERE auth_subject IS NOT NULL"
    }


def test_projects_and_members_constraints() -> None:
    projects: Table = Project.__table__
    members: Table = ProjectMember.__table__

    assert checks(projects) == {"ck_projects_name_not_blank": r"name !~ '^\s*$'"}
    assert "CONSTRAINT fk_projects_created_by_users FOREIGN KEY(created_by)" in ddl(projects)
    assert checks(members) == {"ck_project_members_role": "role IN ('owner')"}
    assert "CONSTRAINT pk_project_members PRIMARY KEY (project_id, user_id)" in ddl(members)
    assert str(members.c.role.server_default.arg) == "'owner'"
