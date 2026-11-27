"""Steps the Alembic revisions share (``migrations/versions``); each revision keeps its own SQL files and passes the
statement it reads.

- ``set_lock_timeout``: the first online statement of a revision (offline output has no session to set it on).
- ``delete_sources_of_kinds``: the sources of the given kinds with their connection_items and the documents they hold
  (a downgrade that narrows ``ck_connections_kind``).
- ``agent_role``: the agent role named by ``DATABASE_AGENT_USER`` (``migrations/env.py`` resolves it), quoted for DDL.
  Online, the revision fails before any change unless the role exists and row-level security applies to it.
- ``connection_columns``: the columns and constraints every per-user OAuth connection table ends with (the token, its
  expiry, the status and the three timestamps, the status check, the user key and the foreign key to ``users``).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql


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


def connection_columns(table: str, statuses: Sequence[str]) -> list[sa.schema.SchemaItem]:
    """What ``<provider>_connections`` ends with, after the columns that name the provider's account: for
    ``op.create_table(table, <user_id and account columns>, *connection_columns(table, statuses))``."""

    timestamp = sa.DateTime(timezone=True)
    allowed = ", ".join(repr(status) for status in statuses)
    return [
        sa.Column("refresh_token", sa.LargeBinary(), nullable=False),
        sa.Column("refresh_expires_at", timestamp, nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("connected_at", timestamp, nullable=False),
        sa.Column("last_used_at", timestamp, nullable=False),
        sa.Column("expired_at", timestamp, nullable=True),
        sa.CheckConstraint(f"status IN ({allowed})", name=op.f(f"ck_{table}_status")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=f"fk_{table}_user_id_users", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", name=f"pk_{table}"),
    ]


def delete_sources_of_kinds(kinds: Sequence[str]) -> None:
    """Delete every ``connections`` row of ``kinds``, its ``connection_items`` and the documents it holds."""

    uuid = postgresql.UUID(as_uuid=True)
    sources = sa.table("connections", sa.column("id", uuid), sa.column("kind", sa.Text()))
    items = sa.table("connection_items", sa.column("connection_id", uuid), sa.column("document_id", uuid))
    documents = sa.table("documents", sa.column("id", uuid), sa.column("source_id", uuid))
    chosen = sa.select(sources.c.id).where(sources.c.kind.in_(kinds))
    held = sa.select(documents.c.id).where(documents.c.source_id.in_(chosen))
    # items first (they name the documents), then the documents the sources hold, then the sources
    op.execute(sa.delete(items).where(sa.or_(items.c.connection_id.in_(chosen), items.c.document_id.in_(held))))
    op.execute(sa.delete(documents).where(documents.c.source_id.in_(chosen)))
    op.execute(sa.delete(sources).where(sources.c.kind.in_(kinds)))
