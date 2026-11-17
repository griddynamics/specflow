"""Project collaborators: a second member role, one owner per project, and invites.

Adds, on top of the 2.0.0 schema (revision 0001_initial, which is frozen):

- ``project_members.role`` is ``owner`` or ``collaborator`` (the CHECK
  ``ck_project_members_role`` is replaced) and has no default any more;
- the partial unique index ``ux_project_members_one_owner`` (one owner per project);
- the table ``project_invites`` (an email invited before its first sign-in) with its
  index ``ix_project_invites_email_norm``, row-level security and the policy
  ``agent_project_scope`` of the agent role, and no grant to that role;
- the index ``ix_users_email_lower`` (case-insensitive email lookups).

Every existing member keeps the role it has (always ``owner`` in the 2.0.0 application), with one
exception: a project that holds more than one ``owner`` row (data the 2.0.0 application never wrote,
but a seed script or a manual insert could have) cannot take the one-owner index. After the new CHECK
exists and before the index is created, those projects are normalised: the project's creator
(``projects.created_by``) stays ``owner`` when that user is an owner member of it, otherwise the owner
row with the earliest ``joined_at`` stays (ties: the smallest ``user_id``); every other owner row of
the project becomes ``collaborator`` and keeps its access. Projects with one owner are not touched,
so a re-run changes nothing.

Locks: the first online statement of upgrade() and downgrade() is ``SET LOCAL lock_timeout = '10s'``.
If a lock is not granted in time the statement fails and the WHOLE transaction (one per run) is
rolled back, so nothing is half applied; the next start of the api retries the revision safely.

Re-runnable on purpose. Some databases were built from a temporary 0001_initial that already
held these objects and are stamped ``0001_initial``; for them this revision changes nothing.
Every statement is ``IF [NOT] EXISTS``, or drops and re-creates what it owns, so a run on such
a database leaves the schema as it is.

downgrade() returns to the 2.0.0 schema exactly. It DELETES every ``collaborator`` row of
project_members and DROPS the table project_invites with its policy and indexes, drops the
indexes ux_project_members_one_owner and ix_users_email_lower, restores the CHECK
``role IN ('owner')`` and the default ``'owner'`` of project_members.role. The collaborators
and the pending invites are lost (story claims of those users in the graph store have no foreign key
and stay behind, dangling); owners and every other table are untouched.

Revision ID: 0002_project_collaborators
Revises: 0001_initial
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import context, op

# revision identifiers, used by Alembic.
revision: str = "0002_project_collaborators"
down_revision: str | Sequence[str] | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LOCK_TIMEOUT = "0002_lock_timeout.sql"
# Statements in order (files of migrations/sql/): the role CHECK, then the owner normalisation, then the
# rest; the policy comes last because it needs the role name.
UPGRADE_CHECK = (
    "0002_drop_role_check.sql",
    "0002_add_role_check.sql",
)
UPGRADE_BEFORE_POLICY = (
    "0002_drop_role_default.sql",
    "0002_one_owner_index.sql",
    "0002_users_email_index.sql",
    "0002_create_invites.sql",
    "0002_invites_email_index.sql",
    "0002_invites_enable_rls.sql",
    "0002_invites_drop_policy.sql",
)
# After the DELETE of the collaborator rows.
DOWNGRADE_STATEMENTS = (
    "0002_drop_invites.sql",
    "0002_drop_one_owner_index.sql",
    "0002_drop_role_check.sql",
    "0002_add_owner_only_check.sql",
    "0002_set_role_default.sql",
    "0002_drop_users_email_index.sql",
)
POLICY_ROLE_PLACEHOLDER = "agent_role"


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


def _require_role(name: str) -> None:
    """Fail online, before any change, unless the agent role exists with RLS applying to it."""

    if context.is_offline_mode():
        op.get_context().impl.static_output("-- agent role existence check skipped (offline)")
        return
    roles = sa.table(
        "pg_roles",
        sa.column("rolname"),
        sa.column("rolsuper"),
        sa.column("rolbypassrls"),
    )
    found = op.get_bind().execute(sa.select(roles.c.rolsuper, roles.c.rolbypassrls).where(roles.c.rolname == name))
    row = found.one_or_none()
    if row is None:
        problem = "does not exist; backend/postgres-init/10-specflow-roles.sh creates it on first start"
    elif row.rolsuper or row.rolbypassrls:
        problem = (
            "has SUPERUSER or BYPASSRLS, so row-level security would not apply; it must be NOSUPERUSER NOBYPASSRLS"
        )
    else:
        return
    raise RuntimeError(f"The agent role named by DATABASE_AGENT_USER ({name!r}) {problem}.")


def _set_lock_timeout() -> None:
    """First online statement: give up on a lock after 10 s (offline output has no session to set it on)."""

    if context.is_offline_mode():
        op.get_context().impl.static_output("-- lock_timeout skipped (offline)")
        return
    op.execute(read_migration_sql(LOCK_TIMEOUT))


def _normalise_owners() -> None:
    """Keep one ``owner`` per project; turn the other owner rows into ``collaborator`` (see the docstring)."""

    members = sa.table(
        "project_members",
        sa.column("project_id", sa.Uuid()),
        sa.column("user_id", sa.Uuid()),
        sa.column("role", sa.Text()),
        sa.column("joined_at", sa.DateTime(timezone=True)),
    )
    projects = sa.table("projects", sa.column("id", sa.Uuid()), sa.column("created_by", sa.Uuid()))
    rank = (
        sa.func.row_number()
        .over(
            partition_by=members.c.project_id,
            order_by=[
                (members.c.user_id == projects.c.created_by).desc(),
                members.c.joined_at,
                members.c.user_id,
            ],
        )
        .label("rank")
    )
    owners = (
        sa.select(members.c.project_id, members.c.user_id, rank)
        .select_from(members.join(projects, projects.c.id == members.c.project_id))
        .where(members.c.role == "owner")
        .subquery("owners")
    )
    extra = sa.select(owners.c.project_id, owners.c.user_id).where(owners.c.rank > 1)
    op.execute(
        sa.update(members)
        .where(
            members.c.role == "owner",
            sa.tuple_(members.c.project_id, members.c.user_id).in_(extra),
        )
        .values(role="collaborator")
    )


def upgrade() -> None:
    """Upgrade schema."""

    name = _agent_user()
    _set_lock_timeout()
    _require_role(name)
    for check in UPGRADE_CHECK:
        op.execute(read_migration_sql(check))
    _normalise_owners()
    for before in UPGRADE_BEFORE_POLICY:
        op.execute(read_migration_sql(before))
    op.execute(read_migration_sql("0002_invites_create_policy.sql").replace(POLICY_ROLE_PLACEHOLDER, _quoted(name)))


def downgrade() -> None:
    """Downgrade schema to the 2.0.0 one; collaborator rows and project_invites are lost."""

    _set_lock_timeout()
    # Core construct, not SQL text: the one DELETE of the revision (collaborators are lost).
    members = sa.table("project_members", sa.column("role", sa.Text()))
    op.execute(sa.delete(members).where(members.c.role == "collaborator"))
    for after in DOWNGRADE_STATEMENTS:
        op.execute(read_migration_sql(after))
