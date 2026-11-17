---
type: DataModel
title: "Project collaborators data model"
description: "Roles, the project_invites table, invite adoption, the limit, migration 0002 with the legacy-owner trigger, row-level security and indexes"
status: stable
tags: [projects, database]
---
# data-model.md: roles, invites, adoption, limit, migration 0002, row-level security, indexes

## Roles

`project_members.role` is `admin` or `collaborator` (`db.models.PROJECT_ROLES`).

| # | Invariant | Enforced by |
|---|---|---|
| DM1 | Every project has exactly one `admin` row, its creator (`projects.created_by`). | `create_project` inserts it; unique index `ux_project_members_one_admin` |
| DM2 | The admin row is never deleted and its role never changes. | API: no route removes or demotes the admin; the admin cannot leave |
| DM3 | Every insert states its role; the column has no default. | migration 0002 |
| DM4 | Access to a project = a member row, whatever the role ([../mcp-server/access-control.md](../mcp-server/access-control.md)). | `db.repositories.get_membership` (one query; `get_member_project` uses it) |

## Invites

`project_invites`:

| Column | Type | Rule |
|---|---|---|
| `id` | uuid PK | uuid4 from Python |
| `project_id` | uuid FK `projects.id` | not null; no cascade (projects are never deleted) |
| `email_norm` | text | not null; CHECK `email_norm = lower(btrim(email_norm))` |
| `invited_by` | uuid FK `users.id` | not null; the admin who added it |
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

## Migration 0002

`0002_project_collaborators`, down revision `0001_initial`:

| # | Step |
|---|---|
| M1 | Online: fail with a clear message when a project has other than exactly one `owner` row. Offline (SQL render): emit a comment and skip the check, as `0001_initial` does for its role check. |
| M2 | Drop `ck_project_members_role` (it allows only `owner`, so the update below would fail while it exists) and the `role` server default. |
| M3 | `UPDATE project_members SET role = 'admin' WHERE role = 'owner'`, then add `ck_project_members_role` as `role IN ('admin', 'collaborator')`. |
| M4 | Create `ux_project_members_one_admin`: UNIQUE `(project_id) WHERE role = 'admin'`. |
| M5 | Create the legacy trigger (below). |
| M6 | Create `project_invites` with its constraints and indexes; enable row-level security and the `agent_project_scope` policy on it, as on every project-scoped table of `0001_initial`; no grant. |
| M7 | Create `ix_users_email_lower` on `lower(email)`. |

Legacy trigger: BEFORE INSERT on `project_members`, `project_members_legacy_owner` sets `NEW.role = 'admin'` when it is
`owner`. During a rolling deploy, API pods still on the old code create projects with `role='owner'`; the trigger keeps
those inserts valid under the new CHECK. A later migration drops it ([questions.md](questions.md#follow-ups)).

Downgrade (reverse order): drop `ix_users_email_lower`, `project_invites`, the trigger and its function,
`ux_project_members_one_admin`; DELETE every `collaborator` row (data loss, stated in the migration docstring);
`admin` back to `owner`; the old CHECK and server default.

## Row-level security

`project_invites` is project-scoped: RLS on, policy `agent_project_scope` on `project_id`, and the agent role gets no
grant (no agent route reads invites). The application role is the table owner and is not subject to RLS.
