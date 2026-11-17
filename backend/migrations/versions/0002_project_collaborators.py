"""project collaborators: roles admin and collaborator, project_invites, legacy owner trigger

Steps M1-M7 of docs/projects/data-model.md#migration-0002:

M1  online: fail when a project has other than exactly one ``owner`` row (offline: comment, skipped)
M2  ``owner`` rows become ``admin`` (the old CHECK, which allows only ``owner``, is dropped first)
M3  ``ck_project_members_role`` allows ``admin`` and ``collaborator``; ``role`` loses its default
M4  ``ux_project_members_one_admin``: one admin per project
M5  BEFORE INSERT trigger ``project_members_legacy_owner``: during a rolling deploy, pods on the old
    code still insert ``owner``; the trigger stores it as ``admin`` (a later migration drops it)
M6  ``project_invites`` with row-level security and the ``agent_project_scope`` policy; no grant
M7  ``ix_users_email_lower`` on ``lower(email)``

downgrade() drops, in reverse order, ``ix_users_email_lower``, ``project_invites``, the trigger and its
function and ``ux_project_members_one_admin``; DELETES every ``collaborator`` row (data loss: only the
admin rows survive); turns ``admin`` back into ``owner``; restores the old CHECK and the ``'owner'``
default. Invites are lost with their table.

Revision ID: 0002_project_collaborators
Revises: 0001_initial
Create Date: 2026-11-17 09:00:00

"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0002_project_collaborators"
down_revision: str | Sequence[str] | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TIMESTAMPTZ = sa.DateTime(timezone=True)

POLICY = "agent_project_scope"
SCOPE = "NULLIF(current_setting('app.project_id', true), '')::uuid"
ROLE_CHECK = "ck_project_members_role"

PROJECTS = sa.table("projects", sa.column("id", UUID))
MEMBERS = sa.table("project_members", sa.column("project_id", UUID), sa.column("role", sa.Text()))


def read_migration_sql(name: str) -> str:
    """One statement of ``migrations/sql/<name>``, on one line."""

    text = (Path(__file__).resolve().parent.parent / "sql" / name).read_text(encoding="utf-8")
    return " ".join(text.split()).replace("( ", "(").replace(" )", ")")


def _agent_user() -> str:
    name = context.config.attributes.get("agent_user")
    if not name:
        raise RuntimeError("DATABASE_AGENT_USER is not resolved; run through migrations/env.py")
    return str(name)


def _quoted(name: str) -> str:
    return op.get_context().dialect.identifier_preparer.quote_identifier(name)


def _require_one_owner_per_project() -> None:
    """M1: every project has exactly one ``owner`` row, else the upgrade cannot name an admin."""

    if context.is_offline_mode():
        op.get_context().impl.static_output("-- owner count check skipped (offline)")
        return
    owners = (
        sa.select(MEMBERS.c.project_id, sa.func.count().label("n"))
        .where(MEMBERS.c.role == "owner")
        .group_by(MEMBERS.c.project_id)
        .subquery()
    )
    query = (
        sa.select(PROJECTS.c.id)
        .select_from(PROJECTS.outerjoin(owners, owners.c.project_id == PROJECTS.c.id))
        .where(sa.func.coalesce(owners.c.n, 0) != 1)
    )
    bad: list[Any] = list(op.get_bind().execute(query).scalars())
    if bad:
        raise RuntimeError(
            f"{len(bad)} project(s) do not have exactly one owner row (first: {bad[0]}); "
            "fix project_members before migrating to 0002_project_collaborators."
        )


def _create_project_invites(role: str) -> None:
    op.create_table(
        "project_invites",
        sa.Column("id", UUID, nullable=False),
        sa.Column("project_id", UUID, nullable=False),
        sa.Column("email_norm", sa.Text(), nullable=False),
        sa.Column("invited_by", UUID, nullable=False),
        sa.Column("created_at", TIMESTAMPTZ, server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("email_norm = lower(btrim(email_norm))", name=op.f("ck_project_invites_email_norm")),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_project_invites_project_id_projects"),
        sa.ForeignKeyConstraint(["invited_by"], ["users.id"], name="fk_project_invites_invited_by_users"),
        sa.PrimaryKeyConstraint("id", name="pk_project_invites"),
        sa.UniqueConstraint("project_id", "email_norm", name="uq_project_invites_project_id_email_norm"),
    )
    op.create_index("ix_project_invites_email_norm", "project_invites", ["email_norm"])
    # Row-level security as on every project-scoped table of 0001_initial; no grant (no agent route reads invites).
    op.execute(read_migration_sql("0002_invites_enable_rls.sql"))
    op.execute(
        f"CREATE POLICY {POLICY} ON project_invites TO {role} "
        f"USING (project_id = {SCOPE}) WITH CHECK (project_id = {SCOPE})"
    )


def upgrade() -> None:
    """Upgrade schema."""

    role = _quoted(_agent_user())
    _require_one_owner_per_project()  # M1
    # M2 and M3 interleave: the 0001 CHECK allows only 'owner', so it goes first, then the rows change (M2),
    # then the new CHECK (M3) is added.
    op.drop_constraint(op.f(ROLE_CHECK), "project_members", type_="check")
    op.execute(sa.update(MEMBERS).where(MEMBERS.c.role == "owner").values(role="admin"))  # M2
    op.create_check_constraint(op.f(ROLE_CHECK), "project_members", "role IN ('admin', 'collaborator')")
    op.alter_column("project_members", "role", server_default=None)
    op.create_index(  # M4
        "ux_project_members_one_admin",
        "project_members",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text("role = 'admin'"),
    )
    op.execute(read_migration_sql("0002_legacy_owner_function.sql"))  # M5
    op.execute(read_migration_sql("0002_legacy_owner_trigger.sql"))
    _create_project_invites(role)  # M6
    op.create_index("ix_users_email_lower", "users", [sa.text("lower(email)")])  # M7


def downgrade() -> None:
    """Downgrade schema: back to 0001; collaborator rows and invites are lost."""

    op.drop_index("ix_users_email_lower", table_name="users")
    op.drop_table("project_invites")
    op.execute(read_migration_sql("0002_drop_legacy_owner_trigger.sql"))
    op.execute(read_migration_sql("0002_drop_legacy_owner_function.sql"))
    op.drop_index("ux_project_members_one_admin", table_name="project_members")
    op.execute(sa.delete(MEMBERS).where(MEMBERS.c.role == "collaborator"))
    op.drop_constraint(op.f(ROLE_CHECK), "project_members", type_="check")
    op.execute(sa.update(MEMBERS).where(MEMBERS.c.role == "admin").values(role="owner"))
    op.create_check_constraint(op.f(ROLE_CHECK), "project_members", "role IN ('owner')")
    op.alter_column("project_members", "role", server_default=sa.text("'owner'"))
