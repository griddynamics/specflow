---
type: Design
title: "Jira destination UI"
description: "The canvas Jira plan destination and the Integrations page's Atlassian card: the connector form, the check-then-add flow, the saved-on target, the node's states, errors shown and the frontend ports"
status: stable
tags: [jira, frontend]
---
# ui.md: the canvas Jira destination: connector form, check-then-add, saved-on target, node states, errors, ports

The web app only configures the sync; there is no "Sync now" button and no preview (D24). Every screen is Graphico
(`packages/ui`): the destination is a canvas Plan-stage connector, and the Atlassian account itself is connected from
the Integrations page's Atlassian card ("Jira and Confluence": consent, then the site). The acting account is the
project's Atlassian integration.

## Where it lives

| Item | Value |
|---|---|
| Destination form | the Plan stage's "Send the plan to Jira" connector (`?add=jira` opens it; `components/v2/canvas/JiraForm.tsx`); no route or tab of its own |
| Atlassian connection | the Integrations page's Atlassian card; the OAuth return lands on the Integrations page with `?atlassian=` and shows one toast |
| Node | `Jira · <epic key, else the created Epic, else the space key>` with its state as words (never by colour alone); who connected it is in the node's dialog |
| Saved target | Add destination saves the target with `enabled` true — there is no switch; the plan syncs to the target once it exists |

## The form

Top to bottom:

1. **Gate** (a read of `jira.get` decides what shows): while it loads, a quiet line; a failed read (the network, not an
   answer) says so and offers to try again; `available` false shows "Jira is not connected to this Specflow server. An
   administrator connects an Atlassian account."; a server that answers `editable` false shows the read-only line.
   The fields render only in the `open` gate.
2. **Target form**: "Jira space key" (placeholder `ABC`), "Epic key" (placeholder `ABC-123`, hint "Leave empty and
   Specflow creates an Epic named after the project."). One primary button:
   - reads "Check access" until a check of the current values passed; a click sends the access check with the typed
     values; while it runs it reads "Checking…" and is disabled;
   - after an `ok` result it reads "Add destination"; a click sends `PUT` with the values and `enabled: true`; on
     success the form closes and the canvas reloads;
   - editing either field returns it to "Check access" and clears the result. An answer belongs to the keys it was
     asked with: a late answer never shows under keys typed after it.
3. **Check result** (under the form): ok shows "Jira space <name>" and either "Epic <key>: <summary>" or
   "Specflow creates an Epic named <project name>"; a failure shows the reason text under the field that `field` names
   (`jira_forbidden` and `jira_not_found` name either field), or as an alert line when `field` is null.

## The node's states

The node waits for the plan ("Waits for the plan"), syncs while a pass runs ("Syncing…", the wire moves only then,
and the target is read again every few seconds while it runs), then shows the status word
(`idle` "Not synced yet", `succeeded` "Synced", `partial` "Synced with errors", `failed` "Failed",
`skipped` "Skipped") and, on a failed pass, the error text of its `error_code`. It stops (badge, dashed wire) only for
what the API reports (D2): an Atlassian connection that expired or went, with a **Reconnect** to the Integrations page.

## Errors shown

| Cause | Where | Text (copy in `lib/copy.ts`) |
|---|---|---|
| `jira_auth_failed` (`token` mode) | alert line | "Jira refused the Specflow account. An administrator checks the Atlassian token." |
| `jira_auth_failed` (`oauth` mode, access check) | alert line | "Atlassian refused the connection. Reconnect Atlassian in Settings → Jira." |
| `jira_auth_failed` (`oauth` mode, status `error_code`) | node meta | "Atlassian refused the connection this target syncs as. Its owner reconnects Atlassian." |
| `jira_not_found` on `jira_project` | under the space field | "No Jira space with this key, or the account cannot see it." |
| `jira_issue_types_missing` | under the space field | "This Jira space has no Epic, Story or Sub-task issue type." |
| `jira_forbidden` | under the field `field` names (`jira_project`, or `epic_key` when the Epic itself is refused) | "The Specflow account may not create, edit, link or rank issues in this space." |
| `jira_not_found` on `epic_key` | under the Epic field | "No issue with this key." |
| `jira_epic_invalid` | under the Epic field | "This issue is not an Epic of the Jira space." |
| `jira_unreachable`, `jira_unexpected` | alert line | "Jira did not answer. Try again." |
| `jira_not_connected` (access check, 409 on `PUT`) | alert line | "Connect your Atlassian account first." |
| `jira_reconnect_required` (access check) | alert line | "Your Atlassian connection expired. Reconnect Atlassian." |
| `jira_not_connected` (status `error_code`) | node meta | "No Atlassian account to sync as. An owner connects Atlassian and saves the target." |
| `jira_reconnect_required` (status `error_code`) | node meta | "The Atlassian connection this target syncs as expired. Its owner reconnects Atlassian." |
| `validation_failed` | under the field of `details.errors[].loc[1]` | "Use a Jira key such as ABC or ABC-123." |
| `jira_unavailable` (503) | the gate's not available line | as above |
| `owner_required`, network, other | toast | "Could not save. Try again." |
| status `error_code` | node meta | the reason texts above; `no_plan` "No plan yet."; `plan_applying` "The plan was being updated."; `target_changed` "The Jira target changed during the sync."; `interrupted` "Stopped by a restart."; `unexpected_error`, `jira_invalid_field` and any code not listed "Something went wrong." |

## Ports

| Port | Members |
|---|---|
| `JiraPort` (`lib/domain/jira.ts`) | `get(projectId) -> JiraSettings`, `save(projectId, {jiraProject, epicKey, enabled}) -> JiraSettings`, `checkAccess(projectId, {jiraProject, epicKey}) -> JiraAccess` |
| Atlassian connection (`oauth` mode) | the Integrations page's Atlassian card reads the caller's connection, asks for the consent URL with a `return_to`, disconnects; `JiraSettings.connection` is `{connectedByName, connectedByMe, status} \| null` |

The http adapter (`lib/data/http/jira.ts`) checks every body's shape; `lib/api.ts` has the three calls. The API codes of
[api.md](api.md#errors) map to `DataErrorCode`s (`jira_unavailable` → `unavailable`); screens branch on
`detail.apiCode`. Access-check failures are answers (`result: "failed"`), not errors. `JiraSync.progress` is
`{step, done, total} | null`. The rules of the form, free of React, are `components/shared/settings/jira-logic.ts`
(the button phase, the texts of the reason codes, the status word).
