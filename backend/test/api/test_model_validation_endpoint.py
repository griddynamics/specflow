"""API tests for POST /api/v1/models/validate.

Mounts the router on a minimal app (no auth middleware, matching the other API
tests) and patches the catalog fetch so no real HTTP is made.
"""
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.dependencies import require_authenticated_user
from app.api.v1.model_validation import router as model_validation_router


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(model_validation_router, prefix="/api/v1")
    # Auth middleware isn't mounted in unit tests; satisfy the router's auth guard.
    app.dependency_overrides[require_authenticated_user] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_validate_requires_authentication():
    """Without the auth guard satisfied, the endpoint rejects with 401."""
    app = FastAPI()
    app.include_router(model_validation_router, prefix="/api/v1")
    resp = TestClient(app).post("/api/v1/models/validate", data={})
    assert resp.status_code == 401


def _patch_catalog(models: set):
    return patch(
        "app.services.model_validation.fetch_catalog_for_provider",
        new=AsyncMock(return_value=models),
    )


def test_validate_all_valid(client):
    catalog = {
        "anthropic/claude-opus-4.6",
        "anthropic/claude-sonnet-4.6",
        "anthropic/claude-haiku-4.5",
    }
    with _patch_catalog(catalog):
        resp = client.post(
            "/api/v1/models/validate",
            data={
                "LLM_HIGH": "anthropic/claude-opus-4.6",
                "LLM_MEDIUM": "anthropic/claude-sonnet-4.6",
                "LLM_LOW": "anthropic/claude-haiku-4.5",
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["all_valid"] is True
    assert body["has_blocking_tier"] is False
    assert body["catalog_available"] is True


def test_validate_blocking_tier_with_suggestion(client):
    catalog = {
        "anthropic/claude-opus-4.6",
        "anthropic/claude-sonnet-4.6",
        "anthropic/claude-haiku-4.5",
    }
    with _patch_catalog(catalog):
        resp = client.post(
            "/api/v1/models/validate",
            data={"LLM_HIGH": "anthropic/claude-opus-4.7"},  # typo
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["has_blocking_tier"] is True
    assert body["all_valid"] is False
    high = next(t for t in body["tiers"] if t["tier"] == "LLM_HIGH")
    assert high["blocking"] is True
    assert high["models"][0]["status"] == "invalid"
    assert high["models"][0]["suggestion"] == "anthropic/claude-opus-4.6"


def test_validate_catalog_unreachable_is_not_blocking(client):
    with _patch_catalog(set()):
        resp = client.post(
            "/api/v1/models/validate",
            data={"LLM_HIGH": "anthropic/anything"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["catalog_available"] is False
    assert body["has_blocking_tier"] is False
