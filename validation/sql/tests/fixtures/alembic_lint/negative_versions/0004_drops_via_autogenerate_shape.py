"""drops the legacy index ix_widget_legacy, superseded by a composite index."""

from __future__ import annotations

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"


def upgrade() -> None:
    # Autogenerate's own shape: the object name comes through `op.f(...)` and a
    # `table_name=` keyword, never a bare positional string.
    op.drop_index(op.f("ix_widget_legacy"), table_name="widget")


def downgrade() -> None:
    op.create_index(op.f("ix_widget_legacy"), "widget", ["legacy_code"])
