"""
Tests for authorization dependencies.

Tests resource ownership verification and authorization checks.
"""

import pytest
from fastapi import HTTPException, Request
from unittest.mock import Mock

from app.api.dependencies import (
    verify_generation_session_owner,
    verify_generation_session_owner_or_admin,
)
from app.database.memory import InMemoryDatabase
from app.state.db_adapter import COL_GENERATION_SESSIONS, StateMachineDBAdapter


@pytest.fixture
def db():
    """In-memory database for tests."""
    return InMemoryDatabase()


@pytest.fixture
def mock_request():
    """Create a mock FastAPI request with user_email and key_uid."""
    def _make_request(
        user_email: str,
        key_uid: str = "uid-alice",
        workspace_pool: str = "default",
        permissions: list[str] | None = None,
    ):
        request = Mock(spec=Request)
        request.state = Mock()
        request.state.user_email = user_email
        request.state.workspace_pool = workspace_pool
        request.state.key_uid = key_uid
        request.state.permissions = permissions or ["user"]
        return request
    return _make_request


@pytest.mark.asyncio
async def test_verify_owner_allows_owner(db, mock_request):
    """
    Scenario: Owner can access their generation.

    Setup:
      1. Create generation for alice@example.com with matching key_uid
      2. Request authenticated as alice@example.com with same key_uid

    Expected:
      - No exception raised
      - Function returns normally
    """
    est_id = "est-alice-123"
    db.set(COL_GENERATION_SESSIONS, est_id, {
        "generation_id": est_id,
        "user_email": "alice@example.com",
        "workspace_pool": "default",
        "key_uid": "uid-alice",
        "status": "completed",
    })
    request = mock_request("alice@example.com", key_uid="uid-alice")

    await verify_generation_session_owner(est_id, request, StateMachineDBAdapter(db))


@pytest.mark.asyncio
async def test_verify_owner_blocks_non_owner(db, mock_request):
    """
    Scenario: Non-owner gets 403.

    Setup:
      1. Create generation for alice@example.com
      2. Request authenticated as bob@example.com

    Expected:
      - HTTPException with status 403
      - Error message explains access denied
    """
    est_id = "est-alice-123"
    db.set(COL_GENERATION_SESSIONS, est_id, {
        "generation_id": est_id,
        "user_email": "alice@example.com",
        "workspace_pool": "default",
        "key_uid": "uid-alice",
        "status": "completed",
    })
    request = mock_request("bob@example.com", key_uid="uid-bob")

    with pytest.raises(HTTPException) as exc_info:
        await verify_generation_session_owner(est_id, request, StateMachineDBAdapter(db))

    assert exc_info.value.status_code == 403
    assert "Access denied" in exc_info.value.detail
    assert "owned by another user" in exc_info.value.detail


@pytest.mark.asyncio
async def test_verify_owner_404_when_not_found(db, mock_request):
    """
    Scenario: Non-existent generation returns 404.

    Expected:
      - HTTPException with status 404
    """
    request = mock_request("alice@example.com", key_uid="uid-alice")

    with pytest.raises(HTTPException) as exc_info:
        await verify_generation_session_owner("est-nonexistent", request, StateMachineDBAdapter(db))

    assert exc_info.value.status_code == 404
    assert "not found" in exc_info.value.detail


@pytest.mark.asyncio
async def test_verify_owner_case_insensitive(db, mock_request):
    """
    Scenario: Email comparison is case-insensitive.

    Setup:
      1. Create generation for Alice@Example.com (mixed case)
      2. Request authenticated as alice@example.com (lowercase)

    Expected:
      - No exception raised (emails match)
    """
    est_id = "est-alice-123"
    db.set(COL_GENERATION_SESSIONS, est_id, {
        "generation_id": est_id,
        "user_email": "Alice@Example.com",
        "workspace_pool": "default",
        "key_uid": "uid-alice",
        "status": "completed",
    })
    request = mock_request("alice@example.com", key_uid="uid-alice")

    await verify_generation_session_owner(est_id, request, StateMachineDBAdapter(db))


@pytest.mark.asyncio
async def test_verify_owner_normalized_email(db, mock_request):
    """
    Scenario: User email is already normalized in middleware.

    Expected:
      - Verification succeeds
    """
    est_id = "est-alice-123"
    db.set(COL_GENERATION_SESSIONS, est_id, {
        "generation_id": est_id,
        "user_email": "alice@example.com",
        "workspace_pool": "default",
        "key_uid": "uid-alice",
        "status": "running",
    })
    request = mock_request("alice@example.com", key_uid="uid-alice")

    await verify_generation_session_owner(est_id, request, StateMachineDBAdapter(db))


@pytest.mark.asyncio
async def test_verify_owner_missing_user_email_field(db, mock_request):
    """
    Scenario: Generation document missing user_email field.

    Expected:
      - HTTPException 403 (treated as empty string, doesn't match)
    """
    est_id = "est-broken-123"
    db.set(COL_GENERATION_SESSIONS, est_id, {
        "generation_id": est_id,
        "workspace_pool": "default",
        "key_uid": "uid-alice",
        # Missing user_email field
        "status": "completed",
    })
    request = mock_request("alice@example.com", key_uid="uid-alice")

    with pytest.raises(HTTPException) as exc_info:
        await verify_generation_session_owner(est_id, request, StateMachineDBAdapter(db))

    assert exc_info.value.status_code == 403


# ────────────────────────────────────────────────────────────
# New tests for hardened key_uid enforcement (Phase 2)
# ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_verify_owner_blocks_when_generation_missing_key_uid(db, mock_request):
    """
    I-12: verify_generation_session_owner blocks when generation has no key_uid.

    After migration all generations must have key_uid. A missing field is
    treated as a configuration bug and access is denied.
    """
    est_id = "est-no-kuid"
    db.set(COL_GENERATION_SESSIONS, est_id, {
        "generation_id": est_id,
        "user_email": "alice@example.com",
        "workspace_pool": "default",
        # no key_uid
        "status": "running",
    })
    request = mock_request("alice@example.com", key_uid="uid-alice")

    with pytest.raises(HTTPException) as exc_info:
        await verify_generation_session_owner(est_id, request, StateMachineDBAdapter(db))

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_verify_owner_blocks_when_request_key_uid_is_none(db, mock_request):
    """
    I-12: verify_generation_session_owner blocks when the calling key has no key_uid in state.
    """
    est_id = "est-kuid-set"
    db.set(COL_GENERATION_SESSIONS, est_id, {
        "generation_id": est_id,
        "user_email": "alice@example.com",
        "workspace_pool": "default",
        "key_uid": "uid-alice",
        "status": "running",
    })
    request = mock_request("alice@example.com", key_uid=None)

    with pytest.raises(HTTPException) as exc_info:
        await verify_generation_session_owner(est_id, request, StateMachineDBAdapter(db))

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_verify_owner_blocks_key_uid_mismatch(db, mock_request):
    """
    I-03: User A (key K1, pool=default) cannot access User A's own generation
    created by key K2 (same email, same pool, different key_uid).
    """
    est_id = "est-k2"
    db.set(COL_GENERATION_SESSIONS, est_id, {
        "generation_id": est_id,
        "user_email": "alice@example.com",
        "workspace_pool": "default",
        "key_uid": "uid-k2",
        "status": "running",
    })
    # K1 tries to access generation created by K2
    request = mock_request("alice@example.com", key_uid="uid-k1")

    with pytest.raises(HTTPException) as exc_info:
        await verify_generation_session_owner(est_id, request, StateMachineDBAdapter(db))

    assert exc_info.value.status_code == 403
    assert "different API key" in exc_info.value.detail


@pytest.mark.asyncio
async def test_verify_owner_pool_mismatch_blocks(db, mock_request):
    """
    I-02: User A cannot access generation created in a different pool.
    """
    est_id = "est-hf"
    db.set(COL_GENERATION_SESSIONS, est_id, {
        "generation_id": est_id,
        "user_email": "alice@example.com",
        "workspace_pool": "hf",
        "key_uid": "uid-alice",
        "status": "running",
    })
    # Request comes in on the default pool
    request = mock_request("alice@example.com", key_uid="uid-alice", workspace_pool="default")

    with pytest.raises(HTTPException) as exc_info:
        await verify_generation_session_owner(est_id, request, StateMachineDBAdapter(db))

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_verify_owner_or_admin_allows_admin_without_ownership(db, mock_request):
    """Admin may access another user's generation without matching key_uid."""
    est_id = "est-alice-123"
    db.set(COL_GENERATION_SESSIONS, est_id, {
        "generation_id": est_id,
        "user_email": "alice@example.com",
        "workspace_pool": "hf",
        "key_uid": "uid-alice",
        "status": "failed",
    })
    request = mock_request(
        "admin@example.com",
        key_uid="uid-admin",
        workspace_pool="default",
        permissions=["admin"],
    )

    await verify_generation_session_owner_or_admin(
        est_id, request, StateMachineDBAdapter(db)
    )


@pytest.mark.asyncio
async def test_verify_owner_or_admin_blocks_non_owner_non_admin(db, mock_request):
    """Non-owner without admin still gets 403."""
    est_id = "est-alice-123"
    db.set(COL_GENERATION_SESSIONS, est_id, {
        "generation_id": est_id,
        "user_email": "alice@example.com",
        "workspace_pool": "default",
        "key_uid": "uid-alice",
        "status": "failed",
    })
    request = mock_request("bob@example.com", key_uid="uid-bob", permissions=["user"])

    with pytest.raises(HTTPException) as exc_info:
        await verify_generation_session_owner_or_admin(
            est_id, request, StateMachineDBAdapter(db)
        )

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_verify_owner_or_admin_404_when_not_found(db, mock_request):
    """Admin still gets 404 for a missing generation session."""
    request = mock_request(
        "admin@example.com",
        key_uid="uid-admin",
        permissions=["admin"],
    )

    with pytest.raises(HTTPException) as exc_info:
        await verify_generation_session_owner_or_admin(
            "est-nonexistent", request, StateMachineDBAdapter(db)
        )

    assert exc_info.value.status_code == 404
