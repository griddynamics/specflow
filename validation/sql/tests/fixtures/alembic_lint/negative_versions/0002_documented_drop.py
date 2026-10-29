"""drops the scratch table: it was a temporary staging table never read in
production, kept only for the first release's manual QA."""

from __future__ import annotations

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"


def upgrade() -> None:
    op.drop_table("scratch")


def downgrade() -> None:
    op.create_table("scratch")
