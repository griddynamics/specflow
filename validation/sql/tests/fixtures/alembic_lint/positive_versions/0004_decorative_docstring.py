"""This revision performs necessary schema upkeep for the release."""

from __future__ import annotations

from alembic import op

revision: str = "0004"
down_revision: str | None = "0002"


def upgrade() -> None:
    op.drop_table("widget")


def downgrade() -> None:
    op.create_table("widget")
