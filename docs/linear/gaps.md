---
type: Design
title: "Linear as a gap channel"
description: "How a project's open gap questions are posted to Linear as sub-issues of one issue and how answers arrive by signed webhook, with a catch-up read: target, posting, webhook, answers, codes"
status: draft
tags: [linear, gap, webhook, mission-control]
---
# Linear as a gap channel

A project's open gap questions are posted to Linear as sub-issues of one issue, and the answers come back as comments,
delivered by a signed webhook. Code: `backend/src/api/linear_gaps/`, `backend/src/db/linear_gaps.py`,
`backend/src/api/routers/linear_gaps.py`, `backend/src/api/routers/linear_webhook.py`. It copies the Jira channel
([../jira-sync/gaps.md](../jira-sync/gaps.md)); the differences are the webhook (no polling) and the Markdown text.

## Who acts

Every call acts as the member the project's Linear is connected by (`project_integrations`, app `linear`; OAuth
`actor=user`). Without that member (not connected, expired) nothing is sent and the closed code
(`linear_not_connected`, `linear_reconnect_required`) is kept on the target (`linear_gap_targets.last_error_code`).
The gateway is `async with runtime.linear.gateway(user_id) as gateway` (the member connections), a
`linear.gateway.LinearGateway`; the context refreshes the token once on a 401 and closes the client.

## Target

`GET/PUT /projects/{pid}/linear-gaps`, `POST /projects/{pid}/linear-gaps/access-check`; any member (D6).

| Field | Meaning |
|---|---|
| `team_key` | The key of the Linear team (`ENG`), required; resolved to the team id at save |
| `linear_project` | Optional: a project of the team by name or link; blank = the team only |
| `grouping` | `one` (a sub-issue per question) or `area` (a sub-issue per work area, questions numbered) |
| `enabled` | Posting on or off. Turning it off needs no call to Linear |

`PUT` answers `404 linear_team_not_found` / `404 linear_project_not_found` for a team or project the member cannot see,
`409 linear_not_connected` / `linear_reconnect_required`, `503 linear_unavailable` (no Linear OAuth app) and
`503 gap_unavailable` (gap runs off). A changed team or project drops the rows and the parent issue, and posts afresh.
The access check answers `{result: "ok", team_name, linear_project_name}` or `{result: "failed", reason, field}` with
`field` `team_key` or `linear_project`; a failed check is an answer, not an error.

## Posting

After every gap run (`GapRunner.add_notice`) and when the target is saved (turned on or its destination changed), the
questions with `route=ask` that have no row in `linear_gap_issues` are posted, at most 50 per pass:

- a parent issue "Open questions · <project name>" (client id `UUIDv5(project, parent, "gaps")`), created once; its
  id and identifier are kept on the target;
- grouping `one`: a sub-issue per gap (client id of the gap); grouping `area`: a sub-issue per work area of the plan
  that the tasks behind the gap sit in ("General" when none), with numbered questions; a question that comes later in a
  known area is a comment with the next number;
- label `graphico-gap` (the team's own when it exists, else created), the issue's priority from the severity;
- the description is the question, why it matters and the options as a lettered list (`A)`, `B)`, ...), then "Reply in
  a comment with a letter or your own answer." (area: "one line per question: the number, a colon, then a letter or your
  own answer").

Severity to Linear priority (agent choice AC-S5-1): `blocker` 1 Urgent, `major` 2 High, `minor` 4 Low; an `area` issue
takes the most severe of its questions. Medium (3) is left to the people who triage in Linear.

A gap is posted once: its row outlives its answer, every create has a client id, so a crash between a create and its row
finds the issue again instead of posting twice.

## Webhook

`POST /api/v1/integrations/linear/webhook` is a **public** route (no sign-in; `archkit.toml` `public_operations`).

1. The RAW body is read. `Linear-Signature` must be the hex HMAC-SHA256 of it with `LINEAR_WEBHOOK_SECRET`
   (constant-time compare); the JSON's `webhookTimestamp` (ms) must be within 60 s of now. Otherwise
   `401 linear_webhook_signature_invalid`. Secret unset: `503 linear_webhook_unavailable`.
2. The route answers `200 {"ok": true}` at once (Linear times out at 5 s) and hands the event to a background task.
3. `Comment` / `create`: a delivery seen before (`Linear-Delivery`, kept in memory) does nothing; the comment goes to the
   one answer path below. Any other `Comment` action or type is acknowledged and ignored.
4. `OAuthApp` / `revoked`: the `linear_connections` rows of that workspace become `revoked`.

## Answers (one path for the webhook and the catch-up)

A comment on an issue the channel posted (found by issue id in `linear_gap_issues`, across projects) answers when its
author (`data.userId` in `organizationId`) is a project member with a not-revoked row in `linear_connections`:

- the channel's own comments (they start with `Graphico:`) and comments of an integration (no user) are skipped, so is
  an issue nobody posted, a disabled channel and a question already answered (its row);
- the text goes through `api.gap.replies.parse_reply` (one letter alone is an option, anything else is an own answer;
  grouping `area` reads numbered lines) and `record_and_apply(..., via="linear")`; a contradiction takes one letter only;
- a confirmation comment lists what was recorded or refused; the sub-issue moves to the team's first `completed` state
  (grouping `one` only). The confirmation and the move are best effort: the answer stays recorded;
- a person who is no member gets one hint comment ("connect your Linear account under Integrations"), once per person and
  issue: the comment's client id is made of both, so a replay or a restart posts nothing more. The same holds for a
  confirmation (made of the triggering comment).

## Catch-up (AC4)

When the target is saved (after the post) and when the API starts, the comments of every issue that still asks
something are read through the gateway (from a minute before its first question was posted) and processed exactly like a
delivery, so a delivery Linear gave up on (it retries 1 min, 1 h, 6 h) is not lost. Every effect is idempotent.

## Setup

One OAuth app per environment. Its webhook URL is the public address of the api plus
`/api/v1/integrations/linear/webhook` (not the web app's `PUBLIC_URL`; on a local stack, a tunnel to the api port 8000).
Tick the webhook events **Comments** and **OAuth app authorization events**; Issues is not needed. The signing secret is
`LINEAR_WEBHOOK_SECRET`. Registration steps: [oauth.md](oauth.md#register-the-linear-oauth-app) and
[../configuration.md](../configuration.md).
