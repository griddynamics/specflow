"""Fixture migration: Core constructs and plain strings are not raw SQL."""

import sqlalchemy as sa
from alembic import op

NOTE = "SELECT id FROM customer_order WHERE id = 1"


def upgrade() -> None:
    op.create_table("warehouse", sa.Column("id", sa.Integer(), primary_key=True))
