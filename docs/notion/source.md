---
type: Design
title: "Notion as a project source"
description: "A Notion page or database picked from what the member shared, imported once as documents: picker, check, import, Markdown conversion, caps, failures, routes"
status: draft
tags: [notion, sources]
---
# source.md: a Notion page or database as a project source

A member ticks Notion pages and databases in a checklist of what they shared with Graphico, and **Add selected** imports each
one as its own source, once (decision D7 of Mission Control: no live sync). The import runs in the api process through the importer seam
([../sources/importers.md](../sources/importers.md)) as the member the project's Notion is connected by (rule R1), the same
shape as a Confluence link. Decisions: N2 to N6.

## Link and check

- The form offers a checklist (N2), not a text field: nobody types or pastes a link, so a mistyped link cannot happen.
  See "Picking a page" below. The routes still take a link: the form posts the link of each item ticked. The form has no
  check step; the check route stays for API callers.
  `notion.links.parse_link` accepts `https` links of `notion.so`, `www.notion.so`, `notion.com`, `www.notion.com`, `app.notion.com` and `*.notion.site`; the
  object id is the last 32 hex digits of the path; `?v=` marks a database. Anything else is `notion_link_invalid`.
- `POST /projects/{pid}/sources/notion/check` `{link}` reads the object as the bound member and answers
  `{kind: page | database, title, pages, capped}`: `pages` is at least the count of the page and the pages and databases it names, or of the
  first page of the database's rows, at most the cap; `capped` is true when there may be more. A page or database that does not exist or was not ticked in the
  picker is `409 notion_not_shared`.
- `POST /projects/{pid}/sources/notion` `{link}` creates the source for the caller (`connected_by`),
  opens the link (add does not check again) and starts the import, which walks once, and answers the source card. `external_id` is a fresh uuid, `link` as pasted. Any member may call both
  (D6). `409 notion_not_connected` when the project has no Notion binding, `409 notion_reconnect_required` when the
  bound member's connection expired.

## Picking a page

`GET /projects/{pid}/sources/notion/shared` lists what the bound member shared with Graphico on Notion's consent page
(`notion.shared`): `{items: [{kind: page | database, title, link, parent_title}], capped}`.

- It is a `POST /v1/search` per kind (`page`, then `data_source`), last edited first, at most 4 pages of 100 each.
- Database rows (a page whose parent is a database or data source) and trashed objects are left out. A database is listed
  once, by the title of its data source; its `link` is the database id, which the check reads as a database.
- `parent_title` is the title of the parent page when that page is listed too ("Onboarding · in Engineering Wiki").
- Titles are scrubbed as the import scrubs them. The list is sorted by title and holds at most 300 items; `capped` says
  there were more.
- Any member may read it. Its refusals are those of the check: `409 notion_not_connected`, `409 notion_reconnect_required`,
  a refused search as its closed code, `503 notion_unavailable`.
- The source form is a checklist like the GitHub repositories and the Box files: pages first (with "in <parent>"), then
  databases, nothing ticked, Select all and Select none, a count, and **Add selected**. Each item is added in turn
  (`POST .../sources/notion`); when one is refused, the items before it are in and leave the list, the refused one is
  named ("Old notes: ...") and stays ticked with the rest. The plan form picks one parent page from a dropdown of the
  pages only. When nothing is shared, the form says to connect Notion again and tick the pages. A page shared then
  unshared since is `notion_not_shared`.

## Reading (N3)

Pages are read as Markdown, never as a block tree: `GET /v1/pages/{id}/markdown?markdown_version=v2` (the gateway follows
`unknown_block_ids` itself). The import then follows:

- every `<page url=…>Title</page>` line: a sub-page, read the same way;
- every `<database url=… data-source-url="collection://…">` line: the rows of that data source, through
  `POST /v1/data_sources/{id}/query` (100 per page, the gateway pages by cursor).

The gateway follows `unknown_block_ids` itself (up to 25 subtrees); a page that is still truncated or incomplete afterwards
ends with a note and the import logs `api.imports.page_incomplete`.

A link to a database imports its rows. Which of the two a link is, is read from Notion: page first; when that read says
`notion_parent_not_a_page` (Notion answers 400 for a database id on a page route), the link is read as a database. A link
without `?v=` can be either. A visited set stops cycles; depth is bounded by the cap. A database row's document is
`# Title`, a table of its properties and then its body (N3): people as display names, relations and files by name, no e-mail
and no user id.

## Conversion

`notion.convert.to_markdown` turns Notion-flavoured Markdown into plain GFM with the rules of
[discovery.md](discovery.md#conversion-test): callouts become blockquotes, heading toggles and tabs become headings, plain toggles a bold line, columns are
flattened, synced blocks keep their content, user mentions keep the display name only, page and database mentions become
links, dates become ISO text, styled tables become GFM tables, `<meeting-notes>` keep summary and notes and skip the
transcript, `<custom-block>` is dropped. Images are named, never fetched. No NFM tag is left in the output and no e-mail outside code spans and fenced code (code is left as written; an address needs an alphabetic final label).
A page nested too deeply is `MarkupTooDeepError`.

## Caps (N4)

`MAX_PAGES = 100` per import, pages and rows together, as Confluence. A hit cap is logged with counts
(`api.imports.page_cap_reached`) and the check says `capped`; the rest of a big wiki is added as a narrower link.

## Originals (N5)

`ImportWriter.add(original=…)` stores JSON `{page: {id, url, title, last_edited_time}, markdown}` per page, where `markdown`
is `scrub_original(nfm)`: every e-mail is removed outside code spans and fenced code, page titles included (a user mention carries `user://<id>/<email>` even when the connection
asks for user information without e-mail). Notion's `properties`, `created_by` and `last_edited_by` are never stored: they
carry people and e-mails. The GFM goes to the document.

A database row's properties reach the document through `render_row`, which shows `people` as display names only and an
`email`-type property as "(e-mail hidden)"; a user id is never shown.

## Failures (N6)

The failure vocabulary stays closed ([../sources/importers.md](../sources/importers.md)):

| What happened | Result |
|---|---|
| a page or row the member cannot see (404) | that item is `no_access`, step `read`; the import goes on |
| Notion refuses a sub-page or row (`notion_item_invalid`, `notion_permissions_missing`) | that item is `unknown_error` or `no_access`, step `read`; the import goes on; only the root page fails the source |
| the link cannot be read at all, or the member's connection is gone | the source fails `no_access` |
| Notion is down or throttled past the retries | the source fails `unknown_error` |
| a page too deep to convert | that item is `convert_failed`, step `convert` |

The check route uses the `notion_*` codes of [codes.md](codes.md) instead, as `ApiError`s.

## Not verified live

An import of a workspace over the cap, `truncated` pages with `unknown_block_ids`, and synced blocks of other pages were
not met in the sandbox (no sandbox page is truncated). The recorded fixtures in `backend/src/notion/fixtures/` are the
sandbox pages; the live tier `test_notion_http_live.py` re-reads them with `NOTION_API_TOKEN`.
