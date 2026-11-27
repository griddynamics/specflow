"""Dropbox: one Dropbox connection per user, the ``dropbox`` project integration and the ``dropbox`` source kind.

Adds, on top of revision 0012_box, the table dropbox_connections (one row per Specflow user who connected a Dropbox
account; the refresh token is Fernet ciphertext in a ``bytea`` column, so a token over 1 KB fits; Dropbox refresh tokens
never rotate or expire, so there is no ``refresh_expires_at``; the display name, the account id and the team are kept,
the e-mail address is not), as in docs/dropbox/oauth.md. Widens ck_project_integrations_app to
``atlassian, github, slack, linear, box, dropbox`` and ck_connections_kind to
``gdrive, upload, confluence, jira_epic, linear_project, box, dropbox`` (a Dropbox source is imported by the API).
Row-level security is enabled on dropbox_connections; it has no policy and no grant: the agent role reads none of it.
No existing row changes.

Locks: the first online statement of upgrade() and downgrade() is ``SET LOCAL lock_timeout = '10s'``. The foreign key
takes a lock on ``users`` and the constraint swaps one on ``project_integrations`` and ``connections``; if a lock is
not granted in time the statement fails and the WHOLE transaction (one per run) is rolled back, so nothing is half
applied; the next start of the api retries the revision.

downgrade() returns to the 0012_box schema exactly: it DROPS dropbox_connections (every Dropbox connection is lost;
each user connects again), DELETES every ``dropbox`` row of project_integrations (every binding is lost) and every
``dropbox`` row of connections with its connection_items and the documents they hold (their objects stay in object
storage until removed by hand), then restores both check constraints.

Other branches (Notion) may claim the same revision number: whoever merges later renumbers.

Revision ID: 0013_dropbox
Revises: 0012_box
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from db.migration_helpers import connection_columns, delete_sources_of_kind, set_lock_timeout, swap_check

# revision identifiers, used by Alembic.
revision: str = "0013_dropbox"
down_revision: str | Sequence[str] | None = "0012_box"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TIMESTAMPTZ = sa.DateTime(timezone=True)

CONNECTIONS = "dropbox_connections"
INTEGRATIONS = "project_integrations"
SOURCES = "connections"
KIND_CHECK = "ck_connections_kind"
APP_CHECK = "ck_project_integrations_app"
LOCK_TIMEOUT = "0003_lock_timeout.sql"
ENABLE_RLS = "0003_enable_rls.sql"
# The table the statement above names; replaced with the new table.
TABLE_PLACEHOLDER = "gap_option"
STATUSES = ("active", "expired")
OLD_APPS = ("atlassian", "github", "slack", "linear", "box")
NEW_APP = "dropbox"
OLD_KINDS = ("gdrive", "upload", "confluence", "jira_epic", "linear_project", "box")
NEW_KIND = "dropbox"


def read_migration_sql(name: str) -> str:
    """One statement of ``migrations/sql/<name>``, on one line."""

    text = (Path(__file__).resolve().parent.parent / "sql" / name).read_text(encoding="utf-8")
    return " ".join(text.split()).replace("( ", "(").replace(" )", ")")


def upgrade() -> None:
    """Upgrade schema: dropbox_connections with row-level security (no policy), the wider app and kind checks."""

    set_lock_timeout(read_migration_sql(LOCK_TIMEOUT))
    op.create_table(
        CONNECTIONS,
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("account_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("team_id", sa.Text(), nullable=True),
        sa.Column("team_name", sa.Text(), nullable=True),
        *connection_columns(CONNECTIONS, STATUSES, refresh_expiry=False),
    )
    op.execute(read_migration_sql(ENABLE_RLS).replace(TABLE_PLACEHOLDER, CONNECTIONS))
    swap_check(APP_CHECK, INTEGRATIONS, "app", (*OLD_APPS, NEW_APP))
    swap_check(KIND_CHECK, SOURCES, "kind", (*OLD_KINDS, NEW_KIND))


def downgrade() -> None:
    """Downgrade schema to 0012_box; every Dropbox connection, binding and Dropbox source is lost."""

    set_lock_timeout(read_migration_sql(LOCK_TIMEOUT))
    delete_sources_of_kind(NEW_KIND)
    swap_check(KIND_CHECK, SOURCES, "kind", OLD_KINDS)
    integrations = sa.table(INTEGRATIONS, sa.column("app", sa.Text()))
    op.execute(sa.delete(integrations).where(integrations.c.app == NEW_APP))
    swap_check(APP_CHECK, INTEGRATIONS, "app", OLD_APPS)
    op.drop_table(CONNECTIONS)
