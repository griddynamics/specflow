---
type: Design
title: "Project members UI"
description: "The Members dialog of the V2 header and the Create Project dialog of the project menu, owner and collaborator views, confirmations, errors shown and the frontend ports"
status: stable
tags: [projects, frontend]
---
# ui.md: the Members dialog, the Create Project dialog, their views, confirmations, errors, ports

Every screen is Graphico: the UI kit of `packages/ui` ([../mission-control/ui.md](../mission-control/ui.md)); no design-system component of our own.

## Where they live

| Item | Value |
|---|---|
| Members | a dialog the V2 header's **Members** link opens, over any V2 screen (`components/v2/members/MembersDialog.tsx`); no route of its own |
| Dialog body | mounts only while the dialog is open: it reads the members when opened and drops its state when closed |
| Create project | the "Create new project" dialog of the header's project menu, and the uncloseable "Create your first project" window of a person with no project (`components/v2/shell/CreateProjectDialog.tsx`) |
| Leaving | losing the project (left, or removed meanwhile) closes the dialog and opens `/`, which picks the next project |

## The dialog body

An inline form above one list card:

| Block | Owner | Collaborator |
|---|---|---|
| Project name | TextInput + Save | not shown (the project menu shows the name) |
| Collaborators heading + "n of 50" | shown | shown |
| Add row | email field + Add; suggestions under the field | not shown |
| List card | rows with x | rows without x |
| Leave project | not shown | button under the list |

Row: avatar, name (the email when the name is null), email as the second line, then a badge. The owner's row has the
badge `Owner` and no x. An invite row has an `@` avatar, the email, "Not signed in yet" and the badge `Invited`; its x
cancels the invite. Order: [api.md](api.md#shapes).

Behavior:

- Save is enabled when the trimmed name differs from the current one and passes the name rule; success refreshes the
  project menu (the `ProjectsPort` rename notifies its subscribers).
- Suggestions: from 3 typed characters, after a 200 ms pause, at most 8; ArrowUp/ArrowDown, Enter and the mouse pick one;
  a pick fills the field. Esc closes only the list. A stale answer (older query) is dropped.
- Add (button or Enter) sends the typed or picked email; success adds the row and clears the field.
- x opens a confirmation (a `ConfirmPanel`) naming the person or email; Confirm removes or cancels. Leave project also asks.
- After leaving: the dialog closes and the app opens `/` (the newest remaining project, or the first-run window).
- Removal with `claims_release_failed`: a toast says some story claims could not be freed and points to Plan > Work.

## Errors shown

| Cause | Where | Text (copy in `lib/copy.ts`) |
|---|---|---|
| `invalid_email` or a local format check | under the email field | "Enter a valid email address." |
| `email_domain_not_allowed` | under the field | "This email domain cannot sign in to SpecFlow." |
| `already_member` / `already_invited` | under the field | "Already a collaborator." / "Already invited." |
| `member_limit` | under the field | "This project has 50 people. Remove someone first." |
| name rule | under the name field | the Create Project name messages |
| `member_not_found`, `invite_not_found`, `cannot_remove_owner` (the list was stale) | toast, then the list reloads | "The list changed. It is up to date now." |
| `owner_required`, network, other | toast | "Could not save. Try again." |

## Create Project dialog

The form is the name only: the collaborators are invited from Members once the project exists (D11). The rules and the
failure texts are `components/shared/create-project/form-logic.ts`; a refused name is said in words under the field and
the dialog stays open. The http port still accepts `collaboratorEmails` and sends them when a caller sets them
(`lib/data/http/projects.ts`); the V2 dialog never does.

## Ports

| Port | Members |
|---|---|
| `MembersPort` (`lib/domain/members.ts`) | `list(projectId)`, `add(projectId, email)`, `remove(projectId, userId)`, `cancelInvite(projectId, inviteId)`, `leave(projectId)`, `suggest(query, projectId?)` |
| `ProjectsPort` | `rename(projectId, name)`; notifies `subscribe` listeners |
| `CreateProjectPort` | input `collaboratorEmails: string[]` (empty from the V2 dialog) |

The http adapter (`lib/data/http/members.ts`) checks every body's shape; `lib/api.ts` has the PATCH helper. A 403 maps
to the `DataErrorCode` `forbidden`; the API code stays in `DataError.detail.apiCode`.
