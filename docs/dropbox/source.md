---
type: Design
title: "Dropbox link source"
description: "The Dropbox link source: accepted links, the path rule, Path-Root, what is imported in which lane, Paper, caps, exclude, the re-add skip, pacing, failure mapping, import once, link back to Dropbox, and the follow-ups left out of version one"
status: draft
tags: [dropbox, sources, import]
---
# source.md: the Dropbox link source

A member pastes **one** Dropbox link: a folder or a file. **Check link** lists what an import would take; the member may uncheck
files; **Add source** imports the rest once (D7). The import runs in the API process through the importer seam (`api.imports`,
AC5; [../sources/importers.md](../sources/importers.md)) as the member the project's Dropbox is connected by
(`project_integrations`, app `dropbox`, R1), never as the caller. Any member may check and add (D6). Graphico only reads Dropbox
([oauth.md](oauth.md)). The source `kind` is `dropbox`; `external_id` is a fresh uuid (Dropbox ids `id:...` do not pass
`ck_connections_external_id`; adding a changed folder again is a new source); `connected_by` is the caller.

## Links

Accepted on `www.dropbox.com` and `dropbox.com`, `https` only, no user name, password or port in the address
(`dropbox.links.parse_link`, DB7):

| Link | Meaning | Resolved with |
|---|---|---|
| `/scl/fo/<id>/<key>?rlkey=...` | a shared link to a folder | `sharing/get_shared_link_metadata` `{url}` |
| `/scl/fi/<id>/<name>?rlkey=...` | a shared link to a file | `sharing/get_shared_link_metadata` `{url}` |
| `/s/<key>/<name>`, `/sh/<key>/<id>` | legacy shared links | `sharing/get_shared_link_metadata` `{url}` |
| `/home/<path>` | a folder or file of the member's own Dropbox (the address bar of the web app) | `files/get_metadata` `{path}` |
| `/home/<path>?preview=<file>` | the file `<path>/<file>` (the address bar while a file is open in its folder) | `files/get_metadata` `{path}` |

The shared link keeps its query (`rlkey` is needed to read it) without `dl`, `st` and the fragment. A web path is URL-decoded;
a `.` or `..` segment, or a `/` inside the `preview` name, is `dropbox_link_invalid` (no path traversal).

**The path rule.** The target must be in the bound member's Dropbox: the metadata must carry `path_lower` (Dropbox sends it only
when the caller has the item in their Dropbox; a link of someone else's folder has none). The whole import then reads by that
path. A link of a folder that is not in the member's Dropbox is `dropbox_no_access` with "add it to that Dropbox first" (a
non-recursive shared-link walk is a follow-up).

| Case | Answer |
|---|---|
| another host, `http`, credentials, a port, another path, not a link | `dropbox_link_invalid` |
| `/home` alone (the Dropbox root) | `dropbox_link_invalid`: import a named folder |
| a password-protected shared link (`shared_link_access_denied`) | `dropbox_link_invalid` (Graphico has no password field) |
| a link type Dropbox does not resolve (`409 unsupported_link_type`) | `dropbox_link_invalid` |
| not found, not readable, refused or unsupported, or not in the member's Dropbox (no `path_lower`) | `dropbox_no_access` |
| the listing cursor expired while the check read the pages (`409 reset`) | `dropbox_unreachable` (try again) |

A refusal about the target is never `dropbox_auth_failed`: that code is kept for a setup fault of the server.

A shared link is a **bearer secret**: it lives only in the importer object for that one run (`DropboxTarget.shared_link`, hidden
from `repr`); it is never stored in the database, logged or shown (DB8). `connections.link` is
`https://www.dropbox.com/home<path_display>` of the resolved item, URL-encoded (`dropbox.links.canonical_link`); it opens only
for people who can see the item. A restart ends the import (`unknown_error`) and the member adds the link again.

**Team spaces (DB6).** Every call sends `Dropbox-API-Path-Root: {".tag":"root","root":<root_namespace_id>}`, read from
`users/get_current_account` once per check and once per import (not stored). A `422 invalid_root` reads it again once. On a
personal account the root is the home namespace, so the header is harmless. Not testable live with the owner's `basic` account:
the tests use the documented shapes and the live check on a Business account is listed for W9.

## What is imported

One recursive `files/list_folder` by path (`recursive=true`, `include_non_downloadable_files=true`, `limit=2000`) and its
`files/list_folder/continue` pages (DB9). A file link imports that one file. Each file goes in one **lane** by its extension and
listed size; the lane table and the size caps are shared with Box (`api/imports/files.py`, DB18):

| Lane | Files | How | Document |
|---|---|---|---|
| **Original** | PDF, DOCX, PPTX, XLSX, HTML, MD, CSV, TXT | `files/download` by `id`, streamed to object storage while hashed (SHA-256); `ImportWriter.add_file` | status `processing`, then parsed by `worker-parse` like an upload. Dropbox always answers `application/octet-stream`, so the format comes from the extension and the parse guard checks the magic bytes. `.txt` is stored as `text/markdown` |
| **Paper** | `.paper` with `is_downloadable=false` and `markdown` in `export_info.export_options` | `files/export` with `export_format=markdown`, capped at 5 MiB; `ImportWriter.add` (original = the Markdown, `text/markdown`) | status `parsed`, parser `dropbox-convert`. **Gate (DB10):** the shapes come from a live recording (task L0); without one before W9, Paper is `skipped` and the pull request says so. A Paper doc without `markdown` in its export options is `skipped`; an export refused or unusable is `convert_failed`/`convert`. Legacy `/paper/*` docs are skipped |
| **Skipped** | everything else: images, video, archives, legacy `.doc`/`.ppt`/`.xls`, `.gdoc`/`.gsheet`/`.gslides`, other non-downloadable files, a file over its cap | declared, never downloaded | the item ends `skipped`; the check counts it |

Names: the item `external_id` is the Dropbox file id `id:<...>` (stable), its name is the file's **path inside the source**
(`Sub/Spec.pdf`). Per item the existing columns keep `size_bytes` and `modified_time` (`server_modified`) for the re-add skip and a
later sync diff (DB15).

Size: a file larger than its upload cap by the listed `size` is `skipped`: PDF 100 MiB, DOCX/PPTX/XLSX 50 MiB, HTML, MD, CSV,
TXT 10 MiB. The cap is held again while the bytes stream: an overflow ends the item `convert_failed`/`download`.

**Exclude.** The add takes `exclude`: the ids of listed files the member unchecked (at most 1000). They are not declared and not
imported, but they still count toward `MAX_FILES` and `MAX_BYTES`: the import stops where the check stopped, so it takes at most
the files the check showed. An id that is not in the listing is ignored.

**Duplicates (DB12).** Before a download, an entry whose `id`, `server_modified` and size match an item of this project that
still links a document is linked as `duplicate` without a download (one query, `db.imports.find_stored_document`). The plain
SHA-256 on insert stays the final dedup: the same bytes already in the project, from any source, are a `duplicate`.

## Caps and pacing

Constants in `api/imports/dropbox.py` (DB11):

| Constant | Value | Meaning |
|---|---|---|
| `MAX_FILES` | 1000 | importable files of one import (and files the check lists) |
| `MAX_ENTRIES` | 5000 | entries (files and folders) one listing takes |
| `MAX_BYTES` | 2 GiB | importable files of one import, by listed size |
| `DOWNLOAD_SLOTS` | 4 | downloads at a time in one import |

A hit cap stops the listing, is logged once with counts (`api.imports.dropbox_cap_reached`: `project_id` or `user_id`, `cap`,
`files`, `entries`, `bytes`), and the check says `capped: true`. Throttling: [oauth.md](oauth.md#rate-limits); one `WaitBudget`
per import (DB14).

## Failure mapping (closed vocabulary, AC24)

| Situation | Where | Reason, step |
|---|---|---|
| the target cannot be read, is not in the member's Dropbox, the connection expired or is gone, a refresh was refused | source | `no_access` (the node offers Reconnect Dropbox) |
| Dropbox is down, or throttled past the wait budget (the one budget of the import), or an answer is unusable | source | `unknown_error`; unfinished items `import_failed` |
| one file answers `409` `not_found` or `restricted_content` | item | `no_access`, `read` |
| a download that failed after its retries (5xx, network) | item | `unknown_error`, `download` |
| a file over its cap while streaming | item | `convert_failed`, `download` |
| a Paper export refused or unusable | item | `convert_failed`, `convert` |
| a restart in the middle | source | `unknown_error` (the recovery of the runner) |

Check refusals are `ApiError`s with the `dropbox_*` codes of [codes.md](codes.md). Dropbox adds no new failure reason.

## Import once and link back

- **Once (D7, DB15).** One listing pass at add time. No cursor, longpoll or webhook is stored (cursors expire, `409 reset`). A
  file deleted or changed in Dropbox stays as imported until the source is removed. Add the link again for a new snapshot;
  unchanged files are linked without a download (DB12).
- **Link back (DB16).** No new column. The source node opens `connections.link`. Each item keeps `id:` and its relative path as
  `name`, shown as `<source>/<path>`. No per-file shared links (they would need a write scope and are public).
- **Logs (DB17).** Ids, the kind, counts, caps and codes. Never a file name, path, link, token or e-mail address.

## In the canvas

The plus of the Graph stage, **Dropbox link**. It needs the project's Dropbox integration, else the card says "Set up first" and
returns with the form open. A link field, **Check link** (`Folder name, N files (size), M skipped`, and when capped that the
first files are read) with the importable files listed with checkboxes (all checked; unchecking leaves a file out, as in the
Drive dialog), then **Add source**. Changing the link asks for a new check. A refused check or add says its closed code in
words. The node is named after the folder or file and counts files while it imports; `no_access` offers **Reconnect Dropbox**.
Remove deletes the source and its documents.

## Follow-ups (not in version one)

- **Sync** (cursor, `list_folder/longpoll`, webhooks).
- **Links of folders not in the member's Dropbox** (a non-recursive shared-link walk with `files/list_folder` + `shared_link`,
  and a second download path through `sharing/get_shared_link_file`).
- A **folder picker**, previews and thumbnails, legacy Office and `.gdoc`/`.gsheet` conversions (`files/get_preview` gives PDF
  or HTML for some of them), team-linked (admin-installed) apps, the production Dropbox app and its deploy values.
