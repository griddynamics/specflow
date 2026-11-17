---
type: DataModel
title: "Project collaborators data model"
description: "Roles, the project_invites table, invite adoption, the limit, the migration (0002), row-level security and indexes"
status: stable
tags: [projects, database]
---
# data-model.md: roles, invites, adoption, limit, migration, row-level security, indexes

## Roles

`project_members.role` is `owner` or `collaborator` (`db.models.PROJECT_ROLES`).

| # | Invariant | Enforced by |
|---|---|---|
| DM1 | Every project has exactly one `owner` row, its creator (`projects.created_by`). | `create_project` inserts it; unique index `ux_project_members_one_owner` |
| DM2 | The owner row is never deleted and its role never changes. | API: no route removes or demotes the owner; the owner cannot leave |
| DM3 | Every insert states its role; the column has no default. | revision `0002_project_collaborators` |
| DM4 | Access to a project = a member row, whatever the role ([../mcp-server/access-control.md](../mcp-server/access-control.md)). | `db.repositories.get_membership` (one query; `get_member_project` uses it) |

## Invites

`project_invites`:

| Column | Type | Rule |
|---|---|---|
| `id` | uuid PK | uuid4 from Python |
| `project_id` | uuid FK `projects.id` | not null; no cascade (projects are never deleted) |
| `email_norm` | text | not null; CHECK `email_norm = lower(btrim(email_norm))` |
| `invited_by` | uuid FK `users.id` | not null; the owner who added it |
| `created_at` | timestamptz | `now()` |

UNIQUE `(project_id, email_norm)`; index `ix_project_invites_email_norm (email_norm)`. An invite never expires; it ends
when adopted or cancelled. An invite exists only for an email no user row had when it was added
([api.md](api.md#add-a-member-or-invite)); a later sign-in adopts it.

## Email matching

The normalised email is `strip()` then `lower()` ([api.md](api.md#email-rules)). `users.email` is stored as the token
sent it and is not unique; every comparison is `lower(users.email) = :norm`, served by `ix_users_email_lower`.
Normalisation happens in Python; the stored `email_norm` is already normalised, so Postgres `lower()` only serves the
`users` lookup (ASCII addresses compare the same either way).
Several user rows with the same email: adding picks the newest `created_at`.

## Invite adoption

At the end of `upsert_user`, in its transaction, on every authenticated request (web and agent routes alike):

```sql
WITH adopted AS (DELETE FROM project_invites WHERE email_norm = :norm RETURNING project_id)
INSERT INTO project_members (project_id, user_id, role) SELECT project_id, :uid, 'collaborator' FROM adopted
ON CONFLICT DO NOTHING RETURNING project_id
```

- `:norm` is the principal's email, normalised. Any sign-in the deployment accepts adopts; the deployment's email rules
  are the gate ([../mcp-server/auth.md](../mcp-server/auth.md#claim-profile-for-any-oidc-provider)). An email the
  normaliser rejects adopts nothing and does not fail the request.
- The statement is built with SQLAlchemy Core (the repository has no inline SQL text); the SQL above shows its meaning.
- It runs on every path of `upsert_user` (existing user, new user, concurrent first sign-in).
- Concurrent requests of the same user: the DELETE locks the rows; the second request deletes none and inserts nothing.
- Running on every request (not only at user creation) also adopts an invite that raced the user's first sign-in, and an
  invite of an email the user's account changed to.
- One `db.invite.adopted` event per project, ids only.

## Limit

`count(project_members) + count(project_invites) <= 50` per project, checked by every add and by Create Project while the
project row is locked (`SELECT ... FOR UPDATE`), so concurrent adds cannot pass it. Adoption moves a row from invites to
members and leaves the count unchanged.

## Migration

The schema change is the revision `0002_project_collaborators` (down revision `0001_initial`), written by hand
(decision D33; `backend/migrations/README`). `0001_initial` is the 2.0.0 schema and is frozen. `specflow migrate`
(`alembic upgrade head`) upgrades a deployed database in place; nothing is recreated and no data is lost. The revision:

- replaces `ck_project_members_role` with `role IN ('owner', 'collaborator')` and drops the default of `project_members.role`;
- creates the unique partial index `ux_project_members_one_owner`: UNIQUE `(project_id) WHERE role = 'owner'`;
- creates `project_invites` with its constraints and indexes, turns row-level security on and adds the `agent_project_scope`
  policy; no grant;
- creates `ix_users_email_lower` on `lower(email)`.

Existing members keep their role (always `owner`), with one exception. A project that holds more than one `owner` row
(the 2.0.0 application never wrote one, but a seed script or a manual insert could have) cannot take the one-owner
index, so the revision normalises it after the new CHECK exists and before the index is created: the project's creator
(`projects.created_by`) stays `owner` when that user is an owner member of the project, otherwise the owner row with the
earliest `joined_at` stays (ties: the smallest `user_id`). Every other owner row of that project becomes `collaborator`
and keeps its access. It is one `UPDATE` (SQLAlchemy Core, in the revision); a project with a single owner is not
touched, so a second run changes nothing. There is no trigger and no other data rewrite.

The first online statement of the upgrade and of the downgrade is `SET LOCAL lock_timeout = '10s'`. If a lock is not
granted in time, the whole transaction rolls back (nothing is half applied) and the next start of the api retries it.

The revision can be run again. A database built from a temporary `0001_initial` that already held these objects is
stamped `0001_initial`, and for it the revision changes nothing: every statement is `IF [NOT] EXISTS`, or drops and
re-creates what the revision owns. The SQL is in `backend/migrations/sql/0002_*.sql`.

The downgrade returns to the 2.0.0 schema: it deletes the `collaborator` rows, drops `project_invites` (with its policy and
indexes) and the two new indexes, and restores `role IN ('owner')` and the default `'owner'`. The collaborators and the
pending invites are lost (story claims of those users in the graph store have no foreign key and stay behind).

## Row-level security

`project_invites` is project-scoped: RLS on, policy `agent_project_scope` on `project_id`, and the agent role gets no
grant (no agent route reads invites). The application role is the table owner and is not subject to RLS.
