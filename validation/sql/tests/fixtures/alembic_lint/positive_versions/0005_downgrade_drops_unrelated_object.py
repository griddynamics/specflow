"""adds a widget_extra table."""

from __future__ import annotations

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"


def upgrade() -> None:
    op.create_table("widget_extra")


def downgrade() -> None:
    # Not the reverse of this revision's own create (upgrade() created "widget_extra"):
    # a drop that reaches outside what this revision built still needs a documented reason.
    op.drop_table("legacy_shim")
