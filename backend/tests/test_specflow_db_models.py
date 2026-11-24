"""Tier 1: the ORM models match SPECS section 6 / AN section 3.5 (no database)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import CheckConstraint, Table, UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from db.models import (
    PROJECT_ROLES,
    AtlassianConnection,
    Document,
    JiraIssue,
    JiraTarget,
    Project,
    ProjectInvite,
    ProjectMember,
    User,
)


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
        "CREATE UNIQUE INDEX ix_users_auth_subject ON users (auth_subject) WHERE auth_subject IS NOT NULL",
        "CREATE INDEX ix_users_email_lower ON users (lower(email))",
    }


def test_projects_and_members_constraints() -> None:
    projects: Table = Project.__table__
    members: Table = ProjectMember.__table__

    assert checks(projects) == {"ck_projects_name_not_blank": r"name !~ '^\s*$'"}
    assert "CONSTRAINT fk_projects_created_by_users FOREIGN KEY(created_by)" in ddl(projects)
    assert PROJECT_ROLES == ("owner", "collaborator")
    assert checks(members) == {"ck_project_members_role": "role IN ('owner', 'collaborator')"}
    assert "CONSTRAINT pk_project_members PRIMARY KEY (project_id, user_id)" in ddl(members)
    assert members.c.role.server_default is None  # DM3: every insert states its role
    assert index_ddl(members) == {
        "CREATE INDEX ix_project_members_user_id ON project_members (user_id)",
        "CREATE UNIQUE INDEX ux_project_members_one_owner ON project_members (project_id) WHERE role = 'owner'",
    }


def test_project_invites_columns_constraints_and_indexes() -> None:
    table: Table = ProjectInvite.__table__

    assert [c.name for c in table.columns] == ["id", "project_id", "email_norm", "invited_by", "created_at"]
    assert all(not c.nullable for c in table.columns)
    assert checks(table) == {"ck_project_invites_email_norm": "email_norm = lower(btrim(email_norm))"}
    text = ddl(table)
    assert "CONSTRAINT uq_project_invites_project_id_email_norm UNIQUE (project_id, email_norm)" in text
    assert "CONSTRAINT fk_project_invites_project_id_projects FOREIGN KEY(project_id) REFERENCES projects (id)" in text
    assert "CONSTRAINT fk_project_invites_invited_by_users FOREIGN KEY(invited_by) REFERENCES users (id)" in text
    assert "ON DELETE" not in text  # projects are never deleted: no cascade
    assert index_ddl(table) == {"CREATE INDEX ix_project_invites_email_norm ON project_invites (email_norm)"}


def test_jira_targets_are_keyed_by_project_with_checked_run_columns_and_no_credential() -> None:
    table: Table = JiraTarget.__table__

    assert [c.name for c in table.columns] == [
        "project_id",
        "jira_project",
        "epic_key",
        "enabled",
        "updated_at",
        "last_status",
        "last_trigger",
        "last_started_at",
        "last_finished_at",
        "last_error_code",
        "last_counts",
        "connected_user_id",
    ]
    assert {c.name for c in table.columns if not c.nullable} == {"project_id", "jira_project", "enabled", "updated_at"}
    assert set(checks(table)) == {"ck_jira_targets_last_status", "ck_jira_targets_last_trigger"}
    assert "'partial'" in checks(table)["ck_jira_targets_last_status"]
    text = ddl(table)
    assert "CONSTRAINT pk_jira_targets PRIMARY KEY (project_id)" in text
    assert "CONSTRAINT fk_jira_targets_project_id_projects FOREIGN KEY(project_id) REFERENCES projects (id)" in text
    # projects are never deleted: no cascade; a deleted user leaves the target without an acting account
    assert "FOREIGN KEY(connected_user_id) REFERENCES users (id) ON DELETE SET NULL" in text
    assert text.count("ON DELETE") == 1


def test_atlassian_connections_are_keyed_by_user_with_a_checked_status_and_a_binary_refresh_token() -> None:
    table: Table = AtlassianConnection.__table__

    assert [c.name for c in table.columns] == [
        "user_id",
        "cloud_id",
        "account_id",
        "account_name",
        "scopes",
        "refresh_token",
        "status",
        "connected_at",
        "last_used_at",
        "expired_at",
    ]
    assert {c.name for c in table.columns if c.nullable} == {"expired_at"}
    assert checks(table) == {"ck_atlassian_connections_status": "status IN ('active', 'expired')"}
    text = ddl(table)
    assert "CONSTRAINT pk_atlassian_connections PRIMARY KEY (user_id)" in text
    assert "refresh_token BYTEA NOT NULL" in text
    assert (
        "CONSTRAINT fk_atlassian_connections_user_id_users FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE"
    ) in text


def test_jira_issues_have_a_composite_key_a_kind_check_and_a_partial_unique_key_index() -> None:
    table: Table = JiraIssue.__table__

    assert {c.name for c in table.columns if not c.nullable} == {"project_id", "node_id", "kind", "requested_at"}
    assert checks(table) == {"ck_jira_issues_kind": "kind IN ('epic', 'story', 'subtask')"}
    text = ddl(table)
    assert "CONSTRAINT pk_jira_issues PRIMARY KEY (project_id, node_id)" in text
    assert "ON DELETE" not in text
    assert index_ddl(table) == {
        "CREATE UNIQUE INDEX ux_jira_issues_jira_key ON jira_issues (project_id, jira_key) WHERE jira_key IS NOT NULL"
    }
