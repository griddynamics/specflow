# INDEX.md: table of contents of the Notion integration specification

Each Specflow user connects their own Notion account through the deployment's public Notion connection. A project member
connects the project to Notion as themselves (`PUT /projects/{pid}/integrations/notion`,
[integrations-api.md](../mission-control/integrations-api.md)); any member then adds a Notion page or database to the
project as a source (Graph stage) and sends the plan to a Notion database (Plan stage), and every read and write uses that
member's account. The decisions behind it are N1 to N13 in the table below; each file says which it implements.

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

## Decisions

| # | Decision |
|---|---|
| N1 | OAuth only: one public connection, one connection per user, the refresh token encrypted; no internal-token mode in the product |
| N2 | A source is a pasted link to a page or database, with Check link, then Add source; no page picker |
| N3 | Pages are read as Markdown, never as a block tree; sub-pages and database rows are followed; a row is a properties table plus its body |
| N4 | One import takes at most 100 pages and rows together; depth is bounded by that cap and a visited set |
| N5 | The stored original has e-mail addresses removed from user mentions; the converted Markdown goes to the document |
| N6 | Import failure reasons stay the closed set of sources; only the check route shows `notion_*` codes |
| N7 | The plan is one database per project, one row per story and per live task, built by the same mapping as the Jira and GitHub Projects exports |
| N8 | A pass overwrites owned fields only while the row's Status is in the to-do group; otherwise it adds one comment; a removed item is marked `Removed`, never deleted |
| N9 | A pass spends at most 240 requests on rows; when rows are left, another pass follows |
| N10 | One request limiter per Notion connection (`bot_id`), shared by import and export; `Retry-After` honoured; a pass or import waits at most 60 s in all |
| N11 | The plan form takes a parent page link; the first pass creates the database under it and stores its ids |
| N12 | One migration, `0012_notion`, adds the three Notion tables and widens the two check constraints |
| N13 | The per-app table of Mission Control lists Notion as a source and as a plan destination |
