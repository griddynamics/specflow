---
type: Design
title: "Box link source"
description: "The Box link source: accepted links, what is imported in which lane, caps, pacing, failure mapping, import once, link back to Box, and the follow-ups left out of version one"
status: draft
tags: [box, sources, import]
---
# source.md: the Box link source

A member pastes **one** Box link: a folder, a file or a shared link. **Check link** lists what an import would take; the
member may uncheck files they do not want. **Add source** imports the checked ones once (D7). The import runs in the API process through the importer seam (`api.imports`, AC5;
[../sources/importers.md](../sources/importers.md)) as the member the project's Box is connected by (`project_integrations`,
app `box`, R1), never as the caller. Any member may check and add (D6). Graphico only reads Box ([oauth.md](oauth.md)).
The source `kind` is `box`; `external_id` is a fresh uuid (adding a changed folder again is a new source); `connected_by` is
the caller.

## Links

Accepted on `app.box.com` and `<name>.app.box.com` (an enterprise domain), `https` only, no user name or password in the
address (`box.links.parse_link`):

| Link | Meaning | Resolved with |
|---|---|---|
| `/folder/<id>` | a folder | `GET /folders/{id}` |
| `/file/<id>` | a file | `GET /files/{id}` |
| `/s/<token>`, `/v/<vanity>` | a shared link to a folder or a file | `GET /shared_items` with `boxapi: shared_link=<link>` |

| Case | Answer |
|---|---|
| folder `0` (All Files) | `box_link_invalid`: import a named folder |
| a web link item (a shared link or id that names a bookmark) | `box_link_invalid` |
| another host, `http`, credentials, another path, not a link | `box_link_invalid` |
| a shared link with a password | `box_link_password` (the check refuses it; Graphico has no password field) |
| not found, or the bound member cannot read it | `box_no_access` |

Id links read as the bound member's own access. A shared link is a **bearer secret**: it lives only in the importer object for
that one run (`BoxTarget.shared_link`, hidden from `repr`) and in the `boxapi` header; it is never stored in the database,
logged or shown (B8). `connections.link` is the canonical address of the resolved item: `https://app.box.com/folder/<id>` or
`https://app.box.com/file/<id>` (`box.links.canonical_link`). The seam starts the import right after the add, so the shared
link is still in memory; a restart ends the import (`unknown_error`) and the member adds the link again.

## What is imported

The whole tree under the folder is walked once (no depth setting), breadth first, with marker paging
(`limit=1000`, `fields=` of the few fields needed). A file link imports that one file. Each file goes in one **lane** by its
extension and size:

| Lane | Files | How | Document |
|---|---|---|---|
| **Original** | PDF, DOCX, PPTX, XLSX, HTML, MD, CSV, TXT | downloaded as bytes and streamed to object storage while hashed (SHA-256); `ImportWriter.add_file` | status `processing`, then parsed by `worker-parse` like an upload (Docling). `.txt` is stored as `text/markdown`, as an upload of it is. Worker-parse gets bytes, never a Box token |
| **Box Note** | `.boxnote` | Box has no export call: the `[extracted_text]` representation (plain text), read from the `url_template` host Box names, `https://dl.boxcloud.com/api/2.0/internal_files/...`, with the token (the only host besides `api.box.com` that gets it; a polling wait is charged to the wait budget); the original is the JSON `{file, text}`; `ImportWriter.add` | status `parsed`, parser `box-convert`; text capped at 5 MiB |
| **Skipped** | everything else: legacy `.doc`, `.ppt`, `.xls`, Google files stored in Box, images, video, archives, web links, files in the trash | declared, never downloaded | the item ends `skipped`; the check counts it |

Names: the item `external_id` is `file:<box file id>`, its name is the file's **path inside the source** (`Folder/Sub/Spec.pdf`),
so two files with one name in two folders stay apart.

Size: a file larger than its upload cap by the **listed `size`** is `skipped`: PDF 100 MiB, DOCX/PPTX/XLSX 50 MiB, HTML, MD, CSV,
TXT 10 MiB (the Local files caps, [../file-parsers/formats.md](../file-parsers/formats.md)). The cap is checked again while
the bytes stream: an overflow ends the item `error` (`convert_failed`, step `download`) and drops the partial object. The
parser limits apply later as for an upload.

Duplicates: the same bytes already in the project, from any source, are a `duplicate` item that links the first document; the
new object is dropped. The document id is `uuid5(connection id, file id)` where the writer needs a stable id.

## Caps and pacing

Constants in `api/imports/box.py`:

| Constant | Value | Meaning |
|---|---|---|
| `MAX_FILES` | 1000 | importable files of one import |
| `MAX_ENTRIES` | 5000 | entries (files and folders) one walk lists |
| `DOWNLOAD_SLOTS` | 4 | downloads at a time in one import |

A hit cap stops the listing, is logged once with counts (`api.imports.box_cap_reached`: `project_id`, `cap`, `files`,
`entries`), and the check says `capped: true`. Skipped files count toward the entries but not toward `MAX_FILES`.

**The file list of the check.** `files` lists the importable files in walk order (folders breadth first), at most
`MAX_FILES`; `files_capped` is true when the walk found more importable files than are listed. Each entry is
`{id, name, path, category}`: `id` the Box file id (digits), `path` the folder names below the source root joined with `/`
(`""` in the root), `category` one of `pdf`, `word`, `powerpoint`, `excel`, `html`, `markdown`, `csv`, `text`, `note`.
`bytes` is the listed size of the listed files. The names are shown by the check and stored nowhere.

**Exclude.** The add takes `exclude`: the Box file ids of listed files the member unchecked (digits only, at most 1000, else
422 `validation_failed`; the check ignores it). An excluded file is neither declared nor downloaded nor parsed, and an id
that is not in the walk is ignored. An excluded file still counts toward `MAX_FILES`, so the import stops where the check
stopped and never takes a file the check did not show. The finished log carries the count (`excluded`), never ids or names.
Throttling and `202`: [oauth.md](oauth.md#rate-limits).

## Failure mapping (closed vocabulary, AC24)

| Situation | Where | Reason, step |
|---|---|---|
| the target cannot be read (not found, forbidden), the connection expired or is gone | source | `no_access` |
| Box is down, or throttled past the wait budget, or an answer is unusable | source | `unknown_error` |
| one file the member cannot read | item | `no_access`, `read` |
| a download that failed after its retries | item | `unknown_error`, `download` |
| a file over its cap while streaming | item | `convert_failed`, `download` |
| a Box Note whose text cannot be read (not ready, refused, a `401`, a URL outside `dl.boxcloud.com`) | item | `unknown_error`, `read` (never a source failure, never a token refresh) |
| a restart in the middle | source | `unknown_error` (the recovery of the runner) |

Check refusals are `ApiError`s with the `box_*` codes of [codes.md](codes.md). Box adds no new failure reason.

## Import once and link back

- **Once (D7, B14).** One listing pass at add time. No events stream, no webhooks, no `manage_webhook` scope. A file deleted or
  changed in Box stays as imported until the source is removed. Add the link again for a new snapshot; unchanged files are
  skipped by content hash.
- **Link back (B15).** No new column. The source node opens `connections.link`. Each item keeps `file:<id>`, from which
  `https://app.box.com/file/<id>` is built (`box.links.file_link`); Box checks the viewer's access when it opens. The shared
  link is never a link back.
- **Logs (B17).** Ids, the kind, counts and caps. Never a file name, link, token, shared-link token or e-mail address.

## In the canvas

The plus of the Graph stage, **Box link**. It needs the project's Box integration, else the card says "Set up first" and
returns with the form open. A link field, **Check link** (`Folder or file name, N files (size), M skipped, K folders`), and
the importable files listed with checkboxes (all checked; **Select all** and **Select none**; a row is the file name with its
folder; "N of M files" or "No files" once one is unchecked; when a cap was hit a note says only the first ones are listed and
imported), then **Add source**, which imports the checked files and is off while none is checked. Changing the link asks for a
new check and starts again with every file checked. A refused check or add says its closed code
in words. The node is named after the folder or file and counts files while it imports; `no_access` offers **Reconnect Box**.
Remove deletes the source and its documents.

## Follow-ups (not in version one)

- **Metadata, comments and tasks as a context document per file.** Not done (B16): comment authors' `login` is an e-mail
  address, comment text is the most sensitive Box data and a deletion in Box never reaches a stored copy under D7; inline
  multi-template `fields` is unverified. Shape when done: one `box-context` Markdown document per file next to the file,
  with `fields=metadata.<scope>.<template>`, and comments by `GET /files/{id}/comments` with the author's name only.
- **Legacy Office and Google files in Box** (`.doc`, `.ppt`, `.xls`, `.gdoc`, `.gsheet`, `.gslides`) through Box representations
  (`[pdf]` or `[markdown]`; `[markdown]` does not support the legacy formats).
- **Box AI** (`/ai/extract_structured`): spends the customer's AI Units, is not source text, needs `ai.readwrite`.
- **Sync** by events or webhooks, **Box MCP for agents**, a **folder picker**, **several links at once**.
