# INDEX.md: table of contents of the MCP server specification

The MCP server lets a developer's coding agent (Claude Code first) read SpecFlow project knowledge over the Model Context
Protocol. It runs as its own backend role (`specflow mcp`), accepts OAuth access tokens from the organization's OIDC
provider, and shows a caller only the projects they are a member of. Version 1 has two read-only tools: `list_projects`
and `list_features`.

# overview.md: purpose, scope, goals, non-goals, actors, boundaries, constraints, key terms
- [overview.md](overview.md)

# architecture.md: process, components, package layout, import rules, transport, settings, errors, operations
- [architecture.md](architecture.md)

# auth.md: OAuth resource server, discovery, client registration, token validation, any-OIDC claim profile, revocation
- [auth.md](auth.md)

# access-control.md: project membership, tenant isolation, untrusted content, audit
- [access-control.md](access-control.md)

# tools.md: MCP tool contracts: list_projects, list_features, pagination, errors
- [tools.md](tools.md)

# deployment.md: role and ingress, IdP setup, Claude Code setup, local stack with Keycloak
- [deployment.md](deployment.md)

# testing.md: test tiers, fixtures, scenario matrix, end-to-end QA with a real client
- [testing.md](testing.md)

# requirements.md: requirement units FR-MCP-AUTH, FR-MCP-ACC, FR-MCP-TOOL, FR-MCP-OPS, NFR-MCP
- [requirements.md](requirements.md)

# later.md, questions.md: deferred features, open questions
- [later.md](later.md), [questions.md](questions.md)

# Related
- [../graph/api.md](../graph/api.md#read-and-search): `GraphReader`, including the paged read `list_features` uses
- [../configuration.md](../configuration.md): the configuration reference, which lists the `MCP_*` and claim-profile
  settings
