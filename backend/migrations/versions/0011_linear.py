"""Linear: member connections, the plan destination, the gap channel, and the wider integration and source kinds.

Adds, on top of revision 0010_github_projects, the tables linear_connections (one row per member: the Linear user and
workspace, the Fernet-encrypted token pair, the scopes and the expiry), linear_targets and linear_issues (the Linear
plan destination of a project and the map of plan nodes to issues, with the mirrored task state), and linear_gap_targets
and linear_gap_issues (the Linear gap channel and the map of gaps to the issues that ask them). Row-level security and
the policy ``agent_project_scope`` of the agent role on the project tables, and no grant to that role, as on every
project table (the statements of 0003 are reused); ``linear_connections`` is not project-scoped: row-level security is
on with no policy and no grant. ``project_integrations.app`` also accepts ``linear`` and ``connections.kind`` also
accepts ``linear_project``.

Locks: the first online statement of upgrade() and downgrade() is ``SET LOCAL lock_timeout = '10s'``. The foreign keys
and the check changes take a lock on ``projects``, ``users``, ``project_integrations`` and ``connections``; if a lock is
not granted in time the statement fails and the WHOLE transaction (one per run) is rolled back, so nothing is half
applied; the next start of the api retries the revision.

downgrade() returns to the 0010_github_projects schema exactly: it deletes the ``linear`` integrations and the
``linear_project`` sources (with their items and documents, as 0008 does), narrows both checks and DROPS the five
tables. Every Linear connection, target and map is lost (the issues in Linear stay).

Revision ID: 0011_linear
Revises: 0010_github_projects
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from db.migration_helpers import agent_role, set_lock_timeout

# revision identifiers, used by Alembic.
revision: str = "0011_linear"
down_revision: str | Sequence[str] | None = "0010_github_projects"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TIMESTAMPTZ = sa.DateTime(timezone=True)

CONNECTIONS = "linear_connections"
TARGETS = "linear_targets"
ISSUES = "linear_issues"
GAP_TARGETS = "linear_gap_targets"
GAP_ISSUES = "linear_gap_issues"
PROJECT_TABLES = (TARGETS, ISSUES, GAP_TARGETS, GAP_ISSUES)
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
OLD_APPS = ("atlassian", "github", "slack")
NEW_APPS = (*OLD_APPS, "linear")
OLD_KINDS = ("gdrive", "upload", "confluence", "jira_epic")
NEW_KINDS = (*OLD_KINDS, "linear_project")
CONNECTION_STATUSES = ("active", "expired", "revoked")
RUN_STATUSES = ("running", "succeeded", "partial", "failed", "skipped")
TRIGGERS = ("plan_run", "mcp", "settings", "recovery")
ISSUE_KINDS = ("story", "subtask")
GROUPINGS = ("one", "area")


SQL_DIR = Path(__file__).resolve().parent.parent / "sql"
# The columns of the last export pass of a target (all nullable), as the GitHub Projects target has them.
RUN_COLUMNS = (
    ("last_status", sa.Text()),
    ("last_trigger", sa.Text()),
    ("last_started_at", TIMESTAMPTZ),
    ("last_finished_at", TIMESTAMPTZ),
    ("last_error_code", sa.Text()),
    ("last_counts", postgresql.JSONB()),
)


def read_migration_sql(name: str) -> str:
    """One statement of ``migrations/sql/<name>``, on one line."""

    one_line = " ".join((SQL_DIR / name).read_text(encoding="utf-8").split())
    return one_line.replace("( ", "(").replace(" )", ")")


def _one_of(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    """Upgrade schema: the Linear tables with row-level security, the wider app and source kind checks."""

    set_lock_timeout(read_migration_sql(LOCK_TIMEOUT))
    role = agent_role(read_migration_sql(AGENT_ROLE))
    op.create_table(
        CONNECTIONS,
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("linear_user_id", sa.Text(), nullable=False),
        sa.Column("linear_user_name", sa.Text(), nullable=False),
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("organization_name", sa.Text(), nullable=False),
        sa.Column("organization_url_key", sa.Text(), nullable=False),
        sa.Column("scopes", sa.Text(), nullable=False),
        sa.Column("access_token", sa.LargeBinary(), nullable=False),
        sa.Column("refresh_token", sa.LargeBinary(), nullable=False),
        sa.Column("expires_at", TIMESTAMPTZ, nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("connected_at", TIMESTAMPTZ, nullable=False),
        sa.Column("last_used_at", TIMESTAMPTZ, nullable=False),
        sa.Column("expired_at", TIMESTAMPTZ, nullable=True),
        sa.CheckConstraint(_one_of("status", CONNECTION_STATUSES), name=op.f(f"ck_{CONNECTIONS}_status")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=f"fk_{CONNECTIONS}_user_id_users", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", name=f"pk_{CONNECTIONS}"),
    )
    # a webhook comment is mapped to a member by the Linear user id of the workspace
    op.create_index(
        f"ix_{CONNECTIONS}_linear_user_id", CONNECTIONS, ["organization_id", "linear_user_id"], unique=False
    )
    op.create_table(
        TARGETS,
        sa.Column("project_id", UUID, nullable=False),
        sa.Column("team_id", sa.Text(), nullable=False),
        sa.Column("team_key", sa.Text(), nullable=False),
        sa.Column("linear_project_id", sa.Text(), nullable=False),
        sa.Column("linear_project_name", sa.Text(), nullable=False),
        sa.Column("linear_project_url", sa.Text(), nullable=True),
        sa.Column("created_by_graphico", sa.Boolean(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False),
        *(sa.Column(name, kind, nullable=True) for name, kind in RUN_COLUMNS),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name=f"fk_{TARGETS}_project_id_projects"),
        sa.CheckConstraint(_one_of("last_status", RUN_STATUSES), name=op.f(f"ck_{TARGETS}_last_status")),
        sa.CheckConstraint(_one_of("last_trigger", TRIGGERS), name=op.f(f"ck_{TARGETS}_last_trigger")),
        sa.PrimaryKeyConstraint("project_id", name=f"pk_{TARGETS}"),
    )
    op.create_table(
        ISSUES,
        sa.Column("project_id", UUID, nullable=False),
        sa.Column("node_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("issue_id", UUID, nullable=True),
        sa.Column("identifier", sa.Text(), nullable=True),
        sa.Column("requested_at", TIMESTAMPTZ, nullable=False),
        sa.Column("content", postgresql.JSONB(), nullable=True),
        sa.Column("content_hash", sa.Text(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=True),
        sa.Column("mirrored_state", sa.Text(), nullable=True),
        sa.Column("removed_at", TIMESTAMPTZ, nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("synced_at", TIMESTAMPTZ, nullable=True),
        sa.CheckConstraint(_one_of("kind", ISSUE_KINDS), name=op.f(f"ck_{ISSUES}_kind")),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name=f"fk_{ISSUES}_project_id_projects"),
        sa.PrimaryKeyConstraint("project_id", "node_id", name=f"pk_{ISSUES}"),
        sa.UniqueConstraint("project_id", "identifier", name=f"uq_{ISSUES}_project_id"),
    )
    op.create_table(
        GAP_TARGETS,
        sa.Column("project_id", UUID, nullable=False),
        sa.Column("team_id", sa.Text(), nullable=False),
        sa.Column("team_key", sa.Text(), nullable=False),
        sa.Column("linear_project_id", sa.Text(), nullable=True),
        sa.Column("grouping", sa.Text(), nullable=False),
        sa.Column("parent_issue_id", UUID, nullable=True),
        sa.Column("parent_issue_identifier", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False),
        sa.Column("last_error_code", sa.Text(), nullable=True),
        sa.CheckConstraint(_one_of("grouping", GROUPINGS), name=op.f(f"ck_{GAP_TARGETS}_grouping")),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name=f"fk_{GAP_TARGETS}_project_id_projects"),
        sa.PrimaryKeyConstraint("project_id", name=f"pk_{GAP_TARGETS}"),
    )
    op.create_table(
        GAP_ISSUES,
        sa.Column("project_id", UUID, nullable=False),
        sa.Column("gap_id", sa.Text(), nullable=False),
        sa.Column("issue_id", UUID, nullable=False),
        sa.Column("identifier", sa.Text(), nullable=False),
        sa.Column("area", sa.Text(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("posted_at", TIMESTAMPTZ, nullable=False),
        sa.Column("answered_at", TIMESTAMPTZ, nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name=f"fk_{GAP_ISSUES}_project_id_projects"),
        sa.PrimaryKeyConstraint("project_id", "gap_id", name=f"pk_{GAP_ISSUES}"),
    )
    # a webhook delivery names the issue only: look the gap up by it
    op.create_index(f"ix_{GAP_ISSUES}_issue_id", GAP_ISSUES, ["issue_id"], unique=False)
    op.execute(read_migration_sql(ENABLE_RLS).replace(TABLE_PLACEHOLDER, CONNECTIONS))
    for table in PROJECT_TABLES:
        op.execute(read_migration_sql(ENABLE_RLS).replace(TABLE_PLACEHOLDER, table))
        policy = read_migration_sql(CREATE_POLICY).replace(TABLE_PLACEHOLDER, table)
        op.execute(policy.replace(POLICY_ROLE_PLACEHOLDER, role))
    op.drop_constraint(op.f(APP_CHECK), INTEGRATIONS, type_="check")
    op.create_check_constraint(op.f(APP_CHECK), INTEGRATIONS, _one_of("app", NEW_APPS))
    op.drop_constraint(op.f(KIND_CHECK), SOURCES, type_="check")
    op.create_check_constraint(op.f(KIND_CHECK), SOURCES, _one_of("kind", NEW_KINDS))


def downgrade() -> None:
    """Downgrade schema to 0010_github_projects; every Linear connection, binding, source and map is lost."""

    set_lock_timeout(read_migration_sql(LOCK_TIMEOUT))
    sources = sa.table(SOURCES, sa.column("id", UUID), sa.column("kind", sa.Text()))
    items = sa.table("connection_items", sa.column("connection_id", UUID), sa.column("document_id", UUID))
    documents = sa.table("documents", sa.column("id", UUID), sa.column("source_id", UUID))
    new = sa.select(sources.c.id).where(sources.c.kind == "linear_project")
    held = sa.select(documents.c.id).where(documents.c.source_id.in_(new))
    # items first (they name the documents), then the documents the sources hold, then the sources
    op.execute(sa.delete(items).where(sa.or_(items.c.connection_id.in_(new), items.c.document_id.in_(held))))
    op.execute(sa.delete(documents).where(documents.c.source_id.in_(new)))
    op.execute(sa.delete(sources).where(sources.c.kind == "linear_project"))
    op.drop_constraint(op.f(KIND_CHECK), SOURCES, type_="check")
    op.create_check_constraint(op.f(KIND_CHECK), SOURCES, _one_of("kind", OLD_KINDS))
    integrations = sa.table(INTEGRATIONS, sa.column("app", sa.Text()))
    op.execute(sa.delete(integrations).where(integrations.c.app == "linear"))
    op.drop_constraint(op.f(APP_CHECK), INTEGRATIONS, type_="check")
    op.create_check_constraint(op.f(APP_CHECK), INTEGRATIONS, _one_of("app", OLD_APPS))
    op.drop_table(GAP_ISSUES)
    op.drop_table(GAP_TARGETS)
    op.drop_table(ISSUES)
    op.drop_table(TARGETS)
    op.drop_table(CONNECTIONS)
