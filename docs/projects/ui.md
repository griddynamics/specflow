---
type: Design
title: "Project Settings UI"
description: "Settings route, sidebar item, General tab in the shell header, admin and collaborator views, confirmations, Create Project emails, errors shown and the frontend ports"
status: stable
tags: [projects, frontend]
---
# ui.md: Settings route, sidebar, header tab, General tab views, confirmations, Create Project emails, errors, ports

The design system rules of [../design-system/USAGE.md](../design-system/USAGE.md) apply; no new design-system component.

## Route and sidebar

| Item | Value |
|---|---|
| Route | `/projects/{projectId}/settings`, optional `?tab=general` (missing or unknown: `general`) |
| Route key | `settings` in `lib/routes.ts` (`ROUTES`, `RouteBuilder.settings(projectId, {tab?})`, `parseSettingsQuery`) |
| Page | `app/(app)/projects/[projectId]/settings/page.tsx`, a server component like the Plan page: `readProjectName`, `notFound()` on `not_found` |
| Sidebar | "Settings", icon `gear-six`, after SpecBot and before the Help group; in `SECTIONS` and `validation/nav-spec.json`; `sectionHref` gives the route |

## Header

The tab bar is in the shell header row through `<ShellHeader tabs={...}>`, level with the project picker, like Plan and
Decisions: a `SegmentedControl` labelled "Settings section" with one option, "General"; a change pushes the URL with
`?tab=`. The body has no H1, no page title and no tab row.

## General tab

Layout A2 (an inline form above one list card):

| Block | Admin | Collaborator |
|---|---|---|
| Project name | TextField + Save | not shown (the project picker shows the name) |
| Collaborators heading + "n of 50" | shown | shown |
| Add row | email field + Add; suggestions under the field | not shown |
| List card | rows with x | rows without x |
| Leave project | not shown | button under the list |

Row: avatar, name (the email when the name is null), email as the second line, then a badge. The admin's row has the
badge `Admin` and no x. An invite row has an `@` avatar, the email, "Not signed in yet" and the badge `Invited`; its x
cancels the invite. Order: [api.md](api.md#shapes).

Behavior:

- Save is enabled when the trimmed name differs from the current one and passes the name rule; success refreshes the
  project switcher.
- Suggestions: from 3 typed characters, after a 200 ms pause, at most 8; ArrowUp/ArrowDown, Enter and the mouse pick one;
  a pick fills the field. Esc closes only the list. A stale answer (older query) is dropped.
- Add (button or Enter) sends the typed or picked email; success adds the row and clears the field.
- x opens a confirmation (DS `Modal`) naming the person or email; Confirm removes or cancels. Leave project also asks.
- After leaving: the switcher list reloads and the app opens `/` (the newest remaining project, or the empty state).
- When the members list no longer holds the caller (removed meanwhile), the next read answers 404 and the shell
  redirects as for any inaccessible project.
- Removal with `claims_release_failed`: a toast says some story claims could not be freed and points to Plan > Work.

## Errors shown

| Cause | Where | Text (copy in `lib/copy.ts`) |
|---|---|---|
| `invalid_email` or a local format check | under the email field | "Enter a valid email address." |
| `email_domain_not_allowed` | under the field | "This email domain cannot sign in to SpecFlow." |
| `already_member` / `already_invited` | under the field | "Already a collaborator." / "Already invited." |
| `member_limit` | under the field | "This project has 50 people. Remove someone first." |
| name rule | under the name field | the Create Project name messages |
| `member_not_found`, `invite_not_found`, `cannot_remove_admin` (the list was stale) | toast, then the list reloads | "The list changed. It is up to date now." |
| `admin_required`, network, other | toast | "Could not save. Try again." |

## Create Project modal

The Contributors field becomes "Collaborators" by email: the `PeoplePicker` with typed-email chips (Enter or comma commits
a valid email) and the same suggestions (without `project_id`). Local checks: format and duplicates. The modal sends
`collaborators` only when it holds at least one email. A 422 marks the chip at `details.errors[].loc[2]` with the
message of the table above; the modal stays open.

## Ports

| Port | Members |
|---|---|
| `MembersPort` (new, `lib/domain/members.ts`) | `list(projectId)`, `add(projectId, email)`, `remove(projectId, userId)`, `cancelInvite(projectId, inviteId)`, `leave(projectId)`, `suggest(query, projectId?)` |
| `ProjectsPort` | + `rename(projectId, name)`; notifies `subscribe` listeners |
| `CreateProjectPort` | input `collaboratorEmails: string[]` replaces `contributorIds` |

The http adapter (`lib/data/http/members.ts`) checks every body's shape; `lib/api.ts` gains a PATCH helper. A 403 maps
to the new `DataErrorCode` `forbidden`; the API code stays in `DataError.detail.apiCode`.
