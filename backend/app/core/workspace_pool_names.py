"""Workspace pool slugs for multi-tenant pool segregation."""

DEFAULT_WORKSPACE_POOL = "default"

# Extend when provisioning new physical pools (must match Firestore workspace docs).
ALLOWED_WORKSPACE_POOLS: frozenset[str] = frozenset({DEFAULT_WORKSPACE_POOL, "hf", "testpool", "customer"})
