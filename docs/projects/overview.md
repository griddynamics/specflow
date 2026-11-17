---
type: Overview
title: "Project collaborators overview"
description: "Purpose, scope, actors, non-goals, boundaries and key terms of project collaborators and the per-project Settings screen"
status: stable
tags: [projects, auth]
---
# overview.md: purpose, scope, actors, non-goals, boundaries and key terms of project collaborators

## Purpose

A project used to have one member, its creator. Collaborators let a team share a project: the creator (the owner) adds
people by email, they see and work on the project in the web app and through their coding agents over MCP, and the
owner can rename the project and remove people. Everything is managed on one per-project Settings screen.

How it works, in short: the owner types an email; a person who already signed in becomes a collaborator at once, anyone
else becomes an invite that turns into a collaborator the first time that email signs in. Removing someone ends their
access on their next request and frees the stories they had claimed.

## Scope

| In scope | Where |
|---|---|
| Two roles, `owner` (the creator) and `collaborator` | [data-model.md](data-model.md#roles) |
| Add a collaborator by email; an email that never signed in becomes a pending invite | [api.md](api.md#add-a-member-or-invite) |
| A pending invite turns into a membership at the invitee's first accepted sign-in, web or MCP | [data-model.md](data-model.md#invite-adoption) |
| Remove a collaborator, cancel an invite, leave a project; story claims of the person who goes are freed | [api.md](api.md#remove-leave-and-claims) |
| Rename a project | [api.md](api.md#rename) |
| Suggestions of people who already signed in while typing an email | [api.md](api.md#suggestions) |
| Create Project with collaborator emails | [api.md](api.md#create-with-collaborators) |
| Settings screen, tab "General" | [ui.md](ui.md) |

## Actors

| Actor | Who | Can |
|---|---|---|
| Owner | the project's creator; exactly one per project | everything a collaborator can, plus rename, add, remove, cancel invites |
| Collaborator | a member added by the owner, or an invitee who signed in | read and work on the project (web and MCP), see the collaborators list, leave |
| Invitee | an email added before its owner ever signed in | nothing until the first sign-in with that email |
| Non-member | any other signed-in user | nothing; the project answers 404 as if it did not exist |

Membership stays the one access rule for reading and working on a project, on every web and agent route
([../mcp-server/access-control.md](../mcp-server/access-control.md)). The role only gates the Settings actions.

## Non-goals

- Email or any other notification when someone is added, removed or invited.
- Project deletion, owner transfer, more than one owner, invite links, invite expiry.
- Roles or permissions beyond owner/collaborator for reading or working on a project; MCP tools behave the same for both.
- Moving the documents, findings or history of a person who leaves; they stay with the project.

## Boundaries

| Component | Change |
|---|---|
| Postgres | `project_members.role` values, new `project_invites` table, the schema folded into `0001_initial` ([data-model.md](data-model.md)) |
| API (`backend/src/api`) | member, invite, leave, rename and suggestion routes; `POST /projects` accepts collaborator emails ([api.md](api.md)) |
| `db.repositories.upsert_user` | also adopts pending invites (both surfaces call it) |
| Graph store | none; removal calls the existing forced story release |
| MCP server | none; collaborators reach projects because membership decides |
| Frontend | Settings route and sidebar item, members port and adapter, Create Project emails ([ui.md](ui.md)) |

## Key terms

| Term | Meaning |
|---|---|
| Member | a `project_members` row: owner or collaborator |
| Invite | a `project_invites` row: a normalised email waiting for its first sign-in |
| Normalised email | trimmed and lower-cased; the form every comparison uses |
| Limit | at most 50 members (owner included) plus pending invites per project |
| Adoption | turning the invites of a signed-in email into memberships |
