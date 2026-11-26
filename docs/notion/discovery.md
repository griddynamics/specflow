---
type: Explainer
title: "Notion integration discovery"
description: "What the Notion API gives a Notion source, a plan export and gap answering; the setup a deployment needs; test results from a sandbox workspace"
status: draft
tags: [notion, sources, plan, gap]
---
# discovery.md: Notion API facts, sandbox results, setup, plan and gap verdicts

Discovery date: 2026-11-25. Notion API version checked: `2026-03-11` (latest).

## Sandbox

A private page "Specflow Notion Lab" in the owner's Notion workspace holds the test content. It is safe to delete.

| Page | Notion features it holds |
|---|---|
| PRD - Checkout Loyalty Points | callout, H2/H3, bold/italic/strike/underline/color, link, numbered and bullet lists, GFM table, display and inline math, to-dos, toggle heading, plain toggle, code block, quote, columns, divider, bookmark, embed, image, user mention, date mention |
| ADR-007 | tabs, synced block, equation block, user mention, date mention |
| Engineering Wiki | child pages two levels deep (Wiki > Runbooks > Runbook - Points backfill) |
| Requirements DB | database with title, status, select, multi-select, people, date, number, URL, unique ID; rows with page bodies, one row with an empty body |
| Specflow Plan (trial) | plan export trial: Feature > Story > Task rows, self-relations `Parent`/`Children` and `Blocked by`/`Blocks` |

## Reading content: Notion gives Markdown

- `GET /v1/pages/{id}/markdown?markdown_version=v2` returns the page as Notion-flavored Markdown (NFM v2). It is the same
  text the Notion MCP `fetch` tool returns. `truncated` and `unknown_block_ids` flag omitted subtrees; pass each unknown
  id to the same endpoint to read it. `v1` is deprecated; always send `v2`.
- NFM v2 is CommonMark + GFM (tables, task lists, strikethrough, `$$` math) plus XML-like tags: `<callout>`, `<toggle>`,
  `<columns>/<column>`, `<tabs>/<tab>`, `<synced-block>`, `<mention>`, `<date>`, `<page>`, `<database>`, `<bookmark>`,
  `<embed>`, `<image>`, `<table>` (styled tables), inline `<b> <i> <s> <u> <code> <span color>`.
- Database rows: query `POST /v1/data_sources/{id}/query` (100 per page) for properties, then read each row's body with
  the Markdown endpoint. A database can hold several data sources (since API `2025-09-03`).
- Child pages appear as `<page url=…>Title</page>` and child databases as `<database url=…>Title</database>` lines, so
  the import can walk a page tree from the Markdown alone.
- User mentions carry the user id and e-mail (`user://<id>/<email>`). The converter must keep the display name only.

### Conversion test

A prototype converter (line-based, no HTML parser, ~120 lines; a throwaway script, not in the repository) turned the PRD
and ADR content into plain GFM with no tags left. Rules that worked:

| NFM | GFM output |
|---|---|
| `<callout icon=…>` | blockquote, icon kept as first character |
| `<toggle heading="h3">Title` | `### Title` + children |
| `<toggle>Title` (plain) | `**Title**` + children |
| `<tabs><tab>Title` | `#### Title` per tab, tabs in order |
| `<columns><column>` | columns flattened top to bottom, blank line between |
| `<synced-block>` | its content |
| `<mention url="user://…">Name</mention>` | `Name` (id and e-mail dropped) |
| page / database mention | `[Title](url)` |
| `<date start=… end=… />` | `2027-02-15` or `start to end` |
| `<b> <i> <s> <u> <code> <span>` | `** * ~~ (plain) \`` (plain) |
| `<bookmark>`, `<embed>`, `<pdf>`, `<file>`, `<video>` | `[caption](source)` |
| image | `Image: <caption>`; the image itself is not fetched (same rule as the Drive converters) |
| `<page>`, `<database>` | `- Sub-page: [Title](url)` and a reference the import follows |
| `<table-of-contents />`, `<p />` | removed |
| `\<tag>` (escaped literal) | `&lt;tag>` |
| fenced code, `$$` math, GFM tables, task lists | unchanged |

Gaps for the real converter: styled `<table>` to GFM, `<meeting-notes>` (keep summary and notes, skip transcript),
nested `<callout>` inside list items, `<custom-block>` (drop).

### REST check with an internal connection

An internal connection ("Specflow dev", content read/update/insert, comments read/insert, user info without e-mail),
added to the sandbox root page, gave these results on `Notion-Version: 2026-03-11`:

- Every sandbox page, child, grandchild and database row answered 200. Adding a connection to a root page grants all its
  descendants.
- The REST Markdown is the same text as the MCP `fetch` output. The prototype converter left no tags and no e-mail in
  the PRD, ADR, wiki and root pages. No page was `truncated`; no `warnings` were returned.
- **User mentions in page Markdown carry the user's e-mail even when the connection has "user info without e-mail"**
  (`GET /v1/users/{id}` returns no e-mail with the same token). The converter must drop it, and any raw copy that is
  stored must have it removed first.
- `<database>` lines in Markdown carry `data-source-url="collection://…"`, so the import can query rows without an
  extra database call.
- A database row's Markdown is its body only. Its properties come from `POST /v1/data_sources/{id}/query`
  (`people` values give names only).
- `last_edited_time` has minute precision (`…T19:39:00.000Z`). Change detection needs `last_edited_time` and a content
  hash together.
- `POST /v1/search` with no filter returned all 16 shared objects, database rows included. The source form therefore
  takes a pasted link (as the Confluence form does), not a picker over search results.

## Writing content

- `POST /v1/pages` accepts `markdown` (with `markdown_version: "v2"`) in place of `children`; large writes may use
  `allow_async`. `PATCH /v1/pages/{id}/markdown` updates a page body as Markdown.
- `POST /v1/databases` creates a database with an initial data source schema; relations (also two-way self-relations)
  are properties. The trial showed `Parent`/`Children` and `Blocked by`/`Blocks` work for a Feature > Story > Task tree.
- `POST /v1/comments` posts on a page (`parent.page_id`), on a block (`parent.block_id`) or as a reply
  (`discussion_id`), with a `markdown` body. `GET /v1/comments?block_id=` lists a page's or block's comments.

## Limits

- Rate limit per connection, fixed 60-second window: 180 requests/minute on Free/Plus, 600 on Business/Enterprise.
  429 and 529 carry `Retry-After` (at most 60 s) and `additional_data.retry_after`. Retry 429/529 always, 5xx only for
  reads (GET, read-only POST), exponential backoff with jitter, 5 attempts. One queue per connection so one job cannot spend the budget.
- Webhooks exist (`page.content_updated`, `database.content_updated`, `comment.created`, …). They are set per connection
  in the developer portal, need a public HTTPS endpoint, a one-time verification token and an `X-Notion-Signature`
  HMAC check. `page.content_updated` is aggregated and arrives about one minute after the edit.

## Access: what a deployment needs

| Option | How a user grants access | Token | Fit |
|---|---|---|---|
| Public connection (OAuth 2.0) | Each user clicks Connect, Notion shows a page picker, the user ticks pages/databases | access + refresh token per authorisation; `expires_in` decides refresh; `workspace_id`, `bot_id`, `owner.user` returned | **Recommended.** Same shape as GitHub and Atlassian. Works for any workspace. |
| Internal connection | A workspace admin creates it; users add it to pages with ••• > Connections | one static token per workspace | Local development and the live tests. Single workspace only. |
| Personal access token | User pastes a token | static, expires (default 1 year) | Rejected: secret pasted into a form, per-user rotation. |

Only the user who authorised a public connection can use it; each member connects for themselves. Pages a user did
not tick in the picker return 404 (`object_not_found`), which the import records as a skipped item
(`no_access`), as the Confluence importer does.

Capabilities the public connection asks for:

| Capability | Needed by |
|---|---|
| Read content | source import, plan export (read back) |
| Insert content, Update content | plan export |
| Read comments, Insert comments | gap answering (phase 2) |
| User information: no e-mail | display names only; turn on "with e-mail" only when gap answering maps Notion users to members |

## Verdicts

Checked against the Mission Control V2 design (`docs/mission-control/DECISIONS.md` D4, D7, D20 and the per-app table).

- **Source: yes.** A pasted Notion page or database link becomes a source of kind `notion` through the importer seam
  (`api.imports`, as Confluence links): the page, its sub-pages and its database rows become documents, imported once
  (D7). The Markdown endpoint removes the block-tree work that Drive needed.
- **Plan destination: yes, as a database, not as a page.** One row per story and task, relations for parent and blocking
  links, the task brief as the row body, registered as a plan destination (`api.plan_destinations`, `ExportRunner`) like
  GitHub Projects, with the same desired set (`api.jira.desired.build`). One long plan page is not useful: it cannot be
  filtered, assigned or tracked, and every update rewrites it. D4 names exactly this case: one Notion connection read as
  a source and written as a plan.
- **Gap: yes, later.** A gap channel like `jira-gaps`: the open questions as comments on a Notion page, replies read back
  as answers. It needs a rule for mapping a Notion commenter to a project member, so it is a separate pull request.

## Sources

- [Retrieve a page as markdown](https://developers.notion.com/reference/retrieve-page-markdown)
- [Working with markdown content](https://developers.notion.com/guides/data-apis/working-with-markdown-content)
- [Create a page](https://developers.notion.com/reference/post-page)
- [Update a page's content as markdown](https://developers.notion.com/reference/update-page-markdown)
- [Public integrations](https://developers.notion.com/guides/connections-and-tokens/public-integrations/overview)
- [Authorization](https://developers.notion.com/guides/authorization/overview)
- [Internal connections](https://developers.notion.com/guides/get-started/internal-connections)
- [Request limits](https://developers.notion.com/reference/request-limits)
- [Webhooks](https://developers.notion.com/reference/webhooks)
- [Create comment](https://developers.notion.com/reference/create-a-comment)
