---
type: Questions
title: "Project collaborators decisions"
description: "Decision record of project collaborators: owner decisions and defaults (D1-D29), design choices with rejected options, follow-ups"
status: stable
tags: [projects]
---
# questions.md: decisions of project collaborators (D1-D29), design choices, follow-ups

Decided 2026-11-17. "Owner" = the product owner; "default" = proposed during design and kept after review. Every spec
file is written as decided; this file records why.

## Decisions

| # | Decision | By |
|---|---|---|
| D1 | The creator is the admin; one admin per project; no admin change or transfer. | owner |
| D2 | Collaborators are identified by email. | owner |
| D3 | No email or other notification on add (out of scope). | owner |
| D4 | Per-project Settings, cog in the sidebar; first tab "General" = project name and collaborators. | owner |
| D5 | An email that never signed in is a pending invite, adopted at the first sign-in with that email; the admin can cancel it. | owner |
| D6 | Only the admin renames the project. | owner |
| D7 | Collaborators see Settings read-only (the name part is replaced by D29). | owner |
| D8 | Create Project takes collaborator emails under the same rules as Settings, invites included. | owner |
| D9 | While typing, suggest people who already signed in. | owner |
| D10 | Suggestions start at 3 characters, match the start of name or email, at most 8. | owner |
| D11 | Only emails of allowed sign-in domains can be added. | owner |
| D12 | Removing a collaborator frees their story claims. | owner |
| D13 | A collaborator can leave; the admin cannot. | owner |
| D14 | Mockup first, two variants; the owner picks. | owner |
| D15 | Spec in `docs/projects/`. | owner |
| D16 | Gates: owner approves spec and plan, then build without stops, owner approves the result before the pull request. | owner |
| D17 | Leaving also frees the leaver's story claims. | default |
| D18 | Replaced by D27. | - |
| D19 | At most 50 members (admin included) plus pending invites per project; invites never expire. | default |
| D20 | A removed person's documents, findings and history stay; access ends on the next request. | default |
| D21 | Collaborators get MCP access automatically; membership stays the one access rule. | default |
| D22 | Log events carry ids only. | default |
| D23 | Out of scope: project deletion, admin transfer, several admins, notifications, invite links. | default |
| D24 | Layout: inline email field + Add, suggestions under the field, simple rows with x, read-only view with "Leave project". | owner |
| D25 | The tab bar sits in the shell header row through `ShellHeader`; no H1 or tab row in the body. | owner |
| D26 | Suggestions may list any signed-in user of the allowed domains (the company directory); at most 8; the query is never logged. | owner |
| D27 | Any sign-in the deployment accepts adopts the invites of its email (case-insensitive); deployments use a company-controlled email claim. | owner |
| D28 | Confirmation before remove and cancel; "n of 50" count; Settings after SpecBot. | default |
| D29 | Layout A2; a collaborator does not see the project name block; collaborator view = read-only list + "Leave project". | owner |

## Design choices

| # | Topic | Chosen | Rejected, and why |
|---|---|---|---|
| C1 | Role values | `admin` / `collaborator`, 0002 rewrites `owner` | keep `owner` + map in the API: permanent naming split; derive admin from `created_by`: two sources of truth |
| C2 | Pending invites | `project_invites` table | placeholder `users` rows: a placeholder would pass the access check and sign-in would re-key user rows by email; nullable `user_id` in `project_members`: breaks the key and every access query |
| C3 | Where adoption runs | `upsert_user`, every request | only at user creation: misses the add-vs-first-sign-in race and email changes; advisory locks: a lock protocol across two paths |
| C4 | Verified email for adoption (D27) | no extra gate | requiring `email_verified`: deployments with directory-managed emails send no flag and would never adopt |
| C5 | Claims on removal | membership first, then best-effort release | release first: an agent could claim again in between and a graph failure would block removal; outbox: new infrastructure for a rare, recoverable case |
| C6 | Create with emails | all-or-nothing | partial success: a project the user thinks failed |
| C7 | Caller role exposure | `MembersOut.role` only | on `ProjectOut` or `/me`: contract churn (MCP included) for one screen |
| C8 | Suggestions method | POST | GET: the typed text would land in URLs and access logs |
| C9 | Leave confirmation | asks, as remove does | no confirmation: leaving cannot be undone by the leaver |

## Follow-ups

| # | Item |
|---|---|
| F1 | Drop the legacy trigger `project_members_legacy_owner` in a later migration, once no pre-0002 API code runs. |
| F2 | Forced release of claims of tasks claimed on their own, if a host starts creating them for people. |
| F3 | Measure the per-request adoption statement under load; it is one indexed DELETE that usually matches nothing. |
| F4 | Identity providers whose email claim users can change: [../mcp-server/auth.md](../mcp-server/auth.md) recipes say which claim to use. |
