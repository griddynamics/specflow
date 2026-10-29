"""Initial revision: the migration SQL lives in a file below `alembic/`, loaded by name."""

from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BASE = Path(__file__).parent.parent


def read_migration_sql(path: str) -> str:
    return (_BASE / path).read_text(encoding="utf-8")


def upgrade() -> None:
    op.execute(read_migration_sql("sql/0001_drop_cache.sql"))


def downgrade() -> None:
    msg = "the dropped cache is rebuilt on demand and has no data to restore"
    raise NotImplementedError(msg)
