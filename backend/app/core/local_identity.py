"""
Local identity constants for single-user local auth mode.

Single source of truth for the sentinel api_keys document used by
LocalAuthMiddleware (Phase 3) and init_db.py sentinel seeding (Phase 5).
"""

LOCAL_API_KEY_DOC_ID: str = "local"
"""Document-id for the local sentinel api_keys doc."""

LOCAL_KEY_UID: str = "00000000-10ca-0000-0000-000000000001"
"""
Fixed key_uid for the local sentinel doc. Valid-hex UUID (the ``10ca`` block
reads as "loca"), distinct from existing seed UUIDs:
  - e2e_tests_user:  00000000-e2e0-0000-0000-000000000001
  - extra_pool_user: 00000000-e2e0-0000-0000-000000000002
"""
