"""
Unit tests for workspace API endpoints.

Tests the workspace pool status and file sync endpoints.
"""

import io
import json
import tarfile
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import require_admin
from app.api.v1.workspaces import (
    router as workspaces_router,
    get_workspace_pool,
    get_generation_session_service,
)
from app.services.workspace_pool import (
    NoAvailableWorkspacesError,
    WorkspaceNotFoundError,
    WorkspacePoolError,
)


@pytest.fixture
def app(test_app, mock_workspace_pool, mock_generation_session_service):
    """Create FastAPI app with workspaces router and mocked dependencies."""
    # Include router
    test_app.include_router(workspaces_router, prefix="/api/v1")
    
    # Override dependencies using FastAPI's dependency override mechanism
    async def override_get_workspace_pool():
        return mock_workspace_pool
    
    async def override_get_generation_session_service():
        return mock_generation_session_service
    
    # Override the dependency functions
    test_app.dependency_overrides[get_workspace_pool] = override_get_workspace_pool
    test_app.dependency_overrides[get_generation_session_service] = override_get_generation_session_service

    # Override admin guard (tests run without auth middleware)
    async def override_require_admin():
        return None
    test_app.dependency_overrides[require_admin] = override_require_admin
    
    yield test_app
    
    # Cleanup
    test_app.dependency_overrides.clear()


@pytest.fixture
def client(app):
    """Create test client."""
    return TestClient(app)


@pytest.fixture
def sample_tar_archive():
    """Create a sample tar.gz archive for testing."""
    archive_buffer = io.BytesIO()
    with tarfile.open(fileobj=archive_buffer, mode="w:gz") as tar:
        # Add a sample file
        content = b"sample file content"
        tarinfo = tarfile.TarInfo(name="sample.txt")
        tarinfo.size = len(content)
        tar.addfile(tarinfo, io.BytesIO(content))
    archive_buffer.seek(0)
    return archive_buffer.read()


class TestGetPoolStatus:
    """Tests for GET /workspace/pool/status endpoint."""
    
    def test_get_pool_status_success(
        self, client, mock_workspace_pool
    ):
        """Test successful retrieval of pool status."""
        # Setup mock
        status_data = {
            "total": 30,
            "available": 24,
            "allocated": 3,
            "cleaning": 3,
            "stuck": 0,
            "available_sets": 8
        }
        mock_workspace_pool.get_pool_status = AsyncMock(return_value=status_data)
        
        response = client.get(
            "/api/v1/workspace/pool/status",
            headers={"X-API-Key": "test-key"}
        )
        
        assert response.status_code == 200
        data = response.json()
        
        assert data["total"] == 30
        assert data["available"] == 24
        assert data["allocated"] == 3
        assert data["cleaning"] == 3
        assert data["stuck"] == 0
        assert data["available_sets"] == 8
        
        mock_workspace_pool.get_pool_status.assert_called_once()
    
    def test_get_pool_status_empty_pool(
        self, client, mock_workspace_pool
    ):
        """Test pool status when pool is empty."""
        status_data = {
            "total": 0,
            "available": 0,
            "allocated": 0,
            "cleaning": 0,
            "stuck": 0,
            "available_sets": 0
        }
        mock_workspace_pool.get_pool_status = AsyncMock(return_value=status_data)
        
        response = client.get(
            "/api/v1/workspace/pool/status",
            headers={"X-API-Key": "test-key"}
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 0
        assert data["available_sets"] == 0
    
    def test_get_pool_status_error(
        self, client, mock_workspace_pool
    ):
        """Test error handling when pool status fails."""
        mock_workspace_pool.get_pool_status = AsyncMock(
            side_effect=Exception("Database error")
        )
        
        response = client.get(
            "/api/v1/workspace/pool/status",
            headers={"X-API-Key": "test-key"}
        )
        
        assert response.status_code == 500
        assert "Failed to get pool status" in response.json()["detail"]


class TestSyncWorkspace:
    """Tests for POST /workspace/sync endpoint."""
    
    def test_sync_workspace_success(
        self, client, mock_workspace_pool, mock_generation_session_service,
        temp_workspace_dir, sample_tar_archive
    ):
        """Test successful workspace sync."""
        # Setup mocks
        generation_id = "est-sync-123"
        workspace_ids = ["ws-01-1", "ws-01-2", "ws-01-3"]

        mock_generation_session_service.create_generation_session = AsyncMock(return_value=generation_id)
        mock_workspace_pool.allocate_workspace_set = AsyncMock(return_value=workspace_ids)
        mock_workspace_pool.workspace_base_path = temp_workspace_dir

        # Mock git initialization
        mock_workspace_pool.initialize_git_repo = AsyncMock()

        # Mock workspace_ids write
        mock_generation_session_service.set_workspace_ids = Mock()

        # Create workspace directories
        for ws_id in workspace_ids:
            (temp_workspace_dir / ws_id).mkdir(parents=True)

        # Prepare form data
        params = json.dumps({
            "spec_file": "spec.md",
            "model": "claude-sonnet-4-20250514",
            "max_retries": 3
        })

        files = {"archive": ("archive.tar.gz", io.BytesIO(sample_tar_archive), "application/gzip")}
        data = {
            "params": params,
            "user_id": "user-123"
        }

        response = client.post(
            "/api/v1/workspace/sync",
            data=data,
            files=files,
            headers={"X-API-Key": "test-key"}
        )

        assert response.status_code == 201
        response_data = response.json()

        assert response_data["generation_id"] == generation_id
        assert response_data["workspace_ids"] == workspace_ids
        assert response_data["status"] == "pending"

        # Verify service calls
        mock_generation_session_service.create_generation_session.assert_called_once()
        mock_workspace_pool.allocate_workspace_set.assert_called_once_with(
            generation_id, count=None
        )
        mock_generation_session_service.set_workspace_ids.assert_called_once()
    
    def test_sync_workspace_invalid_archive_format(
        self, client, mock_workspace_pool, mock_generation_session_service
    ):
        """Test sync fails with invalid archive format."""
        params = json.dumps({"spec_file": "spec.md"})
        
        # Create invalid file (not tar.gz)
        invalid_file = io.BytesIO(b"not a tar.gz file")
        
        files = {"archive": ("file.zip", invalid_file, "application/zip")}
        data = {
            "params": params,
            "user_id": "user-123"
        }
        
        response = client.post(
            "/api/v1/workspace/sync",
            data=data,
            files=files,
            headers={"X-API-Key": "test-key"}
        )
        
        assert response.status_code == 400
        assert "Archive must be .tar.gz or .tgz format" in response.json()["detail"]
    
    def test_sync_workspace_archive_too_large(
        self, client, mock_workspace_pool, mock_generation_session_service
    ):
        """Test sync fails when archive exceeds size limit."""
        # Create large archive (over 100MB) - use raw bytes
        # The endpoint reads the file first and checks size before processing
        large_content = b"x" * (101 * 1024 * 1024)  # 101 MB
        
        params = json.dumps({"spec_file": "spec.md"})
        files = {"archive": ("archive.tar.gz", io.BytesIO(large_content), "application/gzip")}
        data = {
            "params": params,
            "user_id": "user-123"
        }
        
        response = client.post(
            "/api/v1/workspace/sync",
            data=data,
            files=files,
            headers={"X-API-Key": "test-key"}
        )
        
        assert response.status_code == 413
        assert "Archive too large" in response.json()["detail"]
    
    def test_sync_workspace_invalid_json_params(
        self, client, mock_workspace_pool, mock_generation_session_service, sample_tar_archive
    ):
        """Test sync fails with invalid JSON in params."""
        invalid_params = "not valid json {"
        
        files = {"archive": ("archive.tar.gz", io.BytesIO(sample_tar_archive), "application/gzip")}
        data = {
            "params": invalid_params,
            "user_id": "user-123"
        }
        
        response = client.post(
            "/api/v1/workspace/sync",
            data=data,
            files=files,
            headers={"X-API-Key": "test-key"}
        )
        
        assert response.status_code == 400
        assert "Invalid params JSON" in response.json()["detail"]
    
    def test_sync_workspace_invalid_archive_path(
        self, client, mock_workspace_pool, mock_generation_session_service, temp_workspace_dir
    ):
        """Test sync fails with invalid paths in archive."""
        generation_id = "est-invalid-path"
        workspace_ids = ["ws-01-1", "ws-01-2", "ws-01-3"]
        
        mock_generation_session_service.create_generation_session = AsyncMock(return_value=generation_id)
        mock_workspace_pool.allocate_workspace_set = AsyncMock(return_value=workspace_ids)
        mock_workspace_pool.workspace_base_path = temp_workspace_dir
        # Don't mock git commands - we want to fail before that
        
        # Create archive with invalid path (absolute path)
        archive_buffer = io.BytesIO()
        content = b"content"
        with tarfile.open(fileobj=archive_buffer, mode="w:gz") as tar:
            tarinfo = tarfile.TarInfo(name="/absolute/path/file.txt")
            tarinfo.size = len(content)
            tar.addfile(tarinfo, io.BytesIO(content))
        archive_buffer.seek(0)
        invalid_archive = archive_buffer.read()
        
        params = json.dumps({"spec_file": "spec.md"})
        files = {"archive": ("archive.tar.gz", io.BytesIO(invalid_archive), "application/gzip")}
        data = {
            "params": params,
            "user_id": "user-123"
        }
        
        response = client.post(
            "/api/v1/workspace/sync",
            data=data,
            files=files,
            headers={"X-API-Key": "test-key"}
        )
        
        assert response.status_code == 400
        assert "Path traversal detected" in response.json()["detail"]
    
    def test_sync_workspace_no_available_workspaces(
        self, client, mock_workspace_pool, mock_generation_session_service, sample_tar_archive
    ):
        """Test sync fails when no workspaces are available."""
        generation_id = "est-no-ws"
        
        mock_generation_session_service.create_generation_session = AsyncMock(return_value=generation_id)
        mock_workspace_pool.allocate_workspace_set = AsyncMock(
            side_effect=NoAvailableWorkspacesError("No workspaces available")
        )
        mock_generation_session_service.fail_generation_session = AsyncMock()
        
        params = json.dumps({"spec_file": "spec.md"})
        files = {"archive": ("archive.tar.gz", io.BytesIO(sample_tar_archive), "application/gzip")}
        data = {
            "params": params,
            "user_id": "user-123"
        }
        
        response = client.post(
            "/api/v1/workspace/sync",
            data=data,
            files=files,
            headers={"X-API-Key": "test-key"}
        )
        
        assert response.status_code == 503
        assert "No workspaces available" in response.json()["detail"]
        
        # Verify cleanup was called
        mock_generation_session_service.fail_generation_session.assert_called_once_with(
            generation_id,
            "No available workspaces: No workspaces available"
        )
    
    def test_sync_workspace_git_init_error(
        self, client, mock_workspace_pool, mock_generation_session_service,
        temp_workspace_dir, sample_tar_archive
    ):
        """Test sync handles git initialization errors."""
        generation_id = "est-git-error"
        workspace_ids = ["ws-01-1", "ws-01-2", "ws-01-3"]
        
        mock_generation_session_service.create_generation_session = AsyncMock(return_value=generation_id)
        mock_workspace_pool.allocate_workspace_set = AsyncMock(return_value=workspace_ids)
        mock_workspace_pool.workspace_base_path = temp_workspace_dir
        mock_generation_session_service.fail_generation_session = AsyncMock()
        
        # Mock git command to fail
        mock_workspace_pool._run_git_command = AsyncMock(
            side_effect=Exception("Git command failed")
        )
        
        # Create workspace directories
        for ws_id in workspace_ids:
            (temp_workspace_dir / ws_id).mkdir(parents=True)
        
        params = json.dumps({"spec_file": "spec.md"})
        files = {"archive": ("archive.tar.gz", io.BytesIO(sample_tar_archive), "application/gzip")}
        data = {
            "params": params,
            "user_id": "user-123"
        }
        
        response = client.post(
            "/api/v1/workspace/sync",
            data=data,
            files=files,
            headers={"X-API-Key": "test-key"}
        )
        
        assert response.status_code == 500
        assert "Failed to sync files" in response.json()["detail"]
        
        # Verify cleanup was called
        mock_generation_session_service.fail_generation_session.assert_called_once()
    
    def test_sync_workspace_with_existing_git(
        self, client, mock_workspace_pool, mock_generation_session_service,
        temp_workspace_dir, sample_tar_archive
    ):
        """Test sync when workspace already has git initialized."""
        generation_id = "est-existing-git"
        workspace_ids = ["ws-01-1"]

        mock_generation_session_service.create_generation_session = AsyncMock(return_value=generation_id)
        mock_workspace_pool.allocate_workspace_set = AsyncMock(return_value=workspace_ids)
        mock_workspace_pool.workspace_base_path = temp_workspace_dir
        mock_workspace_pool.initialize_git_repo = AsyncMock()
        mock_generation_session_service.set_workspace_ids = Mock()

        # Create workspace with existing .git directory
        workspace_path = temp_workspace_dir / workspace_ids[0]
        workspace_path.mkdir(parents=True)
        (workspace_path / ".git").mkdir()

        params = json.dumps({"spec_file": "spec.md"})
        files = {"archive": ("archive.tar.gz", io.BytesIO(sample_tar_archive), "application/gzip")}
        data = {
            "params": params,
            "user_id": "user-123"
        }

        response = client.post(
            "/api/v1/workspace/sync",
            data=data,
            files=files,
            headers={"X-API-Key": "test-key"}
        )

        assert response.status_code == 201

        # Verify initialize_git_repo was called
        mock_workspace_pool.initialize_git_repo.assert_called_once()
    
    def test_sync_workspace_with_custom_max_retries(
        self, client, mock_workspace_pool, mock_generation_session_service,
        temp_workspace_dir, sample_tar_archive
    ):
        """Test sync with custom max_retries parameter."""
        generation_id = "est-custom-retries"
        workspace_ids = ["ws-01-1", "ws-01-2", "ws-01-3"]

        mock_generation_session_service.create_generation_session = AsyncMock(return_value=generation_id)
        mock_workspace_pool.allocate_workspace_set = AsyncMock(return_value=workspace_ids)
        mock_workspace_pool.workspace_base_path = temp_workspace_dir
        mock_workspace_pool.initialize_git_repo = AsyncMock()
        mock_generation_session_service.set_workspace_ids = Mock()

        # Create workspace directories
        for ws_id in workspace_ids:
            (temp_workspace_dir / ws_id).mkdir(parents=True)

        params = json.dumps({
            "spec_file": "spec.md",
            "max_retries": 5  # Custom retry count
        })
        files = {"archive": ("archive.tar.gz", io.BytesIO(sample_tar_archive), "application/gzip")}
        data = {
            "params": params,
            "user_id": "user-123"
        }

        response = client.post(
            "/api/v1/workspace/sync",
            data=data,
            files=files,
            headers={"X-API-Key": "test-key"}
        )

        assert response.status_code == 201

        # Verify create_generation_session was called with custom max_retries
        call_kwargs = mock_generation_session_service.create_generation_session.call_args[1]
        assert call_kwargs["max_retries"] == 5


# ============================================================
# 2.5d — POST /workspace/{id}/force-release
# ============================================================

class TestForceReleaseWorkspace:
    """Test the force-release endpoint (operator escape hatch)."""

    def test_force_release_allocated_workspace_succeeds(self, client):
        """ALLOCATED workspace can be force-released."""
        from unittest.mock import patch, AsyncMock

        with patch("app.state.WorkspaceStateMachine") as MockWSM:
            mock_wsm = MockWSM.return_value
            mock_wsm.force_release = AsyncMock(return_value={"status": "cleaning"})

            response = client.post(
                "/api/v1/workspace/ws-01-1/force-release",
                json={"reason": "repo corruption", "confirmed_by": "ops@example.com"},
                headers={"X-API-Key": "test-key"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["workspace_id"] == "ws-01-1"
        assert data["status"] == "cleaning"
        mock_wsm.force_release.assert_called_once_with(
            workspace_id="ws-01-1",
            reason="repo corruption",
            confirmed_by="ops@example.com",
        )

    def test_force_release_non_allocated_returns_409(self, client):
        """Non-ALLOCATED workspace returns 409 Conflict."""
        from unittest.mock import patch, AsyncMock
        from app.state.exceptions import InvalidWorkspaceStateError

        with patch("app.state.WorkspaceStateMachine") as MockWSM:
            mock_wsm = MockWSM.return_value
            mock_wsm.force_release = AsyncMock(
                side_effect=InvalidWorkspaceStateError(
                    "ws-01-1", "available", "force_release", ["allocated"]
                )
            )

            response = client.post(
                "/api/v1/workspace/ws-01-1/force-release",
                json={"reason": "test", "confirmed_by": "ops@example.com"},
                headers={"X-API-Key": "test-key"},
            )

        assert response.status_code == 409


# ============================================================
# LV2.1 — pool-status cleaning_sets additive field
# ============================================================


class TestGetPoolStatusCleaningSets:
    """Additive cleaning_sets field on GET /workspace/pool/status."""

    def test_cleaning_sets_present_in_response(self, client, mock_workspace_pool):
        """cleaning_sets field is present in pool-status response."""
        status_data = {
            "total": 6,
            "available": 3,
            "allocated": 0,
            "cleaning": 3,
            "stuck": 0,
            "available_sets": 1,
            "cleaning_sets": [{"set_number": 1, "remaining_grace_seconds": 5400}],
        }
        mock_workspace_pool.get_pool_status = AsyncMock(return_value=status_data)

        response = client.get(
            "/api/v1/workspace/pool/status",
            headers={"X-API-Key": "test-key"},
        )
        assert response.status_code == 200
        data = response.json()
        assert "cleaning_sets" in data
        assert len(data["cleaning_sets"]) == 1
        entry = data["cleaning_sets"][0]
        assert entry["set_number"] == 1
        assert entry["remaining_grace_seconds"] == 5400

    def test_cleaning_sets_empty_when_nothing_cleaning(self, client, mock_workspace_pool):
        """cleaning_sets defaults to [] when get_pool_status omits it."""
        status_data = {
            "total": 6,
            "available": 6,
            "allocated": 0,
            "cleaning": 0,
            "stuck": 0,
            "available_sets": 2,
            # no "cleaning_sets" key — should default to []
        }
        mock_workspace_pool.get_pool_status = AsyncMock(return_value=status_data)

        response = client.get(
            "/api/v1/workspace/pool/status",
            headers={"X-API-Key": "test-key"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["cleaning_sets"] == []

    def test_existing_fields_unchanged_with_cleaning_sets(self, client, mock_workspace_pool):
        """All pre-existing fields still present and correct alongside cleaning_sets."""
        status_data = {
            "total": 30,
            "available": 24,
            "allocated": 3,
            "cleaning": 3,
            "stuck": 0,
            "available_sets": 8,
            "cleaning_sets": [],
        }
        mock_workspace_pool.get_pool_status = AsyncMock(return_value=status_data)

        response = client.get(
            "/api/v1/workspace/pool/status",
            headers={"X-API-Key": "test-key"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 30
        assert data["available"] == 24
        assert data["allocated"] == 3
        assert data["cleaning"] == 3
        assert data["stuck"] == 0
        assert data["available_sets"] == 8

    def test_cleaning_sets_multiple_sets(self, client, mock_workspace_pool):
        """Multiple cleaning sets are returned in the response."""
        status_data = {
            "total": 6,
            "available": 0,
            "allocated": 0,
            "cleaning": 6,
            "stuck": 0,
            "available_sets": 0,
            "cleaning_sets": [
                {"set_number": 1, "remaining_grace_seconds": 1800},
                {"set_number": 2, "remaining_grace_seconds": 5640},
            ],
        }
        mock_workspace_pool.get_pool_status = AsyncMock(return_value=status_data)

        response = client.get(
            "/api/v1/workspace/pool/status",
            headers={"X-API-Key": "test-key"},
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data["cleaning_sets"]) == 2
        assert data["cleaning_sets"][0]["set_number"] == 1
        assert data["cleaning_sets"][1]["set_number"] == 2


# ============================================================
# LV2.3 — POST /workspace/{id}/clear endpoint
# ============================================================


class TestClearWorkspace:
    """Tests for POST /api/v1/workspace/{workspace_id}/clear."""

    def test_clear_workspace_success(self, client, mock_workspace_pool):
        """Successful clear returns 200 with workspace_id and status=available."""
        mock_workspace_pool.cleanup_workspace = AsyncMock(return_value=None)

        response = client.post(
            "/api/v1/workspace/ws-01-1/clear",
            headers={"X-API-Key": "test-key"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["workspace_id"] == "ws-01-1"
        assert data["status"] == "available"
        assert "cleared successfully" in data["message"]
        mock_workspace_pool.cleanup_workspace.assert_awaited_once_with("ws-01-1")

    def test_clear_workspace_not_found_returns_404(self, client, mock_workspace_pool):
        """Non-existent workspace returns 404."""
        mock_workspace_pool.cleanup_workspace = AsyncMock(
            side_effect=WorkspaceNotFoundError("Workspace ws-99-1 not found")
        )

        response = client.post(
            "/api/v1/workspace/ws-99-1/clear",
            headers={"X-API-Key": "test-key"},
        )
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_clear_workspace_wrong_state_returns_409(self, client, mock_workspace_pool):
        """Wrong-state workspace (e.g. available instead of cleaning) returns 409."""
        mock_workspace_pool.cleanup_workspace = AsyncMock(
            side_effect=WorkspacePoolError(
                "Workspace ws-01-1 is in 'available' state, expected 'cleaning'"
            )
        )

        response = client.post(
            "/api/v1/workspace/ws-01-1/clear",
            headers={"X-API-Key": "test-key"},
        )
        assert response.status_code == 409
        assert "available" in response.json()["detail"]

    def test_clear_workspace_internal_error_returns_500(self, client, mock_workspace_pool):
        """Unexpected errors surface as 500."""
        mock_workspace_pool.cleanup_workspace = AsyncMock(
            side_effect=RuntimeError("Unexpected database failure")
        )

        response = client.post(
            "/api/v1/workspace/ws-01-1/clear",
            headers={"X-API-Key": "test-key"},
        )
        assert response.status_code == 500
        assert "Failed to clear workspace" in response.json()["detail"]

    def test_clear_workspace_requires_admin(self, test_app, mock_workspace_pool):
        """Without admin override, the require_admin guard should enforce auth."""
        # Create a fresh app that does NOT bypass require_admin
        from fastapi.testclient import TestClient
        test_app.include_router(workspaces_router, prefix="/api/v1")

        async def override_get_workspace_pool():
            return mock_workspace_pool

        test_app.dependency_overrides[get_workspace_pool] = override_get_workspace_pool
        # NOTE: require_admin is NOT overridden here

        mock_workspace_pool.cleanup_workspace = AsyncMock(return_value=None)

        local_client = TestClient(test_app, raise_server_exceptions=False)
        response = local_client.post(
            "/api/v1/workspace/ws-01-1/clear",
            headers={"X-API-Key": "test-key"},
        )
        # Without admin permissions on request.state, require_admin raises 403
        assert response.status_code == 403
        test_app.dependency_overrides.clear()
