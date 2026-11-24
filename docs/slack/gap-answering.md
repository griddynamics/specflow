---
type: Design
title: "Gap answering in Slack"
description: "Research, alternatives, scoring, review findings and the plan to answer gap questions in Slack instead of the web Gaps screen"
status: draft
tags: [gap, slack, mcp]
---
# gap-answering.md: research, alternatives, scoring and the plan to answer gaps in Slack

## Summary

Gap questions are short, they need little thought, and the answer feeds the graph before the plan. Today a person
must open the web Gaps screen, or ask a coding agent for `answer_gap`. Both are pull: nobody is told that a question
waits. This design moves answering to where the team already talks: Slack.

**Recommendation:** an internal SpecFlow Slack app, run in Socket Mode by the API process, with one flow:

1. **Tell.** After a gap run, the app posts one **summary** to the project's linked private channel. The summary holds
   counts and @mentions only, no client text: "Build 41: 7 new questions (2 blockers). @ana 3, @piotr 2, unassigned 2".
   Each assignee of a blocker or major gap also gets a DM. The summary has three buttons:
   - [Answer my 3]
   - [Answer unassigned]
   - [Open in SpecFlow]
2. **Answer in private.** A button opens a **modal** that only the person who clicked sees. It shows 5 gaps per page.
   Each gap has its question, why, one quote and the options as radio buttons with their edit text, plus "Other" with
   a text field. One **Submit** records all the answers on that page, each through `GapService.answer_gap` with
   `via = "slack"`.
3. **Resolve in one go.** The last page has a box "Apply now", ticked by default. With it ticked, Submit also calls
   `apply_now`: one update build applies the answers. The modal first lists what else this build applies (answers
   staged by others, AI answers) so nobody applies something they did not see. Staging stays as the model; Slack only
   makes "answer and apply" one action.
4. **Show who.** The summary edits itself: "5 of 7 answered (@ana 3, @piotr 2) · applying", then "Resolved in build
   42". Every answer records the linked SpecFlow user, as on the web.

Identity comes from a one-time **account link** made under the person's Keycloak session, not from email matching.
Client text never goes into channel history: it shows only in the modal and in DMs to linked members.

Later phases add:

- an App Home inbox;
- own answers with an edits preview, so contradictions can be answered in Slack too;
- capped reminders;
- opt-in auto-apply, which needs its own design;
- the MCP server in Slackbot, for pull questions such as "which gaps do I own?";
- an email or Teams route for client stakeholders.

Two independent reviews of the first draft changed the design. That draft had per-gap cards in a channel thread,
removed staging for every surface and matched users by email. "Review findings" lists what changed and why.

## The channel flow

The owner changed the direction on 2026-11-23, after trying the private form in a test workspace: a project is bound
to one Slack channel once, every question shows up there by itself, and anyone on the team answers in its thread. Nobody
types a command to see the questions. This replaces the summary, the DMs and the paged form as the main flow; the form
stays behind `/specflow answer <project>`.

| Step | What happens | Code |
|---|---|---|
| Invite | Someone runs `/invite @SpecFlow` in the team's channel | Slack |
| Connect | A member of the project (owner or collaborator, D6 of Mission Control V2) runs `/specflow connect <project id>` in that channel; SpecFlow's Settings, Slack tab, shows the command ready to copy. Typing in the channel proves the caller is in it, and the linked SpecFlow user must be a member of the project (a project that does not exist, or one the caller is not in, gets the same answer). The caller is recorded as the project's Slack integration (`project_integrations`, app `slack`, with the workspace of the bot token; a project bound to another workspace is refused until that connection is removed); the rows of bindings made before the table existed are filled at the api's start. A caller not linked yet gets the sign-in link and the channel connects once they have signed in. Refused: a DM, a channel the app is not in (`/invite @SpecFlow` first), one shared outside the workspace, and one connected to another project (its name is not shown). A channel belongs to one project and a project has one channel; connecting elsewhere moves it. `/specflow disconnect` in the channel, or Disconnect in Settings, undoes it (any member). The Settings tab never lists other channels | `SlackGaps._connect`, `SlackThreads.connect`, `SlackChannels.bind`, `frontend/components/settings/SlackTab.tsx` |
| Post | Every open question of the project (route `ask`) is posted once, as **one line** so a channel of 100 questions stays readable: a number per channel (`#12`), a severity mark, the severity and the title. Its thread starts with the details: kind, question, why, one quote, the options A-D with their edit text and how to answer; no web link, as SpecFlow is meant to run headless here. After each gap run the new ones follow with the next numbers | `SlackThreads.post_open`, `blocks.gap_headline`, `blocks.gap_details` |
| Answer | Anyone in the channel replies in the question's thread: one letter (`B`, `b.`) picks that option; any other text is their own answer (a contradiction takes a letter only, as the service requires edits for it) | `SlackThreads.on_message`, `blocks.reply_answer` |
| Who | The replier's Slack account must be linked to their SpecFlow user once (`/specflow link`). An unlinked replier gets the link privately in the thread; their reply waits and is recorded when they link. A replier who is not a member of the project is told so and nothing is recorded. A gap nobody was assigned to becomes the answerer's (event `assigned`), so the web shows it as theirs; an assigned gap keeps its assignee | `SlackLinks`, `SlackThreads.replay` |
| Apply | Each recorded reply is applied at once: `apply_now`, one update build (a build that runs takes the next answers in one queued request). The thread gets "Recorded B from @ana: applying", and the question's line is only **struck through** behind a check mark (`~#12 · MAJOR · Order limit~`), so the open ones stand out | `SlackThreads._apply`, `blocks.gap_headline` |
| Plan | When the questions are answered, anyone linked runs `/specflow plan` in the channel (or `/specflow plan <project>` anywhere; `/specflow replan` recreates the plan). It starts the plan run exactly as the Plan screen's button does, as that user (`start_plan_run`, shared with `POST /projects/{pid}/plan`), and posts in the channel who started it and how many questions are still open | `SlackGaps._plan`, `api/plan/start.py` |

Decisions changed with it:

- **O1:** each answer in a thread is applied at once. `apply_now` also applies everything else staged, AI answers
  included; the owner accepted that for the pilot.
- **O2:** the question text (title, question, quotes, options) is posted to the connected channel, so it stays in that
  channel's history. Use internal projects, or a channel limited to the project team; client projects need legal or
  DPA approval first.
- **O3 (2026-11-24):** the channel is connected from inside Slack, not picked from a list in SpecFlow. A list showed
  every channel the app was in, private ones included, to every project, and let any owner post into a channel they
  were not in. Channel members who are not project members do see the questions: connecting is the member's deliberate
  choice.

Scopes: reading the replies needs `channels:history` and `groups:history` and the events `message.channels` and
`message.groups` ([manifest](../../config/slack/manifest.yaml)); the app only acts on replies in the threads of its own
gap messages. In the GD review these history scopes need a clear reason; this is it.

Spike storage: the bindings and the posted messages are a JSON file in `GRAPH_STATE_DIR` (`slack-channels.json`), as
the account links are (`slack-links.json`); phase 1b moves both to tables with a migration.

Not yet: editing a question's message when it is answered or resolved elsewhere (the web, MCP, a build), a summary per
gap run, and reminders.

## Today: how a gap is asked, answered and applied

| Step | What exists | Source |
|---|---|---|
| Detect, investigate, route | A gap run follows each build. One investigator per site gives a checked report with 2-4 options, one Recommended. Code routes: `blocker`/`major` to a person, `minor` to AI when all gates hold, `info` to a note | [../graph/gap/overview.md](../graph/gap/overview.md), [answers.md "Routing"](../graph/gap/answers.md) |
| The card | `GapCard` (question, why, quotes, options with label and detail, `updated_at`, `web_url`, `assignee`) for MCP; `GapDetail` adds the options' edits, the answer, history, follow-ups, reach and `ABOUT`/`IMPACTS`. The web card shows each option's edits preview | `backend/src/agent_contracts/gap_models.py`, `backend/src/api/gap/cards.py` |
| Answer | `GapService.answer_gap(AnswerInput)`: one of `option_rank` and `own_text` (1-4000), optional `edits` and `placement`, `answered_by`, `updated_at` (stale check), `via` (a closed `Literal`: `web`, `elicitation`) | `backend/src/graph/gap/models.py`, [answers.md "AnswerInput"](../graph/gap/answers.md) |
| Own text | The web runs `preview_edits` (LLM role `gap_edits`) and shows a diff. Without edits, the service makes a clarifications-document edit. The spec says a contradiction always amends, but the service does not refuse own text without edits for one; MCP `answer_gap` already sends such answers | `graph/gap/service.py` `_answer_edits`, `api/routers/agent_gaps.py` |
| Staging | An answer is staged (gap `answered`, clarification `pending`). "Resolve gaps" (`apply_now`) lists the staged answers with who staged them, then applies **every** pending clarification of the project, AI answers included, in **one** update build. An immediate apply of a blocking development gap also applies everything staged | FR-GANS-08, `GapService.apply_now` / `_apply` |
| Runtime | Gap runs, builds and `GapService` run inside the API process as asyncio tasks ("no Temporal yet"). Gap locks are in-process per project. The chart allows one API replica with strategy `Recreate` | `api/gap/runner.py`, `charts/specflow/templates/validate.yaml` |
| Events | Every gap event (also close-check and run events) goes through the host port `PostgresGapRecords.log`, outside the graph transaction | `api/gap/records.py` |
| Identity | Web and MCP resolve the user by `auth_subject` from a verified token. `users.email` is not unique, and changes only at sign-in | `api/deps.py`, `db/repositories.py`, `db/models.py` |
| Access rules | AC12: people answer on the web card or in the `answer_gap` dialog, and dismiss and resolve on the web. D4: the model never sends an answer, and dismissing stays in the web app | [../mcp-server/access-control.md](../mcp-server/access-control.md), [../mcp-server/apps.md](../mcp-server/apps.md) |
| Deferred | "Notifications (mail or chat) for new blocker gaps" is deferred host work | [../graph/gap/later.md](../graph/gap/later.md) |
| Connections | The design-system `ConnectorCard` preview already shows "Slack: post decision digests to project channels" (status off) | `docs/design-system/components/ConnectorCard` |

What this means for Slack:

- The answer path exists. MCP shows the pattern: a membership check, then `api.gap.answering.record_answer` with its
  own `via`. Slack needs a new `via` value, an identity link and a renderer; it adds no gap logic.
- The renderer draws from `GapDetail`, not `GapCard`, because the options' edits must be visible: "A card never shows
  an option without edits".
- `apply_now` is project-wide and takes AI answers too. Any "answer and apply" in Slack must show what else it applies.

## What Slack can do (research, checked 2026-11-23)

| Surface | Can do | Cannot do / limits | Fit |
|---|---|---|---|
| Message + buttons | `button` (with a `confirm` dialog), `radio_buttons`, `static_select`, `overflow`. `input` blocks, also `plain_text_input`, are allowed in messages, and one Submit reads all inputs from `state.values` | 50 blocks per message; option text 75 characters; at most 10 radio options; selections are private until `chat.update`; everything posted stays in channel history | High for notify, low for client text |
| Modal (`views.open`) | Opened from a button (`trigger_id`, valid 3 s, once). Radios, checkboxes, multiline text. Validation errors on submit. `push`/`update` up to 3 views. `private_metadata` up to 3000 characters | 100 blocks; acknowledge every interaction in 3 s; LLM work must run async (`views.update` later) | High |
| Attribution | Every `block_actions` and `view_submission` carries the signed `user.id` and `team_id` | Mapping that id to our users is our job | High |
| Message update | `chat.update` on the stored channel and `ts` at any time | Not for ephemeral messages | High |
| Thread replies | Events API `message.*` with `thread_ts`; `reactions.add` | Free text: needs a parser or an LLM; edits, deletes, many repliers; needs `*:history` scopes | Medium-low |
| App Home | `views.publish` per user: "your open gaps" with buttons that open the same modal | 100 blocks; not a notification by itself | High (inbox) |
| Work Objects | A gap as an entity (`task`) with editable select fields in a flexpane | New API; Bolt for Python support "coming soon"; we enforce permissions ourselves | Medium |
| Slackbot as MCP client (GA 2026-06-18) | Our MCP server in Slackbot (`mcp:connect`); Slack identity or OAuth auth; results as Block Kit or MCP Apps | Pull only; the user approves every tool call; 60 s timeout; 5 servers per user; elicitation support unknown | Medium |
| Official Slack MCP server | An outside agent reads and posts messages, canvases and lists | Text or markdown only (Block Kit not documented); our app must still take the answer | Low |
| Workflow Builder form | Webhook trigger, then a "collect info" form, then a custom step of our app | Paid plans; the webhook trigger is one-way; many parts | Low-medium |
| Slack Lists | 12 public methods since 2025-09-02; select and people columns | No change events; only the last editor per row; polling; paid plans | Low-medium |
| Canvases | Markdown, tables, checklists | No Block Kit, no events, no documented read-back | Low |

Platform facts that shape the design:

- **Socket Mode** needs no public request URL, because the app opens an outbound WebSocket. This fits an API behind
  Keycloak. Socket Mode apps cannot be listed in the Marketplace. Customer workspaces would need HTTP mode, OAuth
  install and a public endpoint, which is a transport rewrite, not an add-on.
- **2025 rate-limit change:** unlisted distributed apps get `conversations.history`/`replies` at Tier 1 (1 per minute).
  Internal apps keep Tier 3. This design reads no history.
- `chat.postMessage` allows about 1 message per second per channel. The design posts one summary per run, not one
  message per gap.
- `slack-bolt` 1.30.0 (2026-07-15) has `AsyncApp`, `AsyncSocketModeHandler` and a FastAPI adapter.

Sources (docs.slack.dev and pypi):

- Block Kit: blocks, input block, option object, `block_actions` payload.
- Surfaces: modals, App Home, Work Objects.
- Methods: `chat.update`, `users.lookupByEmail`.
- Events: Events API, Socket Mode.
- Changelog: Lists API (2025-09-02), rate-limit change (2025-05-29), Slackbot MCP client (2026-06-18).
- MCP: the Slack MCP server.
- Distribution.
- `slack-bolt` on pypi.

## What other products do

| Product | Slack surface | Structured or free text | Attribution | Back-sync | Idea to copy |
|---|---|---|---|---|---|
| JSM approvals | DM + Approve/Decline | Structured | In the message, with time | Yes, but the buttons stay live after a web decision (JSDCLOUD-13370) | Write "decided by" into the message; **avoid stale buttons** |
| Linear Asks / agents | Mention, emoji, form templates, thread, App Home | Forms; agents use a `select` that free text may override | Per user | Two-way thread | Options are optional; free text overrides them |
| Jira Cloud for Slack | Modal, live unfurl card, "Sync thread" | Fields + comments | **Linked account** | Thread to comments | Account linking; a card that shows the live state |
| GitHub for Slack | Thread per item, scheduled digest | Free text | Per user | Yes | Real time for "assigned to you"; a digest for the backlog |
| Polly / Simple Poll | Buttons, App Home, Slackbot MCP | Structured | Named or anonymous | n/a | Remind only the people who did not answer |
| Zapier HITL / n8n | Message + buttons, "send and wait" | Yes/no or data fields | Restricts to named users; records the responder | Resumes the flow | Only named people may act |
| Range | Modal + channel post, App Home | Semi-structured | Per user | Yes | Modal to write, App Home for "who did it" |
| incident.io / PagerDuty | Slash command to modal, nudges | Structured fields | Per user | Native | Activity-aware, capped nudges |
| Devin, Cursor, Codegen, Copilot, Claude Code in Slack | Thread | Free text (Claude Code: a repository picker) | Per user | Two-way thread | Silence by default; edit own message on completion |

Findings:

1. Products that need a **decision** use buttons or a modal. Products that need a **conversation** use threads. A gap
   answer is a decision with a fixed contract (`option_rank` or `own_text`).
2. Products that act for a person in another system use **account linking** (Jira, Linear, GitHub), not email
   matching.
3. No product documents an LLM that maps a free-text thread reply to an option. That would be our own, unproven
   pattern.
4. The most reported defect of approval bots is stale buttons after an answer elsewhere.
5. Slackbot MCP partners (Rovo, Linear, Polly) add `mcp:connect` to the same app that posts proactive messages. The
   MCP path alone cannot start a conversation.
6. Nothing found shows that answering inside Slack beats a notification with a link. The pilot measures this
   ("Success measures").

## Alternatives

| # | Alternative | How it works |
|---|---|---|
| A1 | DM notify + web link | The app tells the assignee (no client text) and links the web card |
| A2 | Thread reply + LLM | People reply in a thread; an LLM maps the reply to an option or own text; the bot asks to confirm |
| A3 | Per-gap cards in a thread | One message per gap with option buttons; edited after the answer |
| A4 | Summary + private paged modal | A summary (counts, mentions) with "Answer my N"; a modal with 5 gaps per page; one Submit; optional apply |
| A5 | App Home inbox | A per-user list of open and assigned gaps, each opening the A4 modal |
| A6 | Slack List board | A row per gap with an Answer select and an Assignee column; the app polls |
| A7 | Canvas digest | A channel canvas lists the open gaps; answers happen elsewhere |
| A8 | Workflow Builder form | A webhook trigger, a form, our custom step sends the answer back |
| A9 | Work Objects | A gap unfurls as a task entity with an editable Answer field |
| A10 | Slackbot MCP client | Slackbot calls the SpecFlow MCP tools |
| A11 | Agent + official Slack MCP | A SpecFlow agent posts text through `mcp.slack.com` and reads replies |
| A12 | MCP dialog (today) | A person asks a coding agent; `answer_gap` opens an elicitation form |
| A13 | Email digest + link | A mail per run or per day with the person's open gaps |

### Scoring

Scores are 1 (bad) to 5 (good). Cost and risk are scored so that higher is better (cheaper, safer). Friction is
scored for a batch of about 10 questions, not one.

| Criterion | Weight | Question |
|---|---|---|
| Friction | 25 % | How few steps and context switches to answer about 10 questions? |
| Fidelity | 15 % | Does the answer reach `AnswerInput` without a guess, with the edits visible? |
| Attribution | 10 % | Is the answerer a checked SpecFlow member? |
| Reach | 15 % | Does the question come to the right person without them asking? |
| Confidentiality | 15 % | Does client text stay out of places that non-members can see? |
| Cost | 10 % | Build and operation effort |
| Risk | 10 % | API maturity, plan limits, failure modes |

| # | Friction | Fidelity | Attribution | Reach | Confidentiality | Cost | Risk | **Total** |
|---|---|---|---|---|---|---|---|---|
| A1 DM notify + web link | 2 | 5 | 5 | 5 | 5 | 4 | 5 | **4.15** |
| A4 Summary + private paged modal | 4 | 4 | 5 | 4 | 4 | 3 | 4 | **4.00** |
| **A1 + A4 together (recommended)** | 4 | 4 | 5 | 5 | 4 | 3 | 4 | **4.15** |
| A12 MCP dialog (today) | 3 | 4 | 5 | 1 | 5 | 5 | 4 | **3.65** |
| A13 Email digest + link | 2 | 5 | 5 | 4 | 3 | 4 | 4 | **3.60** |
| A5 App Home inbox | 3 | 4 | 5 | 2 | 4 | 3 | 4 | **3.45** |
| A9 Work Objects | 4 | 4 | 4 | 4 | 2 | 2 | 2 | **3.30** |
| A8 Workflow Builder form | 3 | 4 | 4 | 4 | 3 | 2 | 2 | **3.20** |
| A10 Slackbot MCP client | 3 | 3 | 5 | 1 | 4 | 3 | 2 | **2.95** |
| A3 Per-gap cards in a thread | 3 | 4 | 4 | 3 | 1 | 2 | 3 | **2.85** |
| A2 Thread reply + LLM | 4 | 2 | 4 | 4 | 1 | 2 | 2 | **2.85** |
| A11 Agent + official Slack MCP | 3 | 2 | 3 | 4 | 1 | 3 | 2 | **2.60** |
| A6 Slack List board | 3 | 3 | 2 | 3 | 1 | 2 | 2 | **2.40** |
| A7 Canvas digest | 2 | 1 | 1 | 3 | 1 | 4 | 4 | **2.15** |

Notes on the scores:

- **A1 scores high because it is cheap and safe**, but it keeps the web screen as the place to answer, so alone it
  does not meet the goal. It is the first step of the plan and the control in the pilot: if a DM with a link already
  gets most answers in time, the modal is not worth more.
- **A4 + A1** is the recommendation. The summary and the DMs give A1's reach, and the modal removes the web step
  while client text stays private.
- **A3** (the first draft's choice) drops because every card puts client text into channel history, threads started
  by a bot notify nobody, and 30 cards in a thread are unusable on mobile.
- **A2** has low friction, but it puts client text in a thread, needs history scopes, and gives an LLM untrusted
  Slack text to choose from.
- **A6/A7** look natural, as "a list of questions". But Slack gives no change events for them and keeps only the last
  editor of a row. A canvas has no inputs at all.
- **A10** is pull only and each tool call needs approval. It complements A4; it cannot replace the push.

### Slack app or Slack-connected MCP

| | Slack app (bot, Block Kit) | Slackbot MCP client (`mcp:connect`) |
|---|---|---|
| Starts the conversation | Yes, it posts and DMs after a gap run | No: a person must ask Slackbot |
| Answer | A Submit in a modal; no model involved | A tool call the model makes. D4 and AC12 forbid that unless the tool opens a form (elicitation in Slackbot is unverified) |
| Attribution | Signed `user.id` + account link | Slack identity, or our Keycloak OAuth (the MCP server already has protected-resource metadata) |
| Keeps messages current | Yes (`chat.update`) | No message to edit |
| Build | A Bolt app, renderer, handlers | A manifest entry for the existing MCP server, and an auth check |

**Both, in one app, app first.** The MCP path is phase 3: the same app gets `mcp:connect`, so people can ask Slackbot
"which gaps do I own in Shop?". Answers still go through the modal.

## The design

### Parts

```
gap run / gap events (API) --PostgresGapRecords.log wrapper--> slack_outbox (Postgres)
slack_outbox --poster task (API, asyncio)--> Slack: summary post / chat.update, DMs

Slack --Socket Mode--> Bolt AsyncApp in the API lifespan
   ack at once (< 3 s), then a background task:
   block_actions "Answer my N"      -> views.open (modal, page 1)
   view_submission page k           -> check input; record answers; views.update page k+1 or the apply step
   view_submission "Apply now"      -> GapService.apply_now(by=<user id>)
   each answer -> linked user -> member of the gap's project? -> record_answer(..., via="slack")
```

| Part | Where | What it does |
|---|---|---|
| Slack app | One internal app in the GD workspace, from a checked-in manifest (`config/slack/manifest.yaml`) | Bot scopes as in the manifest: `chat:write`, `im:write`, `commands`, `channels:read`, `groups:read`, and for the channel flow `channels:history`, `groups:history`. No `chat:write.public`, no `users:read`, no DM history |
| Socket handler | API lifespan, on when `SLACK_APP_TOKEN` and `SLACK_BOT_TOKEN` are set | Bolt `AsyncApp` + `AsyncSocketModeHandler`; acknowledges every envelope at once and works in a background task |
| Account link | Web route `/settings/slack/link?nonce=…` under the Keycloak session; table `slack_user_link(team_id, slack_user_id, user_id, linked_at)` | Unlinked clicks get an ephemeral "Connect your SpecFlow account" with a one-time link (nonce valid 10 min, bound to the Slack user). Email is used only to suggest the link, never to authorize |
| Channel link | `/specflow link <project>` in the channel; table `slack_channel_link(project_id, team_id, channel_id, linked_by, linked_at)` | Only the project owner; private channels only; refuses Slack Connect (`is_ext_shared`). Stops posting on `channel_shared`. Unlink from Settings deletes the app's messages |
| Renderer | `api/slack/blocks.py`, pure functions | `GapDetail -> modal blocks`, `run counts -> summary blocks`; escapes all gap text (see "Security") |
| Message store | Table `slack_message(project_id, channel_id, ts, kind, run_id)` | Every summary and DM, so later events edit them and unlink deletes them |
| Outbox | Table `slack_outbox` + one poster task in the API | Wraps the host `log` port; coalesces many events per run into one `chat.update`; keeps the rate limit; retries; drops quote text (`cite_removed` detail) |

### The summary and the DM

- **Summary**, one per gap run with new questions, in the linked channel:
  - First line: "Build 41: 7 new questions (2 blockers) · @ana 3 · @piotr 2 · unassigned 2".
  - Buttons: [Answer my N], [Answer unassigned], [Open in SpecFlow].
  - AI answers: "AI answered 4 minor gaps" with a link to the web review list.
  - Later events edit the same message: "5 of 7 answered (@ana 3, @piotr 2) · applying", then "Resolved in build 42 ·
    1 follow-up".
  - It shows who answered and how many, never which option.
- **DM** to each linked assignee of a blocker or major gap: the gap titles (client text, but only to a project
  member) and [Answer these]. Each DM is stored and edited like the summary: when the gaps are answered anywhere, its
  button goes.

### The modal

```
Answer gaps · Shop · 1 of 2                                   (title <= 24 chars)
────────────────────────────────────────────
BLOCKER · contradiction
Which limit triggers a manual order review?
Why: payments-spec says 500 EUR; the review task needs one value.
> orders-spec.md › Order review: "Orders above 1,000 EUR need a manual review"
( ) A  500 EUR everywhere (recommended)
       Replaces in orders-spec.md: "Orders above 500 EUR need a manual review"
( ) B  1,000 EUR everywhere
       Replaces in payments-spec.md: "…"
( ) Other: answer on the web ↗            <- contradictions until phase 2
( ) Skip for now
────────────────────────────────────────────
MAJOR · missing  …  ( ) A … ( ) B … ( ) Other [ text field ]
────────────────────────────────────────────
                                   [Cancel] [Save, next 5]
```

| Rule | Detail |
|---|---|
| Page | 5 gaps per page (about 8 blocks each, within 100). Order as the web inbox: severity, reach, age. "Answer my N" takes the caller's assigned gaps; "Answer unassigned" the unassigned ones |
| Options | `radio_buttons` in `input` blocks, nothing preselected except the recommended option of a `major` gap (as on the web). The label (75 characters) is in the option text; the edit text goes in the option `description` or a `context` block below |
| Other | A `plain_text_input` (1-4000) for own text, placed in the clarifications document as written. For a `contradiction`, "Other" links to the web until phase 2, and the service refuses own text without edits for a contradiction (a fix for every caller; MCP sends such answers today) |
| Skip | The default for an untouched gap: nothing is recorded |
| Version | `private_metadata` holds `project_id`, the page's `gap_id`s and each `updated_at`. Each answer passes its `updated_at` |
| Submit | Acknowledged at once with `response_action: update` to a "Saving…" view; the background task records the answers one by one and then updates the view to the next page or the apply step. Input errors (length, Other without text) use `response_action: errors` synchronously |
| Apply step | "N answers saved. Apply now: one update build. It also applies: 2 answers staged by @piotr, 4 AI answers (review ↗)." The box is ticked by default. Submit calls `apply_now`; `nothing_pending` (someone applied first) shows "Already applying" |
| Errors per gap | `StaleGapError`: "Changed since you opened it; shown again on the next page". `GapStateError` with status `applying`: "Being applied: change it on the web after the build". `GapNotFoundError` (dismissed or obsolete): "No longer open". Two people at once: "Ana answered B a moment ago". Nothing is replaced silently |
| Not a gap | Not in Slack: dismissing stays on the web (D4, AC12). Each gap has a "Not a gap? Open in SpecFlow ↗" link |
| Change an answer | "Answer my N" lists staged answers as "answered" with a "Change" link to the web card. A replace in Slack is phase 2 |

### Security and data

| Topic | Rule |
|---|---|
| Client text | Never in channel history: the summary holds counts, names and links only. Client text shows in the modal (private to the clicker, after the membership check) and in DMs to linked members. Posting needs the owner decision O2 per project, with legal or DPA approval for client projects |
| Who may answer | A linked Slack user whose SpecFlow user is a member of **the gap's project** (from `private_metadata`, checked with `get_member_project`), checked on every submit and every thread reply. A Slack guest or outsider can only answer after linking to a SpecFlow account that is a member of the project |
| Escaping | Gap text is untrusted. The renderer cuts first, then escapes `&`, `<`, `>`. It sets `verbatim: true` on mrkdwn so bare URLs and `@here` do not become links or mentions. It uses `plain_text` where Slack allows |
| Tokens | `SLACK_BOT_TOKEN` (`xoxb-`) and `SLACK_APP_TOKEN` (`xapp-`) are secrets in env and Helm values; never logged |
| Logs | Ids and codes only (NFR-GSEC-02); no gap text, no Slack text |
| Unlink, deletion | Unlinking a channel or deleting a project deletes every stored message (`chat.delete`) and the channel link |
| Kill switch | Unset tokens turn the feature off; the web screen and MCP keep working |
| LLM on Slack text | None in phases 1-2. Any later use treats Slack text as untrusted data (GC2) and records the original author |

### Runtime notes

- The API runs as one replica with `Recreate`. During a deploy the socket is down for a short time, and a click then
  shows Slack's "app did not respond". The modal keeps nothing on our side until Submit, so a retry is safe.
- Gap locks are in-process. A second API replica would break them before it broke Slack, so the design keeps the
  socket in the API and adds no new process.
- `answer_gap` reads the project's documents and waits on the per-project lock, which `apply_now`, dismiss, revert
  and raised gaps also take. So every handler acknowledges first and works in a background task.

### Getting the app into the GD workspace

An internal app needs GD's own review before a Slack admin installs it in the GD workspace: develop in a separate
workspace with no production data, fill GD's application security form (purpose, data, retention, scopes, events,
team, manifest), pass the review with IT and GD Security, then install. Adding a scope later means a new review and a
reinstall, so the [manifest](../../config/slack/manifest.yaml) asks once for the scopes of phases 1-2;
`mcp:connect` in phase 3 is a second review. A Workflow Builder workflow with a webhook trigger needs no review
unless it needs special permissions, which allows a notify-only start (see "Before the app is approved").

The filled form and GD's step list are internal: they are kept outside this public repository.

### Before the app is approved

The owner created a dedicated channel in the GD workspace. Until the review passes, it can carry a notify-only
version of phase 1a without any app:

1. In Workflow Builder, the owner makes a workflow in that channel that starts from a webhook, with the text
   variables `project`, `new_questions`, `blockers`, `majors` and `url`, and one step "Send a message to the
   channel", for example: "{project}: {new_questions} new questions ({blockers} blockers, {majors} major). Answer:
   {url}".
2. The api POSTs those variables to the webhook after each gap run that routes new gaps to people (`ask`): the
   project name, counts and the link only, no gap text (O2 holds). It is on when `SLACK_WORKFLOW_WEBHOOK_URL` (a
   secret) and `SLACK_WORKFLOW_PROJECTS` (the project ids to announce) are set; `PUBLIC_URL` makes the link
   ([configuration](../configuration.md)). Code: `backend/src/api/gap/notice.py`.
3. People open the link and answer on the web. The modal waits for the app.

Workflow Builder needs a paid Slack plan. On a free workspace (the development one), an app with an **incoming
webhook** (`https://hooks.slack.com/services/...`) takes the same setting: the api then sends one ready message
instead of the variables.

Limits: a workflow cannot edit its earlier message, so there is one message per gap run and no "5 of 7 answered"
edits; no DMs; no @mentions unless SpecFlow knows Slack user ids, which needs the account link. What it does
measure: whether a notice alone shortens the time to answer (1a against the baseline).

What does **not** work: posting with the owner's own Slack user token or browser session. A user token still needs
an app installed in the GD workspace (the same approval), cannot open modals, and every message would look like the
owner wrote it. Taking a token from the browser would skip the review IT requires.

## Delivery plan

| Phase | Content | Done when |
|---|---|---|
| 0. Spike (3 days) | Internal app from the manifest; Socket Mode in the API lifespan; account link route; a test summary with "Answer my N"; modal page; `record_answer` with `via = slack` from a background task; `views.update`; one deploy while the modal is open. Record the **baseline** from today's gap events (`answered_at` minus the gap's creation, per severity) | An answer recorded with the linked `answered_by`, shown in a demo; the baseline numbers |
| 1a. Notify (1 week) | Before approval: the Workflow Builder notice in the owner's channel ("Before the app is approved"). With the app: channel link (owner, private, no Slack Connect); summary per gap run with counts, mentions and [Open in SpecFlow]; DMs for assigned blocker and major gaps; outbox; summary edits on events | The summary and DMs arrive after a gap run on a pilot project and stay current |
| 1b. Answer (1-1.5 weeks) | Modal with pages, radios with edit text, Other (document placement), Skip, apply step; the error handling above; `via = "slack"` (`graph.gap` models, `EVENT_WORDS`, web history label); the contradiction rule in `GapService`; spec updates: this file, [answers.md](../graph/gap/answers.md), [../mcp-server/access-control.md](../mcp-server/access-control.md) AC12 (Slack modal as a third answer surface) | A pilot member answers 5 gaps in one modal and applies them; the build resolves them; the summary says so |
| 2. Inbox and own answers | App Home inbox; own answers with `preview_edits` in a pushed view (async `views.update`), which also brings contradictions into Slack; "Change" in Slack; reminders to assignees who did not answer (at most 2, working hours, only non-responders); **opt-in auto-apply** per project in its own design (a timer in the API from the pending answers' `answered_at`, resumed by `recover`, with an explicit rule for AI answers) | Contradictions answered in Slack; reminders measured |
| 3. Pull and reach | `mcp:connect` on the same app (pull: "my open gaps"); an email or Teams route for client stakeholders; customer workspaces over HTTP mode + OAuth install if needed | Slackbot lists "my open gaps" |

### Phase 0: the spike plan

The spike answers the questions that could change the design before phase 1a builds on it. Its code is throwaway, on
a branch that is not merged. Phase 1a starts from `main` with real migrations and tests. The output is a "Spike
findings" section in this file (one verdict per question) and the baseline numbers.

**Before day 1**

- A **separate free Slack workspace** for development, as GD IT asks (see "Getting the app into the GD workspace").
  The spike does not wait for the GD workspace.
- On day 1, start the GD review with IT: send the filled security form and the
  [manifest](../../config/slack/manifest.yaml). Phase 1b needs the approval, and its lead time is unknown.
- Tokens (`SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`) go in `.env.local` only, never in a commit.
- A local stack with one project built from **non-client documents** (the development workspace must hold no
  production or sensitive data), with a finished gap run with at least 10 open `ask` gaps, some of each severity and
  at least one `contradiction`.

**Questions and how each is checked**

| # | Question | Check | Pass |
|---|---|---|---|
| S1 | Does Socket Mode run inside the API lifespan without harm? | Add `slack-bolt` and its async socket transport to the `api` extra; start the `AsyncSocketModeHandler` in `create_app`'s lifespan when both tokens are set, and close it on shutdown | Starts and stops with the API; with tokens unset nothing changes; the image size increase is noted |
| S2 | Can we acknowledge within 3 s and open the modal within the `trigger_id` window while the project lock is held? | Click "Answer my N" while a gap run or `apply_now` holds the per-project lock; time the ack and `views.open` | Ack under 1 s and `views.open` under 2 s at p95 over 20 clicks, with the lock held |
| S3 | Does the account link work under the Keycloak session? | A bare web page at `/settings/slack/link?nonce=…` calls a new API route through the BFF; a throwaway `slack_user_link` table; an unlinked click gets the ephemeral prompt | The link row holds the Keycloak user; a nonce older than 10 min, reused, or opened by another Slack user is refused |
| S4 | Do 5 real gaps fit one modal page from `GapDetail`? | Render the project's gaps; count blocks; check the 75-character option text and option `description` limits against real edit text | Within 100 blocks; a rule for where edit text goes (option `description` or a `context` block below) with real examples |
| S5 | Does a Slack answer land like a web answer? | Submit a page: `record_answer` with `via = "slack"` (the `Literal` widened on the spike branch only), `answered_by` = linked user, from a background task, then `views.update` to the next page | The `answered` event has the linked user and `via = slack`; the web history shows it; stale `updated_at` gives the per-gap message |
| S6 | Does "Apply now" behave? | Tick the box on the last page with another member's answer staged | The modal lists the other staged answer and the AI answers; one update build runs; a second click gets "Already applying" |
| S7 | What does a deploy do to an open modal? | Restart the API container while a modal is open; submit before and after it is back | Slack shows an error during the restart; a submit after the restart records the answers; nothing is half-recorded |
| S8 | Is gap text escaped? | Put `<!here>`, `<https://x.example\|click>`, `&amp;`, `*bold*` and a bare URL in a gap's question and option label | All shown as typed; no mention, link or formatting |

**Baseline (also day 1)**

A read-only script over the project database for the projects that have gap runs, last 90 days:

- per gap: the first `created` event and the first `answered` event in `gap_event`;
- severity and route from the gap nodes in the graph store (`read_gaps`), since `gap_event` does not hold severity;
- per severity: the median time to the first answer, the share of `ask` gaps answered within 48 h, and the answers
  per `via` (`web`, `elicitation`).

The numbers go into this file; the script stays in the spike branch.

**Days**

| Day | Work |
|---|---|
| 1 | Sandbox workspace and manifest; GD admin request; S1; S3; baseline |
| 2 | Summary post with "Answer my N" in a test channel; S4; S5; S2 timing |
| 3 | S6; S7; S8; write "Spike findings" and the go or no-go; a demo to the owner |

**Not in the spike:** channel link, DMs, outbox, summary edits, "Answer unassigned", reminders and every production
migration.

**What changes the plan**

| Result | Change |
|---|---|
| S2 fails (ack or `views.open` too slow under the lock) | Open the modal with a loading view at once and fill it with `views.update`; the background work stays |
| S4: edit text does not fit | Edit text goes in a `context` block under the radios, and the page size drops to 4 if blocks run short |
| S7: a deploy loses submits | A note in the modal ("if Slack shows an error, submit again") is enough while answers stay idempotent through `updated_at`; otherwise revisit a separate Slack process |
| GD admin approval is refused or slow | Phase 1a on the sandbox for the pilot team is not an option (client text, O2); fall back to email for 1a and keep the modal work for when approval arrives |

## Success measures

Measured on 1-2 pilot projects against the baseline of phase 0, after 1a and again after 1b:

| Measure | Target |
|---|---|
| Median time from a gap's creation to its answer, blocker and major | At least 50 % lower than the baseline |
| Share of `ask` gaps answered within 48 h | Higher than the baseline |
| Share of answers with `via = slack` | Shows whether the modal adds value over the notification (1a against 1b) |
| Share of Slack answers changed or reverted, against web answers | Not higher: a quality check |
| Members who mute or leave the channel | Close to zero |

## Decisions for the owner

The owner accepted O1-O5 as recommended on 2026-11-23. Later that day O1 and O2 changed with the channel flow (see "The channel flow").

| # | Decision | Accepted |
|---|---|---|
| O1 | "Resolve in one go": keep staging and make "answer + apply" one Submit in Slack, or remove staging for every surface now | Keep staging; one Submit answers and applies. Auto-apply later, opt-in, in its own design (it would apply AI answers nobody reviewed and multiply builds) |
| O2 | May client text go to Slack, and where | Never in channel history; in modals and member DMs only, per project, after legal or DPA approval for client projects |
| O3 | Which workspace | The GD workspace only (internal app, Socket Mode). Client stakeholders get a link or mail until phase 3 |
| O4 | Identity | An explicit account link; no email matching |
| O5 | Pilot projects | 1-2 internal projects with active gap runs |

Smaller choices with a default, change if wanted: any member links a channel (owner only until Mission Control V2, D6); any member answers, as on the web;
the summary shows who answered, not which option.

## Review findings

Two reviews of the first draft, one against the code and one on product, UX and security, changed these points:

| Finding | Change |
|---|---|
| Per-gap cards put client text (titles, questions, options, quotes) into channel history, visible to non-members and to Slack retention | Summary holds no client text; text only in the private modal and member DMs (O2) |
| Removing staging for every surface would apply AI answers nobody reviewed, drop the "Resolve gaps" review list, multiply builds, and the proposed timer hosts (Temporal, `apply_requested_at`) do not fit the code | Staging kept; "Apply now" in the modal lists what else it applies; auto-apply moved to phase 2 as its own design with a timer in the API |
| Email matching: `users.email` is not unique, changes only at sign-in, and DMs need a reverse mapping | Account link under the Keycloak session |
| Bot threads notify nobody; 30 cards are unusable; per-gap button + confirm is two steps per gap | Summary + paged modal (5 per page), radios + one Submit |
| `GapCard` lacks the options' edits that the spec requires on every card | Render from `GapDetail` |
| Slack dismiss and Slack answers conflict with AC12 and D4 | Dismiss stays on the web; AC12 gets the Slack modal as an answer surface |
| A contradiction can be answered with own text without edits (the service does not refuse it; MCP does it today) | Enforce the rule in `GapService` for every caller |
| One API replica with `Recreate`; `answer_gap` can be slow under the project lock | No replica claim; acknowledge first, work async; deploy behaviour tested in the spike |
| No success measure | Baseline in phase 0; measures above; 1a (notify only) against 1b (modal) |
