"""Fixture migration: raw SQL text reaches execute()."""

from alembic import op


def upgrade() -> None:
    op.execute("UPDATE customer_order SET status = 'OPEN' WHERE status IS NULL")
