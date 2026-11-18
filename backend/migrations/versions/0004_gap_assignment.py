"""Gap assignment: the member who resolves a gap.

Adds, on top of revision 0003_gap_module, the table gap_assignment (docs/graph/gap/data-model.md "Relational
store"): one row per assigned gap, keyed by (project_id, gap_id), with a foreign key to ``projects`` and one to the
member row (project_id, user_id) of ``project_members`` with ``ON DELETE CASCADE``, so a member who leaves or is
removed holds no gaps. Row-level security and the policy ``agent_project_scope`` of the agent role, and no grant to
that role, as on every gap table (the statements of 0003 are reused). No existing table changes.

Locks: the first online statement of upgrade() and downgrade() is ``SET LOCAL lock_timeout = '10s'``. The foreign
keys take a lock on ``projects`` and ``project_members``; if a lock is not granted in time the statement fails and
the WHOLE transaction (one per run) is rolled back, so nothing is half applied; the next start of the api retries the
revision.

downgrade() returns to the 0003_gap_module schema exactly: it DROPS gap_assignment with its index, constraints and
policy. Every assignment is lost (the gap event log keeps the ``assigned`` and ``unassigned`` events).

Revision ID: 0004_gap_assignment
Revises: 0003_gap_module
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from db.migration_helpers import agent_role, set_lock_timeout

# revision identifiers, used by Alembic.
revision: str = "0004_gap_assignment"
down_revision: str | Sequence[str] | None = "0003_gap_module"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TIMESTAMPTZ = sa.DateTime(timezone=True)

TABLE = "gap_assignment"
LOCK_TIMEOUT = "0003_lock_timeout.sql"
AGENT_ROLE = "0003_agent_role.sql"
ENABLE_RLS = "0003_enable_rls.sql"
CREATE_POLICY = "0003_create_policy.sql"
# The table and the role the two statements above name; replaced with this table and the agent role.
TABLE_PLACEHOLDER = "gap_option"
POLICY_ROLE_PLACEHOLDER = "agent_role"


def read_migration_sql(name: str) -> str:
    """One statement of ``migrations/sql/<name>``, on one line."""

    text = (Path(__file__).resolve().parent.parent / "sql" / name).read_text(encoding="utf-8")
    return " ".join(text.split()).replace("( ", "(").replace(" )", ")")


def _agent_role() -> str:
    """The agent role, quoted; online, it must exist and row-level security must apply to it."""

    return agent_role(read_migration_sql(AGENT_ROLE))


def _set_lock_timeout() -> None:
    """First online statement: give up on a lock after 10 s."""

    set_lock_timeout(read_migration_sql(LOCK_TIMEOUT))


def upgrade() -> None:
    """Upgrade schema: the table gap_assignment with row-level security for the agent role."""

    _set_lock_timeout()
    role = _agent_role()
    op.create_table(
        TABLE,
        sa.Column("project_id", UUID, nullable=False),
        sa.Column("gap_id", sa.Text(), nullable=False),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("assigned_by", sa.Text(), nullable=False),
        sa.Column("assigned_at", TIMESTAMPTZ, nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name=f"fk_{TABLE}_project_id_projects"),
        sa.ForeignKeyConstraint(
            ["project_id", "user_id"],
            ["project_members.project_id", "project_members.user_id"],
            name=f"fk_{TABLE}_project_id_project_members",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("project_id", "gap_id", name=f"pk_{TABLE}"),
    )
    op.create_index("ix_gap_assignment_user_id", TABLE, ["project_id", "user_id"])
    op.execute(read_migration_sql(ENABLE_RLS).replace(TABLE_PLACEHOLDER, TABLE))
    op.execute(
        read_migration_sql(CREATE_POLICY).replace(TABLE_PLACEHOLDER, TABLE).replace(POLICY_ROLE_PLACEHOLDER, role)
    )


def downgrade() -> None:
    """Downgrade schema to 0003_gap_module; every assignment is lost."""

    _set_lock_timeout()
    op.drop_table(TABLE)
