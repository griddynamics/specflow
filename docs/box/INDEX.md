# INDEX.md: table of contents of the Box integration specification

Each Graphico user connects their own Box account through the deployment's Box app (OAuth 2.0, one app per deployment).
A project member connects the project to Box as themselves (`PUT /projects/{pid}/integrations/box`,
[integrations-api.md](../mission-control/integrations-api.md)); any member then adds one Box folder, file or shared link to the
project as a source (Graph stage, plus, **Box link**), and the import reads as that member. Graphico only reads Box. The files
become documents that the graph build already reads (D18, nothing in the build changed). Import once (D7): there is no sync.

# discovery.md: facts about Box (scopes, tokens, rate limits, content API, Box Notes, limits) with sources, dated 2026-11-25, and the open points
- [discovery.md](discovery.md)

# oauth.md: the Box app setup, scopes, admin enablement, settings, connect flow, tokens and their rotation, revoke, rate limits, log events
- [oauth.md](oauth.md)

# source.md: the Box link source: link shapes, the three lanes, caps, failure mapping, import once, link back, follow-ups
- [source.md](source.md)

# codes.md: every `box_*` code of the API, the inside-only codes and the closed failure reasons
- [codes.md](codes.md)
