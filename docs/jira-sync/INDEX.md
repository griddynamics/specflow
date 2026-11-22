# INDEX.md: table of contents of the Jira sync specification

Specflow pushes a project's implementation plan into Jira on its own. The project owner sets a Jira project and,
optionally, an Epic on the project's Settings screen (tab "Jira") and turns the sync on; after every plan run that does
not fail, Specflow creates and updates one Epic, a Story per plan story and a Sub-task per task, with labels, story
points, "Blocks" links and rank. Issues still in To Do are overwritten in the fields Specflow owns; any other issue gets
one comment that lists what changed. Coding agents run the sync and read its status over MCP. One Atlassian account,
set in the environment, serves the whole deployment.

# overview.md: purpose, scope, actors, non-goals, boundaries, flow, key terms
- [overview.md](overview.md)

# sync.md: the mapping of plan items to Jira issues, owned fields, ADF layout, comment format, links, rank, status rules, failure handling
- [sync.md](sync.md)

# data-model.md: the tables jira_targets and jira_issues, the canonical content, the migration (0005), row-level security
- [data-model.md](data-model.md)

# api.md: web settings routes, access check, agent routes, shapes, error codes, log events, threats
- [api.md](api.md)

# ui.md: the Jira tab of the Settings screen, its states, copy and ports
- [ui.md](ui.md)

# requirements.md: requirement units FR-JIRA-CFG, RUN, MAP, UPD, REL, MCP, UI and NFR-JIRA
- [requirements.md](requirements.md)

# questions.md: decisions, design choices with rejected options, follow-ups
- [questions.md](questions.md)
