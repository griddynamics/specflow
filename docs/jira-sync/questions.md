---
type: Questions
title: "Jira sync decisions"
description: "Decisions of the Jira sync with who made them, the design choices with the options that were rejected, and follow-ups"
status: stable
tags: [jira]
---
# questions.md: decisions of the Jira sync, design choices, follow-ups

"Owner" is the product owner. "Agent (approved with the plan)" was proposed during planning and approved with it; "agent"
is a later planning decision that the owner reviews in the pull request.

## Decisions

| # | Decision | By |
|---|---|---|
| D1 | One Atlassian account for the deployment: `ATLASSIAN_SITE_URL`, `ATLASSIAN_EMAIL`, `ATLASSIAN_API_TOKEN` from the environment (a secret store in a cluster). No token in the database. Refined: per-user OAuth connections (`oauth` mode, [oauth.md](oauth.md)) are added next to it; the one account stays as the `token` mode (D41), and in `oauth` mode the database holds encrypted refresh tokens (D43). | owner |
| D2 | A Specflow project is one Epic; a story is a Story under it; a task is a Sub-task under its Story. | owner |
| D3 | A work area is the label `area-<slug>` (owner). Every synced issue also has the label `specflow`, which the crash recovery search needs (agent, approved with the plan). | owner; agent (approved with the plan) |
| D4 | Owned fields: summary, description, story points, labels, `Blocks` links, rank, a remote link back to Specflow ([sync.md](sync.md#owned-fields)). | agent (approved with the plan) |
| D5 | An issue in status category To Do is overwritten in the owned fields; any other status gets one comment listing the changes. Status, assignee and existing comments are never touched. | owner |
| D6 | A plan item that disappears is never deleted in Jira. | agent (approved with the plan) |
| D7 | Specflow keeps a map of node to key with the content it applied; unchanged content means no call. | agent (approved with the plan) |
| D8 | One sync per project, merged starts, `Retry-After` honoured. | agent (approved with the plan) |
| D9 | No allow-list of Jira projects for the MVP; only project owners change the Jira settings. | owner |
| D10 | Out of scope: Jira to Specflow, transitions on claim or completion, sprints, fix versions, Jira Plans dates. | agent (approved with the plan) |
| D11 | Jira REST through an async HTTP client behind a gateway; no command-line tool in the service. | agent (approved with the plan) |
| D12 | The sync runs in the API process like the plan and gap runs, not as a workflow on a worker (refines D8). | agent |
| D13 | A removed item in To Do gets the label `specflow-removed` and a comment; in any other status only a comment. A redone task whose issue is past To Do gets only a comment (refines D6). | agent |
| D14 | Any member may start a sync and read its status (web read and MCP); settings and the access check were owner-only (settings and check: superseded by D56). | agent |
| D15 | A disabled target blocks every sync, manual ones too (`409 jira_not_configured`). | agent |
| D16 | The real gateway is used when the three `ATLASSIAN_*` are set; otherwise the Jira routes answer `503 jira_unavailable`. The fake gateway lives in the tests only. | agent |
| D17 | Map keys: base task id (a redo stays the same Sub-task), story id, `EPIC` for a created Epic. | agent |
| D18 | Coalescing: a flag per project makes one more pass after the running one; one pass at a time per process. | agent |
| D19 | The map stores the canonical content (not only a hash), so the comment can list what changed since the last sync; a mapping version is part of it. | agent |
| D20 | Rank: stored positions, only items off the longest increasing subsequence move, only To Do issues move. | agent |
| D21 | A blank Epic key makes the first pass create an Epic named after the project. A new Jira project key drops the map; old issues stay. | agent |
| D22 | The Jira tab: fields with one primary button "Check access" that becomes "Save"; a Switch that applies at once. | agent |
| D23 | A pending map row before each create; recovery by a search on the issue property; item errors end the pass `partial`. | agent |
| D24 | No web route or button starts a sync; the web app only configures. | agent |
| D25 | "To Do" is status category `new` (Backlog statuses included). | agent |
| D26 | `jira_unavailable` (503) means the deployment has no Atlassian account; an unreachable Jira is the sync code `jira_unreachable`. | agent |
| D27 | `sync_to_jira` declares write annotations with `openWorldHint: true` (it writes to Jira); `jira_sync_status` is a read tool. | agent |
| D28 | Story points use the field named `Story Points`; a site without it gets no points and no error. | agent |
| D29 | The access check also checks the issue types and the account's permissions in the project. | agent |
| D30 | Every plan run that does not fail triggers a sync, an unchanged one too (a pass with no change costs no Jira call). | agent |
| D31 | A change of the Jira project or Epic during a pass stops that pass before its next write (`target_changed`); the settings save never waits, and one more pass follows. | agent |
| D32 | An issue deleted in Jira is marked removed and never called again; a deleted created Epic stops the pass. | agent |
| D33 | A task whose story is no longer an active story is handled like a removed task (its Sub-task is labelled or commented, never deleted). | agent |
| D34 | `jira_sync_status` pages its items like every list tool; `sync_to_jira` and the Settings screen get the status without items. | agent |
| D35 | A create, a comment and a link add are never sent again after a timeout or a 5xx (Jira may have done it): the call ends the run with `jira_unreachable` and the next pass recovers it; a 429 is still retried (refines the failure rule of D23). | agent |
| D36 | A start on a project with no target or a disabled one queues nothing; the runner answers `skipped` with `jira_not_configured` and the agent route maps it to 409 (refines D15). | agent |
| D37 | A `Blocks` link a person made before Specflow wanted the same wait is stored in `blocked_by` like a Specflow link and is deleted when the plan drops the wait; links never in `blocked_by` are never deleted (refines D5). | agent |
| D38 | The Jira tab confirms a save with the toast "Jira target saved", and its primary button stays disabled while the typed keys equal the saved target (refines D22). | agent |
| D39 | An Epic read that Jira refuses with 403 during the access check is `jira_forbidden` with `field` `epic_key` (refines D29). | agent |
| D40 | The acting account of a target was the owner who last saved it (superseded by D55). | agent (approved with the plan) |
| D41 | The one-account `token` mode stays as a fallback; `oauth` wins when both are configured (refines D1, D16). | agent (approved with the plan) |
| D42 | The OAuth `state` is HMAC-signed, with no table; it expires after 10 minutes and is bound to the user who asked. | agent (approved with the plan) |
| D43 | One Fernet key, `ATLASSIAN_TOKEN_KEY`, encrypts the refresh tokens and derives the `state` signing key. | agent (approved with the plan) |
| D44 | No keep-alive refresh; `invalid_grant` marks the connection `expired`, never refreshed again until the user reconnects. | agent (approved with the plan) |
| D45 | Confluence as a source stays out of this change; the connection already holds the Confluence scopes. | agent (approved with the plan) |
| D46 | Every settings `PUT` set `connected_user_id` to the saving owner (superseded by D55). | agent |
| D47 | Only `ATLASSIAN_OAUTH_CLIENT_ID` turns `oauth` on; the token variables are then not read (a partial token set is not refused), and a secret or key without the client ID is ignored. `api.jira.mode` is logged at every start, in both modes. | agent |
| D48 | A refresh failure other than `invalid_grant` keeps the connection `active`: `429`, `5xx` and network errors are `jira_unreachable`, any other refusal (a wrong client secret) `jira_auth_failed`. A refresh token that no longer decrypts (a rotated `ATLASSIAN_TOKEN_KEY`) counts as `invalid_grant` (refines D44). | agent |
| D49 | Callback errors: a refused `accessible-resources` call is `atlassian_code_invalid`, a refused `/myself` is `atlassian_site_not_granted`. The callback body stays `{code, state}`: Atlassian's `?error=` never reaches the API; the web app maps it to `atlassian_consent_denied`. | agent |
| D50 | `GET /integrations/atlassian` answers in every mode (`connected` false in `token` mode and off). `JiraSettingsOut.connection` is null outside `oauth` mode and when the project has no target; `connected_by_name` is the Atlassian display name of the acting connection, null when `missing`. | agent |
| D51 | A settings `PUT` with the owner's own connection `expired` was 409 `jira_not_connected` too (superseded by D55). | agent |
| D52 | `return_to` also refuses a backslash, whitespace, control and non-ASCII characters, and is at most 2048 characters. | agent |
| D53 | Access tokens are cached per api process; several replicas refresh on their own and the row's `FOR UPDATE` serializes the rotation. A gateway per user and cloud lives until shutdown, so a running pass keeps its client; a disconnect drops the token and the next call fails `jira_not_connected`, in the process that served it only: another replica keeps its cached access token until it expires (about an hour). | agent |
| D54 | `cryptography` is declared in the api dependency extra (it was only transitive). | agent |
| D55 | The acting account of a target is the member the project's Atlassian connection is bound to (`project_integrations`), whoever saves the target; `token` mode stores null (Mission Control V2 D1, D3). | owner (V2 decisions) |
| D56 | Any member saves the settings and runs the access check; the members, rename and invite routes stay owner-only (Mission Control V2 D6). | owner (V2 decisions) |

## Design choices

| Choice | Taken | Rejected, and why |
|---|---|---|
| Where the sync runs (D12) | in-process runner in the API | a workflow on a worker: the worker has no graph store, a plan snapshot is limited by payload size, and workflows have no offline tests |
| Coalescing (D18) | a dirty flag per project | dropping a start while one runs: a plan change made during a pass would never reach Jira |
| Identity of a redone task (D17) | base task id | the revision id: every redo would mark a Sub-task removed and create a new one |
| Redo of an issue past To Do (D13) | a comment | a new Sub-task: extra state and noise; the owner may still ask for it |
| Change detection (D19) | stored content and hash | hash only: the D5 comment could not say what changed |
| Rank (D20) | stored positions and minimal moves | re-rank all every pass: breaks the zero-call re-run; reading Jira's rank: fights people every pass |
| Gateway selection (D16) | real when the account is configured | the Drive style (`APP_ENV=local` means fake): local QA could not reach Jira without a new switch |
| Crash safety (D23) | pending row and property search, adopting only issues whose property names the same Specflow project | search by summary before every create: a call per create and fuzzy |
| Target change during a pass (D31) | the pass checks the target under a row lock before each write and stops | the save waits for the pass: a pass can take minutes (rate limits), longer than a web request should; refuse the save while a pass runs: the owner retries by hand |
| Issue deleted in Jira (D32) | mark removed, no more calls | create it again: undoes a person's decision; keep failing: the status shows errors on every later pass |
| Whose Atlassian account a sync uses (D40) | the owner who last saved the target | the user who started the pass: plan runs and recovery have no user, and a member could act beyond the owner's choice; a shared technical account: the reason for OAuth |
| `state` storage (D42) | signed, no table | a table of pending states: one more table and a cleanup for a 10-minute value |
| Expired grant (D44) | `expired` until a reconnect | retry the refresh on every pass: a dead grant never comes back, and each retry is a token call that fails |

## Follow-ups

- An allow-list of Jira projects per deployment.
- A new Sub-task instead of a comment when a task past To Do is redone, if teams ask for it.
- Slack commands reuse the agent routes.
- Confluence as a source reuses the Atlassian connection, which already holds the Confluence scopes (D45).
