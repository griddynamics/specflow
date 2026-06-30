"""
Isolation hardening tests — covers all fixes from review-1-plan.md.

Phases covered:
  Phase 0  — get_api_key_by_uid DB adapter
  Phase 2  — github_auth no-legacy mandate
  Phase 3  — token redaction safety
  Phase 4  — workspace allocation fallback for missing workspace_pool
  Phase 5a — generation session end uses key_uid direct lookup (no api_keys query scan)
"""

import pytest
from cryptography.fernet import Fernet

from app.core.github_platform_secrets import (
    get_github_platform_secrets,
    init_github_platform_secrets_for_tests,
    reset_github_platform_secrets,
)
from app.core.workspace_pool_names import DEFAULT_WORKSPACE_POOL
from app.database.memory import InMemoryDatabase
from app.state.api_key_session_concurrency import ApiKeySessionConcurrency, SessionEndReason
from app.state.db_adapter import COL_GENERATION_SESSIONS, StateMachineDBAdapter
from app.services.github_auth import (
    GithubAuthResolutionError,
    resolve_github_auth_for_generation_session_doc,
)
from app.services.workspace_pool import NoAvailableWorkspacesError, WorkspacePoolService


# ─────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────


@pytest.fixture
def db():
    d = InMemoryDatabase()
    yield d
    d.clear()


@pytest.fixture
def secrets():
    key = Fernet.generate_key()
    init_github_platform_secrets_for_tests(
        fernet_key=key,
        github_token_default="platform-default-pat",
        git_user_name_default="platform-git-user",
    )
    yield get_github_platform_secrets()
    reset_github_platform_secrets()


# ─────────────────────────────────────────────
# Phase 0 — get_api_key_by_uid
# ─────────────────────────────────────────────


def test_get_api_key_by_uid_returns_correct_doc(db):
    """I-13: get_api_key_by_uid returns the doc with the matching key_uid."""
    db.set("api_keys", "gain_key1", {"api_key": "gain_key1", "key_uid": "uid-1", "user_id": "a@a.com"})
    db.set("api_keys", "gain_key2", {"api_key": "gain_key2", "key_uid": "uid-2", "user_id": "b@b.com"})

    result = db.get_api_key_by_uid("uid-1")
    assert result is not None
    assert result["key_uid"] == "uid-1"
    assert result["user_id"] == "a@a.com"

    result2 = db.get_api_key_by_uid("uid-2")
    assert result2 is not None
    assert result2["key_uid"] == "uid-2"


def test_get_api_key_by_uid_returns_none_for_unknown_uid(db):
    """get_api_key_by_uid returns None when key_uid is not found."""
    db.set("api_keys", "gain_key1", {"api_key": "gain_key1", "key_uid": "uid-1"})

    result = db.get_api_key_by_uid("uid-does-not-exist")
    assert result is None


def test_get_api_key_by_uid_includes_id_field(db):
    """Result from get_api_key_by_uid includes '_id' field."""
    db.set("api_keys", "gain_mykey", {"api_key": "gain_mykey", "key_uid": "uid-x"})

    result = db.get_api_key_by_uid("uid-x")
    assert result is not None
    assert result["_id"] == "gain_mykey"


def test_github_auth_uses_get_api_key_by_uid(db, secrets):
    """
    github_auth.py must use db.get_api_key_by_uid for key_uid lookups,
    not raw db.query.
    """
    s = secrets
    ct = s.encrypt_token("test-pat")
    db.set("api_keys", "gain_k1", {
        "api_key": "gain_k1",
        "key_uid": "uid-1",
        "workspace_pool": DEFAULT_WORKSPACE_POOL,
        "github_token_ciphertext": ct,
    })
    est_doc = {"generation_id": "est-1", "workspace_pool": DEFAULT_WORKSPACE_POOL, "key_uid": "uid-1"}

    # Wrap db to detect if raw query is called for api_keys key_uid lookup
    original_query = db.query
    query_calls = []

    def tracking_query(collection, filters=None, **kwargs):
        if collection == "api_keys" and any(
            f[0] == "key_uid" for f in (filters or [])
        ):
            query_calls.append(filters)
        return original_query(collection, filters, **kwargs)

    db.query = tracking_query

    ctx = resolve_github_auth_for_generation_session_doc(db, est_doc)
    assert ctx.token == "test-pat"
    # No raw query on api_keys.key_uid should have been issued
    assert len(query_calls) == 0, (
        "github_auth should use db.get_api_key_by_uid, not a raw db.query for key_uid"
    )


# ─────────────────────────────────────────────
# Phase 2 — github_auth no-legacy mandate
# ─────────────────────────────────────────────


def test_github_auth_raises_for_generation_without_key_uid(db, secrets):
    """
    I-09: resolve_github_auth_for_generation_session_doc raises GithubAuthResolutionError
    with an informative message when generation has no key_uid.
    """
    est_doc = {
        "generation_id": "est-no-uid",
        "workspace_pool": DEFAULT_WORKSPACE_POOL,
        # no key_uid field
    }
    with pytest.raises(GithubAuthResolutionError, match="missing key_uid"):
        resolve_github_auth_for_generation_session_doc(db, est_doc)


def test_github_auth_raises_for_missing_api_key_doc(db, secrets):
    """
    resolve_github_auth_for_generation_session_doc raises when key_uid is set but no
    matching api_keys doc exists.
    """
    est_doc = {
        "generation_id": "est-orphan",
        "workspace_pool": DEFAULT_WORKSPACE_POOL,
        "key_uid": "uid-deleted",
    }
    with pytest.raises(GithubAuthResolutionError, match="key_uid=uid-deleted"):
        resolve_github_auth_for_generation_session_doc(db, est_doc)


# ─────────────────────────────────────────────
# Phase 3 — token redaction safety
# ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_initialize_git_repo_raises_safely_when_auth_fails(db):
    """
    I-10: When resolve_github_auth fails, initialize_git_repo raises
    WorkspacePoolError without attempting git operations that could log
    raw error output.
    """
    from app.services.workspace_pool import WorkspacePoolError

    db.set(COL_GENERATION_SESSIONS, "est-1", {
        "generation_id": "est-1",
        "workspace_pool": DEFAULT_WORKSPACE_POOL,
        # no key_uid → auth will fail
    })

    svc = WorkspacePoolService(db, workspace_base_path="/tmp/test-ws")

    with pytest.raises(WorkspacePoolError, match="credentials could not be resolved"):
        await svc.initialize_git_repo(
            workspace_path=svc.workspace_base_path / "ws-test",
            generation_id="est-1",
        )


@pytest.mark.asyncio
async def test_archive_generation_session_work_raises_safely_when_auth_fails(db, tmp_path):
    """
    I-10: When resolve_github_auth fails, _archive_generation_session_work raises
    WorkspacePoolError without logging raw error output.
    """
    from app.services.workspace_pool import WorkspacePoolError

    db.set(COL_GENERATION_SESSIONS, "est-1", {
        "generation_id": "est-1",
        "workspace_pool": DEFAULT_WORKSPACE_POOL,
        # no key_uid
    })

    # Create a workspace dir with .git so the function doesn't early-return
    ws_path = tmp_path / "ws-archive-test"
    ws_path.mkdir()
    (ws_path / ".git").mkdir()

    svc = WorkspacePoolService(db, workspace_base_path=str(tmp_path))

    with pytest.raises(WorkspacePoolError, match="credentials could not be resolved"):
        await svc._archive_generation_session_work(ws_path, "est-1")


@pytest.mark.asyncio
async def test_verify_archive_pushed_returns_false_when_auth_fails(db, tmp_path):
    """
    _verify_archive_pushed returns False (fail-safe) when auth resolution fails.
    No raw error output is logged.
    """
    db.set(COL_GENERATION_SESSIONS, "est-1", {
        "generation_id": "est-1",
        "workspace_pool": DEFAULT_WORKSPACE_POOL,
        # no key_uid
    })

    ws_path = tmp_path / "ws-verify-test"
    ws_path.mkdir()
    (ws_path / ".git").mkdir()

    svc = WorkspacePoolService(db, workspace_base_path=str(tmp_path))

    result = await svc._verify_archive_pushed(ws_path, "est-1")
    assert result is False


def test_sanitize_token_redacts_explicit_token():
    """_sanitize_token_in_message redacts an explicitly provided token."""
    svc = WorkspacePoolService.__new__(WorkspacePoolService)
    msg = "Error: https://user:mysecrettoken@github.com/repo failed"
    sanitized = svc._sanitize_token_in_message(msg, "mysecrettoken")
    assert "mysecrettoken" not in sanitized
    assert "[REDACTED]" in sanitized


def test_sanitize_token_redacts_ghp_pattern():
    """_sanitize_token_in_message redacts ghp_ tokens via regex when no explicit token."""
    svc = WorkspacePoolService.__new__(WorkspacePoolService)
    msg = "remote: Invalid credentials. Token: ghp_ABC123XYZ"  # gitleaks:allow
    sanitized = svc._sanitize_token_in_message(msg, None)
    assert "ghp_ABC123XYZ" not in sanitized
    assert "[REDACTED]" in sanitized


# ─────────────────────────────────────────────
# Phase 4 — workspace allocation fallback
# ─────────────────────────────────────────────


def _make_workspace(ws_id: str, set_num: int, pool=None):
    """Helper to build a minimal workspace doc."""
    doc = {
        "workspace_id": ws_id,
        "set_number": set_num,
        "status": "available",
        "clean_verified": True,
        "repo_url": f"https://github.com/org/{ws_id}.git",
    }
    if pool is not None:
        doc["workspace_pool"] = pool
    return doc


@pytest.mark.asyncio
async def test_allocate_requires_workspace_pool_field(db):
    """
    I-07: Workspaces without 'workspace_pool' field are NOT matched by any pool query.
    After migration all workspace docs must have workspace_pool set; a missing field
    is a configuration error (NoAvailableWorkspacesError), not a silent fallback.
    """
    db.set(COL_GENERATION_SESSIONS, "est-alloc", {
        "generation_id": "est-alloc",
        "workspace_pool": DEFAULT_WORKSPACE_POOL,
        "key_uid": "uid-1",
    })
    # Three workspaces with NO workspace_pool field — should not be found
    for i in range(1, 4):
        ws_id = f"ws-legacy-{i}"
        db.set("workspaces", ws_id, _make_workspace(ws_id, set_num=1))

    svc = WorkspacePoolService(db, workspace_base_path="/tmp/alloc-test")

    with pytest.raises(NoAvailableWorkspacesError):
        await svc.allocate_workspace_set("est-alloc")


@pytest.mark.asyncio
async def test_allocate_does_not_use_legacy_workspace_for_dedicated_pool(db):
    """
    I-08: A workspace doc without 'workspace_pool' is NOT matched when allocating
    for a dedicated (non-default) pool.
    """
    db.set(COL_GENERATION_SESSIONS, "est-hf", {
        "generation_id": "est-hf",
        "workspace_pool": "hf",
        "key_uid": "uid-hf",
    })
    # Three workspaces with NO workspace_pool field (default candidates)
    for i in range(1, 4):
        ws_id = f"ws-nopoolhf-{i}"
        db.set("workspaces", ws_id, _make_workspace(ws_id, set_num=1))

    svc = WorkspacePoolService(db, workspace_base_path="/tmp/alloc-hf-test")

    with pytest.raises(NoAvailableWorkspacesError):
        await svc.allocate_workspace_set("est-hf")


@pytest.mark.asyncio
async def test_allocate_explicit_pool_workspace_matched_for_default_pool(db):
    """
    I-07 variant: Workspaces explicitly tagged workspace_pool='default' are
    included in default pool allocation alongside legacy (no-field) workspaces.
    """
    db.set(COL_GENERATION_SESSIONS, "est-explicit", {
        "generation_id": "est-explicit",
        "workspace_pool": DEFAULT_WORKSPACE_POOL,
        "key_uid": "uid-1",
    })
    # Three workspaces explicitly tagged as 'default'
    for i in range(1, 4):
        ws_id = f"ws-explicit-{i}"
        db.set("workspaces", ws_id, _make_workspace(ws_id, set_num=1, pool=DEFAULT_WORKSPACE_POOL))

    svc = WorkspacePoolService(db, workspace_base_path="/tmp/alloc-explicit")

    async def _noop_clone(workspace_id, ws_doc, generation_id):
        pass

    svc._ensure_repo_cloned = _noop_clone

    workspace_ids = await svc.allocate_workspace_set("est-explicit")
    assert len(workspace_ids) == 3


# ─────────────────────────────────────────────
# Phase 5a — ApiKeySessionConcurrency.end uses key_uid direct lookup
# ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_end_session_uses_key_uid_direct_lookup(db):
    """
    end() resolves the api_keys document via get_api_key_by_uid — NOT a query scan.
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    db.set(COL_GENERATION_SESSIONS, "est-1", {
        "generation_id": "est-1",
        "key_uid": "uid-1",
    })
    db.set("api_keys", "gain_k1", {
        "api_key": "gain_k1",
        "key_uid": "uid-1",
        "max_concurrent_sessions": 5,
        "active_generation_sessions": [
            {
                "generation_id": "est-1",
                "operation": "analysis",
                "lease_started_at": now,
                "lease_ttl_minutes": 60,
            }
        ],
    })

    uid_lookups = []
    original_by_uid = db.get_api_key_by_uid

    def tracking_by_uid(key_uid):
        uid_lookups.append(key_uid)
        return original_by_uid(key_uid)

    db.get_api_key_by_uid = tracking_by_uid

    query_calls_for_api_keys = []
    original_query = db.query

    def tracking_query(collection, filters=None, **kwargs):
        if collection == "api_keys":
            query_calls_for_api_keys.append(filters)
        return original_query(collection, filters, **kwargs)

    db.query = tracking_query

    adapter = StateMachineDBAdapter(db)
    svc = ApiKeySessionConcurrency(adapter)
    await svc.end(generation_id="est-1", reason=SessionEndReason.COMPLETED)

    assert "uid-1" in uid_lookups, "Expected get_api_key_by_uid to be called with uid-1"
    assert len(query_calls_for_api_keys) == 0, (
        "end() should not do a raw query scan on api_keys"
    )

    key_doc = db.get("api_keys", "gain_k1")
    assert key_doc.get("active_generation_sessions") == []


@pytest.mark.asyncio
async def test_end_noop_when_generation_not_in_sessions(db):
    """end(est-old) must not remove unrelated active sessions."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    db.set(COL_GENERATION_SESSIONS, "est-old", {
        "generation_id": "est-old",
        "key_uid": "uid-1",
    })
    db.set(COL_GENERATION_SESSIONS, "est-new", {
        "generation_id": "est-new",
        "key_uid": "uid-1",
    })
    db.set("api_keys", "gain_k1", {
        "api_key": "gain_k1",
        "key_uid": "uid-1",
        "max_concurrent_sessions": 5,
        "active_generation_sessions": [
            {
                "generation_id": "est-new",
                "operation": "analysis",
                "lease_started_at": now,
                "lease_ttl_minutes": 60,
            }
        ],
    })

    adapter = StateMachineDBAdapter(db)
    svc = ApiKeySessionConcurrency(adapter)
    await svc.end(generation_id="est-old", reason=SessionEndReason.COMPLETED)

    key_doc = db.get("api_keys", "gain_k1")
    assert len(key_doc.get("active_generation_sessions") or []) == 1


@pytest.mark.asyncio
async def test_end_noop_when_generation_has_no_key_uid(db):
    """end() returns without crashing when generation has no key_uid."""
    db.set(COL_GENERATION_SESSIONS, "est-no-uid", {
        "generation_id": "est-no-uid",
    })

    adapter = StateMachineDBAdapter(db)
    svc = ApiKeySessionConcurrency(adapter)
    await svc.end(generation_id="est-no-uid", reason=SessionEndReason.COMPLETED)


@pytest.mark.asyncio
async def test_end_noop_when_generation_not_found(db):
    """end() is a no-op when the generation doesn't exist."""
    adapter = StateMachineDBAdapter(db)
    svc = ApiKeySessionConcurrency(adapter)
    await svc.end(generation_id="est-ghost", reason=SessionEndReason.COMPLETED)
