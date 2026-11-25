# INDEX.md: table of contents of the GitHub integration specification

Each Specflow user connects their own GitHub account through the deployment's GitHub App. A project member connects
the project to GitHub as themselves (`PUT /projects/{pid}/integrations/github`,
[integrations-api.md](../mission-control/integrations-api.md)); any member then adds repositories that this account can
read to the project (Settings, tab GitHub), and every clone uses that member's account. The `worker-agents` service clones each
repository and keeps a snapshot of it in object storage. A later feature reads the snapshots (knowledge build, agent
tools); this one stops at "the repository is clonable".

# oauth.md: the GitHub App, settings, connect and install flows, user access tokens, codes, log events
- [oauth.md](oauth.md)

# data-model.md: the tables github_connections and project_repositories, the migration (0007), row-level security
- [data-model.md](data-model.md)

# api.md: the connection routes, the project repository routes, shapes and error codes
- [api.md](api.md)

# clone.md: the clone check on the agents worker, the snapshot and its object keys
- [clone.md](clone.md)

# projects.md: the GitHub Projects plan destination: target, mapping, rules of a pass, permissions, codes
- [projects.md](projects.md)
