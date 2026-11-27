# INDEX.md: table of contents of the Dropbox integration specification

Each Graphico user connects their own Dropbox account through the deployment's Dropbox app (OAuth 2 code flow, offline
access, four read scopes, one app per deployment). A project member connects the project to Dropbox as themselves
(`PUT /projects/{pid}/integrations/dropbox`, [integrations-api.md](../mission-control/integrations-api.md)); any member then
adds one Dropbox folder or file link to the project as a source (Graph stage, plus, **Dropbox link**), unchecks the files
not to import, and the import reads as that member. Graphico only reads Dropbox. The files become documents that the graph
build already reads (D18, nothing in the build changed). Import once (D7): there is no sync. The plan with decisions DB1 to
DB20: `plans/DROPBOX/DROPBOX-PLAN.md`.

# discovery.md: facts about Dropbox (app console, OAuth, scopes, endpoints, shared links, team spaces, rate limits) with sources, dated 2026-11-25, the live exploration and the open points
- [discovery.md](discovery.md)

# oauth.md: the Dropbox app setup, scopes, settings, connect flow, tokens without rotation, revoke, the endpoint allowlist, rate limits, routes, data, log events
- [oauth.md](oauth.md)

# source.md: the Dropbox link source: link shapes, the path rule, Path-Root, lanes, Paper, caps, exclude, re-add skip, failure mapping, link back, follow-ups
- [source.md](source.md)

# codes.md: every `dropbox_*` code of the API, the inside-only codes and the closed failure reasons
- [codes.md](codes.md)
