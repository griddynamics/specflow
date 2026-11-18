"""Steps the Alembic revisions share (``migrations/versions``); each revision keeps its own SQL files and passes the
statement it reads.

- ``set_lock_timeout``: the first online statement of a revision (offline output has no session to set it on).
- ``agent_role``: the agent role named by ``DATABASE_AGENT_USER`` (``migrations/env.py`` resolves it), quoted for DDL.
  Online, the revision fails before any change unless the role exists and row-level security applies to it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op


def set_lock_timeout(statement: str) -> None:
    """Run ``statement`` (``SET LOCAL lock_timeout = ...``) online; offline output gets a comment instead."""

    if context.is_offline_mode():
        op.get_context().impl.static_output("-- lock_timeout skipped (offline)")
        return
    op.execute(statement)


def agent_role(check: str) -> str:
    """The agent role, quoted. ``check`` selects ``bypasses_rls`` from ``pg_roles`` for ``:role_name``."""

    name = context.config.attributes.get("agent_user")
    if not name:
        raise RuntimeError("DATABASE_AGENT_USER is not resolved; run through migrations/env.py")
    if context.is_offline_mode():
        op.get_context().impl.static_output("-- agent role existence check skipped (offline)")
    else:
        found = op.get_bind().execute(sa.text(check), {"role_name": name}).one_or_none()
        if found is None:
            raise RuntimeError(
                f"The agent role named by DATABASE_AGENT_USER ({name!r}) does not exist; "
                "backend/postgres-init/10-specflow-roles.sh creates it on first start."
            )
        if found.bypasses_rls:
            raise RuntimeError(
                f"The agent role named by DATABASE_AGENT_USER ({name!r}) has SUPERUSER or BYPASSRLS, so row-level "
                "security would not apply; it must be NOSUPERUSER NOBYPASSRLS."
            )
    return op.get_context().dialect.identifier_preparer.quote_identifier(str(name))
