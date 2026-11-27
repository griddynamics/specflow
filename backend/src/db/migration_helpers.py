"""Steps the Alembic revisions share (``migrations/versions``); each revision keeps its own SQL files and passes the
statement it reads.

- ``set_lock_timeout``: the first online statement of a revision (offline output has no session to set it on).
- ``agent_role``: the agent role named by ``DATABASE_AGENT_USER`` (``migrations/env.py`` resolves it), quoted for DDL.
  Online, the revision fails before any change unless the role exists and row-level security applies to it.
- ``connection_columns``: the columns and constraints every per-user OAuth connection table ends with (the token, its
  expiry, the status and the three timestamps, the status check, the user key and the foreign key to ``users``).
- ``swap_check``: replace a ``<column> IN (...)`` check constraint with one of other values (an app or kind added or
  removed).
- ``delete_sources_of_kind``: the downgrade of a source kind: its items, the documents its sources hold, the sources.
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


def connection_columns(
    table: str, statuses: Sequence[str], *, refresh_expiry: bool = True
) -> list[sa.schema.SchemaItem]:
    """What ``<provider>_connections`` ends with, after the columns that name the provider's account: for
    ``op.create_table(table, <user_id and account columns>, *connection_columns(table, statuses))``.
    ``refresh_expiry=False`` leaves out ``refresh_expires_at`` (a provider whose refresh tokens never expire)."""

    timestamp = sa.DateTime(timezone=True)
    allowed = ", ".join(repr(status) for status in statuses)
    expiry = [sa.Column("refresh_expires_at", timestamp, nullable=True)] if refresh_expiry else []
    return [
        sa.Column("refresh_token", sa.LargeBinary(), nullable=False),
        *expiry,
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("connected_at", timestamp, nullable=False),
        sa.Column("last_used_at", timestamp, nullable=False),
        sa.Column("expired_at", timestamp, nullable=True),
        sa.CheckConstraint(f"status IN ({allowed})", name=op.f(f"ck_{table}_status")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=f"fk_{table}_user_id_users", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", name=f"pk_{table}"),
    ]


def one_of(column: str, values: Sequence[str]) -> str:
    """The condition ``<column> IN ('a', 'b')``."""

    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def swap_check(name: str, table: str, column: str, values: Sequence[str]) -> None:
    """Drop the check ``name`` of ``table`` and create it again as ``<column> IN (values)``."""

    op.drop_constraint(op.f(name), table, type_="check")
    op.create_check_constraint(op.f(name), table, one_of(column, values))


def delete_sources_of_kind(kind: str) -> None:
    """Delete every ``connections`` row of ``kind`` with its ``connection_items`` and the documents those sources hold:
    items first (they name the documents), then the documents, then the sources."""

    uuid_type = postgresql.UUID(as_uuid=True)
    sources = sa.table("connections", sa.column("id", uuid_type), sa.column("kind", sa.Text()))
    items = sa.table("connection_items", sa.column("connection_id", uuid_type), sa.column("document_id", uuid_type))
    documents = sa.table("documents", sa.column("id", uuid_type), sa.column("source_id", uuid_type))
    of_kind = sa.select(sources.c.id).where(sources.c.kind == kind)
    held = sa.select(documents.c.id).where(documents.c.source_id.in_(of_kind))
    op.execute(sa.delete(items).where(sa.or_(items.c.connection_id.in_(of_kind), items.c.document_id.in_(held))))
    op.execute(sa.delete(documents).where(documents.c.source_id.in_(of_kind)))
    op.execute(sa.delete(sources).where(sources.c.kind == kind))
