# INDEX.md: table of contents of the project collaborators specification

A project's creator is its owner. The owner adds collaborators by email in the project's Members dialog (the V2
header's "Members"), or when creating the project; an email that never signed in waits as an invite until its first sign-in.
The owner renames the project, removes collaborators and cancels invites; a collaborator can leave. Everything else, the
integrations (a Jira target, GitHub repositories, a Slack channel) included, any member does (Mission Control V2, D6;
[integrations-api.md](../mission-control/integrations-api.md)). Removal frees the
person's story claims. Membership stays the only rule for reading and working on a project, on the web and over MCP.

# overview.md: purpose, scope, actors, non-goals, boundaries, key terms
- [overview.md](overview.md)

# data-model.md: roles, invites, email matching, invite adoption, limit, the migration (0002), row-level security
- [data-model.md](data-model.md)

# api.md: access, routes, shapes, errors, email rules, add, remove/leave and claims, rename, suggestions, Create Project, log events
- [api.md](api.md)

# ui.md: the Members dialog, the Create Project dialog, their views, errors shown, ports
- [ui.md](ui.md)

# requirements.md: requirement units FR-PRJ-ROLE, ACC, MEM, INV, CLM, NAME, SUG, CRT, UI and NFR-PRJ
- [requirements.md](requirements.md)

# questions.md: decisions D1-D33, design choices with rejected options, follow-ups
- [questions.md](questions.md)
