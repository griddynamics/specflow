# INDEX.md: table of contents of the MCP server specification

The MCP server lets a developer's coding agent (Claude Code first) read SpecFlow project knowledge over the Model Context
Protocol. It runs as its own backend role (`specflow mcp`), accepts OAuth access tokens from the organization's OIDC
provider, and shows a caller only the projects they are a member of. Version 1 has two read-only tools, `list_projects`
and `list_features`, and one prompt, `select_project`, which binds a product repository to a project through
`.specflow/config.json`.

# overview.md: purpose, scope, goals (including the workspace binding), non-goals, actors, boundaries, constraints, key terms
- [overview.md](overview.md)

# architecture.md: process, components (including guidance), package layout, import rules, transport, settings, errors, operations, decisions
- [architecture.md](architecture.md)

# auth.md: OAuth resource server, discovery, client registration, token validation, any-OIDC claim profile, revocation
- [auth.md](auth.md)

# access-control.md: project membership, tenant isolation, untrusted content (binding file and instructions), audit
- [access-control.md](access-control.md)

# tools.md: MCP tool contracts: list_projects, list_features, pagination, workspace binding (file, instructions, select_project prompt), errors
- [tools.md](tools.md)

# deployment.md: role and ingress, IdP setup, Claude Code setup and project binding, local stack with Keycloak
- [deployment.md](deployment.md)

# testing.md: test tiers, fixtures and temp workspace, scenario matrix (E1 to E17), end-to-end QA with a real client
- [testing.md](testing.md)

# requirements.md: requirement units FR-MCP-AUTH, FR-MCP-ACC, FR-MCP-TOOL, FR-MCP-BIND, FR-MCP-OPS, NFR-MCP
- [requirements.md](requirements.md)

# implementation.md: implementation log: milestone status and a quick summary per milestone or change
- [implementation.md](implementation.md)

# later.md, questions.md: deferred features, open questions
- [later.md](later.md), [questions.md](questions.md)

# Related
- [../graph/api.md](../graph/api.md#read-and-search): `GraphReader`, including the paged read `list_features` uses
- [../configuration.md](../configuration.md): the configuration reference, which lists the `MCP_*` and claim-profile
  settings
