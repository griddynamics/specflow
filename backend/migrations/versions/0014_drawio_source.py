"""The diagrams.net link source: the source kind ``drawio``.

Adds, on top of revision 0013_dropbox, the kind ``drawio`` to the check constraint ck_connections_kind, which
becomes ``gdrive, upload, confluence, jira_epic, linear_project, box, dropbox, drawio`` (a source imported by the API
from a diagrams.net link). No table changes.

Dropbox took revision 0013 first, so this revision follows it.

Locks: the first online statement of upgrade() and downgrade() is ``SET LOCAL lock_timeout = '10s'``. The constraint
swap takes a lock on ``connections``; if it is not granted in time the statement fails and the WHOLE transaction (one
per run) is rolled back, so nothing is half applied; the next start of the api retries the revision.

downgrade() returns to the 0013_dropbox schema exactly. A source of a kind the old constraint refuses cannot
stay, so the downgrade DELETES every ``drawio`` row of connections with its connection_items and the documents they hold
(their objects stay in object storage until removed by hand).

Revision ID: 0014_drawio_source
Revises: 0013_dropbox
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from db.migration_helpers import delete_sources_of_kind, set_lock_timeout, swap_check

# revision identifiers, used by Alembic.
revision: str = "0014_drawio_source"
down_revision: str | Sequence[str] | None = "0013_dropbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SOURCES = "connections"
KIND_CHECK = "ck_connections_kind"
LOCK_TIMEOUT = "0003_lock_timeout.sql"
OLD_KINDS = ("gdrive", "upload", "confluence", "jira_epic", "linear_project", "box", "dropbox")
NEW_KIND = "drawio"


def read_migration_sql(name: str) -> str:
    """One statement of ``migrations/sql/<name>``, on one line."""

    text = (Path(__file__).resolve().parent.parent / "sql" / name).read_text(encoding="utf-8")
    return " ".join(text.split()).replace("( ", "(").replace(" )", ")")


def upgrade() -> None:
    """Upgrade schema: the wider source kinds."""

    set_lock_timeout(read_migration_sql(LOCK_TIMEOUT))
    swap_check(KIND_CHECK, SOURCES, "kind", (*OLD_KINDS, NEW_KIND))


def downgrade() -> None:
    """Downgrade schema to 0013_dropbox; every drawio source is lost."""

    set_lock_timeout(read_migration_sql(LOCK_TIMEOUT))
    delete_sources_of_kind(NEW_KIND)
    swap_check(KIND_CHECK, SOURCES, "kind", OLD_KINDS)
