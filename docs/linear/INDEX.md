# INDEX.md: table of contents of the Linear integration specification

Linear has three roles in Graphico, the same three as Jira: a **source** (Graph zone), a **gap channel** (Gap zone) and a
**plan destination** (Plan zone). Each member connects their own Linear account through the deployment's Linear OAuth app
(OAuth 2.0, `actor=user`). A project member then binds the project to Linear as themselves
(`PUT /projects/{pid}/integrations/linear`, [integrations-api.md](../mission-control/integrations-api.md)). Every read and
every write of the three roles is made as that member, never as the caller and never as a bot.

There is one Linear OAuth app per deployment or environment (local, each shared environment): a Linear app has one
webhook URL. The app is **public**, so members of other Linear workspaces (not only the workspace that owns the app) can
connect. Linear stays off until `LINEAR_OAUTH_CLIENT_ID` is set. The api is the only role that reads the Linear settings.

| Role | What it does | Document |
|---|---|---|
| Source | Imports a Linear project (its issues and documents) or one issue (with its sub-issues) as Markdown documents | [source.md](source.md) |
| Gap channel | Posts the open gap questions as sub-issues of one issue; answers come back as comments, by signed webhook | [gaps.md](gaps.md) |
| Plan destination | Exports the plan as issues, sub-issues, labels, milestones and relations | [plan.md](plan.md) |

# oauth.md: the Linear OAuth app, settings, connect flow, tokens, the member gateway, codes, log events
- [oauth.md](oauth.md)

# data-model.md: the five tables, the migration (0011), row-level security, the widened app and source kinds
- [data-model.md](data-model.md)

# api.md: the connection routes, the role routes, the webhook, the MCP tools, shapes and error codes
- [api.md](api.md)

# plan.md: the Linear plan destination: target, mapping, rules of a pass, status mirror, permissions, codes
- [plan.md](plan.md)

# source.md: the Linear source importer: check, add, what is read, documents, dedupe, failures
- [source.md](source.md)

# gaps.md: the Linear gap channel and its signed webhook
- [gaps.md](gaps.md)
