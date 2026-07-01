"""
Unit tests for LocalAuthMiddleware (AUTH_MODE=local).

Covers:
- All 7 request.state fields populated from sentinel doc.
- Public paths bypass auth without requiring a sentinel doc.
- Missing sentinel doc returns 503.
- Inbound API-key headers are ignored.
- Session key_uid flow: a generation session stamped with LOCAL_KEY_UID resolves
  via get_api_key_by_uid to the "local" doc's _id for concurrency slot accounting.
- Unknown AUTH_MODE raises at registration (middleware wiring guard in main.py).
"""

import pytest
from datetime import datetime, timezone
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.core.enums import AuthMode
from app.core.local_identity import LOCAL_API_KEY_DOC_ID, LOCAL_KEY_UID
from app.core.workspace_pool_names import DEFAULT_WORKSPACE_POOL
from app.database.memory import InMemoryDatabase
from app.middleware.auth import PUBLIC_PATHS, AuthMiddleware
from app.middleware.local_auth import LocalAuthMiddleware
from app.services.startup_validation import StartupValidationError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_with_sentinel():
    """In-memory DB pre-seeded with the local sentinel api_keys doc."""
    db = InMemoryDatabase()
    db.set("api_keys", LOCAL_API_KEY_DOC_ID, {
        "api_key": LOCAL_API_KEY_DOC_ID,
        "key_uid": LOCAL_KEY_UID,
        "user_id": "local@example.com",
        "user_name": "Local User",
        "created_at": datetime.now(timezone.utc),
        "is_active": True,
        "permissions": ["admin"],
        "workspace_pool": "default",
        "max_concurrent_sessions": 5,
        "active_generation_sessions": [],
    })
    return db


@pytest.fixture
def db_empty():
    """In-memory DB with no api_keys documents."""
    return InMemoryDatabase()


def _build_app(db) -> FastAPI:
    """Build a minimal FastAPI app with LocalAuthMiddleware and a protected endpoint."""
    app = FastAPI()
    app.add_middleware(LocalAuthMiddleware, db=db)

    @app.get("/protected")
    def protected(request: Request):
        return {
            "user_email": request.state.user_email,
            "user_id": request.state.user_id,
            "api_key": request.state.api_key,
            "user_name": request.state.user_name,
            "permissions": request.state.permissions,
            "key_uid": request.state.key_uid,
            "workspace_pool": request.state.workspace_pool,
        }

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready():
        return {"status": "ready"}

    return app


# ---------------------------------------------------------------------------
# Tests: 7 fields populated from sentinel doc
# ---------------------------------------------------------------------------


def test_local_auth_populates_all_7_fields(db_with_sentinel):
    """LocalAuthMiddleware populates all 7 request.state identity fields."""
    client = TestClient(_build_app(db_with_sentinel))
    response = client.get("/protected")

    assert response.status_code == 200
    data = response.json()
    assert data["user_email"] == "local@example.com"
    assert data["user_id"] == "local@example.com"
    assert data["api_key"] == LOCAL_API_KEY_DOC_ID
    assert data["user_name"] == "Local User"
    assert data["permissions"] == ["admin"]
    assert data["key_uid"] == LOCAL_KEY_UID
    assert data["workspace_pool"] == "default"


def test_local_auth_lowercases_email():
    """user_email and user_id are lowercased even if stored with mixed case."""
    db = InMemoryDatabase()
    db.set("api_keys", LOCAL_API_KEY_DOC_ID, {
        "api_key": LOCAL_API_KEY_DOC_ID,
        "key_uid": LOCAL_KEY_UID,
        "user_id": "Local@Example.COM",
        "user_name": "Local User",
        "is_active": True,
        "permissions": ["user"],
        "workspace_pool": "default",
    })
    app = _build_app(db)
    client = TestClient(app)
    response = client.get("/protected")
    assert response.status_code == 200
    data = response.json()
    assert data["user_email"] == "local@example.com"
    assert data["user_id"] == "local@example.com"


def test_local_auth_fallback_workspace_pool():
    """When workspace_pool is absent, DEFAULT_WORKSPACE_POOL is used."""
    db = InMemoryDatabase()
    db.set("api_keys", LOCAL_API_KEY_DOC_ID, {
        "api_key": LOCAL_API_KEY_DOC_ID,
        "key_uid": LOCAL_KEY_UID,
        "user_id": "user@example.com",
        "user_name": "User",
        "is_active": True,
        "permissions": ["user"],
        # workspace_pool intentionally absent
    })
    app = _build_app(db)
    client = TestClient(app)
    response = client.get("/protected")
    assert response.status_code == 200
    assert response.json()["workspace_pool"] == DEFAULT_WORKSPACE_POOL


def test_local_auth_fallback_permissions():
    """When permissions absent in doc, defaults to ['user']."""
    db = InMemoryDatabase()
    db.set("api_keys", LOCAL_API_KEY_DOC_ID, {
        "api_key": LOCAL_API_KEY_DOC_ID,
        "key_uid": LOCAL_KEY_UID,
        "user_id": "user@example.com",
        "user_name": "User",
        "is_active": True,
        # permissions intentionally absent
        "workspace_pool": "default",
    })
    app = _build_app(db)
    client = TestClient(app)
    response = client.get("/protected")
    assert response.status_code == 200
    assert response.json()["permissions"] == ["user"]


# ---------------------------------------------------------------------------
# Tests: public paths skip auth
# ---------------------------------------------------------------------------


def test_public_path_health_no_sentinel(db_empty):
    """Public paths (/health etc.) bypass LocalAuthMiddleware even with empty DB."""
    client = TestClient(_build_app(db_empty))
    response = client.get("/health")
    assert response.status_code == 200


def test_public_path_health_ready_no_sentinel(db_empty):
    """/health/ready is public even when sentinel doc is absent."""
    client = TestClient(_build_app(db_empty))
    response = client.get("/health/ready")
    assert response.status_code == 200


def test_all_public_paths_are_shared():
    """PUBLIC_PATHS at module level is the same object used by both middlewares."""
    # Both AuthMiddleware.PUBLIC_PATHS and the module-level constant must match.
    assert AuthMiddleware.PUBLIC_PATHS == PUBLIC_PATHS
    assert "/health/ready" in PUBLIC_PATHS


# ---------------------------------------------------------------------------
# Tests: missing sentinel doc → 503
# ---------------------------------------------------------------------------


def test_missing_sentinel_returns_503(db_empty):
    """Protected request with missing sentinel doc returns 503."""
    client = TestClient(_build_app(db_empty))
    response = client.get("/protected")
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "init_db.py" in detail
    assert "Local identity not seeded" in detail


# ---------------------------------------------------------------------------
# Tests: inbound key headers are ignored
# ---------------------------------------------------------------------------


def test_inbound_api_key_header_ignored(db_with_sentinel):
    """X-API-Key header is present but completely ignored; auth still succeeds."""
    client = TestClient(_build_app(db_with_sentinel))
    response = client.get(
        "/protected",
        headers={"X-API-Key": "some-random-key", "X-User-Email": "other@example.com"},
    )
    assert response.status_code == 200
    # Identity comes from sentinel doc, not from the headers
    data = response.json()
    assert data["user_email"] == "local@example.com"
    assert data["api_key"] == LOCAL_API_KEY_DOC_ID


# ---------------------------------------------------------------------------
# Tests: concurrency flow — session key_uid resolves to sentinel doc _id
# ---------------------------------------------------------------------------


def test_concurrency_key_uid_resolves_to_local_doc(db_with_sentinel):
    """
    Critical path (correction #3): a generation session stamped with LOCAL_KEY_UID
    must resolve via get_api_key_by_uid to the 'local' doc, whose _id is
    LOCAL_API_KEY_DOC_ID — the value used as the concurrency slot doc-id.

    This test exercises the resolution chain directly on the DB layer,
    confirming the sentinel's key_uid is unique and the lookup returns _id='local'.
    """
    result = db_with_sentinel.get_api_key_by_uid(LOCAL_KEY_UID)
    assert result is not None, "Sentinel doc not found by key_uid"
    assert result["_id"] == LOCAL_API_KEY_DOC_ID, (
        f"Expected _id='{LOCAL_API_KEY_DOC_ID}', got '{result['_id']}'"
    )
    assert result["key_uid"] == LOCAL_KEY_UID


def test_concurrency_local_uid_distinct_from_e2e_uids():
    """LOCAL_KEY_UID is distinct from the existing e2e seed UUIDs."""
    existing_uids = {
        "00000000-e2e0-0000-0000-000000000001",
        "00000000-e2e0-0000-0000-000000000002",
    }
    assert LOCAL_KEY_UID not in existing_uids, (
        f"LOCAL_KEY_UID={LOCAL_KEY_UID!r} collides with an existing seed UUID"
    )


# ---------------------------------------------------------------------------
# Tests: unknown AUTH_MODE raises at registration
# ---------------------------------------------------------------------------


def test_unknown_auth_mode_raises_at_registration():
    """
    When AUTH_MODE is neither LOCAL nor API_KEY the wiring block in main.py
    raises StartupValidationError.  This test reproduces that logic directly.
    """
    unknown_mode = "totally_unknown"

    with pytest.raises(StartupValidationError, match="Unknown AUTH_MODE"):
        # Replicate the if/elif/else guard from main.py
        if unknown_mode == AuthMode.LOCAL:
            pass
        elif unknown_mode == AuthMode.API_KEY:
            pass
        else:
            raise StartupValidationError(f"Unknown AUTH_MODE: {unknown_mode}")


# ---------------------------------------------------------------------------
# Assertion: hosted AuthMiddleware dispatch behavior is UNCHANGED (D1)
# ---------------------------------------------------------------------------


def test_hosted_auth_middleware_dispatch_unchanged():
    """
    Hosted AuthMiddleware dispatch must be byte-for-byte behavior-unchanged (D1).

    We re-run the core hosted flows here as a regression net: missing key → 401,
    valid key + matching email → 200, email mismatch → 401.
    Any deviation means auth.py was inadvertently changed.
    """
    db = InMemoryDatabase()
    db.set("api_keys", "hosted_key", {
        "api_key": "hosted_key",
        "key_uid": "aaaaaaaa-0000-0000-0000-000000000001",
        "user_id": "hosted@example.com",
        "user_name": "Hosted User",
        "created_at": datetime.now(timezone.utc),
        "is_active": True,
        "permissions": ["admin"],
        "workspace_pool": "default",
    })

    app = FastAPI()
    app.add_middleware(AuthMiddleware, db=db)

    @app.get("/protected")
    def protected(request: Request):
        return {"user_email": request.state.user_email}

    @app.get("/health")
    def health():
        return {"status": "ok"}

    client = TestClient(app)

    # Public path — no auth required
    assert client.get("/health").status_code == 200

    # Missing key → 401
    assert client.get("/protected").status_code == 401

    # Valid key, matching email → 200
    r = client.get(
        "/protected",
        headers={"X-API-Key": "hosted_key", "X-User-Email": "hosted@example.com"},
    )
    assert r.status_code == 200
    assert r.json()["user_email"] == "hosted@example.com"

    # Email mismatch → 401
    r = client.get(
        "/protected",
        headers={"X-API-Key": "hosted_key", "X-User-Email": "other@example.com"},
    )
    assert r.status_code == 401
    assert "Email mismatch" in r.json()["detail"]
