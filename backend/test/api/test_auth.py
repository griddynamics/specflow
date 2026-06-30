"""
Unit tests for API key management endpoints.

Tests creating, listing, revoking, and reactivating API keys.
"""

import pytest
import os
from datetime import datetime, timezone
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from app.database.factory import clear_test_data
from app.api.v1.auth import router as auth_router
from app.state.db_adapter import COL_GENERATION_SESSIONS


class MockAdminMiddleware(BaseHTTPMiddleware):
    """Test middleware that sets admin permissions on all requests."""

    def __init__(self, app, permissions=None):
        super().__init__(app)
        self.permissions = permissions or ["admin"]

    async def dispatch(self, request: Request, call_next):
        request.state.user_email = "admin@test.com"
        request.state.user_id = "admin@test.com"
        request.state.api_key = "gain_test_admin_key"
        request.state.user_name = "Test Admin"
        request.state.permissions = self.permissions
        return await call_next(request)


def _create_app(permissions=None):
    """Create a test app with mock admin middleware."""
    clear_test_data()
    if "DATABASE_TYPE" not in os.environ:
        os.environ["DATABASE_TYPE"] = "memory"
    test_app = FastAPI()
    test_app.add_middleware(MockAdminMiddleware, permissions=permissions or ["admin"])
    test_app.include_router(auth_router, prefix="/api/v1")
    return test_app


@pytest.fixture
def app():
    """Create a minimal FastAPI app with admin permissions for testing."""
    test_app = _create_app(permissions=["admin"])
    yield test_app
    clear_test_data()


@pytest.fixture
def app_non_admin():
    """Create a minimal FastAPI app with non-admin permissions."""
    test_app = _create_app(permissions=["generation:read"])
    yield test_app
    clear_test_data()


@pytest.fixture
def app_with_auth():
    """Create FastAPI app with real auth middleware for testing /auth/me endpoint."""
    clear_test_data()
    if "DATABASE_TYPE" not in os.environ:
        os.environ["DATABASE_TYPE"] = "memory"
    test_app = FastAPI()
    from app.middleware.auth import AuthMiddleware
    from app.database.factory import get_database
    test_app.add_middleware(AuthMiddleware, db=get_database())
    test_app.include_router(auth_router, prefix="/api/v1")
    yield test_app
    clear_test_data()


@pytest.fixture
def client(app):
    """Create test client with admin permissions."""
    return TestClient(app)


@pytest.fixture
def client_non_admin(app_non_admin):
    """Create test client with non-admin permissions."""
    return TestClient(app_non_admin)


# =================================================================
# Admin Guard Tests
# =================================================================

def test_create_api_key_requires_admin(client_non_admin):
    """Test creating API key requires admin permissions."""
    response = client_non_admin.post(
        "/api/v1/auth/keys",
        json={"user_id": "test@company.com", "user_name": "Test"}
    )
    assert response.status_code == 403
    assert "Admin access required" in response.json()["detail"]


def test_list_api_keys_requires_admin(client_non_admin):
    """Test listing API keys requires admin permissions."""
    response = client_non_admin.get("/api/v1/auth/keys")
    assert response.status_code == 403


def test_revoke_api_key_requires_admin(client_non_admin):
    """Test revoking API key requires admin permissions."""
    response = client_non_admin.delete("/api/v1/auth/keys/gain_test123456")
    assert response.status_code == 403


def test_reactivate_api_key_requires_admin(client_non_admin):
    """Test reactivating API key requires admin permissions."""
    response = client_non_admin.post("/api/v1/auth/keys/gain_test123456/reactivate")
    assert response.status_code == 403


def test_wildcard_permission_rejected(client):
    """Test that ["*"] is no longer a valid permission and is rejected."""
    response = client.post(
        "/api/v1/auth/keys",
        json={"user_id": "test@company.com", "user_name": "Test", "permissions": ["*"]}
    )
    assert response.status_code == 422


def test_explicit_admin_permission_grants_access():
    """Test that ["admin"] permission grants admin access."""
    app = _create_app(permissions=["admin"])
    client = TestClient(app)
    response = client.post(
        "/api/v1/auth/keys",
        json={"user_id": "test@company.com", "user_name": "Test"}
    )
    assert response.status_code == 201
    clear_test_data()


# =================================================================
# Original API Key CRUD Tests (with admin permissions)
# =================================================================

def test_create_api_key(client):
    """Test creating a new API key."""
    response = client.post(
        "/api/v1/auth/keys",
        json={
            "user_id": "john.doe@company.com",
            "user_name": "John Doe",
            "expires_days": 365,
            "permissions": ["user"]
        }
    )

    assert response.status_code == 201
    data = response.json()

    assert data["user_id"] == "john.doe@company.com"
    assert data["user_name"] == "John Doe"
    assert data["api_key"].startswith("gain_")
    assert len(data["api_key"]) > 20
    assert data["key_uid"]
    assert data["workspace_pool"] == "default"
    assert "created_at" in data
    assert "expires_at" in data


def test_create_api_key_rejects_unknown_pool(client):
    response = client.post(
        "/api/v1/auth/keys",
        json={
            "user_id": "x@y.com",
            "user_name": "X",
            "workspace_pool": "unknown-pool",
        },
    )
    assert response.status_code == 422


def test_create_api_key_no_expiration(client):
    """Test creating an API key that never expires."""
    response = client.post(
        "/api/v1/auth/keys",
        json={
            "user_id": "jane.doe@company.com",
            "user_name": "Jane Doe"
        }
    )

    assert response.status_code == 201
    data = response.json()
    assert data["expires_at"] is None


def test_list_api_keys_empty(client):
    """Test listing API keys when none exist."""
    response = client.get("/api/v1/auth/keys")

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 0
    assert data["keys"] == []


def test_list_api_keys(client):
    """Test listing API keys."""
    client.post(
        "/api/v1/auth/keys",
        json={"user_id": "user1@company.com", "user_name": "User 1"}
    )
    client.post(
        "/api/v1/auth/keys",
        json={"user_id": "user2@company.com", "user_name": "User 2"}
    )

    response = client.get("/api/v1/auth/keys")

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 2
    assert len(data["keys"]) == 2

    for key_info in data["keys"]:
        assert key_info["api_key_prefix"].startswith("gain_")
        assert key_info["api_key_prefix"].endswith("...")
        assert len(key_info["api_key_prefix"]) == 18
        assert key_info["is_active"] is True


def test_revoke_api_key(client):
    """Test revoking an API key."""
    create_response = client.post(
        "/api/v1/auth/keys",
        json={"user_id": "revoke.test@company.com", "user_name": "Revoke Test"}
    )
    api_key = create_response.json()["api_key"]
    api_key_prefix = api_key[:15]

    response = client.delete(f"/api/v1/auth/keys/{api_key_prefix}")

    assert response.status_code == 200
    data = response.json()
    assert data["message"] == "API key revoked successfully"
    assert data["api_key_prefix"] == api_key_prefix

    list_response = client.get("/api/v1/auth/keys")
    keys = list_response.json()["keys"]
    revoked_key = next(k for k in keys if k["api_key_prefix"].startswith(api_key_prefix))
    assert revoked_key["is_active"] is False


def test_revoke_nonexistent_key(client):
    """Test revoking a key that doesn't exist."""
    response = client.delete("/api/v1/auth/keys/gain_nonexist")

    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


def test_reactivate_api_key(client):
    """Test reactivating a revoked API key."""
    create_response = client.post(
        "/api/v1/auth/keys",
        json={"user_id": "reactivate.test@company.com", "user_name": "Reactivate Test"}
    )
    api_key = create_response.json()["api_key"]
    api_key_prefix = api_key[:15]

    client.delete(f"/api/v1/auth/keys/{api_key_prefix}")

    response = client.post(f"/api/v1/auth/keys/{api_key_prefix}/reactivate")

    assert response.status_code == 200
    data = response.json()
    assert data["message"] == "API key reactivated successfully"

    list_response = client.get("/api/v1/auth/keys")
    keys = list_response.json()["keys"]
    reactivated_key = next(k for k in keys if k["api_key_prefix"].startswith(api_key_prefix))
    assert reactivated_key["is_active"] is True


def test_reactivate_nonexistent_key(client):
    """Test reactivating a key that doesn't exist."""
    response = client.post("/api/v1/auth/keys/gain_nonexist/reactivate")

    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


def test_api_key_format(client):
    """Test that generated API keys have correct format."""
    response = client.post(
        "/api/v1/auth/keys",
        json={"user_id": "format.test@company.com", "user_name": "Format Test"}
    )

    api_key = response.json()["api_key"]
    assert api_key.startswith("gain_")
    key_part = api_key[5:]
    assert all(c.isalnum() or c in ["-", "_"] for c in key_part)
    assert len(api_key) >= 40


def test_create_key_with_custom_permissions(client):
    """Test creating a key with custom permissions."""
    response = client.post(
        "/api/v1/auth/keys",
        json={
            "user_id": "limited.user@company.com",
            "user_name": "Limited User",
            "permissions": ["user"]
        }
    )
    assert response.status_code == 201


def test_expiration_timestamp_format(client):
    """Test that expiration timestamps are in ISO format."""
    response = client.post(
        "/api/v1/auth/keys",
        json={
            "user_id": "expire.test@company.com",
            "user_name": "Expire Test",
            "expires_days": 30
        }
    )

    data = response.json()
    expires_at = data["expires_at"]
    assert expires_at is not None
    parsed = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    diff = (parsed - now).days
    assert 29 <= diff <= 30


def test_put_github_token_encrypts_and_stores(app_with_auth):
    """Authenticated key can upload a PAT; ciphertext stored in Firestore."""
    from cryptography.fernet import Fernet

    from app.core.github_platform_secrets import (
        init_github_platform_secrets_for_tests,
        reset_github_platform_secrets,
    )
    from app.database.factory import get_database

    init_github_platform_secrets_for_tests(fernet_key=Fernet.generate_key())
    try:
        client = TestClient(app_with_auth)
        admin_app = _create_app(permissions=["admin"])
        client_admin = TestClient(admin_app)
        create_response = client_admin.post(
            "/api/v1/auth/keys",
            json={"user_id": "pat.user@company.com", "user_name": "PAT User"},
        )
        assert create_response.status_code == 201
        api_key = create_response.json()["api_key"]

        response = client.put(
            "/api/v1/auth/github-token",
            json={"token": "ghp_test_upload_token", "git_user_name": "pat-user"},
            headers={
                "X-API-Key": api_key,
                "X-User-Email": "pat.user@company.com",
            },
        )
        assert response.status_code == 200
        assert response.json().get("ok") is True

        db = get_database()
        doc = db.get("api_keys", api_key)
        assert doc.get("github_token_ciphertext")
        assert doc.get("git_user_name") == "pat-user"
    finally:
        reset_github_platform_secrets()


# =================================================================
# GET /auth/me Tests (Session Recovery for MCP)
# =================================================================

def test_get_auth_me_returns_current_process(app_with_auth):
    """Test GET /auth/me returns current_process for session recovery."""
    client = TestClient(app_with_auth)

    # Create an API key using an app with admin middleware
    admin_app = _create_app(permissions=["admin"])
    client_admin = TestClient(admin_app)

    create_response = client_admin.post(
        "/api/v1/auth/keys",
        json={"user_id": "test.user@company.com", "user_name": "Test User"}
    )
    api_key = create_response.json()["api_key"]
    key_uid = create_response.json()["key_uid"]

    from app.database.factory import get_database
    db = get_database()
    # Create the generation so get_auth_me can verify it still exists
    db.set(COL_GENERATION_SESSIONS, "est-abc123", {
        "generation_id": "est-abc123",
        "status": "running",
        "key_uid": key_uid,
    })
    now = datetime.now(timezone.utc)
    db.update("api_keys", api_key, {
        "active_generation_sessions": [
            {
                "generation_id": "est-abc123",
                "operation": "analysis",
                "lease_started_at": now,
                "lease_ttl_minutes": 60,
            }
        ],
        "max_concurrent_sessions": 5,
    })

    response = client.get(
        "/api/v1/auth/me",
        headers={
            "X-API-Key": api_key,
            "X-User-Email": "test.user@company.com"
        }
    )

    assert response.status_code == 200
    data = response.json()
    assert data["current_process"] == "est-abc123"
    assert data["in_progress"] is True
    assert data["user_id"] == "test.user@company.com"
    assert data.get("key_uid")
    assert data.get("workspace_pool") == "default"


def test_get_auth_me_without_current_process(app_with_auth):
    """Test GET /auth/me when no current_process is set."""
    client = TestClient(app_with_auth)

    admin_app = _create_app(permissions=["admin"])
    client_admin = TestClient(admin_app)

    create_response = client_admin.post(
        "/api/v1/auth/keys",
        json={"user_id": "new.user@company.com", "user_name": "New User"}
    )
    api_key = create_response.json()["api_key"]

    response = client.get(
        "/api/v1/auth/me",
        headers={
            "X-API-Key": api_key,
            "X-User-Email": "new.user@company.com"
        }
    )

    assert response.status_code == 200
    data = response.json()
    assert data["current_process"] is None
    assert data["in_progress"] is False
    assert data["user_id"] == "new.user@company.com"


def test_get_auth_me_without_authentication(app_with_auth):
    """Test GET /auth/me returns 401 when not authenticated."""
    client = TestClient(app_with_auth)
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 401
    assert "Missing API key" in response.json()["detail"]


def test_get_auth_me_with_invalid_api_key(app_with_auth):
    """Test GET /auth/me returns 401 for non-existent API key."""
    client = TestClient(app_with_auth)
    response = client.get(
        "/api/v1/auth/me",
        headers={
            "X-API-Key": "specflow_nonexistent_key_12345",
            "X-User-Email": "test@example.com"
        }
    )
    assert response.status_code == 401
    assert "Invalid or expired API key" in response.json()["detail"]


def test_get_auth_me_mcp_session_recovery_flow(app_with_auth):
    """Test the full MCP session recovery flow."""
    client = TestClient(app_with_auth)

    admin_app = _create_app(permissions=["admin"])
    client_admin = TestClient(admin_app)

    create_response = client_admin.post(
        "/api/v1/auth/keys",
        json={"user_id": "mcp.user@company.com", "user_name": "MCP User"}
    )
    api_key = create_response.json()["api_key"]
    key_uid = create_response.json()["key_uid"]

    from app.database.factory import get_database
    db = get_database()
    # Create the generation so get_auth_me can verify it still exists
    db.set(COL_GENERATION_SESSIONS, "est-recovery-test", {
        "generation_id": "est-recovery-test",
        "status": "running",
        "key_uid": key_uid,
    })
    now = datetime.now(timezone.utc)
    db.update("api_keys", api_key, {
        "active_generation_sessions": [
            {
                "generation_id": "est-recovery-test",
                "operation": "analysis",
                "lease_started_at": now,
                "lease_ttl_minutes": 90,
            }
        ],
        "max_concurrent_sessions": 5,
    })

    response = client.get(
        "/api/v1/auth/me",
        headers={
            "X-API-Key": api_key,
            "X-User-Email": "mcp.user@company.com"
        }
    )

    assert response.status_code == 200
    data = response.json()
    assert data["current_process"] == "est-recovery-test"
    assert data["in_progress"] is True
    assert data["user_id"] == "mcp.user@company.com"


# =================================================================
# max_concurrent_sessions validation (P0 — Phase 7)
# =================================================================

def test_create_api_key_default_max_concurrent_sessions(client):
    """max_concurrent_sessions defaults to 5 when not specified."""
    response = client.post(
        "/api/v1/auth/keys",
        json={"user_id": "session.default@company.com", "user_name": "Session Default"},
    )
    assert response.status_code == 201
    from app.database.factory import get_database
    db = get_database()
    api_key = response.json()["api_key"]
    doc = db.get("api_keys", api_key)
    assert doc["max_concurrent_sessions"] == 5


def test_create_api_key_custom_max_concurrent_sessions(client):
    """max_concurrent_sessions can be set to any valid value."""
    response = client.post(
        "/api/v1/auth/keys",
        json={"user_id": "session.custom@company.com", "user_name": "Session Custom", "max_concurrent_sessions": 3},
    )
    assert response.status_code == 201
    from app.database.factory import get_database
    db = get_database()
    api_key = response.json()["api_key"]
    doc = db.get("api_keys", api_key)
    assert doc["max_concurrent_sessions"] == 3


def test_create_api_key_rejects_zero_max_concurrent_sessions(client):
    """max_concurrent_sessions must be at least 1 (ge=1)."""
    response = client.post(
        "/api/v1/auth/keys",
        json={"user_id": "session.zero@company.com", "user_name": "Session Zero", "max_concurrent_sessions": 0},
    )
    assert response.status_code == 422


def test_create_api_key_rejects_over_limit_max_concurrent_sessions(client):
    """max_concurrent_sessions must not exceed 20 (le=20)."""
    response = client.post(
        "/api/v1/auth/keys",
        json={"user_id": "session.over@company.com", "user_name": "Session Over", "max_concurrent_sessions": 21},
    )
    assert response.status_code == 422


def test_create_api_key_accepts_boundary_values(client):
    """max_concurrent_sessions accepts the boundary values 1 and 20."""
    for limit in (1, 20):
        response = client.post(
            "/api/v1/auth/keys",
            json={"user_id": f"session.boundary{limit}@company.com", "user_name": f"Boundary {limit}", "max_concurrent_sessions": limit},
        )
        assert response.status_code == 201


def test_get_auth_me_hides_completed_generation_current_process(app_with_auth):
    """Completed generations must not be returned as recoverable current_process."""
    client = TestClient(app_with_auth)

    admin_app = _create_app(permissions=["admin"])
    client_admin = TestClient(admin_app)

    create_response = client_admin.post(
        "/api/v1/auth/keys",
        json={"user_id": "done.user@company.com", "user_name": "Done User"},
    )
    api_key = create_response.json()["api_key"]

    from app.database.factory import get_database

    db = get_database()
    db.set(
        COL_GENERATION_SESSIONS,
        "est-completed-1",
        {
            "status": "completed",
            "checkpoint": "outputs_archived",
        },
    )
    now = datetime.now(timezone.utc)
    db.update(
        "api_keys",
        api_key,
        {
            "active_generation_sessions": [
                {
                    "generation_id": "est-completed-1",
                    "operation": "analysis",
                    "lease_started_at": now,
                    "lease_ttl_minutes": 60,
                }
            ],
            "max_concurrent_sessions": 5,
        },
    )

    response = client.get(
        "/api/v1/auth/me",
        headers={
            "X-API-Key": api_key,
            "X-User-Email": "done.user@company.com",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["current_process"] is None
    assert data["user_id"] == "done.user@company.com"
