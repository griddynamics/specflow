"""GitHub: one GitHub connection per user and the repositories of a project.

Adds, on top of revision 0006_atlassian_connections, the tables github_connections (one row per Specflow user who
connected a GitHub account; the refresh token is Fernet ciphertext) and project_repositories (one row per GitHub
repository a project owner added, with its last clone check and snapshot), as in docs/github/data-model.md.
Row-level security is enabled on both. github_connections has no policy and no grant: the agent role reads none of
it. project_repositories gets the policy ``agent_project_scope`` of the agent role and no grant, as on every project
table (the statements of 0003 are reused). No existing table changes.

Locks: the first online statement of upgrade() and downgrade() is ``SET LOCAL lock_timeout = '10s'``. The foreign
keys take a lock on ``users`` and ``projects``; if a lock is not granted in time the statement fails and the WHOLE
transaction (one per run) is rolled back, so nothing is half applied; the next start of the api retries the revision.

downgrade() returns to the 0006_atlassian_connections schema exactly: it DROPS project_repositories and
github_connections. Every GitHub connection is lost (each user connects again) and so is every project repository
(the owners add them again; their snapshots stay in object storage until removed by hand).

Revision ID: 0007_github
Revises: 0006_atlassian_connections
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from db.migration_helpers import agent_role, set_lock_timeout

# revision identifiers, used by Alembic.
revision: str = "0007_github"
down_revision: str | Sequence[str] | None = "0006_atlassian_connections"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TIMESTAMPTZ = sa.DateTime(timezone=True)

CONNECTIONS = "github_connections"
REPOSITORIES = "project_repositories"
LOCK_TIMEOUT = "0003_lock_timeout.sql"
AGENT_ROLE = "0003_agent_role.sql"
ENABLE_RLS = "0003_enable_rls.sql"
CREATE_POLICY = "0003_create_policy.sql"
# The table and the role the statements above name; replaced with each table and the agent role.
TABLE_PLACEHOLDER = "gap_option"
POLICY_ROLE_PLACEHOLDER = "agent_role"
STATUSES = ("active", "expired")
CLONE_STATES = ("pending", "cloning", "cloned", "failed")
FAILURE_CODES = (
    "github_not_connected",
    "github_reconnect_required",
    "github_repo_not_accessible",
    "github_repo_too_large",
    "github_unreachable",
    "github_auth_failed",
    "github_clone_failed",
    "github_clone_timeout",
    "storage_failed",
)


def read_migration_sql(name: str) -> str:
    """One statement of ``migrations/sql/<name>``, on one line."""

    text = (Path(__file__).resolve().parent.parent / "sql" / name).read_text(encoding="utf-8")
    return " ".join(text.split()).replace("( ", "(").replace(" )", ")")


def _one_of(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    """Upgrade schema: the GitHub tables with row-level security (a project policy on the repositories only)."""

    set_lock_timeout(read_migration_sql(LOCK_TIMEOUT))
    role = agent_role(read_migration_sql(AGENT_ROLE))
    op.create_table(
        CONNECTIONS,
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("github_user_id", sa.BigInteger(), nullable=False),
        sa.Column("login", sa.Text(), nullable=False),
        sa.Column("refresh_token", sa.LargeBinary(), nullable=False),
        sa.Column("refresh_expires_at", TIMESTAMPTZ, nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("connected_at", TIMESTAMPTZ, nullable=False),
        sa.Column("last_used_at", TIMESTAMPTZ, nullable=False),
        sa.Column("expired_at", TIMESTAMPTZ, nullable=True),
        sa.CheckConstraint(_one_of("status", STATUSES), name=op.f(f"ck_{CONNECTIONS}_status")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=f"fk_{CONNECTIONS}_user_id_users", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", name=f"pk_{CONNECTIONS}"),
    )
    op.create_table(
        REPOSITORIES,
        sa.Column("id", UUID, nullable=False),
        sa.Column("project_id", UUID, nullable=False),
        sa.Column("github_repo_id", sa.BigInteger(), nullable=False),
        sa.Column("full_name", sa.Text(), nullable=False),
        sa.Column("html_url", sa.Text(), nullable=False),
        sa.Column("private", sa.Boolean(), nullable=False),
        sa.Column("added_by", UUID, nullable=True),
        sa.Column("clone_state", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("failure_code", sa.Text(), nullable=True),
        sa.Column("default_branch", sa.Text(), nullable=True),
        sa.Column("head_sha", sa.Text(), nullable=True),
        sa.Column("file_count", sa.Integer(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("snapshot_key", sa.Text(), nullable=True),
        sa.Column("snapshot_bytes", sa.BigInteger(), nullable=True),
        sa.Column("checked_at", TIMESTAMPTZ, nullable=True),
        sa.Column("cloned_at", TIMESTAMPTZ, nullable=True),
        sa.Column("created_at", TIMESTAMPTZ, server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", TIMESTAMPTZ, server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(_one_of("clone_state", CLONE_STATES), name=op.f(f"ck_{REPOSITORIES}_clone_state")),
        sa.CheckConstraint(_one_of("failure_code", FAILURE_CODES), name=op.f(f"ck_{REPOSITORIES}_failure_code")),
        sa.CheckConstraint(
            "(clone_state = 'failed') = (failure_code IS NOT NULL)",
            name=op.f(f"ck_{REPOSITORIES}_failure_code_matches_state"),
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name=f"fk_{REPOSITORIES}_project_id_projects"),
        sa.ForeignKeyConstraint(
            ["added_by"], ["users.id"], name=f"fk_{REPOSITORIES}_added_by_users", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=f"pk_{REPOSITORIES}"),
        sa.UniqueConstraint("project_id", "github_repo_id", name="uq_project_repositories_project_repo"),
    )
    op.execute(read_migration_sql(ENABLE_RLS).replace(TABLE_PLACEHOLDER, CONNECTIONS))
    op.execute(read_migration_sql(ENABLE_RLS).replace(TABLE_PLACEHOLDER, REPOSITORIES))
    op.execute(
        read_migration_sql(CREATE_POLICY)
        .replace(TABLE_PLACEHOLDER, REPOSITORIES)
        .replace(POLICY_ROLE_PLACEHOLDER, role)
    )


def downgrade() -> None:
    """Downgrade schema to 0006_atlassian_connections; every GitHub connection and project repository is lost."""

    set_lock_timeout(read_migration_sql(LOCK_TIMEOUT))
    op.drop_table(REPOSITORIES)
    op.drop_table(CONNECTIONS)
