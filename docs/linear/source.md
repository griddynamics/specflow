---
type: Design
title: "Linear as a source"
description: "What the Linear importer reads (a project or an issue), the documents it makes, the cap, e-mail stripping, dedupe, the acting account, codes and failures"
status: draft
tags: [linear, sources, import]
---
# source.md: the Linear source importer

A **source** is one `connections` row; an **importer** reads from an app and stores what it read as `documents` that the
graph build already reads. The Linear importer is the third one of the importers in
[../sources/importers.md](../sources/importers.md) (after Confluence and the Jira epic) and shares its rules: it runs in
the API process through the importer seam (`api.imports`), as the member the project's Linear is connected by, and it
imports once (D7). Code: `backend/src/api/imports/linear_project.py`, `backend/src/api/imports/linear_access.py`,
`backend/src/linear/issue_document.py`, `backend/src/api/routers/source_linear.py`. Routes and shapes:
[api.md](api.md#source-routes).

## Acting account

The import reads as the member the project's Linear is bound to (`project_integrations`, rule R1), **never as the
caller**; the caller is only `connections.connected_by`. `project_access` (`linear_access.py`) reads the binding and the
member's connection: no binding or no connection is 409 `linear_not_connected`; a connection that is not `active` is 409
`linear_reconnect_required`. The gateway is `async with runtime.linear.gateway(user_id)`
([oauth.md](oauth.md#the-member-gateway)): the token is refreshed once on a refusal. An import that finds the connection
not `active` when it runs fails the source with `no_access`.

## Check and add

`POST /projects/{pid}/sources/linear/check` and `POST /projects/{pid}/sources/linear`, both `{link, exclude?}`, any member.

| Input | Target |
|---|---|
| a project link (`https://linear.app/<workspace>/project/<slug>-<id>`) | the project: its issues and its documents |
| an issue link (`https://linear.app/<workspace>/issue/<ID>/...`) | the issue and all its sub-issues, level by level |
| a project identifier (`P-ENG-1`) | the same as a project link, in the workspace of the member |
| an issue identifier (`ENG-12`) | the same as an issue link, in the workspace of the member |
| anything else | refused: 422 `linear_link_invalid` |
| a link of another workspace, or an object the member cannot see | refused: 409 `linear_not_found` (the link's workspace is compared with the viewer's, AC-S2-2) |

The **check** resolves the link, reads what an import would take (nothing is stored) and answers the kind, the name, the
issue count, the first issues (identifier, title, state name, parent identifier), `capped` and the number of project
documents. Linear has no count call, so the check pages through up to 200 issues (AC-S4-1); `capped` says more exist.

The **add** resolves the link again, starts the import on `runtime.imports` and answers 201 with the new source. The
source's name is the project name or the issue title; its `link` is the resolved URL; its `kind` is `linear_project` (for
an issue source too). `exclude` lists issue identifiers (`^[A-Z][A-Z0-9_]*-\d+$`, at most 200) not to import.

## What is read

| Target | Read | Cap |
|---|---|---|
| Project | its issues (pages of 100, AC-S2-3) and its documents | 200 issues; project documents are not capped |
| Issue | the issue itself, then its sub-issues level by level (children before grandchildren) | 200 sub-issues, and the issue is one more (AC-S4-2) |

- For every issue taken, its comments are read, and its parent when the parent is not in the import (read once; a parent
  the member cannot see is left out of the document).
- The cap is a count of issues. When it is reached the import goes on with the issues it has and logs
  `api.imports.linear_cap_reached` (`project_id`, `cap`). The member sees only the check's `capped` flag.
- The issue of an issue source is **never excludable**: `exclude` is applied to the issues under it only.
- Only what the member can see is read: Linear does not return an issue the member cannot see.

## Documents

Each item is stored twice, as for the other importers: the original as JSON (`original.json`) and a Markdown document
(`origin = connection`, `status = parsed`, `parser = linear_project-convert`). One Markdown document **per issue** and
**one per project document**. A project's own text (its short description and its Markdown overview, joined) is one
more document, first, under the project's id; a project with no text has none, so a project with no issues and only a
description still imports one document. External ids are `issue:<IDENTIFIER>` and `document:<document id>`.

An issue document (`issue_markdown`) holds, in this order: the heading `# <IDENTIFIER> <title>`; one line of facts
separated by `|` (state, labels, priority name, estimate, assignee; a missing fact is left out); `Parent: <parent
heading>`; `URL: <issue url>`; the description; and, with comments, `## Comments` and each comment as
`**author** (YYYY-MM-DD):` then its text. A project document holds its title as the heading and its content.
Priority names: 1 Urgent, 2 High, 3 Medium, 4 Low; priority 0 (none) is left out.

The output is deterministic (AC-S4-4): the stored text is the cleaned text and no read time is in it, so the same issue
read twice gives the same bytes.

### Personal data

`linear/issue_document.py` `clean_text` is applied to every title, description, comment, name and document, in the
Markdown **and** in the stored original:

- a mention (`<mention>` or `<user>` tag, a profile link, a profile URL, `@<uuid>`) is written as the display name, or
  `@someone` when none is known (so no user id or handle is kept);
- a `mailto:` link keeps its text only;
- every e-mail address is replaced by `[email removed]`.

An author with no name is "Someone".

## Dedupe

The writer (`api.imports.writer`) hashes the content. The same content already in the project, from any source, is a
`duplicate` item that links the first document. Adding the same link again is a **new source** (D7: the import is a
snapshot); its unchanged documents are duplicates, its changed ones are new versions, and the graph holds both until
the old source is removed (removing a source removes its documents).

## Failures

- **An item the member cannot read** (a comment read answers `not found` or `forbidden`) is one item in error
  (`no_access`, step `read`); the import goes on and the card counts it as a problem file.
- **A target that cannot be read at all**, or an expired or revoked connection, fails the source with `no_access`
  (`not authorized`, `forbidden` and `not found` all map to it). Any other Linear failure (unreachable, rate limited,
  invalid) fails it with `unknown_error` (AC24: no new failure vocabulary). The gateway does not retry a 429.
- **A restart** ends the imports in flight as `failed` (`unknown_error`); the member adds the source again.
- **Logs** carry ids, the kind, the cap and counts (`api.imports.added`, `api.imports.linear_cap_reached`,
  `api.imports.linear`). A title or a text lives in the document and its original only.
- **Live check left to owner.** The importer is tested over the fake gateway; the first import against a real workspace is
  the owner's to try.

## In the canvas

The plus of the Graph stage lists **Linear project** ("A project or issue and its issues"). It needs the project's Linear
integration, else the card says "Set up first". The form (`frontend/components/v2/canvas/LinearSourceForm.tsx`,
`linear-source-logic.ts`) has **Check link** and **Add source**. Changing the link asks for a new check. The exact wording
is not specified here.
