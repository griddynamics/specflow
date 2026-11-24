---
type: Design
title: "Importers: Confluence links and Jira epics"
description: "The sources the API reads itself from Atlassian: what a Confluence link or a Jira epic imports, the caps, the acting account, the Markdown conversion, how a changed page is added again, codes and failure reasons"
status: draft
tags: [sources, confluence, jira, import]
---
# importers.md: sources the API imports itself

A **source** is one `connections` row (the Sources status shows it as a card); an **importer** reads from an app and stores
what it read as `documents` that the graph build already reads (nothing in the build changed). Google Drive and Local
files have their own paths ([../projects/](../projects/)); the importers here run inside the API process through the
importer seam of [../mission-control/integrations-api.md](../mission-control/integrations-api.md) (`api.imports`, AC5).

Rules shared by every importer:

- **Acting account.** The import reads as the member the project's Atlassian is connected by (`project_integrations`, R1),
  never as the caller. The caller is only `connections.connected_by` (who added the source). No new Atlassian scope is
  used: `read:page`, `read:hierarchical-content`, `read:space` (Confluence) and `read:jira-work` (Jira) were granted already
  ([../jira-sync/oauth.md](../jira-sync/oauth.md)). The site is the deployment's one site (`ATLASSIAN_SITE_URL`); a link
  on another host is `atlassian_site_mismatch`.
- **One source per link.** `kind` is `confluence` or `jira_epic`; `external_id` is a fresh uuid, `link` the link as pasted.
- **Originals and Markdown.** Each item is stored twice: the API's answer as JSON (`original.json` in object storage) and
  a Markdown document (`origin = connection`, `status = parsed`, `parser = <kind>-convert`). The document starts with the
  title as its heading; the graph reads it like any other (`doc:<document id>` lineage, path `<source name>/<item name>`).
- **Import once** (D7). There is no sync. Adding a changed page again creates a new source; its documents differ by
  content hash, so the graph holds the old and the new version until the old source is removed (removing a source removes
  its documents). The same content already in the project, from any source, is a `duplicate` item that links the first
  document.
- **What the member cannot see is skipped, not failed.** A page or issue the bound member cannot read ends as one item in
  error (`no_access`, step `read`) and the import goes on; the card counts it as a problem file. A target that cannot be
  read at all fails the source with `no_access`; any other failure of Atlassian (down, rate limited after the retries, an
  answer that is not JSON) fails it with `unknown_error` (AC24: no new failure vocabulary).
- **Refresh.** A `401` refreshes the member's token once and repeats the call (`jira/http.py` `BearerAuth`); an expired
  connection fails the source with `no_access` (the member connects again, the source is added again).
- **Logs.** Events carry ids, the kind, caps and counts. A title, a page text or an issue text lives in the document and
  its original only.
- **A restart** ends the imports in flight as `failed` (`unknown_error`); the member adds the source again.
- **Live check left to owner.** The importers are tested over recorded-shape fakes of the REST APIs; the first import against
  the real site is the owner's to try with the project's Atlassian connection.

## Confluence link

`POST /projects/{pid}/sources/confluence/check` and `POST /projects/{pid}/sources/confluence`, both
`{link, follow}`, any member. Shapes and codes: [integrations-api.md](../mission-control/integrations-api.md) "Confluence source".

| Link | Imports |
|---|---|
| a page URL (`/wiki/spaces/<KEY>/pages/<id>/<title>`, `/wiki/pages/viewpage.action?pageId=<id>`) | the page and every page below it (descendants; folders, databases and whiteboards are left out) |
| a space URL (`/wiki/spaces/<KEY>`, `.../overview`) | the current pages of the space |
| a short link (`/wiki/x/<tiny id>`) | refused: `confluence_short_link_unsupported`. Confluence documents no tiny-id algorithm to decode locally, and the call that resolves one (the v1 content API) needs a scope the site admin did not grant; paste the full page address |

Reads (Confluence Cloud REST v2, `https://api.atlassian.com/ex/confluence/{cloud id}`): `GET /pages/{id}?body-format=storage`,
`GET /pages/{id}/descendants`, `GET /spaces?keys=`, `GET /spaces/{id}/pages`, `GET /pages?space-id=&title=`; results follow
`_links.next`.

**Caps** (constants in `api/imports/confluence.py`): `MAX_PAGES = 100` pages per import (the target, its descendants and the
followed pages together) and `MAX_LINKED_ISSUES = 50` Jira issues. A cap that is hit stops the listing there, is logged once
as a count (`api.imports.page_cap_reached`, `api.imports.issue_cap_reached`) and the check says `capped: true`.

**Follow links** (`follow: true`, AC11). From every imported page: same-site page links (`/wiki/spaces/<KEY>/pages/<id>`,
`viewpage.action?pageId=`, and `ac:link` to a page by title, in its space key or the space of the page it is on) are read
**one hop**: the links of a followed page are not followed again. Jira issues linked by `/browse/<KEY>` links or the Jira
macro become documents too (the issue with its comments, as in the Jira epic importer below). Links to other sites are
ignored; a link that does not resolve or that the member cannot see is skipped.

**Storage format to Markdown** (`jira/storage_markdown.py`): headings, paragraphs, bold, italic, code, strikethrough, line
breaks, links as text links (`[text](url)`; a page link keeps its title as text), nested bullet and numbered lists, task
lists (`- [x]`), tables (first row is the header; pipes escaped), code macros as fenced blocks with their language, panel
macros as their text (the title in bold), quotes and rules. Images, attachments, placeholders and macros without text are
dropped.

Item names: `page:<id>` is the item's `external_id`, the page title its name; followed issues are `issue:<KEY>` named
`KEY: summary`.
