"""adds the nullable note column to ledger_entry."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"


def upgrade() -> None:
    op.add_column("ledger_entry", sa.Column("note", sa.String(200), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("ledger_entry") as batch:
        batch.drop_column("note")
