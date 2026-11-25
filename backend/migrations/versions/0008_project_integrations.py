"""Project integrations: the member whose per-user connection an app runs on, and the new source kinds.

Adds, on top of revision 0007_github, the table project_integrations (one row per app per project: atlassian, github or
slack; ``user_id`` is the member whose connection the project's uses of the app act as, set null when that member is
gone; the workspace is the one Atlassian site, GitHub account or Slack workspace of the project) and widens the check
constraint ck_connections_kind to ``gdrive, upload, confluence, jira_epic`` (the two new kinds are sources imported by
the API). The table has row-level security and the policy ``agent_project_scope`` of the agent role, no grant, as on
every project table (the statements of 0003 are reused).

The upgrade backfills what the existing tables record: a project whose Jira target has an acting account with a
connection gets an ``atlassian`` row, and a project with a repository whose first adder has a GitHub connection gets a
``github`` row. A Slack channel binding lives in a file of the graph state folder, not in a table; the API fills the
``slack`` rows from it at its start.

Locks: the first online statement of upgrade() and downgrade() is ``SET LOCAL lock_timeout = '10s'``. The foreign keys
take a lock on ``users`` and ``projects`` and the constraint swap one on ``connections``; if a lock is not granted in
time the statement fails and the WHOLE transaction (one per run) is rolled back, so nothing is half applied; the next
start of the api retries the revision.

downgrade() returns to the 0007_github schema exactly: it DROPS the table project_integrations (every binding is lost;
the members connect again) and restores ck_connections_kind. A source of a kind the old constraint refuses cannot stay,
so the downgrade DELETES every ``confluence`` and ``jira_epic`` row of connections with its connection_items and the
documents they hold (their objects stay in object storage until removed by hand).

Revision ID: 0008_project_integrations
Revises: 0007_github
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from db.migration_helpers import agent_role, set_lock_timeout

# revision identifiers, used by Alembic.
revision: str = "0008_project_integrations"
down_revision: str | Sequence[str] | None = "0007_github"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TIMESTAMPTZ = sa.DateTime(timezone=True)

INTEGRATIONS = "project_integrations"
SOURCES = "connections"
KIND_CHECK = "ck_connections_kind"
LOCK_TIMEOUT = "0003_lock_timeout.sql"
AGENT_ROLE = "0003_agent_role.sql"
ENABLE_RLS = "0003_enable_rls.sql"
CREATE_POLICY = "0003_create_policy.sql"
# The table and the role the statements above name; replaced with the new table and the agent role.
TABLE_PLACEHOLDER = "gap_option"
POLICY_ROLE_PLACEHOLDER = "agent_role"
APPS = ("atlassian", "github", "slack")
OLD_KINDS = ("gdrive", "upload")
NEW_KINDS = ("confluence", "jira_epic")


def read_migration_sql(name: str) -> str:
    """One statement of ``migrations/sql/<name>``, on one line."""

    text = (Path(__file__).resolve().parent.parent / "sql" / name).read_text(encoding="utf-8")
    return " ".join(text.split()).replace("( ", "(").replace(" )", ")")


def _one_of(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _backfill() -> None:
    """The Atlassian and GitHub bindings the earlier tables record (see the docstring)."""

    integrations = sa.table(
        INTEGRATIONS,
        sa.column("project_id", UUID),
        sa.column("app", sa.Text()),
        sa.column("user_id", UUID),
        sa.column("workspace_id", sa.Text()),
        sa.column("workspace_name", sa.Text()),
        sa.column("connected_at", TIMESTAMPTZ),
    )
    targets = sa.table("jira_targets", sa.column("project_id", UUID), sa.column("connected_user_id", UUID))
    atlassian = sa.table(
        "atlassian_connections",
        sa.column("user_id", UUID),
        sa.column("cloud_id", sa.Text()),
        sa.column("connected_at", TIMESTAMPTZ),
    )
    jira = (
        sa.select(
            targets.c.project_id,
            sa.literal("atlassian"),
            atlassian.c.user_id,
            atlassian.c.cloud_id,
            atlassian.c.cloud_id,  # the site's name is not stored; the API shows the configured site
            atlassian.c.connected_at,
        )
        .select_from(targets.join(atlassian, atlassian.c.user_id == targets.c.connected_user_id))
        .where(targets.c.connected_user_id.is_not(None))
    )
    columns = [c.name for c in integrations.c]
    op.execute(integrations.insert().from_select(columns, jira))

    repositories = sa.table(
        "project_repositories",
        sa.column("id", UUID),
        sa.column("project_id", UUID),
        sa.column("added_by", UUID),
        sa.column("created_at", TIMESTAMPTZ),
    )
    github = sa.table(
        "github_connections",
        sa.column("user_id", UUID),
        sa.column("login", sa.Text()),
        sa.column("connected_at", TIMESTAMPTZ),
    )
    rank = (
        sa.func.row_number()
        .over(
            partition_by=repositories.c.project_id,
            order_by=[repositories.c.created_at, repositories.c.id],
        )
        .label("rank")
    )
    first = (
        sa.select(repositories.c.project_id, repositories.c.added_by, rank)
        .where(repositories.c.added_by.is_not(None))
        .subquery("first_adders")
    )
    repository = (
        sa.select(
            first.c.project_id,
            sa.literal("github"),
            github.c.user_id,
            github.c.login,
            github.c.login,
            github.c.connected_at,
        )
        .select_from(first.join(github, github.c.user_id == first.c.added_by))
        .where(first.c.rank == 1)
    )
    op.execute(integrations.insert().from_select(columns, repository))


def upgrade() -> None:
    """Upgrade schema: the integrations table (row-level security and the agent policy), the wider source kinds."""

    set_lock_timeout(read_migration_sql(LOCK_TIMEOUT))
    role = agent_role(read_migration_sql(AGENT_ROLE))
    op.create_table(
        INTEGRATIONS,
        sa.Column("project_id", UUID, nullable=False),
        sa.Column("app", sa.Text(), nullable=False),
        sa.Column("user_id", UUID, nullable=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("workspace_name", sa.Text(), nullable=False),
        sa.Column("connected_at", TIMESTAMPTZ, nullable=False),
        sa.CheckConstraint(_one_of("app", APPS), name=op.f(f"ck_{INTEGRATIONS}_app")),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name=f"fk_{INTEGRATIONS}_project_id_projects"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=f"fk_{INTEGRATIONS}_user_id_users", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("project_id", "app", name=f"pk_{INTEGRATIONS}"),
    )
    op.execute(read_migration_sql(ENABLE_RLS).replace(TABLE_PLACEHOLDER, INTEGRATIONS))
    op.execute(
        read_migration_sql(CREATE_POLICY)
        .replace(TABLE_PLACEHOLDER, INTEGRATIONS)
        .replace(POLICY_ROLE_PLACEHOLDER, role)
    )
    op.drop_constraint(op.f(KIND_CHECK), SOURCES, type_="check")
    op.create_check_constraint(op.f(KIND_CHECK), SOURCES, _one_of("kind", (*OLD_KINDS, *NEW_KINDS)))
    _backfill()


def downgrade() -> None:
    """Downgrade schema to 0007_github; every project integration and every confluence or jira_epic source is lost."""

    set_lock_timeout(read_migration_sql(LOCK_TIMEOUT))
    sources = sa.table(SOURCES, sa.column("id", UUID), sa.column("kind", sa.Text()))
    items = sa.table("connection_items", sa.column("connection_id", UUID), sa.column("document_id", UUID))
    documents = sa.table("documents", sa.column("id", UUID), sa.column("source_id", UUID))
    new = sa.select(sources.c.id).where(sources.c.kind.in_(NEW_KINDS))
    held = sa.select(documents.c.id).where(documents.c.source_id.in_(new))
    # items first (they name the documents), then the documents the sources hold, then the sources
    op.execute(sa.delete(items).where(sa.or_(items.c.connection_id.in_(new), items.c.document_id.in_(held))))
    op.execute(sa.delete(documents).where(documents.c.source_id.in_(new)))
    op.execute(sa.delete(sources).where(sources.c.kind.in_(NEW_KINDS)))
    op.drop_constraint(op.f(KIND_CHECK), SOURCES, type_="check")
    op.create_check_constraint(op.f(KIND_CHECK), SOURCES, _one_of("kind", OLD_KINDS))
    op.drop_table(INTEGRATIONS)
