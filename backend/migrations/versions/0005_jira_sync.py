"""Jira sync: the target of a project and the map of plan nodes to Jira issues.

Adds, on top of revision 0004_gap_assignment, the tables jira_targets (one row per project with Jira settings, the
last run kept on it) and jira_issues (one row per plan node an issue was created for), as in
docs/jira-sync/data-model.md, with a partial unique index on (project_id, jira_key) where the key is
set. Row-level security and the policy
``agent_project_scope`` of the agent role, and no grant to that role, as on every project table (the statements of
0003 are reused). No existing table changes.

Locks: the first online statement of upgrade() and downgrade() is ``SET LOCAL lock_timeout = '10s'``. The foreign
keys take a lock on ``projects``; if a lock is not granted in time the statement fails and the WHOLE transaction (one
per run) is rolled back, so nothing is half applied; the next start of the api retries the revision.

downgrade() returns to the 0004_gap_assignment schema exactly: it DROPS jira_issues and jira_targets with their
index, constraints and policies. Every Jira target and the whole node-to-issue map is lost (the issues in Jira stay).

Revision ID: 0005_jira_sync
Revises: 0004_gap_assignment
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from db.migration_helpers import agent_role, set_lock_timeout

# revision identifiers, used by Alembic.
revision: str = "0005_jira_sync"
down_revision: str | Sequence[str] | None = "0004_gap_assignment"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TIMESTAMPTZ = sa.DateTime(timezone=True)

TARGETS = "jira_targets"
ISSUES = "jira_issues"
LOCK_TIMEOUT = "0003_lock_timeout.sql"
AGENT_ROLE = "0003_agent_role.sql"
ENABLE_RLS = "0003_enable_rls.sql"
CREATE_POLICY = "0003_create_policy.sql"
KEY_INDEX = "0005_jira_issues_key_index.sql"
# The table and the role the two statements above name; replaced with each table and the agent role.
TABLE_PLACEHOLDER = "gap_option"
POLICY_ROLE_PLACEHOLDER = "agent_role"
RUN_STATUSES = ("running", "succeeded", "partial", "failed", "skipped")
TRIGGERS = ("plan_run", "mcp", "settings", "recovery")
ISSUE_KINDS = ("epic", "story", "subtask")


def read_migration_sql(name: str) -> str:
    """One statement of ``migrations/sql/<name>``, on one line."""

    text = (Path(__file__).resolve().parent.parent / "sql" / name).read_text(encoding="utf-8")
    return " ".join(text.split()).replace("( ", "(").replace(" )", ")")


def _one_of(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _scope(table: str, role: str) -> None:
    op.execute(read_migration_sql(ENABLE_RLS).replace(TABLE_PLACEHOLDER, table))
    op.execute(
        read_migration_sql(CREATE_POLICY).replace(TABLE_PLACEHOLDER, table).replace(POLICY_ROLE_PLACEHOLDER, role)
    )


def upgrade() -> None:
    """Upgrade schema: the Jira tables with row-level security for the agent role."""

    set_lock_timeout(read_migration_sql(LOCK_TIMEOUT))
    role = agent_role(read_migration_sql(AGENT_ROLE))
    op.create_table(
        TARGETS,
        sa.Column("project_id", UUID, nullable=False),
        sa.Column("jira_project", sa.Text(), nullable=False),
        sa.Column("epic_key", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False),
        sa.Column("last_status", sa.Text(), nullable=True),
        sa.Column("last_trigger", sa.Text(), nullable=True),
        sa.Column("last_started_at", TIMESTAMPTZ, nullable=True),
        sa.Column("last_finished_at", TIMESTAMPTZ, nullable=True),
        sa.Column("last_error_code", sa.Text(), nullable=True),
        sa.Column("last_counts", postgresql.JSONB(), nullable=True),
        sa.CheckConstraint(_one_of("last_status", RUN_STATUSES), name=op.f(f"ck_{TARGETS}_last_status")),
        sa.CheckConstraint(_one_of("last_trigger", TRIGGERS), name=op.f(f"ck_{TARGETS}_last_trigger")),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name=f"fk_{TARGETS}_project_id_projects"),
        sa.PrimaryKeyConstraint("project_id", name=f"pk_{TARGETS}"),
    )
    op.create_table(
        ISSUES,
        sa.Column("project_id", UUID, nullable=False),
        sa.Column("node_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("jira_key", sa.Text(), nullable=True),
        sa.Column("requested_at", TIMESTAMPTZ, nullable=False),
        sa.Column("content", postgresql.JSONB(), nullable=True),
        sa.Column("content_hash", sa.Text(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=True),
        sa.Column("removed_at", TIMESTAMPTZ, nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("synced_at", TIMESTAMPTZ, nullable=True),
        sa.CheckConstraint(_one_of("kind", ISSUE_KINDS), name=op.f(f"ck_{ISSUES}_kind")),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name=f"fk_{ISSUES}_project_id_projects"),
        sa.PrimaryKeyConstraint("project_id", "node_id", name=f"pk_{ISSUES}"),
    )
    op.execute(read_migration_sql(KEY_INDEX))
    _scope(TARGETS, role)
    _scope(ISSUES, role)


def downgrade() -> None:
    """Downgrade schema to 0004_gap_assignment; every Jira target and the whole map are lost."""

    set_lock_timeout(read_migration_sql(LOCK_TIMEOUT))
    op.drop_table(ISSUES)
    op.drop_table(TARGETS)
