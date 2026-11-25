"""GitHub Projects: the target of a project's GitHub Projects plan destination and the map of plan nodes to issues.

Adds, on top of revision 0009_jira_gaps, the tables github_project_targets (one row per project: the repository the
issues go to, the organization project they are added to, the run columns and the member whose GitHub connection the
export acts as) and github_project_items (one row per plan node: the issue, its project item, the applied content and
its position), as in docs/github/projects.md. Row-level security and the policy ``agent_project_scope`` of the agent
role, and no grant to that role, as on every project table (the statements of 0003 are reused). No existing table
changes.

Locks: the first online statement of upgrade() and downgrade() is ``SET LOCAL lock_timeout = '10s'``. The foreign keys
take a lock on ``projects``, ``project_repositories`` and ``users``; if a lock is not granted in time the statement
fails and the WHOLE transaction (one per run) is rolled back, so nothing is half applied; the next start of the api
retries the revision.

downgrade() returns to the 0009_jira_gaps schema exactly: it DROPS both tables with their constraints and policies.
Every GitHub Projects target and the whole plan-to-issue map is lost (the issues on GitHub stay).

Revision ID: 0010_github_projects
Revises: 0009_jira_gaps
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from db.migration_helpers import agent_role, set_lock_timeout

# revision identifiers, used by Alembic.
revision: str = "0010_github_projects"
down_revision: str | Sequence[str] | None = "0009_jira_gaps"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TIMESTAMPTZ = sa.DateTime(timezone=True)

TARGETS = "github_project_targets"
ITEMS = "github_project_items"
LOCK_TIMEOUT = "0003_lock_timeout.sql"
AGENT_ROLE = "0003_agent_role.sql"
ENABLE_RLS = "0003_enable_rls.sql"
CREATE_POLICY = "0003_create_policy.sql"
# The table and the role the statements above name; replaced with each table and the agent role.
TABLE_PLACEHOLDER = "gap_option"
POLICY_ROLE_PLACEHOLDER = "agent_role"
RUN_STATUSES = ("running", "succeeded", "partial", "failed", "skipped")
TRIGGERS = ("plan_run", "settings", "recovery")
ITEM_KINDS = ("story", "subtask")


def read_migration_sql(name: str) -> str:
    """One statement of ``migrations/sql/<name>``, on one line."""

    text = (Path(__file__).resolve().parent.parent / "sql" / name).read_text(encoding="utf-8")
    return " ".join(text.split()).replace("( ", "(").replace(" )", ")")


def _one_of(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    """Upgrade schema: the GitHub Projects tables with row-level security for the agent role."""

    set_lock_timeout(read_migration_sql(LOCK_TIMEOUT))
    role = agent_role(read_migration_sql(AGENT_ROLE))
    op.create_table(
        TARGETS,
        sa.Column("project_id", UUID, nullable=False),
        sa.Column("repository_id", UUID, nullable=False),
        sa.Column("project_node_id", sa.Text(), nullable=False),
        sa.Column("project_title", sa.Text(), nullable=False),
        sa.Column("project_url", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False),
        sa.Column("last_status", sa.Text(), nullable=True),
        sa.Column("last_trigger", sa.Text(), nullable=True),
        sa.Column("last_started_at", TIMESTAMPTZ, nullable=True),
        sa.Column("last_finished_at", TIMESTAMPTZ, nullable=True),
        sa.Column("last_error_code", sa.Text(), nullable=True),
        sa.Column("last_counts", postgresql.JSONB(), nullable=True),
        sa.Column("connected_user_id", UUID, nullable=True),
        sa.CheckConstraint(_one_of("last_status", RUN_STATUSES), name=op.f(f"ck_{TARGETS}_last_status")),
        sa.CheckConstraint(_one_of("last_trigger", TRIGGERS), name=op.f(f"ck_{TARGETS}_last_trigger")),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name=f"fk_{TARGETS}_project_id_projects"),
        sa.ForeignKeyConstraint(
            ["repository_id"],
            ["project_repositories.id"],
            name=f"fk_{TARGETS}_repository_id_project_repositories",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["connected_user_id"], ["users.id"], name=f"fk_{TARGETS}_connected_user_id_users", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("project_id", name=f"pk_{TARGETS}"),
    )
    op.create_table(
        ITEMS,
        sa.Column("project_id", UUID, nullable=False),
        sa.Column("node_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("issue_number", sa.Integer(), nullable=True),
        sa.Column("issue_id", sa.BigInteger(), nullable=True),
        sa.Column("issue_node_id", sa.Text(), nullable=True),
        sa.Column("item_id", sa.Text(), nullable=True),
        sa.Column("requested_at", TIMESTAMPTZ, nullable=False),
        sa.Column("content", postgresql.JSONB(), nullable=True),
        sa.Column("content_hash", sa.Text(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=True),
        sa.Column("removed_at", TIMESTAMPTZ, nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("synced_at", TIMESTAMPTZ, nullable=True),
        sa.CheckConstraint(_one_of("kind", ITEM_KINDS), name=op.f(f"ck_{ITEMS}_kind")),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name=f"fk_{ITEMS}_project_id_projects"),
        sa.PrimaryKeyConstraint("project_id", "node_id", name=f"pk_{ITEMS}"),
        sa.UniqueConstraint("project_id", "issue_number", name=f"uq_{ITEMS}_project_id"),
    )
    for table in (TARGETS, ITEMS):
        op.execute(read_migration_sql(ENABLE_RLS).replace(TABLE_PLACEHOLDER, table))
        policy = read_migration_sql(CREATE_POLICY).replace(TABLE_PLACEHOLDER, table)
        op.execute(policy.replace(POLICY_ROLE_PLACEHOLDER, role))


def downgrade() -> None:
    """Downgrade schema to 0009_jira_gaps; every GitHub Projects target and the whole map are lost."""

    set_lock_timeout(read_migration_sql(LOCK_TIMEOUT))
    op.drop_table(ITEMS)
    op.drop_table(TARGETS)
