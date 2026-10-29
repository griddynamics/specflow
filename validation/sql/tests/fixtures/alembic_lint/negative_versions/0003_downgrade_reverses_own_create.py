"""adds a widget cache table for the catalog service."""

from __future__ import annotations

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"


def upgrade() -> None:
    op.create_table("widget_cache")


def downgrade() -> None:
    # The mechanical reverse of this same revision's own create -- exempt from the
    # "documented reason" rule: undoing your own revision needs no separate justification.
    op.drop_table("widget_cache")
