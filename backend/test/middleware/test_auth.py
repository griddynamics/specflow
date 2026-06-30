"""
Unit tests for authentication middleware.

Tests API key validation, request authentication, and error handling.
"""

import pytest
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.middleware.auth import AuthMiddleware
from app.database.memory import InMemoryDatabase


@pytest.fixture
def db():
    """Create in-memory database for testing."""
    return InMemoryDatabase()


@pytest.fixture
def app_with_auth(db):
    """FastAPI app with auth middleware."""
    app = FastAPI()
    
    # Add test API keys
    # Valid key
    db.set("api_keys", "test_valid_key", {
        "api_key": "test_valid_key",
        "user_id": "test@example.com",
        "user_name": "Test User",
        "created_at": datetime.now(timezone.utc),
        "is_active": True,
        "permissions": ["admin"],
    })
    
    # Expired key
    db.set("api_keys", "test_expired_key", {
        "api_key": "test_expired_key",
        "user_id": "expired@example.com",
        "user_name": "Expired User",
        "created_at": datetime.now(timezone.utc),
        "expires_at": datetime.now(timezone.utc) - timedelta(days=1),
        "is_active": True,
        "permissions": ["admin"],
    })
    
    # Inactive key
    db.set("api_keys", "test_inactive_key", {
        "api_key": "test_inactive_key",
        "user_id": "inactive@example.com",
        "user_name": "Inactive User",
        "created_at": datetime.now(timezone.utc),
        "is_active": False,
        "permissions": ["admin"],
    })
    
    # Add auth middleware
    app.add_middleware(AuthMiddleware, db=db)
    
    # Add test endpoints
    @app.get("/protected")
    def protected_endpoint(request: Request):
        return {
            "user_email": request.state.user_email,
            "user_name": request.state.user_name,
            "permissions": request.state.permissions
        }
    
    @app.get("/health")
    def health():
        return {"status": "ok"}
    
    @app.get("/status")
    def status():
        return {"status": "ok", "details": "detailed info"}
    
    return app


def test_public_endpoint_no_auth(app_with_auth):
    """Public endpoints don't require authentication."""
    client = TestClient(app_with_auth)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_protected_endpoint_missing_key(app_with_auth):
    """Protected endpoint returns 401 without API key."""
    client = TestClient(app_with_auth)
    response = client.get("/protected")
    assert response.status_code == 401
    assert "Missing API key" in response.json()["detail"]


def test_protected_endpoint_missing_user_email(app_with_auth):
    """Protected endpoint returns 400 without X-User-Email header."""
    client = TestClient(app_with_auth)
    response = client.get(
        "/protected",
        headers={"X-API-Key": "test_valid_key"}
    )
    assert response.status_code == 400
    assert "Missing X-User-Email header" in response.json()["detail"]


def test_protected_endpoint_invalid_key(app_with_auth):
    """Protected endpoint returns 401 with invalid API key."""
    client = TestClient(app_with_auth)
    response = client.get(
        "/protected",
        headers={
            "X-API-Key": "invalid_key",
            "X-User-Email": "user@example.com"
        }
    )
    assert response.status_code == 401
    assert "Invalid or expired" in response.json()["detail"]


def test_protected_endpoint_expired_key(app_with_auth):
    """Protected endpoint returns 401 with expired API key."""
    client = TestClient(app_with_auth)
    response = client.get(
        "/protected",
        headers={
            "X-API-Key": "test_expired_key",
            "X-User-Email": "user@example.com"
        }
    )
    assert response.status_code == 401
    assert "Invalid or expired" in response.json()["detail"]


def test_protected_endpoint_inactive_key(app_with_auth):
    """Protected endpoint returns 401 with inactive API key."""
    client = TestClient(app_with_auth)
    response = client.get(
        "/protected",
        headers={
            "X-API-Key": "test_inactive_key",
            "X-User-Email": "user@example.com"
        }
    )
    assert response.status_code == 401
    assert "Invalid or expired" in response.json()["detail"]


def test_protected_endpoint_valid_key(app_with_auth):
    """Protected endpoint succeeds with valid API key and matching user email."""
    client = TestClient(app_with_auth)
    response = client.get(
        "/protected",
        headers={
            "X-API-Key": "test_valid_key",
            "X-User-Email": "test@example.com"  # Must match key owner
        }
    )
    assert response.status_code == 200
    data = response.json()
    assert data["user_email"] == "test@example.com"
    assert data["user_name"] == "Test User"
    assert data["permissions"] == ["admin"]


def test_email_mismatch_rejected(app_with_auth):
    """
    Scenario: User provides email that doesn't match API key owner.
    
    Setup:
      1. API key "test_valid_key" belongs to test@example.com
      2. Request sent with X-User-Email: different@example.com
    
    Expected:
      - 401 response
      - Error message explains mismatch
      - Suggests checking MCP configuration
    """
    client = TestClient(app_with_auth)
    response = client.get(
        "/protected",
        headers={
            "X-API-Key": "test_valid_key",
            "X-User-Email": "different@example.com"  # Doesn't match key owner
        }
    )
    assert response.status_code == 401
    detail = response.json()["detail"]
    assert "Email mismatch" in detail
    assert "test@example.com" in detail  # Key owner email mentioned
    assert "different@example.com" in detail  # Provided email mentioned
    assert "MCP configuration" in detail  # Helpful guidance


def test_email_match_case_insensitive(app_with_auth):
    """
    Scenario: Email comparison is case-insensitive.
    
    Setup:
      1. API key belongs to test@example.com (lowercase)
      2. Request sent with X-User-Email: Test@Example.com (mixed case)
    
    Expected:
      - 200 response (emails match after normalization)
      - User email normalized to lowercase in request.state
    """
    client = TestClient(app_with_auth)
    response = client.get(
        "/protected",
        headers={
            "X-API-Key": "test_valid_key",
            "X-User-Email": "Test@Example.com"  # Mixed case
        }
    )
    assert response.status_code == 200
    data = response.json()
    # Email should be normalized to lowercase
    assert data["user_email"] == "test@example.com"


def test_email_validation_with_bearer_token(app_with_auth):
    """Email validation works with Bearer token format."""
    client = TestClient(app_with_auth)
    
    # Valid email match
    response = client.get(
        "/protected",
        headers={
            "Authorization": "Bearer test_valid_key",
            "X-User-Email": "test@example.com"
        }
    )
    assert response.status_code == 200
    
    # Invalid email mismatch
    response = client.get(
        "/protected",
        headers={
            "Authorization": "Bearer test_valid_key",
            "X-User-Email": "wrong@example.com"
        }
    )
    assert response.status_code == 401
    assert "Email mismatch" in response.json()["detail"]


def test_bearer_token_format(app_with_auth):
    """API key can be passed as Bearer token."""
    client = TestClient(app_with_auth)
    response = client.get(
        "/protected",
        headers={
            "Authorization": "Bearer test_valid_key",
            "X-User-Email": "test@example.com"  # Must match key owner
        }
    )
    assert response.status_code == 200
    assert response.json()["user_email"] == "test@example.com"


def test_last_used_updated(app_with_auth, db):
    """Last used timestamp is updated on successful auth."""
    client = TestClient(app_with_auth)
    
    # Initial last_used should be None
    key_doc = db.get("api_keys", "test_valid_key")
    assert key_doc.get("last_used_at") is None
    
    # Make request
    response = client.get(
        "/protected",
        headers={
            "X-API-Key": "test_valid_key",
            "X-User-Email": "test@example.com"  # Must match key owner
        }
    )
    assert response.status_code == 200
    
    # Last_used should be updated
    key_doc = db.get("api_keys", "test_valid_key")
    assert key_doc["last_used_at"] is not None
    assert isinstance(key_doc["last_used_at"], datetime)


def test_multiple_protected_endpoints(app_with_auth):
    """Multiple protected endpoints all require auth."""
    app = app_with_auth
    
    # Add another protected endpoint
    @app.get("/another_protected")
    def another_protected(request: Request):
        return {"message": "success"}
    
    client = TestClient(app)
    
    # Without auth
    response = client.get("/another_protected")
    assert response.status_code == 401
    
    # With auth
    response = client.get(
        "/another_protected",
        headers={
            "X-API-Key": "test_valid_key",
            "X-User-Email": "test@example.com"  # Must match key owner
        }
    )
    assert response.status_code == 200


def test_public_paths_list():
    """Verify public paths are correctly defined."""
    public_paths = AuthMiddleware.PUBLIC_PATHS
    assert "/health" in public_paths
    assert "/health/live" in public_paths
    assert "/health/ready" in public_paths
    assert "/docs" in public_paths
    assert "/openapi.json" in public_paths


def test_docs_endpoints_public(app_with_auth):
    """Documentation endpoints are public."""
    client = TestClient(app_with_auth)
    
    # OpenAPI schema should be accessible
    response = client.get("/openapi.json")
    assert response.status_code == 200


def test_status_endpoint_requires_auth(app_with_auth):
    """Status endpoint requires authentication (not in PUBLIC_PATHS)."""
    client = TestClient(app_with_auth)
    
    # Without auth - should return 401
    response = client.get("/status")
    assert response.status_code == 401
    assert "Missing API key" in response.json()["detail"]
    
    # With auth - should succeed
    response = client.get(
        "/status",
        headers={
            "X-API-Key": "test_valid_key",
            "X-User-Email": "test@example.com"  # Must match key owner
        }
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "details": "detailed info"}
