"""Jira gaps: the target of a project's Jira gap channel and the map of gaps to the sub-tasks that ask them.

Adds, on top of revision 0008_project_integrations, the tables jira_gap_targets (one row per project: the Jira project,
how the questions are grouped, the one Story they hang under, the poll cursor and the last poll) and jira_gap_issues
(one row per gap posted: the sub-task, the number of the question in it and when it was posted and answered), as in
docs/jira-sync/gaps.md. Row-level security and the policy ``agent_project_scope`` of the agent role, and no grant to
that role, as on every project table (the statements of 0003 are reused). No existing table changes.

Locks: the first online statement of upgrade() and downgrade() is ``SET LOCAL lock_timeout = '10s'``. The foreign keys
take a lock on ``projects``; if a lock is not granted in time the statement fails and the WHOLE transaction (one per
run) is rolled back, so nothing is half applied; the next start of the api retries the revision.

downgrade() returns to the 0008_project_integrations schema exactly: it DROPS both tables with their constraints and
policies. Every Jira gap target and the whole gap-to-sub-task map is lost (the issues in Jira stay).

Revision ID: 0009_jira_gaps
Revises: 0008_project_integrations
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from db.migration_helpers import agent_role, set_lock_timeout

# revision identifiers, used by Alembic.
revision: str = "0009_jira_gaps"
down_revision: str | Sequence[str] | None = "0008_project_integrations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TIMESTAMPTZ = sa.DateTime(timezone=True)

TARGETS = "jira_gap_targets"
ISSUES = "jira_gap_issues"
LOCK_TIMEOUT = "0003_lock_timeout.sql"
AGENT_ROLE = "0003_agent_role.sql"
ENABLE_RLS = "0003_enable_rls.sql"
CREATE_POLICY = "0003_create_policy.sql"
# The table and the role the statements above name; replaced with each table and the agent role.
TABLE_PLACEHOLDER = "gap_option"
POLICY_ROLE_PLACEHOLDER = "agent_role"
GROUPINGS = ("one", "area")


def read_migration_sql(name: str) -> str:
    """One statement of ``migrations/sql/<name>``, on one line."""

    text = (Path(__file__).resolve().parent.parent / "sql" / name).read_text(encoding="utf-8")
    return " ".join(text.split()).replace("( ", "(").replace(" )", ")")


def _one_of(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    """Upgrade schema: the Jira gap tables with row-level security for the agent role."""

    set_lock_timeout(read_migration_sql(LOCK_TIMEOUT))
    role = agent_role(read_migration_sql(AGENT_ROLE))
    op.create_table(
        TARGETS,
        sa.Column("project_id", UUID, nullable=False),
        sa.Column("jira_project", sa.Text(), nullable=False),
        sa.Column("grouping", sa.Text(), nullable=False),
        sa.Column("story_key", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False),
        sa.Column("poll_cursor", TIMESTAMPTZ, nullable=True),
        sa.Column("last_polled_at", TIMESTAMPTZ, nullable=True),
        sa.Column("last_error_code", sa.Text(), nullable=True),
        sa.CheckConstraint(_one_of("grouping", GROUPINGS), name=op.f(f"ck_{TARGETS}_grouping")),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name=f"fk_{TARGETS}_project_id_projects"),
        sa.PrimaryKeyConstraint("project_id", name=f"pk_{TARGETS}"),
    )
    op.create_table(
        ISSUES,
        sa.Column("project_id", UUID, nullable=False),
        sa.Column("gap_id", sa.Text(), nullable=False),
        sa.Column("issue_key", sa.Text(), nullable=False),
        sa.Column("area", sa.Text(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("posted_at", TIMESTAMPTZ, nullable=False),
        sa.Column("answered_at", TIMESTAMPTZ, nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name=f"fk_{ISSUES}_project_id_projects"),
        sa.PrimaryKeyConstraint("project_id", "gap_id", name=f"pk_{ISSUES}"),
    )
    for table in (TARGETS, ISSUES):
        op.execute(read_migration_sql(ENABLE_RLS).replace(TABLE_PLACEHOLDER, table))
        policy = read_migration_sql(CREATE_POLICY).replace(TABLE_PLACEHOLDER, table)
        op.execute(policy.replace(POLICY_ROLE_PLACEHOLDER, role))


def downgrade() -> None:
    """Downgrade schema to 0008_project_integrations; every Jira gap target and the whole map are lost."""

    set_lock_timeout(read_migration_sql(LOCK_TIMEOUT))
    op.drop_table(ISSUES)
    op.drop_table(TARGETS)
