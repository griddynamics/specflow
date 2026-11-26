"""Box: one Box connection per user, the ``box`` project integration and the ``box`` source kind.

Adds, on top of revision 0010_github_projects, the table box_connections (one row per Specflow user who connected a
Box account; the refresh token is Fernet ciphertext; the Box display name, the Box user id and the enterprise are kept,
the Box login e-mail is not), as in docs/box/oauth.md. Widens ck_project_integrations_app to
``atlassian, github, slack, box`` and ck_connections_kind to ``gdrive, upload, confluence, jira_epic, box`` (a Box
source is imported by the API). Row-level security is enabled on box_connections; it has no policy and no grant: the
agent role reads none of it. No existing row changes.

Locks: the first online statement of upgrade() and downgrade() is ``SET LOCAL lock_timeout = '10s'``. The foreign key
takes a lock on ``users`` and the constraint swaps one on ``project_integrations`` and ``connections``; if a lock is
not granted in time the statement fails and the WHOLE transaction (one per run) is rolled back, so nothing is half
applied; the next start of the api retries the revision.

downgrade() returns to the 0010_github_projects schema exactly: it DROPS box_connections (every Box connection is lost;
each user connects again), DELETES every ``box`` row of project_integrations (every binding is lost) and every ``box``
row of connections with its connection_items and the documents they hold (their objects stay in object storage until
removed by hand), then restores both check constraints.

Other branches (Notion, Linear) also claim revision 0011: whoever merges second renumbers.

Revision ID: 0011_box
Revises: 0010_github_projects
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from db.migration_helpers import connection_columns, set_lock_timeout

# revision identifiers, used by Alembic.
revision: str = "0011_box"
down_revision: str | Sequence[str] | None = "0010_github_projects"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TIMESTAMPTZ = sa.DateTime(timezone=True)

CONNECTIONS = "box_connections"
INTEGRATIONS = "project_integrations"
SOURCES = "connections"
KIND_CHECK = "ck_connections_kind"
APP_CHECK = "ck_project_integrations_app"
LOCK_TIMEOUT = "0003_lock_timeout.sql"
ENABLE_RLS = "0003_enable_rls.sql"
# The table the statement above names; replaced with the new table.
TABLE_PLACEHOLDER = "gap_option"
STATUSES = ("active", "expired")
OLD_APPS = ("atlassian", "github", "slack")
NEW_APP = "box"
OLD_KINDS = ("gdrive", "upload", "confluence", "jira_epic")
NEW_KIND = "box"


def read_migration_sql(name: str) -> str:
    """One statement of ``migrations/sql/<name>``, on one line."""

    text = (Path(__file__).resolve().parent.parent / "sql" / name).read_text(encoding="utf-8")
    return " ".join(text.split()).replace("( ", "(").replace(" )", ")")


def _one_of(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    """Upgrade schema: box_connections with row-level security (no policy), the wider app and kind checks."""

    set_lock_timeout(read_migration_sql(LOCK_TIMEOUT))
    op.create_table(
        CONNECTIONS,
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("box_user_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("enterprise_id", sa.Text(), nullable=True),
        sa.Column("enterprise_name", sa.Text(), nullable=True),
        *connection_columns(CONNECTIONS, STATUSES),
    )
    op.execute(read_migration_sql(ENABLE_RLS).replace(TABLE_PLACEHOLDER, CONNECTIONS))
    op.drop_constraint(op.f(APP_CHECK), INTEGRATIONS, type_="check")
    op.create_check_constraint(op.f(APP_CHECK), INTEGRATIONS, _one_of("app", (*OLD_APPS, NEW_APP)))
    op.drop_constraint(op.f(KIND_CHECK), SOURCES, type_="check")
    op.create_check_constraint(op.f(KIND_CHECK), SOURCES, _one_of("kind", (*OLD_KINDS, NEW_KIND)))


def downgrade() -> None:
    """Downgrade schema to 0010_github_projects; every Box connection, binding and Box source is lost."""

    set_lock_timeout(read_migration_sql(LOCK_TIMEOUT))
    sources = sa.table(SOURCES, sa.column("id", UUID), sa.column("kind", sa.Text()))
    items = sa.table("connection_items", sa.column("connection_id", UUID), sa.column("document_id", UUID))
    documents = sa.table("documents", sa.column("id", UUID), sa.column("source_id", UUID))
    integrations = sa.table(INTEGRATIONS, sa.column("app", sa.Text()))
    new = sa.select(sources.c.id).where(sources.c.kind == NEW_KIND)
    held = sa.select(documents.c.id).where(documents.c.source_id.in_(new))
    # items first (they name the documents), then the documents the sources hold, then the sources
    op.execute(sa.delete(items).where(sa.or_(items.c.connection_id.in_(new), items.c.document_id.in_(held))))
    op.execute(sa.delete(documents).where(documents.c.source_id.in_(new)))
    op.execute(sa.delete(sources).where(sources.c.kind == NEW_KIND))
    op.drop_constraint(op.f(KIND_CHECK), SOURCES, type_="check")
    op.create_check_constraint(op.f(KIND_CHECK), SOURCES, _one_of("kind", OLD_KINDS))
    op.execute(sa.delete(integrations).where(integrations.c.app == NEW_APP))
    op.drop_constraint(op.f(APP_CHECK), INTEGRATIONS, type_="check")
    op.create_check_constraint(op.f(APP_CHECK), INTEGRATIONS, _one_of("app", OLD_APPS))
    op.drop_table(CONNECTIONS)
