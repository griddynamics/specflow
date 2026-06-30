"""
API-level isolation hardening tests.

Covers:
  PR255 — /workspace/sync reuse only while generation session is PENDING
  Steel Commandments II/V + HR-2 — ensure_workspaces_for_sync reuses or reallocates safely
  Phase 2/T5 — get_auth_me clears stale current_process pointing to wrong key
  Scenario 17 — at-capacity guard before workspace allocation
"""

import io
import json
import os
import tarfile
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.v1.auth import router as auth_router
from app.api.v1.workspaces import (
    get_generation_session_service,
    get_workspace_pool,
    router as workspaces_router,
)
from app.core.github_platform_secrets import (
    init_github_platform_secrets_for_tests,
    reset_github_platform_secrets,
)
from app.database.dependencies import get_db
from app.database.factory import clear_test_data, get_database
from app.schemas.generation_workflow_enums import GenerationStatus, WorkspaceStatus
from app.services.contract_validator import RejectionCode
from app.services.generation_session import GenerationSessionService
from app.services.workspace_pool import WorkspacePoolService
from app.state.db_adapter import COL_GENERATION_SESSIONS


# ─────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────


def _small_tar_gz() -> bytes:
    """Return a minimal valid tar.gz archive."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        content = b"hello"
        ti = tarfile.TarInfo(name="hello.txt")
        ti.size = len(content)
        tar.addfile(ti, io.BytesIO(content))
    return buf.getvalue()


def _generation_contract_tar(
    outputs_dir: str = "docs",
    plan_content: bytes = b"# Implementation Plan\n\n## Phase 1\n\nBuild the smallest useful slice.\n",
) -> bytes:
    """Return a minimal archive satisfying deterministic generation contract checks."""
    files = {
        "specs/spec.md": b"# Spec\n",
        f"{outputs_dir}/analysis/specification_completeness.md": (
            b"# Specification Completeness\n\n## Part F\n\nLOCAL_ONLY\n"
        ),
        f"{outputs_dir}/planning/IMPLEMENTATION_PLAN.md": plan_content,
    }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in files.items():
            ti = tarfile.TarInfo(name=name)
            ti.size = len(content)
            tar.addfile(ti, io.BytesIO(content))
    return buf.getvalue()


class MockUserMiddleware(BaseHTTPMiddleware):
    """Sets request.state from constructor kwargs — no DB involved."""

    def __init__(self, app, user_email="u@u.com", key_uid="uid-test",
                 workspace_pool="default", api_key="gain_testkey"):
        super().__init__(app)
        self.user_email = user_email
        self.key_uid = key_uid
        self.workspace_pool = workspace_pool
        self.api_key = api_key

    async def dispatch(self, request: Request, call_next):
        request.state.user_email = self.user_email
        request.state.user_id = self.user_email
        request.state.api_key = self.api_key
        request.state.key_uid = self.key_uid
        request.state.workspace_pool = self.workspace_pool
        request.state.permissions = ["admin"]
        request.state.user_name = "Test User"
        return await call_next(request)


# ─────────────────────────────────────────────
# Phase 5b — workspace reuse ownership guard
# ─────────────────────────────────────────────


@pytest.fixture(autouse=True)
def clean_db():
    """Ensure fresh DB for each test."""
    if "DATABASE_TYPE" not in os.environ:
        os.environ["DATABASE_TYPE"] = "memory"
    clear_test_data()
    yield
    clear_test_data()


@pytest.fixture(autouse=True)
def _github_platform_secrets():
    """allocate_workspace_set requires platform GitHub PAT resolution."""
    init_github_platform_secrets_for_tests(
        fernet_key=Fernet.generate_key(),
        github_token_default="unit-test-default-github-token",
        git_user_name_default="unit-test-git-user",
    )
    yield
    reset_github_platform_secrets()


@pytest.fixture
def sample_workspaces():
    """Pool workspaces available for allocation (workspace_pool=default)."""
    db = get_database()
    now = datetime.now(timezone.utc)
    for set_num in (1, 2):
        for i in range(1, 4):
            ws_id = f"ws-{set_num:02d}-{i}"
            db.set(
                "workspaces",
                ws_id,
                {
                    "repo_url": f"https://github.com/org/{ws_id}",
                    "p10y_repository_id": 74900 + (set_num * 10) + i,
                    "workspace_pool": "default",
                    "set_number": set_num,
                    "status": WorkspaceStatus.AVAILABLE.value,
                    "locked_by": None,
                    "locked_at": None,
                    "lease_expires_at": None,
                    "clean_verified": True,
                    "last_used_by": None,
                    "last_cleaned_at": now,
                    "allocation_history": [],
                    "error": None,
                },
            )


def _seed_api_key_alice():
    db = get_database()
    db.set(
        "api_keys",
        "gain_testkey",
        {
            "api_key": "gain_testkey",
            "key_uid": "uid-alice",
            "workspace_pool": "default",
            "user_id": "alice@example.com",
            "user_name": "Alice",
            "is_active": True,
            "expires_at": None,
            "permissions": ["user"],
            "max_concurrent_sessions": 5,
            "active_generation_sessions": [],
        },
    )


def _seed_pending_session(
    est_id: str,
    *,
    workspace_ids: list[str] | None = None,
    key_uid: str = "uid-alice",
    user_email: str = "alice@example.com",
    workspace_pool: str = "default",
) -> None:
    db = get_database()
    db.set(
        COL_GENERATION_SESSIONS,
        est_id,
        {
            "generation_id": est_id,
            "user_email": user_email,
            "workspace_pool": workspace_pool,
            "key_uid": key_uid,
            "workspace_ids": workspace_ids or [],
            "status": GenerationStatus.PENDING.value,
            "parameters": {"workspace_count": 1},
        },
    )


def _mark_workspace_allocated(ws_id: str, generation_id: str) -> None:
    db = get_database()
    db.update(
        "workspaces",
        ws_id,
        {
            "status": WorkspaceStatus.ALLOCATED.value,
            "locked_by": generation_id,
        },
    )


@pytest.fixture
def isolation_app(tmp_path, sample_workspaces):
    """
    App wired for workspace sync tests with real GenerationSessionService +
    WorkspacePoolService (in-memory DB). Tracks allocate_workspace_set calls.
    """
    _seed_api_key_alice()
    db = get_database()
    pool = WorkspacePoolService(db, workspace_base_path=tmp_path)

    async def _mock_ensure_repo_cloned(workspace_id: str, ws_doc: dict, generation_id: str):
        path = pool._get_workspace_path(workspace_id)
        path.mkdir(parents=True, exist_ok=True)
        (path / ".git").mkdir(exist_ok=True)

    pool._ensure_repo_cloned = AsyncMock(side_effect=_mock_ensure_repo_cloned)
    pool.initialize_git_repo = AsyncMock()

    alloc_calls: list[tuple] = []
    _real_allocate = pool.allocate_workspace_set

    async def _tracked_allocate(*args, **kwargs):
        alloc_calls.append((args, kwargs))
        return await _real_allocate(*args, **kwargs)

    pool.allocate_workspace_set = _tracked_allocate

    gen_svc = GenerationSessionService(db, pool)

    app = FastAPI()
    app.add_middleware(
        MockUserMiddleware,
        user_email="alice@example.com",
        key_uid="uid-alice",
        workspace_pool="default",
        api_key="gain_testkey",
    )
    app.include_router(workspaces_router, prefix="/api/v1")

    async def override_pool():
        return pool

    async def override_est_svc():
        return gen_svc

    async def override_db():
        return db

    app.dependency_overrides[get_workspace_pool] = override_pool
    app.dependency_overrides[get_generation_session_service] = override_est_svc
    app.dependency_overrides[get_db] = override_db

    yield app, pool, gen_svc, alloc_calls


def _sync_request(params: dict, archive_bytes: bytes):
    """Build multipart form data for /workspace/sync."""
    return {
        "files": {"archive": ("test.tar.gz", io.BytesIO(archive_bytes), "application/gzip")},
        "data": {"params": json.dumps(params)},
    }


class TestWorkspaceSyncGenerationPreflight:
    """First-run generation contract rejections must happen before allocation."""

    def test_rejects_first_generation_sync_before_session_or_allocation(self, isolation_app):
        app, _pool, _gen_svc, alloc_calls = isolation_app
        db = get_database()

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/api/v1/workspace/sync",
            **_sync_request(
                {
                    "workflow_type": "generation_run",
                    "outputs_dir": "docs",
                    "workspace_count": 1,
                },
                _small_tar_gz(),
            ),
        )

        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert detail["code"] == RejectionCode.ANALYSIS_MISSING.value
        assert len(alloc_calls) == 0
        assert db.query(COL_GENERATION_SESSIONS) == []

    @pytest.mark.parametrize(
        ("plan_content", "code"),
        [
            (b"# Implementation Plan\n\nNo phases yet.\n", RejectionCode.PLAN_NO_PHASES.value),
            (b"# Implementation Plan\n\n## Phase 1\n", RejectionCode.PLAN_UNPARSEABLE.value),
        ],
    )
    def test_rejects_bad_plan_before_session_or_allocation(
        self, isolation_app, plan_content, code
    ):
        app, _pool, _gen_svc, alloc_calls = isolation_app
        db = get_database()

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/api/v1/workspace/sync",
            **_sync_request(
                {
                    "workflow_type": "generation_run",
                    "outputs_dir": "docs",
                    "workspace_count": 1,
                },
                _generation_contract_tar(plan_content=plan_content),
            ),
        )

        assert resp.status_code == 400
        assert resp.json()["detail"]["code"] == code
        assert len(alloc_calls) == 0
        assert db.query(COL_GENERATION_SESSIONS) == []

    def test_allows_valid_first_generation_sync(self, isolation_app):
        app, _pool, _gen_svc, alloc_calls = isolation_app
        db = get_database()

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/api/v1/workspace/sync",
            **_sync_request(
                {
                    "workflow_type": "generation_run",
                    "outputs_dir": "docs",
                    "workspace_count": 1,
                },
                _generation_contract_tar(),
            ),
        )

        assert resp.status_code == 201
        data = resp.json()
        assert len(data["workspace_ids"]) == 1
        assert len(alloc_calls) == 1
        sessions = db.query(COL_GENERATION_SESSIONS)
        assert len(sessions) == 1
        assert sessions[0]["generation_id"] == data["generation_id"]


class TestWorkspaceSyncReuseStatusGuard:
    """PR255: sync with generation_id is only allowed while session status is PENDING."""

    @pytest.mark.parametrize(
        "status",
        [
            GenerationStatus.RUNNING,
            GenerationStatus.FAILED,
            GenerationStatus.COMPLETED,
            GenerationStatus.INITIALIZING,
        ],
    )
    def test_blocks_reuse_when_session_not_pending(self, isolation_app, tmp_path, status):
        app, pool, _gen_svc, alloc_calls = isolation_app
        est_id = "est-active"
        db = get_database()
        _seed_pending_session(est_id, workspace_ids=["ws-01-1"])
        db.update(COL_GENERATION_SESSIONS, est_id, {"status": status.value})
        _mark_workspace_allocated("ws-01-1", est_id)
        (tmp_path / "ws-01-1").mkdir(parents=True, exist_ok=True)

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/api/v1/workspace/sync",
            **_sync_request({"generation_id": est_id, "spec_file": "spec.md"}, _small_tar_gz()),
        )

        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert est_id in detail
        assert status.value in detail
        assert "retry_generation" in detail
        assert len(alloc_calls) == 0, "must not allocate while session is not pending"


class TestWorkspaceSyncEnsureWorkspaces:
    """HR-2 / Commandment V: reuse valid ALLOCATED workspaces or allocate a fresh set."""

    def test_reuse_succeeds_when_pending_and_workspaces_still_locked(
        self, isolation_app, tmp_path
    ):
        app, pool, _gen_svc, alloc_calls = isolation_app
        est_id = "est-happy"
        _seed_pending_session(est_id, workspace_ids=["ws-01-1"])
        _mark_workspace_allocated("ws-01-1", est_id)
        (tmp_path / "ws-01-1").mkdir(parents=True, exist_ok=True)

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/api/v1/workspace/sync",
            **_sync_request({"generation_id": est_id, "spec_file": "spec.md"}, _small_tar_gz()),
        )

        assert resp.status_code == 201
        data = resp.json()
        assert data["generation_id"] == est_id
        assert data["workspace_ids"] == ["ws-01-1"]
        assert len(alloc_calls) == 0, "must reuse existing ALLOCATED workspaces"

        ws_doc = get_database().get("workspaces", "ws-01-1")
        assert ws_doc["status"] == WorkspaceStatus.ALLOCATED.value
        assert ws_doc["locked_by"] == est_id

    def test_reallocates_when_pending_and_workspace_ids_cleared_after_contract_reject(
        self, isolation_app, tmp_path
    ):
        """Contract rejection path: PENDING + empty workspace_ids → fresh allocation."""
        app, _pool, _gen_svc, alloc_calls = isolation_app
        est_id = "est-reject-retry"
        _seed_pending_session(est_id, workspace_ids=[])

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/api/v1/workspace/sync",
            **_sync_request(
                {"generation_id": est_id, "spec_file": "spec.md", "workspace_count": 1},
                _small_tar_gz(),
            ),
        )

        assert resp.status_code == 201
        data = resp.json()
        assert data["generation_id"] == est_id
        assert len(data["workspace_ids"]) == 1
        assert len(alloc_calls) == 1

        est_doc = get_database().get(COL_GENERATION_SESSIONS, est_id)
        assert est_doc["workspace_ids"] == data["workspace_ids"]
        new_ws = data["workspace_ids"][0]
        (tmp_path / new_ws).mkdir(parents=True, exist_ok=True)
        ws_doc = get_database().get("workspaces", new_ws)
        assert ws_doc["status"] == WorkspaceStatus.ALLOCATED.value
        assert ws_doc["locked_by"] == est_id

    def test_reallocates_when_stale_workspace_no_longer_allocated(
        self, isolation_app, tmp_path
    ):
        """Stale workspace_ids after release must not block upload — allocate fresh."""
        app, _pool, _gen_svc, alloc_calls = isolation_app
        est_id = "est-zombie"
        _seed_pending_session(est_id, workspace_ids=["ws-zombie-1"])
        db = get_database()
        db.set(
            "workspaces",
            "ws-zombie-1",
            {
                "workspace_id": "ws-zombie-1",
                "workspace_pool": "default",
                "set_number": 99,
                "status": WorkspaceStatus.AVAILABLE.value,
                "locked_by": None,
                "clean_verified": True,
            },
        )

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/api/v1/workspace/sync",
            **_sync_request(
                {"generation_id": est_id, "spec_file": "spec.md", "workspace_count": 1},
                _small_tar_gz(),
            ),
        )

        assert resp.status_code == 201
        data = resp.json()
        assert "ws-zombie-1" not in data["workspace_ids"]
        assert len(alloc_calls) == 1
        est_doc = get_database().get(COL_GENERATION_SESSIONS, est_id)
        assert est_doc["workspace_ids"] == data["workspace_ids"]

    def test_reallocates_when_workspace_locked_by_different_generation(
        self, isolation_app, tmp_path
    ):
        """Must not sync into a workspace owned by another generation (theft guard)."""
        app, _pool, _gen_svc, alloc_calls = isolation_app
        est_id = "est-original"
        _seed_pending_session(est_id, workspace_ids=["ws-stolen"])
        db = get_database()
        db.set(
            "workspaces",
            "ws-stolen",
            {
                "workspace_id": "ws-stolen",
                "workspace_pool": "default",
                "set_number": 99,
                "status": WorkspaceStatus.ALLOCATED.value,
                "locked_by": "est-different",
                "clean_verified": True,
            },
        )

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/api/v1/workspace/sync",
            **_sync_request(
                {"generation_id": est_id, "spec_file": "spec.md", "workspace_count": 1},
                _small_tar_gz(),
            ),
        )

        assert resp.status_code == 201
        data = resp.json()
        assert "ws-stolen" not in data["workspace_ids"]
        assert len(alloc_calls) == 1
        new_ws = data["workspace_ids"][0]
        ws_doc = db.get("workspaces", new_ws)
        assert ws_doc["locked_by"] == est_id


class TestWorkspaceSyncOwnership:
    """I-03/I-04: generation_id reuse requires matching owner and key_uid."""

    def test_blocks_wrong_key_uid(self, isolation_app, tmp_path):
        app, _pool, _gen_svc, alloc_calls = isolation_app
        est_id = "est-k2-only"
        _seed_pending_session(est_id, workspace_ids=[], key_uid="uid-k2")

        app_k1 = FastAPI()
        app_k1.add_middleware(
            MockUserMiddleware,
            user_email="alice@example.com",
            key_uid="uid-alice",
            workspace_pool="default",
            api_key="gain_testkey",
        )
        app_k1.include_router(workspaces_router, prefix="/api/v1")
        app_k1.dependency_overrides.update(app.dependency_overrides)

        client = TestClient(app_k1, raise_server_exceptions=False)
        resp = client.post(
            "/api/v1/workspace/sync",
            **_sync_request({"generation_id": est_id, "spec_file": "spec.md"}, _small_tar_gz()),
        )

        assert resp.status_code == 403
        assert "different API key" in resp.json()["detail"]
        assert len(alloc_calls) == 0

    def test_blocks_wrong_user_email(self, isolation_app):
        app, _pool, _gen_svc, alloc_calls = isolation_app
        est_id = "est-bob"
        _seed_pending_session(
            est_id,
            workspace_ids=[],
            user_email="bob@example.com",
            key_uid="uid-bob",
        )

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/api/v1/workspace/sync",
            **_sync_request({"generation_id": est_id, "spec_file": "spec.md"}, _small_tar_gz()),
        )

        assert resp.status_code == 403
        assert "owned by another user" in resp.json()["detail"]
        assert len(alloc_calls) == 0


# ─────────────────────────────────────────────
# Phase 2/T5 — get_auth_me stale current_process guard
# ─────────────────────────────────────────────


@pytest.fixture
def auth_app():
    """App wired for get_auth_me tests, using the real in-memory DB."""
    app = FastAPI()
    # AuthMiddleware does a db.get("api_keys", api_key) — use real DB
    from app.middleware.auth import AuthMiddleware
    db = get_database()
    app.add_middleware(AuthMiddleware, db=db)
    app.include_router(auth_router, prefix="/api/v1")
    yield app, db


def test_get_auth_me_clears_stale_current_process_wrong_key_uid(auth_app):
    """
    I-05: get_auth_me returns current_process=None when the active generation session
    points at an generation owned by a different key_uid (read-only; no api_keys writes).
    """
    app, db = auth_app
    now = datetime.now(timezone.utc)

    # Two different API keys for the same user
    db.set("api_keys", "gain_k1", {
        "api_key": "gain_k1",
        "key_uid": "uid-k1",
        "workspace_pool": "default",
        "user_id": "alice@example.com",
        "user_name": "Alice",
        "is_active": True,
        "expires_at": None,
        "permissions": ["user"],
        "max_concurrent_sessions": 5,
        "active_generation_sessions": [
            {
                "generation_id": "est-k2-owned",
                "operation": "analysis",
                "lease_started_at": now,
                "lease_ttl_minutes": 60,
            }
        ],
    })
    # The generation was created by K2, not K1
    db.set(COL_GENERATION_SESSIONS, "est-k2-owned", {
        "generation_id": "est-k2-owned",
        "user_email": "alice@example.com",
        "key_uid": "uid-k2",   # different key!
        "status": "running",
    })

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get(
        "/api/v1/auth/me",
        headers={
            "X-API-Key": "gain_k1",
            "X-User-Email": "alice@example.com",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["current_process"] is None

    key_doc = db.get("api_keys", "gain_k1")
    assert len(key_doc.get("active_generation_sessions") or []) == 1


def test_get_auth_me_keeps_valid_current_process(auth_app):
    """
    T5 happy path: generation session belongs to the same key → expose as current_process.
    """
    app, db = auth_app
    now = datetime.now(timezone.utc)

    db.set("api_keys", "gain_k2", {
        "api_key": "gain_k2",
        "key_uid": "uid-k2",
        "workspace_pool": "default",
        "user_id": "bob@example.com",
        "user_name": "Bob",
        "is_active": True,
        "expires_at": None,
        "permissions": ["user"],
        "max_concurrent_sessions": 5,
        "active_generation_sessions": [
            {
                "generation_id": "est-k2-owned",
                "operation": "analysis",
                "lease_started_at": now,
                "lease_ttl_minutes": 60,
            }
        ],
    })
    db.set(COL_GENERATION_SESSIONS, "est-k2-owned", {
        "generation_id": "est-k2-owned",
        "user_email": "bob@example.com",
        "key_uid": "uid-k2",   # same key — valid
        "status": "running",
    })

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get(
        "/api/v1/auth/me",
        headers={
            "X-API-Key": "gain_k2",
            "X-User-Email": "bob@example.com",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["current_process"] == "est-k2-owned"


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 17 — workspace sync rejects AT_CAPACITY before allocating workspaces
# ─────────────────────────────────────────────────────────────────────────────


def test_workspace_sync_rejects_at_capacity_before_allocation(isolation_app, tmp_path):
    """
    Scenario 17: When the API key is at max_concurrent_sessions capacity the
    /workspace/sync endpoint must return 409 WITHOUT calling the workspace allocator.

    This prevents an orphan generation doc and orphan workspace allocation when
    the key cannot accept a new generation session.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock, patch

    app, _pool, _gen_svc, alloc_calls = isolation_app

    at_capacity_snap = SimpleNamespace(at_capacity=True)

    # Patch ApiKeySessionConcurrency so its snapshot() always reports AT_CAPACITY.
    mock_concurrency = MagicMock()
    mock_concurrency.snapshot = AsyncMock(return_value=at_capacity_snap)

    with patch("app.api.v1.workspaces.ApiKeySessionConcurrency", return_value=mock_concurrency):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/api/v1/workspace/sync",
            **_sync_request(
                params={
                    "spec_path": "specifications",
                    "outputs_dir": "specflow",
                    "user_email": "alice@example.com",
                },
                archive_bytes=_small_tar_gz(),
            ),
        )

    assert resp.status_code == 409
    assert "maximum concurrent generation sessions" in resp.json()["detail"]
    assert len(alloc_calls) == 0, "must not allocate when API key is at capacity"
