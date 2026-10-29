"""Tier 1: auth gate, /me, projects, error envelope, CORS (AC-7.1 to 7.4, 7.12)."""

from __future__ import annotations

import pytest

from db import repositories as repo
from specflow_api_support import bearer

AUTH_ROUTES = [
    ("GET", "/api/v1/me"),
    ("GET", "/api/v1/projects"),
    ("POST", "/api/v1/projects"),
    ("GET", "/api/v1/projects/00000000-0000-0000-0000-000000000001"),
    ("GET", "/api/v1/projects/00000000-0000-0000-0000-000000000001/documents"),
    (
        "POST",
        "/api/v1/projects/00000000-0000-0000-0000-000000000001/documents?filename=a.pdf",
    ),
    (
        "GET",
        "/api/v1/projects/00000000-0000-0000-0000-000000000001/documents/00000000-0000-0000-0000-000000000002",
    ),
    (
        "POST",
        "/api/v1/projects/00000000-0000-0000-0000-000000000001/documents/00000000-0000-0000-0000-000000000002/parse",
    ),
    (
        "POST",
        "/api/v1/projects/00000000-0000-0000-0000-000000000001/documents/00000000-0000-0000-0000-000000000002/summarize",
    ),
    ("POST", "/api/v1/projects/00000000-0000-0000-0000-000000000001/graph_build"),
    ("GET", "/api/v1/projects/00000000-0000-0000-0000-000000000001/graph_build"),
]


@pytest.mark.parametrize(("method", "path"), AUTH_ROUTES)
async def test_every_api_route_requires_a_bearer_token(harness, method, path):
    for headers in (
        {},
        {"Authorization": "Bearer not-a-token"},
        {"Authorization": "Basic abc"},
    ):
        response = await harness.client.request(method, path, headers=headers)
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
        error = response.json()["error"]
        assert error["code"] == "invalid_token"
        assert set(error) == {"code", "message", "details"}


async def test_health_endpoints_need_no_token(harness):
    assert (await harness.client.get("/healthz")).json() == {"status": "ok"}
    assert (await harness.client.get("/readyz")).status_code == 200


async def test_email_policy_failures_are_403_without_www_authenticate(harness):
    unverified = await harness.client.get("/api/v1/me", headers=bearer(email_verified=False))
    other_domain = await harness.client.get("/api/v1/me", headers=bearer("mallory@evil.test"))
    for response, code in (
        (unverified, "email_not_verified"),
        (other_domain, "domain_not_allowed"),
    ):
        assert response.status_code == 403
        assert response.json()["error"]["code"] == code
        assert "www-authenticate" not in response.headers


async def test_me_provisions_just_in_time_and_refreshes_without_duplicates(harness):
    first = (await harness.client.get("/api/v1/me", headers=bearer("Alice.Smith@example.com"))).json()
    assert first["actor_id"] == "human:alice.smith"
    assert set(first) == {"id", "actor_id", "email", "display_name"}
    again = await harness.client.get("/api/v1/me", headers=bearer("Alice.Smith@example.com", name="Alice S."))
    assert again.json()["id"] == first["id"]
    assert again.json()["display_name"] == "Alice S."
    assert len(harness.repo.users) == 1


async def test_create_project_returns_201_with_an_owner_member(harness):
    response = await harness.client.post("/api/v1/projects", json={"name": "  Demo  "}, headers=bearer())
    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"id", "name", "created_at"}
    assert body["name"] == "Demo"
    [(key, role)] = harness.repo.roles.items()
    assert str(key[0]) == body["id"] and role == "owner"


@pytest.mark.parametrize("name", ["", "   ", "x" * 201])
async def test_a_blank_or_long_project_name_is_422(harness, name):
    response = await harness.client.post("/api/v1/projects", json={"name": name}, headers=bearer())
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_failed"
    assert "xxx" not in response.text  # the submitted value is never echoed back


async def test_200_chars_is_accepted_and_unknown_fields_are_refused(harness):
    ok = await harness.client.post("/api/v1/projects", json={"name": "x" * 200}, headers=bearer())
    extra = await harness.client.post("/api/v1/projects", json={"name": "a", "role": "x"}, headers=bearer())
    assert ok.status_code == 201 and extra.status_code == 422


async def test_projects_list_paginates_newest_first(harness):
    for index in range(3):
        await harness.client.post("/api/v1/projects", json={"name": f"p{index}"}, headers=bearer())
    first = (await harness.client.get("/api/v1/projects?limit=2", headers=bearer())).json()
    assert [p["name"] for p in first["items"]] == ["p2", "p1"]
    second = (
        await harness.client.get(f"/api/v1/projects?limit=2&cursor={first['next_cursor']}", headers=bearer())
    ).json()
    assert [p["name"] for p in second["items"]] == ["p0"] and second["next_cursor"] is None


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "cursor=%25%25%25"])
async def test_bad_pagination_is_422(harness, query):
    response = await harness.client.get(f"/api/v1/projects?{query}", headers=bearer())
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_failed"


async def test_other_users_projects_are_404_never_403(harness):
    project = (await harness.client.post("/api/v1/projects", json={"name": "mine"}, headers=bearer())).json()
    pid = project["id"]
    bob = bearer("bob@example.com")
    assert (await harness.client.get(f"/api/v1/projects/{pid}", headers=bob)).status_code == 404
    listing = (await harness.client.get("/api/v1/projects", headers=bob)).json()
    assert listing["items"] == []
    for method, path in (
        ("GET", f"/api/v1/projects/{pid}/documents"),
        (
            "GET",
            f"/api/v1/projects/{pid}/documents/00000000-0000-0000-0000-000000000002",
        ),
        ("POST", f"/api/v1/projects/{pid}/documents?filename=a.pdf"),
    ):
        response = await harness.client.request(method, path, headers={**bob, "Content-Type": "application/pdf"})
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "project_not_found"


async def test_an_unknown_route_and_a_bad_uuid_use_the_envelope(harness):
    missing = await harness.client.get("/api/v1/nope", headers=bearer())
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"
    bad = await harness.client.get("/api/v1/projects/not-a-uuid", headers=bearer())
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "validation_failed"


async def test_an_unhandled_error_is_a_500_without_details(harness, monkeypatch):

    async def boom(*args, **kwargs):
        raise RuntimeError("password=hunter2 postgres://secret")

    monkeypatch.setattr(repo, "list_projects", boom)
    response = await harness.client.get("/api/v1/projects", headers=bearer())
    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "internal_error",
            "message": "An internal error occurred.",
            "details": {},
        }
    }
    assert "hunter2" not in response.text


async def test_cors_is_off(harness):
    response = await harness.client.options(
        "/api/v1/me",
        headers={"Origin": "https://evil.test", "Access-Control-Request-Method": "GET"},
    )
    assert "access-control-allow-origin" not in response.headers
    simple = await harness.client.get("/healthz", headers={"Origin": "https://evil.test"})
    assert "access-control-allow-origin" not in simple.headers
