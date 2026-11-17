"""initial schema: every application table, row-level security and the agent grant

The one revision of the development schema (see migrations/README). It creates the
final shape directly; there is no migration history, backfill or upgrade path from
an older database. On a schema change this revision is edited and the database is
recreated (``make down-volumes`` and ``make up``).

Written out by hand (not from the models); ``alembic check`` proves the models still
match. It never imports ``src``.

Tables: users, projects, project_members, connections, documents, connection_items.
A Drive folder and a Local files batch (kind ``upload``) are both ``connections``
rows; ``documents.source_id`` is the one source that holds each piece of content.

Agent access, per the project-scoped data access design:
the agent role (``DATABASE_AGENT_USER``, resolved in migrations/env.py into
``config.attributes["agent_user"]``) gets the policy ``agent_project_scope`` on every
project-scoped table (projects, project_members, documents, connections,
connection_items) and exactly one grant: SELECT on the ``documents`` columns its tools
read, including ``markdown`` (DEV-2b). No grant on any other table; those policies stay
as defense in depth. The table owner (the application role) is not subject to RLS.

The role is provisioned outside migrations (C21): the first-start script
backend/postgres-init/10-specflow-roles.sh creates it on an empty PostgreSQL volume,
locally and on a managed deployment. Online, a missing role, or one with SUPERUSER or BYPASSRLS
(RLS would not apply to it), fails this revision; offline (``--sql``) the check is
skipped with a comment.

downgrade() drops the six tables this revision creates, users, projects,
project_members, connections, documents and connection_items, which also drops their
indexes, constraints, the agent_project_scope policies and the agent role's grant.

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-28 09:00:00

"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001_initial"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TIMESTAMPTZ = sa.DateTime(timezone=True)

POLICY = "agent_project_scope"
# Table -> the column that holds its project id.
SCOPED_TABLES = (
    ("projects", "id"),
    ("project_members", "project_id"),
    ("documents", "project_id"),
    ("connections", "project_id"),
    ("connection_items", "project_id"),
)
AGENT_DOCUMENT_COLUMNS = ("id", "project_id", "original_filename", "version", "status", "markdown")
SCOPE = "NULLIF(current_setting('app.project_id', true), '')::uuid"
# In creation order; downgrade() drops them in reverse, each before the tables it references.
CREATED_TABLES = ("users", "projects", "project_members", "connections", "documents", "connection_items")

CONNECTION_KINDS = ("gdrive", "upload")
CONNECTION_STATES = ("listing", "importing", "done", "failed")
FAILURE_REASONS = (
    "invalid_link",
    "no_access",
    "not_a_folder",
    "already_connected",
    "drive_unavailable",
    "setup_error",
    "too_slow",
    "unknown_error",
)
CATEGORIES = (
    "google_docs",
    "google_slides",
    "google_sheets",
    "pdf",
    "word",
    "powerpoint",
    "excel",
    "images",
    "videos",
    "other_files",
    "other_google",
    "shortcuts",
)
PROCESSING_CLASSES = ("processed", "stored_only", "skipped")
ITEM_STATES = ("pending", "processing", "processed", "stored", "skipped", "error", "duplicate")
FILE_ERROR_REASONS = (
    "no_access",
    "drive_unavailable",
    "convert_failed",
    "parse_failed",
    "store_failed",
    "import_failed",
    "unknown_error",
)
ERROR_STEPS = ("list", "download", "read", "convert", "store", "parse")


def read_migration_sql(name: str) -> str:
    """One statement of ``migrations/sql/<name>``, on one line."""

    text = (Path(__file__).resolve().parent.parent / "sql" / name).read_text(encoding="utf-8")
    return " ".join(text.split()).replace("( ", "(").replace(" )", ")")


def _one_of(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _timestamp(name: str = "created_at") -> sa.Column[Any]:
    return sa.Column(name, TIMESTAMPTZ, server_default=sa.text("now()"), nullable=False)


def _agent_user() -> str:
    name = context.config.attributes.get("agent_user")
    if not name:
        raise RuntimeError("DATABASE_AGENT_USER is not resolved; run through migrations/env.py")
    return str(name)


def _quoted(name: str) -> str:
    return op.get_context().dialect.identifier_preparer.quote_identifier(name)


def _require_role(name: str) -> None:
    if context.is_offline_mode():
        op.get_context().impl.static_output("-- agent role existence check skipped (offline)")
        return
    query = (
        sa.select(sa.column("rolsuper", sa.Boolean), sa.column("rolbypassrls", sa.Boolean))
        .select_from(sa.table("pg_roles"))
        .where(sa.column("rolname") == name)
    )
    row = op.get_bind().execute(query).one_or_none()
    if row is None:
        raise RuntimeError(
            f"The agent role named by DATABASE_AGENT_USER ({name!r}) does not exist. "
            "backend/postgres-init/10-specflow-roles.sh creates it on PostgreSQL's "
            "first start; on another database create the role with "
            "the SQL of that script."
        )
    if row.rolsuper or row.rolbypassrls:
        # Row-level security would silently not apply to this role.
        raise RuntimeError(
            f"The agent role named by DATABASE_AGENT_USER ({name!r}) has SUPERUSER or "
            "BYPASSRLS; it must be NOSUPERUSER NOBYPASSRLS (create the role with the "
            "SQL of that script)."
        )


def _create_users() -> None:
    op.create_table(
        "users",
        sa.Column("id", UUID, nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("auth_subject", sa.Text(), nullable=True),
        _timestamp(),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("actor_id", name="uq_users_actor_id"),
    )
    op.create_index(
        "ix_users_auth_subject",
        "users",
        ["auth_subject"],
        unique=True,
        postgresql_where=sa.text("auth_subject IS NOT NULL"),
    )


def _create_projects() -> None:
    op.create_table(
        "projects",
        sa.Column("id", UUID, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("created_by", UUID, nullable=False),
        sa.Column("pinned_commit_sha", sa.Text(), nullable=True),
        _timestamp(),
        _timestamp("updated_at"),
        # Files of sources removed since the last build (DATA-SRC-0003).
        sa.Column("removed_files", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint(r"name !~ '^\s*$'", name=op.f("ck_projects_name_not_blank")),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name="fk_projects_created_by_users"),
        sa.PrimaryKeyConstraint("id", name="pk_projects"),
    )
    op.create_index("ix_projects_created_by", "projects", ["created_by"])


def _create_project_members() -> None:
    op.create_table(
        "project_members",
        sa.Column("project_id", UUID, nullable=False),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("role", sa.Text(), server_default=sa.text("'owner'"), nullable=False),
        sa.Column("bio", sa.Text(), nullable=True),
        _timestamp("joined_at"),
        sa.CheckConstraint("role IN ('owner')", name=op.f("ck_project_members_role")),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_project_members_project_id_projects"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_project_members_user_id_users"),
        sa.PrimaryKeyConstraint("project_id", "user_id", name="pk_project_members"),
    )
    op.create_index("ix_project_members_user_id", "project_members", ["user_id"])


def _create_connections() -> None:
    # A Drive folder (INT-GDUI-0003) or a Local files batch (kind ``upload``, DATA-SRC-0001).
    op.create_table(
        "connections",
        sa.Column("id", UUID, nullable=False),
        sa.Column("project_id", UUID, nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("link", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), server_default=sa.text("'listing'"), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("connected_by", UUID, nullable=False),
        sa.Column("last_synced_at", TIMESTAMPTZ, nullable=True),
        _timestamp(),
        _timestamp("updated_at"),
        sa.Column("expected_files", sa.Integer(), nullable=True),
        sa.Column("ended_at", TIMESTAMPTZ, nullable=True),
        sa.Column("name_suffix", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.CheckConstraint(_one_of("kind", CONNECTION_KINDS), name=op.f("ck_connections_kind")),
        sa.CheckConstraint(
            "external_id ~ '^[A-Za-z0-9_-]{10,}$'",
            name=op.f("ck_connections_external_id"),
        ),
        sa.CheckConstraint(_one_of("state", CONNECTION_STATES), name=op.f("ck_connections_state")),
        sa.CheckConstraint(
            _one_of("failure_reason", FAILURE_REASONS),
            name=op.f("ck_connections_failure_reason"),
        ),
        sa.CheckConstraint(
            "(state = 'failed') = (failure_reason IS NOT NULL)",
            name=op.f("ck_connections_failure_reason_matches_state"),
        ),
        sa.CheckConstraint("kind <> 'gdrive' OR link IS NOT NULL", name=op.f("ck_connections_link_for_drive")),
        sa.CheckConstraint(
            "expected_files IS NULL OR expected_files BETWEEN 1 AND 10000",
            name=op.f("ck_connections_expected_files"),
        ),
        sa.CheckConstraint("name_suffix >= 1", name=op.f("ck_connections_name_suffix")),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_connections_project_id_projects"),
        sa.ForeignKeyConstraint(["connected_by"], ["users.id"], name="fk_connections_connected_by_users"),
        sa.PrimaryKeyConstraint("id", name="pk_connections"),
        sa.UniqueConstraint(
            "project_id",
            "kind",
            "external_id",
            name="uq_connections_project_kind_external",
        ),
    )
    op.create_index("ix_connections_project_id", "connections", ["project_id"])


def _create_documents() -> None:
    op.create_table(
        "documents",
        sa.Column("id", UUID, nullable=False),
        sa.Column("project_id", UUID, nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("original_filename", sa.Text(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("original_key", sa.Text(), nullable=False),
        sa.Column("markdown_key", sa.Text(), nullable=True),
        sa.Column("parser", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("previous_revision_id", UUID, nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'uploaded'"), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("skipped_fragments", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("uploaded_by", UUID, nullable=True),
        sa.Column("markdown", sa.Text(), nullable=True),
        sa.Column("summarization", sa.Text(), nullable=True),
        sa.Column(
            "summarization_status",
            sa.Text(),
            server_default=sa.text("'none'"),
            nullable=False,
        ),
        sa.Column("summarization_model", sa.Text(), nullable=True),
        sa.Column("summarization_error", sa.Text(), nullable=True),
        sa.Column("summarized_at", TIMESTAMPTZ, nullable=True),
        _timestamp(),
        _timestamp("updated_at"),
        # The one source that holds this content (DATA-SRC-0002); NULL until adopted.
        sa.Column("source_id", UUID, nullable=True),
        sa.CheckConstraint(
            "status IN ('uploaded', 'processing', 'parsed', 'failed', 'ingested', 'partially_ingested')",
            name=op.f("ck_documents_status"),
        ),
        sa.CheckConstraint(
            "summarization_status IN ('none', 'running', 'succeeded', 'failed')",
            name=op.f("ck_documents_summarization_status"),
        ),
        sa.CheckConstraint("origin IN ('upload', 'connection', 'todo_answer')", name=op.f("ck_documents_origin")),
        sa.CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_documents_content_hash_sha256")),
        sa.CheckConstraint(
            "(previous_revision_id IS NULL) = (version = 1)",
            name=op.f("ck_documents_previous_revision"),
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_documents_project_id_projects"),
        sa.ForeignKeyConstraint(
            ["previous_revision_id"],
            ["documents.id"],
            name="fk_documents_previous_revision_id_documents",
        ),
        sa.ForeignKeyConstraint(["uploaded_by"], ["users.id"], name="fk_documents_uploaded_by_users"),
        sa.ForeignKeyConstraint(["source_id"], ["connections.id"], name="fk_documents_source_id_connections"),
        sa.PrimaryKeyConstraint("id", name="pk_documents"),
        sa.UniqueConstraint("project_id", "content_hash", name="uq_documents_project_id_content_hash"),
    )
    op.create_index(
        "ix_documents_project_id_created_at",
        "documents",
        ["project_id", sa.text("created_at DESC")],
    )
    op.create_index("ix_documents_project_id_status", "documents", ["project_id", "status"])
    op.create_index("ix_documents_source_id", "documents", ["source_id"])
    # Documents no source holds yet (adopted by db.source_repair): DATA-SRC-0002.
    op.create_index(
        "ix_documents_unheld",
        "documents",
        ["project_id"],
        postgresql_where=sa.text("source_id IS NULL AND origin IN ('upload', 'connection')"),
    )


def _create_connection_items() -> None:
    # One listed file of a connection and how far its import got (DATA-GDUI-0001).
    op.create_table(
        "connection_items",
        sa.Column("id", UUID, nullable=False),
        sa.Column("connection_id", UUID, nullable=False),
        sa.Column("project_id", UUID, nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("processing_class", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("modified_time", TIMESTAMPTZ, nullable=True),
        sa.Column("drive_version", sa.BigInteger(), nullable=True),
        sa.Column("md5", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("error_reason", sa.Text(), nullable=True),
        sa.Column("error_step", sa.Text(), nullable=True),
        sa.Column("document_id", UUID, nullable=True),
        _timestamp(),
        _timestamp("updated_at"),
        sa.CheckConstraint(_one_of("category", CATEGORIES), name=op.f("ck_connection_items_category")),
        sa.CheckConstraint(
            _one_of("processing_class", PROCESSING_CLASSES),
            name=op.f("ck_connection_items_processing_class"),
        ),
        sa.CheckConstraint(_one_of("state", ITEM_STATES), name=op.f("ck_connection_items_state")),
        sa.CheckConstraint(
            _one_of("error_reason", FILE_ERROR_REASONS),
            name=op.f("ck_connection_items_error_reason"),
        ),
        sa.CheckConstraint(
            _one_of("error_step", ERROR_STEPS),
            name=op.f("ck_connection_items_error_step"),
        ),
        sa.CheckConstraint(
            "(state = 'error') = (error_reason IS NOT NULL)",
            name=op.f("ck_connection_items_error_reason_matches_state"),
        ),
        sa.CheckConstraint(
            "state <> 'duplicate' OR document_id IS NOT NULL",
            name=op.f("ck_connection_items_duplicate_has_document"),
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["connections.id"],
            name="fk_connection_items_connection_id_connections",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_connection_items_project_id_projects",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name="fk_connection_items_document_id_documents",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_connection_items"),
        sa.UniqueConstraint(
            "connection_id",
            "external_id",
            name="uq_connection_items_connection_external",
        ),
    )
    op.create_index(
        "ix_connection_items_connection_id_state",
        "connection_items",
        ["connection_id", "state"],
    )
    op.create_index("ix_connection_items_project_id", "connection_items", ["project_id"])
    op.create_index("ix_connection_items_document_id", "connection_items", ["document_id"])


def _grant_agent_access(role: str) -> None:
    for table, column in SCOPED_TABLES:
        op.execute(read_migration_sql("0001_enable_rls.sql").replace("projects", table))
        op.execute(
            f"CREATE POLICY {POLICY} ON {table} TO {role} USING ({column} = {SCOPE}) WITH CHECK ({column} = {SCOPE})"
        )
    columns = ", ".join(AGENT_DOCUMENT_COLUMNS)
    op.execute(f"GRANT SELECT ({columns}) ON documents TO {role}")


def upgrade() -> None:
    """Upgrade schema."""

    name = _agent_user()
    _require_role(name)
    _create_users()
    _create_projects()
    _create_project_members()
    _create_connections()
    _create_documents()
    _create_connection_items()
    _grant_agent_access(_quoted(name))


def downgrade() -> None:
    """Downgrade schema: drop every table this revision created, newest first."""

    for table in reversed(CREATED_TABLES):
        op.drop_table(table)
