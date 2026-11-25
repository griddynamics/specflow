# INDEX.md: table of contents of the Notion integration specification

Each Specflow user connects their own Notion account through the deployment's public Notion connection. A project member
connects the project to Notion as themselves (`PUT /projects/{pid}/integrations/notion`,
[integrations-api.md](../mission-control/integrations-api.md)); any member then adds a Notion page or database to the
project as a source (Graph stage) and sends the plan to a Notion database (Plan stage), and every read and write uses that
member's account. The decisions behind it are N1 to N13 of the plan of this feature; each file says which it implements.

# discovery.md: Notion API facts, sandbox results, setup, plan and gap verdicts
- [discovery.md](discovery.md)

# oauth.md: the public connection, settings, the connect flow, tokens, rate limit, log events
- [oauth.md](oauth.md)

# source.md: a Notion page or database as a project source: link, check, import, conversion, failures
- [source.md](source.md)

# plan.md: the Notion plan destination: target, database schema, mapping, rules of a pass, runs, routes
- [plan.md](plan.md)

# codes.md: the closed set of `notion_*` codes, where each is used, limits, what is not verified live
- [codes.md](codes.md)
