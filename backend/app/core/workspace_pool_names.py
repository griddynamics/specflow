"""Workspace pool slugs and shape constants for multi-tenant pool segregation."""

DEFAULT_WORKSPACE_POOL = "default"

# Extend when provisioning new physical pools (must match the seeded workspace documents).
ALLOWED_WORKSPACE_POOLS: frozenset[str] = frozenset({DEFAULT_WORKSPACE_POOL, "hf", "testpool", "customer"})

# Workspaces per set. A set is the unit of allocation: allocation takes all three at once so
# parallel variants of one generation are isolated per repo. Lives here rather than on
# WorkspacePoolService because seeding, expansion, and allocation must all agree on it.
WORKSPACES_PER_SET = 3
