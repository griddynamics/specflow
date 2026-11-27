"""Notion: the users' Notion connections, the plan destination's target and map, the new app and source kind.

Adds, on top of revision 0013_dropbox, the tables notion_connections (one row per Specflow user: the Notion workspace,
the connection's bot, the Fernet refresh token and, for a grant with no refresh token only, the Fernet access token),
notion_plan_targets (one row per project: the parent page the plan database is created under, the database and its data
source once the first pass made them, the run columns) and notion_plan_items (one row per plan node: the row's page, the
applied content and its hash), as in docs/notion/oauth.md, docs/notion/plan.md. It widens ck_project_integrations_app to
``atlassian, github, slack, linear, box, dropbox, notion`` and ck_connections_kind to ``gdrive, upload, confluence,
jira_epic, linear_project, box, dropbox, notion`` (a Notion link is a source imported by the API). Row-level security
everywhere; notion_connections has no policy and no grant (not project-scoped: the agent role reads none of it); the two
plan tables get the policy ``agent_project_scope`` and no grant, as every project table (the statements of 0003 are
reused).

Locks: the first online statement of upgrade() and downgrade() is ``SET LOCAL lock_timeout = '10s'``. The foreign keys
take a lock on ``users`` and ``projects`` and the constraint swaps one on ``project_integrations`` and ``connections``;
if a lock is not granted in time the statement fails and the WHOLE transaction (one per run) is rolled back, so nothing
is half applied; the next start of the api retries the revision.

downgrade() returns to the 0013_dropbox schema exactly: it DROPS the three tables (every Notion connection,
plan target and plan-to-page map is lost; the pages and databases on Notion stay), DELETES every ``notion`` binding of
project_integrations and every ``notion`` source of connections with its connection_items and the documents they hold
(their objects stay in object storage until removed by hand), and restores both check constraints.

Revision ID: 0014_notion
Revises: 0013_dropbox
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from db.migration_helpers import agent_role, set_lock_timeout

# revision identifiers, used by Alembic.
revision: str = "0014_notion"
down_revision: str | Sequence[str] | None = "0013_dropbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TIMESTAMPTZ = sa.DateTime(timezone=True)

CONNECTIONS = "notion_connections"
TARGETS = "notion_plan_targets"
ITEMS = "notion_plan_items"
INTEGRATIONS = "project_integrations"
SOURCES = "connections"
APP_CHECK = "ck_project_integrations_app"
KIND_CHECK = "ck_connections_kind"
LOCK_TIMEOUT = "0003_lock_timeout.sql"
AGENT_ROLE = "0003_agent_role.sql"
ENABLE_RLS = "0003_enable_rls.sql"
CREATE_POLICY = "0003_create_policy.sql"
# The table and the role the statements above name; replaced with each table and the agent role.
TABLE_PLACEHOLDER = "gap_option"
POLICY_ROLE_PLACEHOLDER = "agent_role"
OLD_APPS = ("atlassian", "github", "slack", "linear", "box", "dropbox")
OLD_KINDS = ("gdrive", "upload", "confluence", "jira_epic", "linear_project", "box", "dropbox")
STATUSES = ("active", "expired")
RUN_STATUSES = ("running", "succeeded", "partial", "failed", "skipped")
TRIGGERS = ("plan_run", "settings", "recovery")
ITEM_KINDS = ("story", "task")


def read_migration_sql(name: str) -> str:
    """One statement of ``migrations/sql/<name>``, on one line."""

    text = (Path(__file__).resolve().parent.parent / "sql" / name).read_text(encoding="utf-8")
    return " ".join(text.split()).replace("( ", "(").replace(" )", ")")


def _one_of(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _lifecycle_columns() -> list[sa.Column[Any]]:
    """The status and the instants of a connection row, after its identity and token columns."""

    return [
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("connected_at", TIMESTAMPTZ, nullable=False),
        sa.Column("last_used_at", TIMESTAMPTZ, nullable=False),
        sa.Column("expired_at", TIMESTAMPTZ, nullable=True),
    ]


def _run_columns() -> list[sa.Column[Any]]:
    """The enabled flag and the last pass of an export target (the ``last_*`` columns)."""

    return [
        sa.Column("last_status", sa.Text(), nullable=True),
        sa.Column("last_trigger", sa.Text(), nullable=True),
        sa.Column("last_started_at", TIMESTAMPTZ, nullable=True),
        sa.Column("last_finished_at", TIMESTAMPTZ, nullable=True),
        sa.Column("last_error_code", sa.Text(), nullable=True),
        sa.Column("last_counts", postgresql.JSONB(), nullable=True),
    ]


def upgrade() -> None:
    """Upgrade schema: the Notion tables with row-level security, the wider app and source kind constraints."""

    set_lock_timeout(read_migration_sql(LOCK_TIMEOUT))
    role = agent_role(read_migration_sql(AGENT_ROLE))
    op.create_table(
        CONNECTIONS,
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("workspace_name", sa.Text(), nullable=False),
        sa.Column("bot_id", sa.Text(), nullable=False),
        *_lifecycle_columns(),
        sa.Column("refresh_expires_at", TIMESTAMPTZ, nullable=True),
        sa.Column("refresh_token", sa.LargeBinary(), nullable=False),
        sa.Column("access_token", sa.LargeBinary(), nullable=True),
        sa.CheckConstraint(_one_of("status", STATUSES), name=op.f(f"ck_{CONNECTIONS}_status")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=f"fk_{CONNECTIONS}_user_id_users", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", name=f"pk_{CONNECTIONS}"),
    )
    op.create_table(
        TARGETS,
        sa.Column("project_id", UUID, nullable=False),
        sa.Column("parent_page_id", sa.Text(), nullable=False),
        sa.Column("parent_page_title", sa.Text(), nullable=False),
        sa.Column("parent_page_url", sa.Text(), nullable=False),
        sa.Column("database_id", sa.Text(), nullable=True),
        sa.Column("data_source_id", sa.Text(), nullable=True),
        sa.Column("database_url", sa.Text(), nullable=True),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        *_run_columns(),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name=f"fk_{TARGETS}_project_id_projects"),
        sa.CheckConstraint(_one_of("last_status", RUN_STATUSES), name=op.f(f"ck_{TARGETS}_last_status")),
        sa.CheckConstraint(_one_of("last_trigger", TRIGGERS), name=op.f(f"ck_{TARGETS}_last_trigger")),
        sa.PrimaryKeyConstraint("project_id", name=f"pk_{TARGETS}"),
    )
    op.create_table(
        ITEMS,
        sa.Column("project_id", UUID, nullable=False),
        sa.Column("node_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("page_id", sa.Text(), nullable=True),
        sa.Column("requested_at", TIMESTAMPTZ, nullable=False),
        sa.Column("synced_at", TIMESTAMPTZ, nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("removed_at", TIMESTAMPTZ, nullable=True),
        sa.Column("content_hash", sa.Text(), nullable=True),
        sa.Column("content", postgresql.JSONB(), nullable=True),
        sa.CheckConstraint(_one_of("kind", ITEM_KINDS), name=op.f(f"ck_{ITEMS}_kind")),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name=f"fk_{ITEMS}_project_id_projects"),
        sa.PrimaryKeyConstraint("project_id", "node_id", name=f"pk_{ITEMS}"),
        sa.UniqueConstraint("project_id", "page_id", name=f"uq_{ITEMS}_project_id"),
    )
    op.execute(read_migration_sql(ENABLE_RLS).replace(TABLE_PLACEHOLDER, CONNECTIONS))
    for table in (TARGETS, ITEMS):
        op.execute(read_migration_sql(ENABLE_RLS).replace(TABLE_PLACEHOLDER, table))
        policy = read_migration_sql(CREATE_POLICY).replace(TABLE_PLACEHOLDER, table)
        op.execute(policy.replace(POLICY_ROLE_PLACEHOLDER, role))
    op.drop_constraint(op.f(APP_CHECK), INTEGRATIONS, type_="check")
    op.create_check_constraint(op.f(APP_CHECK), INTEGRATIONS, _one_of("app", (*OLD_APPS, "notion")))
    op.drop_constraint(op.f(KIND_CHECK), SOURCES, type_="check")
    op.create_check_constraint(op.f(KIND_CHECK), SOURCES, _one_of("kind", (*OLD_KINDS, "notion")))


def downgrade() -> None:
    """Downgrade schema to 0013_dropbox; every Notion connection, binding, plan target and map, and every
    ``notion`` source is lost."""

    set_lock_timeout(read_migration_sql(LOCK_TIMEOUT))
    sources = sa.table(SOURCES, sa.column("id", UUID), sa.column("kind", sa.Text()))
    items = sa.table("connection_items", sa.column("connection_id", UUID), sa.column("document_id", UUID))
    documents = sa.table("documents", sa.column("id", UUID), sa.column("source_id", UUID))
    integrations = sa.table(INTEGRATIONS, sa.column("app", sa.Text()))
    new = sa.select(sources.c.id).where(sources.c.kind == "notion")
    held = sa.select(documents.c.id).where(documents.c.source_id.in_(new))
    # items first (they name the documents), then the documents the sources hold, then the sources
    op.execute(sa.delete(items).where(sa.or_(items.c.connection_id.in_(new), items.c.document_id.in_(held))))
    op.execute(sa.delete(documents).where(documents.c.source_id.in_(new)))
    op.execute(sa.delete(sources).where(sources.c.kind == "notion"))
    op.execute(sa.delete(integrations).where(integrations.c.app == "notion"))
    op.drop_constraint(op.f(KIND_CHECK), SOURCES, type_="check")
    op.create_check_constraint(op.f(KIND_CHECK), SOURCES, _one_of("kind", OLD_KINDS))
    op.drop_constraint(op.f(APP_CHECK), INTEGRATIONS, type_="check")
    op.create_check_constraint(op.f(APP_CHECK), INTEGRATIONS, _one_of("app", OLD_APPS))
    op.drop_table(ITEMS)
    op.drop_table(TARGETS)
    op.drop_table(CONNECTIONS)
