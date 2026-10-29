"""branches off 0001 instead of stacking on 0002 -- creates a second head"""

from __future__ import annotations

revision: str = "0003"
down_revision: str | None = "0001"


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
