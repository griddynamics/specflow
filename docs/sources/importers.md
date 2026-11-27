---
type: Design
title: "Importers: Confluence links, Jira epics, Box links and diagrams.net links"
description: "The sources the API reads itself: what a Confluence link, a Jira epic, a Box link or a diagrams.net link imports, the caps, the acting account, the Markdown conversion, how a changed page is added again, codes and failure reasons"
status: draft
tags: [sources, confluence, jira, box, drawio, import]
---
# importers.md: sources the API imports itself

A **source** is one `connections` row (the Sources status shows it as a card); an **importer** reads from an app and stores
what it read as `documents` that the graph build already reads (nothing in the build changed). Google Drive and Local
files have their own paths ([../projects/](../projects/)); the importers here run inside the API process through the
importer seam of [../mission-control/integrations-api.md](../mission-control/integrations-api.md) (`api.imports`, AC5).

Rules shared by every importer:

- **Acting account.** (Confluence and Jira; the diagrams.net link needs no Atlassian account, see its section.) The import reads as the member the project's Atlassian is connected by (`project_integrations`, R1),
  never as the caller. The caller is only `connections.connected_by` (who added the source). No new Atlassian scope is
  used: `read:page`, `read:hierarchical-content`, `read:space` (Confluence) and `read:jira-work` (Jira) were granted already
  ([../jira-sync/oauth.md](../jira-sync/oauth.md)). The site is the deployment's one site (`ATLASSIAN_SITE_URL`); a link
  on another host is `atlassian_site_mismatch`.
- **One source per link.** `kind` is `confluence`, `jira_epic`, `box` or `drawio` (the Box link follows the same rules with Box's account and
  its own parts, see "Box link" below; the diagrams.net link needs no account, see "diagrams.net link"); `external_id` is a fresh uuid, `link` the link as pasted.
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
  answer that is not JSON) fails it with `unknown_error` (AC24: no new failure vocabulary). A page or issue nested too deep to
  convert (about 100 levels or more) is one item in error (`convert_failed`, step `convert`), and the import goes on.
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

**In the canvas** (the plus of the Graph stage, "Confluence link"; it needs the project's Atlassian integration, else the card says "Set up first" and returns with the form open): a link field and "Follow links" (on), **Check link** (counts the pages an import would take, and with follow the Jira issues the first page links to), then **Add source**. Changing the link or the choice asks for a new check. A refused check or add says its closed code in words (another site, a short link, not found, not allowed, Atlassian not connected or in need of a reconnect). The node is named after the page or space and counts pages while it imports; it stops only when the API reports the import failed (`no_access` offers Reconnect Atlassian). Remove deletes the source and its documents; to read a changed page, add the link again.

| Link | Imports |
|---|---|
| a page URL (`/wiki/spaces/<KEY>/pages/<id>/<title>`, `/wiki/pages/viewpage.action?pageId=<id>`) | the page and every page below it (descendants; folders, databases and whiteboards are left out) |
| a space URL (`/wiki/spaces/<KEY>`, `.../overview`) | the current pages of the space |
| a link that is not `https`, or has credentials in it (`https://user@host/...`) | refused: `confluence_link_invalid` (a Jira key or link the same: `jira_epic_link_invalid`); a host that is not the site's, lookalikes included, is `atlassian_site_mismatch` |
| a short link (`/wiki/x/<tiny id>`) | refused: `confluence_short_link_unsupported`. Confluence documents no tiny-id algorithm to decode locally, and the call that resolves one (the v1 content API) needs a scope the site admin did not grant; paste the full page address |

Reads (Confluence Cloud REST v2, `https://api.atlassian.com/ex/confluence/{cloud id}`): `GET /pages/{id}?body-format=storage`,
`GET /pages/{id}/descendants`, `GET /spaces?keys=`, `GET /spaces/{id}/pages`, `GET /pages?space-id=&title=`; results follow
`_links.next` only while it is a path of the site (starts with one `/`); an absolute or protocol-relative link ends the paging, so
the bearer token is never sent to another host.

**Caps** (constants in `api/imports/confluence.py`): `MAX_PAGES = 100` pages per import (the target, its descendants and the
followed pages together) and `MAX_LINKED_ISSUES = 50` Jira issues. A cap that is hit stops the listing there, is logged once
as a count (`api.imports.page_cap_reached`, `api.imports.issue_cap_reached`) and the check says `capped: true`.

**Follow links** (`follow: true`, AC11). From every imported page: same-site page links (`/wiki/spaces/<KEY>/pages/<id>`,
`viewpage.action?pageId=`, and `ac:link` to a page by title, in its space key or the space of the page it is on) are read
**one hop**: the links of a followed page are not followed again. Jira issues linked by `/browse/<KEY>` links or the Jira
macro become documents too (the issue with its comments, as in the Jira epic importer below). Links to other sites are
ignored; a link that does not resolve or that the member cannot see is skipped.

**Storage format to Markdown** (`jira/storage_markdown.py`): headings, paragraphs, bold, italic, code, strikethrough, line
breaks, links as text links (`[text](url)`; a page link keeps its title as text; only `http`, `https` and `mailto` addresses are
linked, with spaces and parentheses percent-encoded and `]` in the text escaped, any other address leaves the text alone), nested bullet and numbered lists, task
lists (`- [x]`), tables (first row is the header; pipes escaped), code macros as fenced blocks with their language (the fence is longer than any run of backticks in the code), panel
macros as their text (the title in bold), quotes and rules. Images, attachments, placeholders and macros without text are
dropped.

Item names: `page:<id>` is the item's `external_id`, the page title its name; followed issues are `issue:<KEY>` named
`KEY: summary`.

## Jira epic

`POST /projects/{pid}/sources/jira-epic/check` and `POST /projects/{pid}/sources/jira-epic`, both `{key_or_link}`, any
member. The check also answers `children` (`{key, summary, type, status}` of the issues under the epic, in key order, at
most the cap, read with a search of those three fields only) and `capped`; the add takes an optional `exclude` (issue keys
such as `ABC-12`, at most 200, else 422 `validation_failed`): the epic is always imported, and every child whose key is
listed is neither declared nor imported (keys that are not children are ignored; the log carries only their count). Shapes and codes: [integrations-api.md](../mission-control/integrations-api.md) "Jira epic source".

**In the canvas** (the plus of the Graph stage, "Jira epic"; it needs the project's Atlassian integration, else the card says "Set up first" and returns with the form open): one field for the epic key or link, **Check epic** (says `KEY · summary · N issues`, and over the cap that the first 200 are read), then **Add source**. Changing the key or link asks for a new check. A refused check or add says its closed code in words (another site, not an epic key, not an epic, no such issue, not allowed, Atlassian not connected or in need of a reconnect). The node is named by the key and counts issues while it imports; it stops only when the API reports the import failed (`no_access` offers Reconnect Atlassian). Remove deletes the source and its documents.

`key_or_link` is an issue key (`ABC-12`, any case) or a link of the configured site (`/browse/ABC-12`, or a board link with
`selectedIssue=ABC-12`). The issue must be an epic (`issuetype.hierarchyLevel` 1, or named `Epic`), else
`jira_epic_not_an_epic`. The source is named `KEY: summary` and its `link` is `https://<site>/browse/KEY`.

Reads (Jira Cloud REST v3, `https://api.atlassian.com/ex/jira/{cloud id}`): `GET /issue/{key}`, `POST /search/jql` with
`parent = "<KEY>" ORDER BY key` (paged by `nextPageToken`, fields summary, description, status, issuetype, labels and
comment), `POST /search/approximate-count` (the check), `GET /issue/{key}/comment` (only for an issue whose comments
did not all come with the search). One document per issue, the epic first: `issue:<KEY>` named `KEY: summary`.

**Cap**: `MAX_EPIC_ISSUES = 200` issues under the epic (`api/imports/jira_epic.py`), logged once as a count
(`api.imports.epic_cap_reached`). Issues the bound member cannot see are not returned by Jira; one whose comments are
refused while it is read is skipped and counted (`no_access`, step `read`). Sub-tasks are issues under a story, not under
the epic: they are not read.

**Issue to Markdown** (`jira/issue_document.py`): `# KEY: summary`, a line `Type: ... | Status: ... | Labels: ...`, the
description, then `## Comments` with one `**Author** (date):` block per comment (all comments, paged). The description and
every comment are Atlassian Document Format; `jira/adf_reader.py` turns them into Markdown: paragraphs, headings, bold,
italic, code, strikethrough, links, line breaks, bullet, ordered and task lists, tables, code blocks, quotes and panels
(as quotes), expands (title in bold), rules, mentions as names, emoji, dates, statuses, cards as links and media as
`[attachment]`. A node it does not know renders the text of its children. The Confluence "follow links" import of a linked
issue uses the same reader and document.

## Box link

`POST /projects/{pid}/sources/box/check` and `POST /projects/{pid}/sources/box`, both `{link}`, any member. The import reads as the
member the project's Box is connected by (R1), with that member's own Box access (scope `root_readwrite`, which Box needs to
download; Graphico only reads). One link per source: a folder (the whole tree below it), a file, or a shared link
(`app.box.com/folder/<id>`, `/file/<id>`, `/s/<token>`, `/v/<vanity>`; also `<name>.app.box.com`). The check answers `kind`
(`folder` or `file`), `name`, `files` (importable, at most 1000), `skipped` (other types), `folders` and `capped`.

Two things differ from the Atlassian importers. The files are binary: PDF, DOCX, PPTX, XLSX, HTML, MD, CSV and TXT are
downloaded and stored as originals (`ImportWriter.add_file`) and then parsed by `worker-parse` like an upload, so a Box file
is `processing` until the parser finishes. A Box Note (`.boxnote`) is read as plain text through its `[extracted_text]`
representation and stored `parsed` (parser `box-convert`). Everything else is declared and ends `skipped`. And a **shared link
is never stored**: `connections.link` is the canonical `https://app.box.com/folder/<id>` or `/file/<id>` of the resolved item.

Caps: `MAX_FILES = 1000` importable files, `MAX_ENTRIES = 5000` walked entries (`api/imports/box.py`), cap events
`api.imports.box_cap_reached`; a file over its upload cap (PDF 100 MiB, OOXML 50 MiB, text 10 MiB) is `skipped`. Item names are
`file:<id>`, named by the path inside the source. Failure reasons are the closed set above (no new reason). Link shapes, lanes,
failure mapping, link back and follow-ups: [../box/source.md](../box/source.md); codes: [../box/codes.md](../box/codes.md); the
connection: [../box/oauth.md](../box/oauth.md).

## diagrams.net link

`POST /projects/{pid}/sources/drawio/check` and `POST /projects/{pid}/sources/drawio`, both `{link}`, any member. Source
`kind` `drawio`. There is no integration, no acting account and no key: the request itself reads the link, so the
caller is only `connections.connected_by`. Shapes and codes: [integrations-api.md](../mission-control/integrations-api.md) "diagrams.net source".

**In the canvas** (the plus of the Graph stage, "diagrams.net"): one field for the link and a "Show me how" with three steps (File → Publish → Link in diagrams.net, copy the link, paste it here), **Check link** (says `name · N pages · N shapes`, and over the cap that only the first 50 pages are read), then **Add source**. Changing the link asks for a new check. A refused check or add says its closed code in words; a diagram stored in Google Drive, GitHub and the like says where and to download it as `.drawio` and add it with Upload. Remove deletes the source and its documents.

| Link | Imports |
|---|---|
| `https://app.diagrams.net/#R<payload>` (also `viewer.diagrams.net`, `embed.diagrams.net`, `www.draw.io`, `app.draw.io`) | the diagram held in the link; nothing is fetched |
| `...#P{json}` with a `data` member | the diagram XML in `data`; nothing is fetched |
| `...#U<url>`, or the older `?url=<url>` | the file at that public `https` URL, fetched |
| a bare `https` URL whose path ends `.drawio` (any host, no fragment) | the file at that URL, fetched |
| a storage hash (`#G` Google Drive, `#H` GitHub, `#D` Dropbox (retired by diagrams.net, still named), `#W` OneDrive, `#A` GitLab, `#L` browser storage, `#T` Trello, any other letter) | refused: `drawio_link_unsupported` (names the storage when known) |
| not `https`, credentials in the link, a port other than 443, another host, an empty or malformed fragment | refused: `drawio_link_invalid` |
| a link over 256 KiB | refused: `drawio_too_large` |
| a link over 512 KiB (twice that) | refused by validation (422 `validation_failed`) before anything reads it |
| a `#P` fragment nested too deep to parse | refused: `drawio_link_invalid` |

**What is fetched, and how safely** (`integrations/safe_fetch.py`, only for `#U`, `?url=` and `.drawio` URLs): `https` on port 443 only, no credentials. The host is resolved and the fetch is refused (`blocked_address`) when any resolved address is loopback, private, link-local, reserved, multicast, carrier-grade NAT (`100.64.0.0/10`) or a cloud metadata address, IPv4 forms embedded in IPv6 included. The connection goes to the checked IP with the `Host` header and the host's SNI. At most 3 redirects, each resolved and checked again. No proxy variables, no cookies, no auth, a client per hop. A `Content-Type` other than XML, text, octet-stream, SVG, PNG or `application/vnd.jgraph.mxfile` (or none) is refused. The request sends `Accept-Encoding: identity` and a response with any other `Content-Encoding` is refused (`bad_encoding`, so `drawio_fetch_failed`): no body is inflated on the way in. The body is streamed raw and cut at 10 MiB of wire bytes; the whole fetch has 15 seconds. At most 4 public fetches run at once in a process, the others wait inside their 15 seconds. Every failure is a closed code with no URL in its message; the request maps them: a bad URL to `drawio_link_invalid`, over the cap to `drawio_too_large`, all others to `drawio_fetch_failed`. The kind of the bytes is read from the content (PNG, SVG, else XML), not from the URL.

**Caps** (constants in `file_parsers/diagrams/model.py`, not settings): 10 MiB for the file, the fetched body or the decoded link payload; 20 MiB for the XML after inflating; `MAX_PAGES = 50` pages and `MAX_CELLS_PER_PAGE = 5000` cells per page (document order; an `<object>` or `<UserObject>` wrapper counts once, with its `mxCell`, and a wrapper over the cap is dropped whole). Pages or cells over the cap are dropped and the check says `capped: true`; over the byte caps is `drawio_too_large`. The Markdown of a page is capped at `MAX_MARKDOWN_BYTES = 2 MiB` and that of an uploaded file at `MAX_MARKDOWN_FILE_BYTES = 10 MiB`; over it is `too_large` (a link import ends `unknown_error`, an upload is refused as too large). Input with a DTD or entity declaration (only the outer `<svg>` of a `.drawio.svg` may carry one DOCTYPE without an internal subset, found by a bounded scan; a second DOCTYPE or any ENTITY is refused), bad base64 or deflate, malformed XML, or no `mxfile` or `mxGraphModel` is `drawio_not_a_diagram`.

**One document per page.** Item `page:<page id>` (a repeated id gets `#<position>`), named `<diagram name> · <page name>`; the diagram name is the `title` parameter of the link (URL-decoded, a `.drawio`, `.xml`, `.svg` or `.png` suffix removed), else the file name of the fetched URL (same suffix rule), else `diagrams.net diagram`; one helper gives it to the source, the check answer and the item names. An uploaded or Drive file has no name in the converter, so its Markdown starts `# Diagram`. The source `link` is the viewer link without its fragment for `#R` and `#P`, and only `scheme://host/path` of the fetched URL for `#U`, `?url=` and a bare `.drawio` URL (no query, no fragment: either may carry a token). The original is `original.drawio` (`application/vnd.jgraph.mxfile`): a single-page mxfile of that page, so every page has its own content hash. `parser = drawio-convert`.

**Markdown mapping** (`file_parsers/diagrams/markdown.py`; the same text for a link page and an uploaded file, apart from the heading):

- `# <diagram name> · <page name>`, then `Source: diagrams.net · page <n> of <m>`.
- `## Shapes`: one `- **<label>** (<role>)` per labelled shape, in order of `y`, then `x`; text after ` · ` (a line break in the label) follows as `: <text>`; custom properties of the shape are nested `- key: value` lines. Roles: `decision` (rhombus, `mxgraph.flowchart.decision`), `store` (cylinder, data store, `mxgraph.flowchart.database`, `mxgraph.flowchart.stored_data`), `document` (document, note, `mxgraph.flowchart.document`, `mxgraph.flowchart.multi-document`), `actor`, `group` (swimlane, container or a parent of another shape), else `step`. A vertex without a label is not a shape.
- A group is a `### Group: <title>` section holding its members; the title names the group and its nearest two ancestors, `… › A › B › C` (the `…` when the path is longer). Every label, property and title is cut at 300 characters (`…`).
- A page with several layers has `### Layer: <name>` sections.
- `## Arrows`: `- <source> → <target>` with `: <label>` when it has one; an end that is missing or unlabelled reads `(unlabelled shape)`. An end whose label is not unique on the page is told apart by its nearest group, `Review (in Design)`, and when that is still not unique (or it has no group) by its place in the page order, `Review #2`.
- `## Notes`: text shapes. Empty sections are left out. Labels are HTML-stripped and escaped, so nothing in a label becomes a heading, list, link or code; a `link` property stays text.

**Privacy.** The fragment of a `#R` or `#P` link holds the diagram, so it is not stored (`connections.link` keeps the link without it) and no link, label or host is logged: events carry ids, the form and counts. **Import once** (D7): the diagram is read once at the add; a changed diagram is added again as a new source (see the shared rules). The import gets the pages decoded by the request, so a failure after the add can only end the source as `unknown_error`; every other refusal happens in the request, before any source exists.

Uploads of `.drawio`, `.drawio.svg` and `.drawio.png` files and Drive files with those names use the same conversion (one document per file): [../file-parsers/formats.md](../file-parsers/formats.md).
