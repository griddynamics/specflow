"""creates the ledger_entry table and its account index."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None


def upgrade() -> None:
    op.create_table(
        "ledger_entry",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account", sa.String(32), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
    )
    op.create_index("ix_ledger_entry_account", "ledger_entry", ["account"])


def downgrade() -> None:
    op.drop_index("ix_ledger_entry_account", table_name="ledger_entry")
    op.drop_table("ledger_entry")
