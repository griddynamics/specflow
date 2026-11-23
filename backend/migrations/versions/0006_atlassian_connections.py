"""Atlassian connections: one OAuth connection per user and the acting account of a Jira target.

Adds, on top of revision 0005_jira_sync, the table atlassian_connections (one row per Specflow user who connected an
Atlassian account; the refresh token is Fernet ciphertext) and the column jira_targets.connected_user_id (the owner
whose connection a sync acts as, set null when that user is deleted), as in docs/jira-sync/oauth.md "Data".
Row-level security is enabled on atlassian_connections with no policy and no grant: the agent role reads none of it
(the statement of 0003 is reused).

Locks: the first online statement of upgrade() and downgrade() is ``SET LOCAL lock_timeout = '10s'``. The foreign
keys take a lock on ``users`` and the new column one on ``jira_targets``; if a lock is not granted in time the
statement fails and the WHOLE transaction (one per run) is rolled back, so nothing is half applied; the next start of
the api retries the revision.

downgrade() returns to the 0005_jira_sync schema exactly: it DROPS the column jira_targets.connected_user_id and the
table atlassian_connections. Every Atlassian connection is lost (each user connects again) and so is the acting
account of every target (the owners save their targets again).

Revision ID: 0006_atlassian_connections
Revises: 0005_jira_sync
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from db.migration_helpers import set_lock_timeout

# revision identifiers, used by Alembic.
revision: str = "0006_atlassian_connections"
down_revision: str | Sequence[str] | None = "0005_jira_sync"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TIMESTAMPTZ = sa.DateTime(timezone=True)

CONNECTIONS = "atlassian_connections"
TARGETS = "jira_targets"
ACTING_COLUMN = "connected_user_id"
ACTING_KEY = f"fk_{TARGETS}_{ACTING_COLUMN}_users"
LOCK_TIMEOUT = "0003_lock_timeout.sql"
ENABLE_RLS = "0003_enable_rls.sql"
# The table the statement above names; replaced with the new table.
TABLE_PLACEHOLDER = "gap_option"
STATUSES = ("active", "expired")


def read_migration_sql(name: str) -> str:
    """One statement of ``migrations/sql/<name>``, on one line."""

    text = (Path(__file__).resolve().parent.parent / "sql" / name).read_text(encoding="utf-8")
    return " ".join(text.split()).replace("( ", "(").replace(" )", ")")


def _one_of(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    """Upgrade schema: the connections table (row-level security, no policy) and the acting account column."""

    set_lock_timeout(read_migration_sql(LOCK_TIMEOUT))
    op.create_table(
        CONNECTIONS,
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("cloud_id", sa.Text(), nullable=False),
        sa.Column("account_id", sa.Text(), nullable=False),
        sa.Column("account_name", sa.Text(), nullable=False),
        sa.Column("scopes", sa.Text(), nullable=False),
        sa.Column("refresh_token", sa.LargeBinary(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("connected_at", TIMESTAMPTZ, nullable=False),
        sa.Column("last_used_at", TIMESTAMPTZ, nullable=False),
        sa.Column("expired_at", TIMESTAMPTZ, nullable=True),
        sa.CheckConstraint(_one_of("status", STATUSES), name=op.f(f"ck_{CONNECTIONS}_status")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=f"fk_{CONNECTIONS}_user_id_users", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", name=f"pk_{CONNECTIONS}"),
    )
    op.execute(read_migration_sql(ENABLE_RLS).replace(TABLE_PLACEHOLDER, CONNECTIONS))
    op.add_column(TARGETS, sa.Column(ACTING_COLUMN, UUID, nullable=True))
    op.create_foreign_key(ACTING_KEY, TARGETS, "users", [ACTING_COLUMN], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    """Downgrade schema to 0005_jira_sync; every connection and the acting account of every target are lost."""

    set_lock_timeout(read_migration_sql(LOCK_TIMEOUT))
    op.drop_constraint(ACTING_KEY, TARGETS, type_="foreignkey")
    op.drop_column(TARGETS, ACTING_COLUMN)
    op.drop_table(CONNECTIONS)
