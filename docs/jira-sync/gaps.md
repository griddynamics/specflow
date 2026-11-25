---
type: Design
title: "Jira as a gap channel"
description: "How a project's open gap questions are posted to Jira as sub-tasks of one Story and how answers are read back from the comments by polling: target, grouping, posting, answers, the acting account, codes, limits"
status: draft
tags: [jira, gap, mission-control]
---
# gaps.md: the Jira gap channel

A project can post the open questions of its gap runs to a Jira project and read the answers back from the sub-tasks. It
mirrors the Slack channel ([../slack/gap-answering.md](../slack/gap-answering.md)) and reuses the plan sync's gateway
([sync.md](sync.md), `jira.gateway`); it changes neither the plan sync's tables nor its behaviour. Routes and shapes for the
canvas form: [../mission-control/integrations-api.md](../mission-control/integrations-api.md) "Jira gap channel".

## Target

One row per project in `jira_gap_targets` (migration `0009_jira_gaps`): the Jira project key, the **grouping**, the Story,
the poll cursor and the last poll. `enabled` turns posting and polling on.

| Grouping | One sub-task per |
|---|---|
| `one` | open question (the question is the title; the answer is the whole comment) |
| `area` | work area of the plan: the area of the first plan task behind the gap (`reach.tasks`); questions no task is behind go in `General`. The questions are numbered `1.`, `2.` ... |

`jira_gap_issues` has one row per gap posted: the sub-task (`issue_key`), its group (`area`; the gap id for `one`), the
number of the question in the sub-task, `posted_at` and `answered_at`. No Jira text is stored anywhere.

Changing the Jira project forgets the rows, the Story and the cursor (the open questions are posted again to the new
project); changing the grouping keeps what was posted, the new grouping applies to questions posted later.

## In the canvas

The plus of the Gaps stage lists **Jira sub-tasks** next to the Slack channel; it needs the project's Atlassian integration, else the card says
"Set up first" and returns with the form open. The form asks for the Jira space key and the grouping (a sub-task per question, or per work
area), says "answers are read about every N seconds" (N is `poll_seconds` of `GET`), and runs **Check access** (as the member Atlassian is
connected by, any member may press it); a changed key or grouping asks for a new check. **Add channel** saves the target turned on; the questions
are posted in the background. Any member can use it: a server without Atlassian OAuth or gap runs says so in one line. The node is named
`Jira · <space> gaps`, waits for the gaps, then counts `posted` and `answered` (the canvas reads the target every 5 seconds while it is on).
It stops only for what the API reports: `jira_not_connected` or `jira_reconnect_required` (or a lost Atlassian binding) offer Reconnect Atlassian;
any other closed code on the target says what it is.

## Acting account

The member the project's Atlassian is connected by (`project_integrations`, R1) posts and comments, with their own OAuth
connection ([oauth.md](oauth.md)); nothing new is requested: creating issues and comments uses the scopes of the plan sync,
reading uses `read:jira-work`. When that connection is expired or gone nothing is sent, the closed code
(`jira_reconnect_required`, `jira_not_connected`) stays on the target and the channel works again after the member connects.
The **access check** runs as that member (not the caller) and asks Jira for: authentication, a Story and a Sub-task type in
the project, and the permissions `BROWSE_PROJECTS`, `CREATE_ISSUES` (Jira Cloud has no separate permission for a sub-task;
it is created with Create issues) and `ADD_COMMENTS`; a missing one is `jira_forbidden` on the field `jira_project`.

## Posting

The gap runner calls the channel after every run (`add_notice`, as Slack does); saving the target turned on (or with
another Jira project) posts in the background at once. For every open question routed to people (`route=ask`, the first 50)
that has no row:

1. The **Story** `Open questions · <project name>` is created once (entity property `specflow` = project id, node
   `gaps:story`), its key is saved on the target at once and reused.
2. A **sub-task** under the Story per question (`one`) or per area (`area`), with the question, why it matters and the
   options as a lettered list (`A) label (recommended) - detail`) and how to answer. A question that comes later in an
   area that has a sub-task is added as a comment with the next number.
3. The rows are written after the Jira call. No transaction is open across a Jira call. A question is posted once: the
   row outlives its answer, so an answered question that reopens is not posted again. A crash between the create and the
   row can post that question twice after the restart (the same window as the sync's create).

A refused Jira call ends the pass, keeps its closed code on the target and leaves the questions for the next run.

## Answers (polling)

There is no webhook (`manage:jira-webhook` is not granted and a new scope means every member consents again) and no public
route. One in-process loop wakes every **60 seconds** (`POLL_SECONDS`; the form says "answers are read about every 60
seconds") and, for each enabled target with an open question, makes **one search**: the children of the Story changed since
the cursor (`parent = "KEY" AND updated >= -Nm`, a relative window, so no time zone is involved; the first poll has no
window). The cursor is the newest `updated` the search returned less 2 minutes. The comments of the sub-tasks that still ask
something are read (the pages a search does not bring).

For each comment, oldest first:

- By the **acting account**: never read (Graphico's confirmations; the person who connected Atlassian cannot answer from
  Jira).
- By a **project member with a connected Atlassian account** (matched by `atlassian_connections.account_id`, not by name or
  email): the text is read as in Slack, one option letter alone or the person's own words (`api.gap.replies`; an own answer
  is cut at 4000 characters). In an `area` sub-task each line `<n>: <answer>` (also `<n>) ...`, `<n>. ...`) answers question
  n; lines that follow it belong to it; text without a number is discussion and is ignored. The answer is recorded with
  `via="jira"` as that member, the gap is assigned to them when nobody holds it, and `apply_now` runs (one update build);
  then one **confirmation comment** mentions the person and lists what was recorded. A question another channel
  answered meanwhile, or one that is gone, is closed here and the comment says so.
- By **anyone else**: one comment mentioning them says the comment was not recorded and how to link (join the project and
  connect Atlassian under Integrations). It is posted once per person and sub-task.
- Own words on a **contradiction** are refused (it takes one letter), as in Slack.

Every effect is idempotent, so the cursor only bounds the search and a restart repeats nothing: an answered question has
`answered_at` (its comments are skipped), the hint is found again by its mention, and a refusal is not repeated once a
comment of the acting account follows the refused comment. The comment text is read only to find the answer: never logged,
stored (except in the gap answer itself) or quoted back.

## Shared with Slack

The reading of a reply and the sequence after it (record the answer, assign the gap, apply now) are one module,
`api.gap.replies`, used by `api.slack.threads` and the Jira poller; the Slack behaviour is unchanged.

## Codes

No new API error code. The routes use `jira_not_connected`, `atlassian_oauth_unavailable`, `gap_unavailable` and the access
result reasons of [api.md](api.md). Closed codes kept on the target (`last_error_code`): `jira_not_connected`,
`jira_reconnect_required`, `jira_forbidden`, `jira_not_found`, `jira_auth_failed`, `jira_issue_types_missing`,
`jira_unreachable`, `jira_unexpected`, `jira_unavailable` (no Atlassian OAuth). Log events (ids, codes and counts only):
`api.jira_gaps.saved`, `.access_checked`, `.story_created`, `.posted`, `.post_refused`, `.post_failed`, `.poll_refused`,
`.poll_failed`, `.answered`, `.answer_refused`, `.assign_failed`.

## Limits

- One target, one Story and one Jira project per Mission Control project; the single replica of the API is assumed (the
  poller and the posting lock are in process, as the Slack bindings and the sync runner).
- At most 50 questions are posted per run; the rest follow after the next gap run.
- Answers arrive up to a minute after the comment (and the search index of Jira may lag a few seconds more).
