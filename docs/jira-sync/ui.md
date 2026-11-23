---
type: Design
title: "Jira tab of project Settings"
description: "The Jira tab of the per-project Settings screen: route, header tab, owner and collaborator views, the access check then Save flow, the Sync switch, the status line, errors shown, the Plan line and the frontend ports"
status: stable
tags: [jira, frontend]
---
# ui.md: Settings tab "Jira": route, views, check-then-save flow, switch, status line, errors, ports

The web app only configures the sync; there is no "Sync now" button and no preview (D24). The design system rules of
[../design-system/USAGE.md](../design-system/USAGE.md) apply; the screen header rule of the frontend applies (no H1, no
tab row in the body). No new design-system component: the Switch is the system's existing component, given a React
wrapper in the app's UI folder.

## Route and header

| Item | Value |
|---|---|
| Route | `/projects/{projectId}/settings?tab=jira`; a missing or unknown tab still opens General |
| Route key | `SettingsTab` gains `jira`; `SETTINGS_TABS` lists `general`, `jira` |
| Header | the Settings `SegmentedControl` in the shell header gets a second option, "Jira", after "General" |
| Body | `data-tab` follows the tab; the Jira tab renders only while selected |

## Jira tab

Layout: one column, like General. Top to bottom:

1. **Not available notice** (only when `available` is false): text "Jira is not connected to this Specflow server. An
   administrator connects an Atlassian account." Everything below is disabled.
2. **Target form**: `TextField` "Jira space key" (placeholder `ABC`), `TextField` "Epic key" (placeholder `ABC-123`,
   hint "Leave empty and Specflow creates an Epic named after the project."). One primary `Button`:
   - reads "Check access" until a check of the current values passed; a click sends the access check with the typed
     values; while it runs it reads "Checking…" and is disabled;
   - after an `ok` result it reads "Save"; a click sends `PUT` with the values and the current `enabled`; while it runs
     it reads "Saving" and is disabled; a success toast "Jira target saved" confirms the save and the button returns to
     "Check access";
   - it is also disabled while the typed keys equal the saved target and no result waits, so a saved view shows no
     primary action; the first edit of a field enables it again as "Check access";
   - editing either field returns it to "Check access" and clears the result.
3. **Check result** (under the form): ok shows "Jira space <name>" and either "Epic <key>: <summary>" or
   "Specflow creates an Epic named <project name>"; a failure shows the reason text under the field that `field` names
   (`jira_forbidden` and `jira_not_found` name either field), or as an alert line when `field` is null.
4. **Switch** "Sync to Jira" (DS `Switch`, label states the setting): disabled until the target is saved; a change sends
   `PUT` with the saved values and the new `enabled` at once (the switch applies immediately, as the DS requires);
   turning it on starts a sync on the server. While the call runs the switch is marked busy and ignores clicks; the
   answer always replaces the shown settings. Hint under it: "After every plan run, Specflow updates the Epic, its
   Stories and Sub-tasks."
5. **Created Epic** (when `created_epic_key` is set and no `epic_key`): "Epic created by Specflow: <key>" as a link to
   `<site_url>/browse/<key>`.
6. **Status line** (only while `sync` is not null; a target-less project has none): a `Tag` with the status word, the
   finish time and, when there is one, the error text. Words:
   `idle` "Never synced", `running` "Syncing", `succeeded` "Synced", `partial` "Synced with errors", `failed` "Failed",
   `skipped` "Skipped". The tab reloads the settings every 5 seconds while the status is `running`.
   While `running` with `sync.progress` set, the line also shows the step ("Creating and updating issues", "Adding
   links", "Ordering issues"), the count "done of total" (for example "23 of 60") and a progress bar
   (`role="progressbar"`, DS `gdg-progress`). `done` is clamped to `total`; the total can grow once during the first
   step. A total of 0 shows the step alone. `progress` null, or missing from an older server, shows only the word.

Owner versus collaborator: `editable` from `GET` decides. A collaborator sees the same blocks with every control
disabled and no primary button.

Loading shows a `Skeleton` block; a failed load shows the General tab's "Try again" alert; a `project_not_found` sends the
user home as the General tab does.

## Errors shown

| Cause | Where | Text (copy in `lib/copy.ts`) |
|---|---|---|
| `jira_auth_failed` | alert line | "Jira refused the Specflow account. An administrator checks the Atlassian token." |
| `jira_not_found` on `jira_project` | under the space field | "No Jira space with this key, or the account cannot see it." |
| `jira_issue_types_missing` | under the space field | "This Jira space has no Epic, Story or Sub-task issue type." |
| `jira_forbidden` | under the field `field` names (`jira_project`, or `epic_key` when the Epic itself is refused) | "The Specflow account may not create, edit, link or rank issues in this space." |
| `jira_not_found` on `epic_key` | under the Epic field | "No issue with this key." |
| `jira_epic_invalid` | under the Epic field | "This issue is not an Epic of the Jira space." |
| `jira_unreachable`, `jira_unexpected` | alert line | "Jira did not answer. Try again." |
| `validation_failed` | under the field of `details.errors[].loc[1]` | "Use a Jira key such as ABC or ABC-123." |
| `jira_unavailable` (503) | the not available notice | as above |
| `owner_required`, network, other | toast | "Could not save. Try again." (existing) |
| status `error_code` | status line | the reason texts above (`jira_not_found` of a run reads as the space text); `no_plan` "No plan yet."; `plan_applying` "The plan was being updated."; `jira_not_configured` "Sync is off."; `interrupted` "Stopped by a restart."; `target_changed` "The Jira target changed during the sync."; `unexpected_error`, `jira_invalid_field` and any code not listed "Something went wrong." |

## Plan line

The ready Plan (all its views) shows one compact Jira sync line above its content while the project's Jira settings are
`available` and `enabled`, a target is saved, `sync` is not null and its status is not `idle` or `skipped`. It reads
`JiraPort.get` on mount and every 5 seconds while the last answer was available and enabled, stops on unmount and never
blocks the plan. A failed read (503 `jira_unavailable` too) hides the line, with no toast, until a read works again. Any
member sees it.

| Status | Line |
|---|---|
| `running`, `progress` set | step, "done of total" and a progress bar, as on the status line |
| `running`, `progress` null | "Jira sync queued" |
| `succeeded` | "Jira is up to date" and, with an https `site_url` and an Epic key (`epic_key` or `created_epic_key`), a link "Epic <key>" to `<site_url>/browse/<key>` |
| `partial` | "Jira sync finished with errors." and a link "Open Jira settings" to `/projects/{projectId}/settings?tab=jira` |
| `failed` | "Jira sync failed." and the same link |

## Ports

| Port | Members |
|---|---|
| `JiraPort` (new, `lib/domain/jira.ts`) | `get(projectId) -> JiraSettings`, `save(projectId, {jiraProject, epicKey, enabled}) -> JiraSettings`, `checkAccess(projectId, {jiraProject, epicKey}) -> JiraAccess` |

The http adapter (`lib/data/http/jira.ts`) checks every body's shape; `lib/api.ts` gets three calls. The API codes of
[api.md](api.md#errors) map to `DataErrorCode`s (`jira_unavailable` → `unavailable`); screens branch on
`detail.apiCode`. Access-check failures are answers (`result: "failed"`), not errors. `JiraSync.progress` is
`{step, done, total} | null`.
