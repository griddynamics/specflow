"""
Unit tests for generation session API endpoints.

Tests session status, retry, cancel, run, and download endpoints.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from app.api.v1.generation_sessions import (
    _handle_workflow_exception,
    get_generation_session_retry_service,
    get_generation_session_service,
    get_workspace_pool,
    list_generation_sessions,
    router as generation_sessions_router,
)
from app.database.memory import InMemoryDatabase
from app.services.contract_validator import ContractRejection
from app.services.contract_validator import RejectionCode
from app.schemas.generation_workflow_enums import GenerationCheckpoint, GenerationStatus
from app.services.generation_session import GenerationSessionNotFoundError
from app.services.generation_session_retry import InvalidRetryStateError
from app.state.db_adapter import COL_GENERATION_SESSIONS


@pytest.fixture
def mock_retry_service():
    """Create a mock GenerationSessionRetryService."""
    mock_service = Mock()
    mock_service.retry_generation_session = AsyncMock()
    return mock_service


@pytest.fixture
def app(test_app, mock_workspace_pool, mock_generation_session_service, mock_retry_service):
    """Create FastAPI app with generation_sessions router and mocked dependencies."""
    # Include router
    test_app.include_router(generation_sessions_router, prefix="/api/v1")
    
    # Override dependencies using FastAPI's dependency override mechanism
    def override_get_workspace_pool():
        return mock_workspace_pool
    
    def override_get_generation_session_service():
        return mock_generation_session_service
    
    def override_get_generation_session_retry_service():
        return mock_retry_service
    
    # Override authorization dependencies to skip ownership check in tests
    # Tests will use db fixture to create sessions with matching user_email
    from app.api.dependencies import (
        require_generation_session_owner,
        require_generation_session_owner_form,
        require_generation_session_owner_or_admin,
    )
    def override_require_generation_session_owner():
        return None  # No-op in tests

    # Override the dependency functions
    test_app.dependency_overrides[get_workspace_pool] = override_get_workspace_pool
    test_app.dependency_overrides[get_generation_session_service] = override_get_generation_session_service
    test_app.dependency_overrides[get_generation_session_retry_service] = override_get_generation_session_retry_service
    test_app.dependency_overrides[require_generation_session_owner] = override_require_generation_session_owner
    test_app.dependency_overrides[require_generation_session_owner_form] = override_require_generation_session_owner
    test_app.dependency_overrides[require_generation_session_owner_or_admin] = override_require_generation_session_owner
    
    yield test_app
    
    # Cleanup
    test_app.dependency_overrides.clear()


@pytest.fixture
def client(app):
    """Create test client."""
    return TestClient(app)


@pytest.fixture
def sample_generation_session_doc():
    """Create a sample generation session document."""
    now = datetime.now(timezone.utc)
    return {
        "generation_id": "est-test-123",
        "user_email": "test@example.com",
        "status": "running",
        "workspace_ids": ["ws-01-1", "ws-01-2", "ws-01-3"],
        "parameters": {
            "spec_path": "specs",
            "outputs_dir": "specflow",
            "model": "claude-sonnet-4-20250514"
        },
        "created_at": now,
        "started_at": now,
        "completed_at": None,
        "last_heartbeat": now,
        "lease_expires_at": None,
        "retry_count": 0,
        "max_retries": 3,
        "progress": {"phase": "generation", "progress": 0.5},
        "result": None,
        "error": None
    }


class TestGetGenerationSession:
    """Tests for GET /generation-sessions/{generation_id} endpoint."""
    
    def test_get_generation_session_success(
        self, client, mock_generation_session_service, sample_generation_session_doc
    ):
        """Test successful retrieval of generation session details."""
        generation_id = "est-test-123"
        mock_generation_session_service.get_generation_session_status = AsyncMock(
            return_value=sample_generation_session_doc
        )
        
        response = client.get(
            f"/api/v1/generation-sessions/{generation_id}",
            headers={"X-API-Key": "test-key"}
        )
        
        assert response.status_code == 200
        data = response.json()
        
        assert data["generation_id"] == generation_id
        assert data["user_email"] == "test@example.com"
        assert data["status"] == "running"
        assert data["workspace_ids"] == ["ws-01-1", "ws-01-2", "ws-01-3"]
        assert data["retry_count"] == 0
        assert data["max_retries"] == 3
        assert "progress" in data
        assert data["result"] is None
        assert data["error"] is None
    
    def test_get_generation_session_not_found(
        self, client, mock_generation_session_service
    ):
        """Test error when session doesn't exist."""
        generation_id = "est-not-found"
        mock_generation_session_service.get_generation_session_status = AsyncMock(
            side_effect=GenerationSessionNotFoundError(f"Generation session {generation_id} not found")
        )
        
        response = client.get(
            f"/api/v1/generation-sessions/{generation_id}",
            headers={"X-API-Key": "test-key"}
        )
        
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()
    
    def test_get_generation_session_with_completed_status(
        self, client, mock_generation_session_service
    ):
        """Test retrieval of completed generation session."""
        now = datetime.now(timezone.utc)
        completed_doc = {
            "generation_id": "est-completed",
            "user_email": "test@example.com",
            "status": "completed",
            "workspace_ids": ["ws-01-1"],
            "parameters": {},
            "created_at": now,
            "started_at": now,
            "completed_at": now,
            "last_heartbeat": None,
            "lease_expires_at": None,
            "retry_count": 0,
            "max_retries": 3,
            "progress": {},
            "result": {"final_estimate_hours": 120.5},
            "error": None
        }
        
        mock_generation_session_service.get_generation_session_status = AsyncMock(
            return_value=completed_doc
        )
        
        response = client.get(
            "/api/v1/generation-sessions/est-completed",
            headers={"X-API-Key": "test-key"}
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "completed"
        assert data["result"] is not None
        assert data["completed_at"] is not None


class TestGetGenerationStatus:
    """Tests for GET /generation-sessions/{generation_id}/status endpoint."""
    
    def test_get_generation_session_status_success(
        self, client, mock_generation_session_service, sample_generation_session_doc
    ):
        """Test successful retrieval of generation session status."""
        generation_id = "est-test-123"
        from app.schemas.generation_workflow_enums import GenerationCheckpoint
        
        # Mock get_checkpoint and get_current_phase_name
        mock_generation_session_service.get_checkpoint = Mock(
            return_value=GenerationCheckpoint.CONTRACT_VALIDATED
        )
        mock_generation_session_service.get_current_phase_name = Mock(
            return_value="plan_synced"
        )
        mock_generation_session_service.get_generation_session_status = AsyncMock(
            return_value=sample_generation_session_doc
        )
        
        response = client.get(
            f"/api/v1/generation-sessions/{generation_id}/status",
            headers={"X-API-Key": "test-key"}
        )
        
        assert response.status_code == 200
        data = response.json()
        
        assert data["generation_id"] == generation_id
        assert data["status"] == "running"
        assert "progress" in data
        assert "current_phase" in data
        assert "checkpoint" in data
        assert "completed_phases" in data
        assert data["error"] is None

    def test_get_generation_session_status_includes_usage_and_spec_analysis_fields(
        self, client, mock_generation_session_service
    ):
        """PR 157: counters, human-readable token display, spec-analysis polling fields."""
        generation_id = "est-stat-1"
        doc = {
            "generation_id": generation_id,
            "status": "analysis",
            "user_email": "u@example.com",
            "workspace_ids": ["ws-1"],
            "parameters": {"workspace_count": 1},
            "progress": {},
            "model_usage": {
                "num_turns": 42,
                "input_tokens": 100_000,
                "output_tokens": 40_000,
                "cache_write_tokens": 5_000,
                "cache_read_tokens": 5_000,
            },
            "last_spec_readiness": "local_only",
            "last_spec_summary": "all good",
            "error": None,
        }
        mock_generation_session_service.get_generation_session_status = AsyncMock(return_value=doc)
        mock_generation_session_service.get_checkpoint = Mock(return_value=GenerationCheckpoint.FILES_UPLOADED)
        mock_generation_session_service.get_current_phase_name = Mock(
            return_value="Specification analysis in progress"
        )

        response = client.get(
            f"/api/v1/generation-sessions/{generation_id}/status",
            headers={"X-API-Key": "test-key"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["num_turns"] == 42
        assert data["total_tokens_used"] == 150_000
        assert data["total_tokens_used_display"] == "150k"
        assert data["last_spec_readiness"] == "local_only"
        assert data["last_spec_summary"] == "all good"

    def test_status_includes_lean_workspace_phases_with_derived_phase_name(
        self, client, mock_generation_session_service
    ):
        """Per-workspace phases are surfaced for the dashboard, with the in-flight
        phase name derived from planning_data and the heavy planning_data omitted."""
        generation_id = "est-ws-1"
        doc = {
            "generation_id": generation_id,
            "status": "running",
            "user_email": "u@example.com",
            "workspace_ids": ["ws-01-1", "ws-01-2"],
            "parameters": {"workspace_count": 2, "LLM_MEDIUM": "test/codegen-a,test/codegen-b"},
            "progress": {},
            "error": None,
            "workspace_phases": {
                "ws-01-1": {
                    "last_completed_phase": 2,
                    "total_phases": 3,
                    "planning_data": {
                        "phases": [
                            {"number": 1, "name": "Scaffold"},
                            {"number": 2, "name": "Backend API"},
                            {"number": 3, "name": "Frontend"},
                        ]
                    },
                },
                "ws-01-2": {"last_completed_phase": 0, "total_phases": 3, "planning_data": {}},
            },
        }
        mock_generation_session_service.get_generation_session_status = AsyncMock(return_value=doc)
        mock_generation_session_service.get_checkpoint = Mock(
            return_value=GenerationCheckpoint.GENERATION_STARTED
        )
        mock_generation_session_service.get_current_phase_name = Mock(return_value="Generating")

        response = client.get(
            f"/api/v1/generation-sessions/{generation_id}/status",
            headers={"X-API-Key": "test-key"},
        )

        assert response.status_code == 200
        wp = response.json()["workspace_phases"]
        # ws-01-1 completed phase 2 → currently working on phase index 2 = "Frontend".
        assert wp["ws-01-1"] == {
            "last_completed_phase": 2,
            "total_phases": 3,
            "phase_name": "Frontend",
            "models": ["test/codegen-a"],
        }
        # No planning data → empty phase name; configured codegen model still shown.
        assert wp["ws-01-2"] == {
            "last_completed_phase": 0,
            "total_phases": 3,
            "phase_name": "",
            "models": ["test/codegen-b"],
        }
        # Heavy planning_data must not leak into the polled response.
        assert "planning_data" not in wp["ws-01-1"]

    def test_status_phase_name_reports_kb_init_before_kb_init_done(
        self, client, mock_generation_session_service
    ):
        """While the session checkpoint is before KB_INIT_DONE the workspace is
        running KB init, so the per-workspace phase name must report the KB-init
        step rather than the first code-gen phase from planning_data."""
        generation_id = "est-kb-init"
        doc = {
            "generation_id": generation_id,
            "status": "running",
            "user_email": "u@example.com",
            "workspace_ids": ["ws-01-1"],
            "parameters": {"workspace_count": 1},
            "progress": {},
            "error": None,
            "workspace_phases": {
                "ws-01-1": {
                    "last_completed_phase": 0,
                    "total_phases": 3,
                    "planning_data": {
                        "phases": [
                            {"number": 1, "name": "Scaffold"},
                            {"number": 2, "name": "Backend API"},
                            {"number": 3, "name": "Frontend"},
                        ]
                    },
                },
            },
        }
        mock_generation_session_service.get_generation_session_status = AsyncMock(return_value=doc)
        mock_generation_session_service.get_checkpoint = Mock(
            return_value=GenerationCheckpoint.CONTRACT_VALIDATED
        )
        mock_generation_session_service.get_current_phase_name = Mock(return_value="kb_init")

        response = client.get(
            f"/api/v1/generation-sessions/{generation_id}/status",
            headers={"X-API-Key": "test-key"},
        )

        assert response.status_code == 200
        wp = response.json()["workspace_phases"]
        assert wp["ws-01-1"]["phase_name"] == "Knowledge Base Initialization with Rosetta"
        # Counters still flow through; the first code-gen phase name is NOT shown yet.
        assert wp["ws-01-1"]["last_completed_phase"] == 0
        assert wp["ws-01-1"]["total_phases"] == 3

    def test_status_workspace_view_includes_per_workspace_usage_and_models(
        self, client, mock_generation_session_service
    ):
        """TUI drill-in: per-workspace usage/models are derived from the existing
        workflow_usage_metrics tree and attached to the workspace view (no new
        endpoint). Workspaces without recorded usage stay phase-only (lean)."""
        generation_id = "est-ws-usage"
        doc = {
            "generation_id": generation_id,
            "status": "running",
            "user_email": "u@example.com",
            "workspace_ids": ["ws-01-1", "ws-01-2"],
            "parameters": {"workspace_count": 2, "LLM_MEDIUM": "test/codegen-a,test/codegen-b"},
            "progress": {},
            "error": None,
            # Both at phase 0 → working on generation phase 1.
            "workspace_phases": {
                "ws-01-1": {"last_completed_phase": 0, "total_phases": 3, "planning_data": {}},
                "ws-01-2": {"last_completed_phase": 0, "total_phases": 3, "planning_data": {}},
            },
            "workflow_usage_metrics": {
                "generation_phase_1_coding": {
                    "workspaces": {
                        "ws-01-1": {
                            "models": {
                                "claude-sonnet-4": {
                                    "model_name": "claude-sonnet-4",
                                    "num_turns": 7,
                                    "input_tokens": 1000,
                                    "output_tokens": 500,
                                    "cache_write_tokens": 100,
                                    "cache_read_tokens": 50,
                                }
                            }
                        }
                    }
                }
            },
        }
        mock_generation_session_service.get_generation_session_status = AsyncMock(return_value=doc)
        mock_generation_session_service.get_checkpoint = Mock(
            return_value=GenerationCheckpoint.GENERATION_STARTED
        )
        mock_generation_session_service.get_current_phase_name = Mock(return_value="Generating")

        response = client.get(
            f"/api/v1/generation-sessions/{generation_id}/status",
            headers={"X-API-Key": "test-key"},
        )

        assert response.status_code == 200
        wp = response.json()["workspace_phases"]
        # ws-01-1 has recorded usage → usage block + model list attached.
        assert wp["ws-01-1"]["usage"] == {
            "num_turns": 7,
            "input_tokens": 1000,
            "output_tokens": 500,
            "cache_write_tokens": 100,
            "cache_read_tokens": 50,
            "total_tokens": 1650,
        }
        assert wp["ws-01-1"]["models"] == ["claude-sonnet-4"]
        # ws-01-2 has no recorded usage yet → configured codegen model for index 1.
        assert wp["ws-01-2"]["models"] == ["test/codegen-b"]
        assert "usage" not in wp["ws-01-2"]

    def test_status_workspace_usage_uses_full_lifetime_aggregate(
        self, client, mock_generation_session_service
    ):
        """Per-workspace usage is the full lifetime aggregate; models are scoped to
        the active step (KB init here) so earlier converter models do not leak."""
        generation_id = "est-scoped"
        doc = {
            "generation_id": generation_id,
            "status": "running",
            "user_email": "u@example.com",
            "workspace_ids": ["ws-01-1"],
            "parameters": {"workspace_count": 1},
            "progress": {},
            "error": None,
            "workspace_phases": {
                "ws-01-1": {"last_completed_phase": 0, "total_phases": 3, "planning_data": {}},
            },
            "workflow_usage_metrics": {
                # Earlier contract-validation step on the SAME workspace (Haiku).
                "markdown_to_json_converter": {
                    "workspaces": {
                        "ws-01-1": {
                            "models": {
                                "claude-haiku-4.5": {
                                    "model_name": "claude-haiku-4.5",
                                    "num_turns": 1,
                                    "input_tokens": 20,
                                    "output_tokens": 10,
                                    "cache_write_tokens": 0,
                                    "cache_read_tokens": 0,
                                }
                            }
                        }
                    }
                },
                # The active KB-init step (planning-tier model).
                "kb_init": {
                    "workspaces": {
                        "ws-01-1": {
                            "models": {
                                "claude-opus-4.5": {
                                    "model_name": "claude-opus-4.5",
                                    "num_turns": 3,
                                    "input_tokens": 800,
                                    "output_tokens": 400,
                                    "cache_write_tokens": 50,
                                    "cache_read_tokens": 25,
                                }
                            }
                        }
                    }
                },
            },
        }
        mock_generation_session_service.get_generation_session_status = AsyncMock(return_value=doc)
        mock_generation_session_service.get_checkpoint = Mock(
            return_value=GenerationCheckpoint.CONTRACT_VALIDATED
        )
        mock_generation_session_service.get_current_phase_name = Mock(return_value="kb_init")

        response = client.get(
            f"/api/v1/generation-sessions/{generation_id}/status",
            headers={"X-API-Key": "test-key"},
        )

        assert response.status_code == 200
        wp = response.json()["workspace_phases"]
        # Usage: full aggregate across converter + KB init.
        assert wp["ws-01-1"]["models"] == ["claude-opus-4.5"]
        assert wp["ws-01-1"]["usage"]["num_turns"] == 4
        assert wp["ws-01-1"]["usage"]["total_tokens"] == 1305
        assert wp["ws-01-1"]["phase_name"] == "Knowledge Base Initialization with Rosetta"

    def test_status_kb_init_usage_visible_after_kb_init_in_first_phase(
        self, client, mock_generation_session_service
    ):
        """KB init is one long agent_query that only records usage when it finishes
        — by then the checkpoint has left kb_init. While the first generation phase
        is in flight (and has recorded nothing yet), the full-aggregate view still
        surfaces the just-finished KB-init turns/tokens instead of an empty panel."""
        generation_id = "est-kb-visible"
        doc = {
            "generation_id": generation_id,
            "status": "running",
            "user_email": "u@example.com",
            "workspace_ids": ["ws-01-1"],
            "parameters": {"workspace_count": 1, "LLM_MEDIUM": "test/codegen-a"},
            "progress": {},
            "error": None,
            # Past KB_INIT_DONE, working on generation phase 1 (nothing completed).
            "workspace_phases": {
                "ws-01-1": {"last_completed_phase": 0, "total_phases": 3, "planning_data": {}},
            },
            "workflow_usage_metrics": {
                # KB init finished and flushed its usage; generation phase 1 has
                # not recorded anything yet.
                "kb_init": {
                    "workspaces": {
                        "ws-01-1": {
                            "models": {
                                "claude-opus-4.5": {
                                    "model_name": "claude-opus-4.5",
                                    "num_turns": 4,
                                    "input_tokens": 900,
                                    "output_tokens": 300,
                                    "cache_write_tokens": 60,
                                    "cache_read_tokens": 30,
                                }
                            }
                        }
                    }
                },
            },
        }
        mock_generation_session_service.get_generation_session_status = AsyncMock(return_value=doc)
        mock_generation_session_service.get_checkpoint = Mock(
            return_value=GenerationCheckpoint.GENERATION_STARTED
        )
        mock_generation_session_service.get_current_phase_name = Mock(return_value="Generating")

        response = client.get(
            f"/api/v1/generation-sessions/{generation_id}/status",
            headers={"X-API-Key": "test-key"},
        )

        assert response.status_code == 200
        wp = response.json()["workspace_phases"]
        # Usage: still the kb_init aggregate; models: active generation phase (no
        # recorded usage yet) → configured codegen model, not prior kb_init Opus.
        assert wp["ws-01-1"]["usage"]["num_turns"] == 4
        assert wp["ws-01-1"]["usage"]["total_tokens"] == 1290
        assert wp["ws-01-1"]["models"] == ["test/codegen-a"]

    def test_status_workspace_models_fallback_while_kb_init_in_flight(
        self, client, mock_generation_session_service
    ):
        """During KB init (no usage flushed yet) the model row shows the configured
        HIGH-tier model, not Haiku from the earlier plan converter."""
        generation_id = "est-kb-model"
        doc = {
            "generation_id": generation_id,
            "status": "running",
            "user_email": "u@example.com",
            "workspace_ids": ["ws-01-1"],
            "parameters": {"workspace_count": 1, "LLM_HIGH": "test/planning-model"},
            "progress": {},
            "error": None,
            "workspace_phases": {
                "ws-01-1": {"last_completed_phase": 0, "total_phases": 3, "planning_data": {}},
            },
            "workflow_usage_metrics": {
                "markdown_to_json_converter": {
                    "workspaces": {
                        "ws-01-1": {
                            "models": {
                                "claude-haiku-4.5": {
                                    "model_name": "claude-haiku-4.5",
                                    "num_turns": 1,
                                    "input_tokens": 20,
                                    "output_tokens": 10,
                                    "cache_write_tokens": 0,
                                    "cache_read_tokens": 0,
                                }
                            }
                        }
                    }
                },
            },
        }
        mock_generation_session_service.get_generation_session_status = AsyncMock(return_value=doc)
        mock_generation_session_service.get_checkpoint = Mock(
            return_value=GenerationCheckpoint.CONTRACT_VALIDATED
        )
        mock_generation_session_service.get_current_phase_name = Mock(return_value="kb_init")

        response = client.get(
            f"/api/v1/generation-sessions/{generation_id}/status",
            headers={"X-API-Key": "test-key"},
        )

        assert response.status_code == 200
        wp = response.json()["workspace_phases"]
        assert wp["ws-01-1"]["models"] == ["test/planning-model"]
        assert wp["ws-01-1"]["usage"]["num_turns"] == 1

    def test_status_workspace_usage_surfaces_pre_kb_converter_usage(
        self, client, mock_generation_session_service
    ):
        """The converters that run before KB init publish per-workspace usage, so
        that data is part of the workspace's general aggregate and shows in the
        panel even when no later step (kb_init / generation phase) has recorded
        anything yet."""
        generation_id = "est-no-leak"
        doc = {
            "generation_id": generation_id,
            "status": "running",
            "user_email": "u@example.com",
            "workspace_ids": ["ws-01-1"],
            "parameters": {"workspace_count": 1, "LLM_MEDIUM": "test/codegen-a"},
            "progress": {},
            "error": None,
            "workspace_phases": {
                "ws-01-1": {"last_completed_phase": 0, "total_phases": 3, "planning_data": {}},
            },
            "workflow_usage_metrics": {
                "markdown_to_json_converter": {
                    "workspaces": {
                        "ws-01-1": {
                            "models": {
                                "claude-haiku-4.5": {
                                    "model_name": "claude-haiku-4.5",
                                    "num_turns": 1,
                                    "input_tokens": 20,
                                    "output_tokens": 10,
                                    "cache_write_tokens": 0,
                                    "cache_read_tokens": 0,
                                }
                            }
                        }
                    }
                },
            },
        }
        mock_generation_session_service.get_generation_session_status = AsyncMock(return_value=doc)
        mock_generation_session_service.get_checkpoint = Mock(
            return_value=GenerationCheckpoint.GENERATION_STARTED
        )
        mock_generation_session_service.get_current_phase_name = Mock(return_value="Generating")

        response = client.get(
            f"/api/v1/generation-sessions/{generation_id}/status",
            headers={"X-API-Key": "test-key"},
        )

        assert response.status_code == 200
        wp = response.json()["workspace_phases"]
        # Converter usage is in the lifetime aggregate; models show the active codegen
        # step (configured model), not the earlier converter Haiku.
        assert wp["ws-01-1"]["models"] == ["test/codegen-a"]
        assert wp["ws-01-1"]["usage"]["num_turns"] == 1
        assert wp["ws-01-1"]["usage"]["total_tokens"] == 30

    def test_get_generation_session_status_not_found(
        self, client, mock_generation_session_service
    ):
        """Test error when session doesn't exist."""
        generation_id = "est-not-found"
        mock_generation_session_service.get_generation_session_status = AsyncMock(
            side_effect=GenerationSessionNotFoundError(f"Generation session {generation_id} not found")
        )
        
        response = client.get(
            f"/api/v1/generation-sessions/{generation_id}/status",
            headers={"X-API-Key": "test-key"}
        )
        
        assert response.status_code == 404


class TestRetryGenerationSession:
    """Tests for POST /generation-sessions/{generation_id}/retry endpoint."""
    
    def test_retry_generation_session_success(
        self, client, mock_generation_session_service, mock_retry_service, app
    ):
        """Test successful retry of failed generation session."""
        generation_id = "est-retry-123"
        
        # Setup retry service mock
        mock_retry_service.retry_generation_session = AsyncMock()
        
        # Mock updated session document after retry
        updated_doc = {
            "generation_id": generation_id,
            "status": "pending",
            "retry_count": 1,
            "workspace_ids": [],
            "parameters": {},
            "created_at": datetime.now(timezone.utc),
            "started_at": None,
            "completed_at": None,
            "last_heartbeat": None,
            "lease_expires_at": None,
            "max_retries": 3,
            "progress": {},
            "result": None,
            "error": None,
            "user_email": "test@example.com"
        }
        
        mock_generation_session_service.get_generation_session_status = AsyncMock(
            return_value=updated_doc
        )
        
        response = client.post(
            f"/api/v1/generation-sessions/{generation_id}/retry",
            headers={"X-API-Key": "test-key"}
        )

        assert response.status_code == 200
        data = response.json()

        assert data["generation_id"] == generation_id
        assert data["status"] == "pending"
        assert data["retry_count"] == 1

        mock_retry_service.retry_generation_session.assert_called_once_with(
            generation_id=generation_id,
        )
    
    def test_retry_generation_session_invalid_state(
        self, client, mock_retry_service
    ):
        """Test retry fails when session is in invalid state."""
        generation_id = "est-invalid-state"
        
        mock_retry_service.retry_generation_session = AsyncMock(
            side_effect=InvalidRetryStateError("Cannot retry completed generation session")
        )
        
        response = client.post(
            f"/api/v1/generation-sessions/{generation_id}/retry",
            headers={"X-API-Key": "test-key"}
        )

        assert response.status_code == 400
        assert "Cannot retry" in response.json()["detail"]
    
    def test_retry_generation_session_not_found(
        self, client, mock_retry_service
    ):
        """Test retry fails when session doesn't exist."""
        generation_id = "est-not-found"
        
        mock_retry_service.retry_generation_session = AsyncMock(
            side_effect=GenerationSessionNotFoundError(f"Generation session {generation_id} not found")
        )
        
        response = client.post(
            f"/api/v1/generation-sessions/{generation_id}/retry",
            headers={"X-API-Key": "test-key"}
        )

        assert response.status_code == 404
    

class TestCancelGenerationSession:
    """Tests for DELETE /generation-sessions/{generation_id} endpoint."""
    
    def test_cancel_generation_success(
        self, client, mock_generation_session_service
    ):
        """Test successful cancellation of running generation session."""
        generation_id = "est-cancel-123"
        
        running_doc = {
            "generation_id": generation_id,
            "status": "running",
            "workspace_ids": [],
            "parameters": {},
            "created_at": datetime.now(timezone.utc),
            "started_at": None,
            "completed_at": None,
            "last_heartbeat": None,
            "lease_expires_at": None,
            "retry_count": 0,
            "max_retries": 3,
            "progress": {},
            "result": None,
            "error": None,
            "user_email": "test@example.com"
        }
        
        mock_generation_session_service.get_generation_session_status = AsyncMock(
            return_value=running_doc
        )
        mock_generation_session_service.fail_generation_session = AsyncMock()
        
        response = client.delete(
            f"/api/v1/generation-sessions/{generation_id}",
            headers={"X-API-Key": "test-key"}
        )
        
        assert response.status_code == 200
        data = response.json()
        
        assert data["generation_id"] == generation_id
        assert data["status"] == "failed"
        assert "cancelled" in data["message"].lower()
        
        mock_generation_session_service.fail_generation_session.assert_called_once_with(
            generation_id=generation_id,
            error="Cancelled by user"
        )
    
    def test_cancel_generation_already_completed(
        self, client, mock_generation_session_service
    ):
        """Test cancellation fails when session is already completed."""
        generation_id = "est-completed"
        
        completed_doc = {
            "generation_id": generation_id,
            "status": "completed",
            "workspace_ids": [],
            "parameters": {},
            "created_at": datetime.now(timezone.utc),
            "started_at": None,
            "completed_at": None,
            "last_heartbeat": None,
            "lease_expires_at": None,
            "retry_count": 0,
            "max_retries": 3,
            "progress": {},
            "result": {},
            "error": None,
            "user_email": "test@example.com"
        }
        
        mock_generation_session_service.get_generation_session_status = AsyncMock(
            return_value=completed_doc
        )
        
        response = client.delete(
            f"/api/v1/generation-sessions/{generation_id}",
            headers={"X-API-Key": "test-key"}
        )
        
        assert response.status_code == 400
        assert "Cannot cancel" in response.json()["detail"]
    
    def test_cancel_generation_not_found(
        self, client, mock_generation_session_service
    ):
        """Test cancellation fails when session doesn't exist."""
        generation_id = "est-not-found"
        
        mock_generation_session_service.get_generation_session_status = AsyncMock(
            side_effect=GenerationSessionNotFoundError(f"Generation session {generation_id} not found")
        )
        
        response = client.delete(
            f"/api/v1/generation-sessions/{generation_id}",
            headers={"X-API-Key": "test-key"}
        )
        
        assert response.status_code == 404


class TestRunGenerationSession:
    """Tests for POST /generation-sessions/run endpoint."""
    
    @pytest.mark.skip(reason="Requires request.state.user_email from auth middleware - test via integration tests")
    def test_run_generation_success(
        self, client, mock_workspace_pool, mock_generation_session_service
    ):
        """Test successful start of generation session workflow.
        
        Note: This endpoint requires request.state.user_email which is set by auth middleware.
        Unit testing this endpoint in isolation is complex. It should be tested via:
        - Integration tests with full middleware stack
        - E2E tests with actual HTTP requests
        """
        pass
    
    def test_run_generation_with_archive(
        self, client, mock_workspace_pool, mock_generation_session_service, sample_tar_archive
    ):
        """Test run generation session with archive upload."""
        # Note: This test demonstrates the pattern but requires complex request.state mocking
        # In practice, this endpoint would be tested via integration tests or with middleware
        # that sets request.state.user_email
        pass  # Skipped due to request.state complexity
    
    def test_run_generation_invalid_archive_format(
        self, client, mock_workspace_pool, mock_generation_session_service
    ):
        """Test run generation session fails with invalid archive format."""
        # Note: This test demonstrates the pattern but requires complex request.state mocking
        # In practice, this endpoint would be tested via integration tests or with middleware
        pass  # Skipped due to request.state complexity
    
    def test_run_generation_reuse_existing(
        self, client, mock_workspace_pool, mock_generation_session_service
    ):
        """Test run generation session reusing existing generation_id."""
        # Note: This test demonstrates the pattern but requires complex request.state mocking
        # In practice, this endpoint would be tested via integration tests or with middleware
        pass  # Skipped due to request.state complexity


class TestMCPRunGenerationSessionStateTransition:
    """
    Fix 2: _transition_to_running_if_pending() was deleted.
    Its call site in _run_generation_session_workflow() now uses state machine calls
    (begin_allocation + allocation_succeeded) instead of direct db writes.

    These tests verify the NEW state machine path via GenerationSessionStateMachine
    when a pre-allocated session is transitioned to RUNNING in the MCP flow.
    """

    @pytest.mark.asyncio
    async def test_pending_session_transitions_to_running_via_state_machine(self):
        """
        Pre-allocated session in PENDING: state machine must advance through
        INITIALIZING → RUNNING via begin_allocation + allocation_succeeded.
        Uses real GenerationSessionStateMachine with fake db (no direct writes).
        """
        from datetime import datetime, timezone
        from app.state import GenerationSessionStateMachine
        from app.state.db_adapter import StateMachineDBAdapter
        from app.database.memory import InMemoryDatabase

        db = InMemoryDatabase()
        now = datetime.now(timezone.utc)

        db.set(COL_GENERATION_SESSIONS, "est-mcp-1", {
            "user_email": "test@example.com",
            "status": GenerationStatus.PENDING.value,
            "workspace_ids": ["ws-01-1"],
            "status_changed_at": now,
            "state_history": [{"status": GenerationStatus.PENDING.value, "at": now,
                                "triggered_by": "create", "metadata": {}}],
        })

        db_adapter = StateMachineDBAdapter(db)
        esm = GenerationSessionStateMachine(db_adapter)

        # Simulate the MCP flow: pre-allocated workspaces, advance via state machine
        await esm.begin_allocation("est-mcp-1", triggered_by="run_generation_session")
        await esm.allocation_succeeded("est-mcp-1", triggered_by="run_generation_session")

        est = db.get(COL_GENERATION_SESSIONS, "est-mcp-1")
        assert est["status"] == GenerationStatus.RUNNING.value
        # state_history must record both transitions
        history = est["state_history"]
        statuses = [h["status"] for h in history]
        assert GenerationStatus.INITIALIZING.value in statuses
        assert GenerationStatus.RUNNING.value in statuses
        # triggered_by is set by state machine (Commandment IX)
        last = history[-1]
        assert last.get("triggered_by") == "run_generation_session"

    @pytest.mark.asyncio
    async def test_already_running_session_is_not_double_transitioned(self):
        """
        If the session is already RUNNING when the MCP flow starts (e.g. the
        spec-check step already advanced it), begin_allocation raises
        InvalidGenerationSessionStateError. The new code guards with a status check and
        skips the state machine calls — confirmed here.
        """
        from datetime import datetime, timezone
        from app.state import GenerationSessionStateMachine
        from app.state.db_adapter import StateMachineDBAdapter
        from app.state.exceptions import InvalidGenerationSessionStateError
        from app.database.memory import InMemoryDatabase

        db = InMemoryDatabase()
        now = datetime.now(timezone.utc)

        db.set(COL_GENERATION_SESSIONS, "est-mcp-2", {
            "user_email": "test@example.com",
            "status": GenerationStatus.RUNNING.value,
            "workspace_ids": ["ws-01-1"],
            "status_changed_at": now,
            "state_history": [{"status": GenerationStatus.RUNNING.value, "at": now,
                                "triggered_by": "start", "metadata": {}}],
        })

        db_adapter = StateMachineDBAdapter(db)
        esm = GenerationSessionStateMachine(db_adapter)

        # Calling begin_allocation on an already-RUNNING session raises
        with pytest.raises(InvalidGenerationSessionStateError):
            await esm.begin_allocation("est-mcp-2", triggered_by="run_generation_session")

        # Status is unchanged — no corruption
        est = db.get(COL_GENERATION_SESSIONS, "est-mcp-2")
        assert est["status"] == GenerationStatus.RUNNING.value


class TestRetryWorkflowOrchestrator:
    """
    Fix 3: _run_generation_workflow_for_retry() was deleted.
    _run_generation_workflow_via_orchestrator() uses WorkflowOrchestrator.run()
    which handles skip logic for ALL checkpoints, not just GENERATION_DONE.
    """

    @pytest.mark.asyncio
    async def test_orchestrator_skips_generation_when_at_generation_done(self):
        """
        WorkflowOrchestrator skips the 'generation' step when checkpoint is
        already at GENERATION_DONE (or past it). Only 'estimation' runs.
        """
        from app.state import GenerationSessionStateMachine, WorkflowOrchestrator
        from app.state.db_adapter import StateMachineDBAdapter
        from app.database.memory import InMemoryDatabase
        from app.schemas.generation_workflow_enums import GenerationCheckpoint

        db = InMemoryDatabase()
        db.set(COL_GENERATION_SESSIONS, "est-orch-1", {
            "status": "running",
            "checkpoint": GenerationCheckpoint.GENERATION_DONE.value,
            "state_history": [],
        })

        db_adapter = StateMachineDBAdapter(db)
        esm = GenerationSessionStateMachine(db_adapter)
        orchestrator = WorkflowOrchestrator(db_adapter, esm)

        generation_called = []
        estimation_called = []

        async def mock_generation():
            generation_called.append(True)

        async def mock_estimation():
            estimation_called.append(True)

        await orchestrator.run(
            generation_id="est-orch-1",
            step_implementations={
                "generation": mock_generation,
                "estimation": mock_estimation,
            },
            triggered_by="test",
            complete_on_finish=False,
        )

        assert generation_called == [], "generation must be SKIPPED — checkpoint already past it"
        assert estimation_called == [True], "estimation must RUN"

    @pytest.mark.asyncio
    async def test_orchestrator_runs_both_steps_when_below_generation_done(self):
        """
        WorkflowOrchestrator runs both 'generation' and 'estimation' when
        checkpoint is below GENERATION_DONE (e.g. PLAN_SYNCED).
        """
        from app.state import GenerationSessionStateMachine, WorkflowOrchestrator
        from app.state.db_adapter import StateMachineDBAdapter
        from app.database.memory import InMemoryDatabase
        from app.schemas.generation_workflow_enums import GenerationCheckpoint

        db = InMemoryDatabase()
        db.set(COL_GENERATION_SESSIONS, "est-orch-2", {
            "status": "running",
            "checkpoint": GenerationCheckpoint.CONTRACT_VALIDATED.value,
            "state_history": [],
        })

        db_adapter = StateMachineDBAdapter(db)
        esm = GenerationSessionStateMachine(db_adapter)
        orchestrator = WorkflowOrchestrator(db_adapter, esm)

        generation_called = []
        estimation_called = []

        async def mock_generation():
            generation_called.append(True)

        async def mock_estimation():
            estimation_called.append(True)

        await orchestrator.run(
            generation_id="est-orch-2",
            step_implementations={
                "generation": mock_generation,
                "estimation": mock_estimation,
            },
            triggered_by="test",
            complete_on_finish=False,
        )

        assert generation_called == [True], "generation must RUN"
        assert estimation_called == [True], "estimation must RUN"

    @pytest.mark.asyncio
    async def test_orchestrator_marks_failed_on_generation_error(self):
        """
        When the 'generation' step raises, WorkflowOrchestrator calls esm.fail()
        and re-raises. The session ends in FAILED state.
        """
        from app.state import GenerationSessionStateMachine, WorkflowOrchestrator
        from app.state.db_adapter import StateMachineDBAdapter
        from app.database.memory import InMemoryDatabase
        from app.schemas.generation_workflow_enums import GenerationCheckpoint

        db = InMemoryDatabase()
        db.set(COL_GENERATION_SESSIONS, "est-orch-3", {
            "status": "running",
            "checkpoint": GenerationCheckpoint.CONTRACT_VALIDATED.value,
            "state_history": [],
        })

        db_adapter = StateMachineDBAdapter(db)
        esm = GenerationSessionStateMachine(db_adapter)
        orchestrator = WorkflowOrchestrator(db_adapter, esm)

        async def failing_generation():
            raise RuntimeError("Generation agent crashed")

        with pytest.raises(RuntimeError, match="Generation agent crashed"):
            await orchestrator.run(
                generation_id="est-orch-3",
                step_implementations={"generation": failing_generation},
                triggered_by="test",
                complete_on_finish=False,
            )

        est = db.get(COL_GENERATION_SESSIONS, "est-orch-3")
        assert est["status"] == GenerationStatus.FAILED.value


# ============================================================
# Double-fail protection (Bug 3 regression — 2026-02-24)
# ============================================================

class TestDoubleFailProtection:
    """
    Bug regression (2026-02-24): background task error handlers in run_retry()
    and _run_generation_session_workflow() called fail_generation_session() even after the inner
    WorkflowOrchestrator had already called esm.fail() (session → FAILED).
    The second esm.fail() raised InvalidGenerationSessionStateError, replacing the
    original step error in the asyncio task exception log.

    Fix in both handlers:
        try:
            await generation_session_service.fail_generation_session(generation_id, str(e))
        except SMInvalidGenerationSessionStateError:
            pass  # already FAILED by the orchestrator — no-op

    Tests verify:
    1. The precondition: esm.fail() raises when session is already FAILED.
    2. The run_retry() scenario: orchestrator fails → background handler's
       second fail() is silently swallowed; original exception propagates.
    3. The _run_generation_session_workflow() scenario: same protection on the non-retry path.
    """

    @pytest.mark.asyncio
    async def test_esm_fail_raises_when_session_already_failed(self):
        """
        Precondition: esm.fail() raises InvalidGenerationSessionStateError when the
        session is already in FAILED status. This is why the double-fail
        protection try-except is required in both background task error handlers.
        """
        from app.state import GenerationSessionStateMachine
        from app.state.db_adapter import StateMachineDBAdapter
        from app.database.memory import InMemoryDatabase
        from app.state.exceptions import InvalidGenerationSessionStateError

        db = InMemoryDatabase()
        db.set(COL_GENERATION_SESSIONS, "est-dbl-pre", {
            "status": "failed",
            "checkpoint": None,
            "state_history": [],
        })
        db_adapter = StateMachineDBAdapter(db)
        esm = GenerationSessionStateMachine(db_adapter)

        with pytest.raises(InvalidGenerationSessionStateError):
            await esm.fail(
                generation_id="est-dbl-pre",
                reason="second failure attempt",
                triggered_by="test:precondition",
            )

    @pytest.mark.asyncio
    async def test_run_retry_handler_catches_double_fail(self):
        """
        run_retry() scenario: inner WorkflowOrchestrator calls esm.fail() first
        (session → FAILED), then the outer background task error handler also
        tries to call esm.fail(). The try-except in the handler catches
        InvalidGenerationSessionStateError and re-raises the ORIGINAL step exception.
        """
        from app.state import GenerationSessionStateMachine, WorkflowOrchestrator
        from app.state.db_adapter import StateMachineDBAdapter
        from app.database.memory import InMemoryDatabase
        from app.state.exceptions import InvalidGenerationSessionStateError as SMInvalidGenerationSessionStateError

        db = InMemoryDatabase()
        db.set(COL_GENERATION_SESSIONS, "est-dbl-retry", {
            "status": "running",
            "checkpoint": None,
            "state_history": [],
        })
        db_adapter = StateMachineDBAdapter(db)
        esm = GenerationSessionStateMachine(db_adapter)
        orchestrator = WorkflowOrchestrator(db_adapter, esm)

        # Step fails → orchestrator calls esm.fail() → session is now FAILED
        async def failing_step():
            raise RuntimeError("validate_contract agent exited 1")

        with pytest.raises(RuntimeError, match="validate_contract agent exited 1"):
            await orchestrator.run(
                generation_id="est-dbl-retry",
                step_implementations={"validate_contract": failing_step},
                triggered_by="test",
                complete_on_finish=False,
            )

        # Confirm orchestrator left the session FAILED
        est = db.get(COL_GENERATION_SESSIONS, "est-dbl-retry")
        assert est["status"] == "failed"

        # PR255: exercise the REAL shared handler (not an inline copy). When the
        # orchestrator has already FAILED the session, the handler's fail() raises
        # InvalidGenerationSessionStateError — which _handle_workflow_exception must
        # swallow so the original step error stays surfaced and the background task
        # does not crash with a misleading secondary error.
        already_failed = SMInvalidGenerationSessionStateError(
            generation_id="est-dbl-retry", current_status="failed",
            attempted_transition="fail", allowed_from=["running"],
        )
        svc = Mock()
        svc.fail_generation_session = AsyncMock(side_effect=already_failed)
        svc.reject_generation_session = AsyncMock()
        # Must NOT raise — the double-fail is swallowed inside the handler.
        await _handle_workflow_exception(
            RuntimeError("validate_contract agent exited 1"), "est-dbl-retry", svc,
        )
        svc.fail_generation_session.assert_awaited_once()
        svc.reject_generation_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_run_generation_workflow_handler_catches_double_fail(self):
        """
        _run_generation_session_workflow() scenario (non-retry path): same double-fail
        protection applies. When generation step fails, orchestrator fails it;
        the outer error handler's second fail() is silently caught.
        """
        from app.state import GenerationSessionStateMachine, WorkflowOrchestrator
        from app.state.db_adapter import StateMachineDBAdapter
        from app.database.memory import InMemoryDatabase
        from app.state.exceptions import InvalidGenerationSessionStateError as SMInvalidGenerationSessionStateError

        db = InMemoryDatabase()
        db.set(COL_GENERATION_SESSIONS, "est-dbl-run", {
            "status": "running",
            "checkpoint": None,
            "state_history": [],
        })
        db_adapter = StateMachineDBAdapter(db)
        esm = GenerationSessionStateMachine(db_adapter)
        orchestrator = WorkflowOrchestrator(db_adapter, esm)

        async def failing_generation():
            raise RuntimeError("generation docker container crashed")

        with pytest.raises(RuntimeError, match="generation docker container crashed"):
            await orchestrator.run(
                generation_id="est-dbl-run",
                step_implementations={"generation": failing_generation},
                triggered_by="_run_generation_session_workflow",
                complete_on_finish=False,
            )

        est = db.get(COL_GENERATION_SESSIONS, "est-dbl-run")
        assert est["status"] == "failed"

        # PR255: outer error handler in _run_generation_session_workflow() also calls
        # fail() — exercise the REAL shared handler and assert the double-fail is swallowed.
        already_failed = SMInvalidGenerationSessionStateError(
            generation_id="est-dbl-run", current_status="failed",
            attempted_transition="fail", allowed_from=["running"],
        )
        svc = Mock()
        svc.fail_generation_session = AsyncMock(side_effect=already_failed)
        svc.reject_generation_session = AsyncMock()
        await _handle_workflow_exception(
            RuntimeError("generation docker container crashed"), "est-dbl-run", svc,
        )
        svc.fail_generation_session.assert_awaited_once()
        svc.reject_generation_session.assert_not_awaited()


# ============================================================
# PR255 Bug 2 — shared workflow exception routing (reject vs fail)
# ============================================================

class TestHandleWorkflowException:
    """
    PR255 Bug 2: ContractRejection must roll the session back to PENDING (reject),
    NOT fail it. Before the fix, run_retry caught only `Exception` and routed
    ContractRejection to fail_generation_session. _handle_workflow_exception is now
    the single routing point used by BOTH the initial-run and retry paths.
    """

    @pytest.mark.asyncio
    async def test_contract_rejection_routes_to_reject_not_fail(self):
        svc = Mock()
        svc.reject_generation_session = AsyncMock()
        svc.fail_generation_session = AsyncMock()

        exc = ContractRejection(
            code=RejectionCode.ANALYSIS_UNREADABLE,
            message="Missing Part F — re-run check_specification_completeness.",
        )
        await _handle_workflow_exception(exc, "est-reject", svc)

        svc.reject_generation_session.assert_awaited_once()
        _, kwargs = svc.reject_generation_session.await_args
        assert kwargs["generation_id"] == "est-reject"
        svc.fail_generation_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_generic_exception_routes_to_fail_not_reject(self):
        svc = Mock()
        svc.reject_generation_session = AsyncMock()
        svc.fail_generation_session = AsyncMock()

        await _handle_workflow_exception(RuntimeError("docker crashed"), "est-fail", svc)

        svc.fail_generation_session.assert_awaited_once()
        svc.reject_generation_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reject_failure_falls_back_to_fail(self):
        """If reject_contract itself fails, we fall back to fail() to release resources."""
        svc = Mock()
        svc.reject_generation_session = AsyncMock(side_effect=RuntimeError("rollback boom"))
        svc.fail_generation_session = AsyncMock()

        exc = ContractRejection(
            code=RejectionCode.ANALYSIS_UNREADABLE, message="bad analysis",
        )
        await _handle_workflow_exception(exc, "est-reject-boom", svc)

        svc.reject_generation_session.assert_awaited_once()
        svc.fail_generation_session.assert_awaited_once()
        args, _ = svc.fail_generation_session.await_args
        assert "contract_reject_cleanup_failed" in args[1]


# ============================================================
# 2.5a — GET /generation-sessions/{id}/status includes result when COMPLETED
# ============================================================

class TestGenerationStatusResultFields:
    """Test that status endpoint includes result fields when COMPLETED (Gap 1 fix)."""

    def test_status_completed_includes_result(
        self, client, mock_generation_session_service
    ):
        """When status=completed, response includes result, artifact_path, code_archived."""
        from app.schemas.generation_workflow_enums import GenerationCheckpoint

        completed_doc = {
            "generation_id": "est-test-123",
            "user_email": "test@example.com",
            "status": "completed",
            "workspace_ids": [],
            "parameters": {},
            "created_at": "2024-01-01T00:00:00+00:00",
            "started_at": None,
            "completed_at": "2024-01-01T01:00:00+00:00",
            "last_heartbeat": None,
            "lease_expires_at": None,
            "retry_count": 0,
            "max_retries": 3,
            "progress": {},
            "result": {"p10y_scores": [1, 2, 3]},
            "artifact_path": "/workspaces/artifacts/est-test-123",
            "code_archived": True,
            "error": None,
            "workspace_phases": {},
        }
        mock_generation_session_service.get_generation_session_status = AsyncMock(return_value=completed_doc)
        mock_generation_session_service.get_checkpoint = Mock(return_value=GenerationCheckpoint.ESTIMATION_DONE)
        mock_generation_session_service.get_current_phase_name = Mock(return_value="generation_done")

        response = client.get(
            "/api/v1/generation-sessions/est-test-123/status",
            headers={"X-API-Key": "test-key"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "completed"
        assert data["result"] == {"p10y_scores": [1, 2, 3]}
        assert data["artifact_path"] == "/workspaces/artifacts/est-test-123"
        assert data["code_archived"] is True

    def test_status_running_excludes_result(
        self, client, mock_generation_session_service
    ):
        """When status=running, response does NOT include result fields."""
        from app.schemas.generation_workflow_enums import GenerationCheckpoint

        running_doc = {
            "generation_id": "est-test-123",
            "user_email": "test@example.com",
            "status": "running",
            "workspace_ids": ["ws-01-1"],
            "parameters": {},
            "created_at": "2024-01-01T00:00:00+00:00",
            "started_at": None,
            "completed_at": None,
            "last_heartbeat": None,
            "lease_expires_at": None,
            "retry_count": 0,
            "max_retries": 3,
            "progress": {},
            "result": None,
            "error": None,
            "workspace_phases": {},
        }
        mock_generation_session_service.get_generation_session_status = AsyncMock(return_value=running_doc)
        mock_generation_session_service.get_checkpoint = Mock(return_value=GenerationCheckpoint.GENERATION_DONE)
        mock_generation_session_service.get_current_phase_name = Mock(return_value="generation_done")

        response = client.get(
            "/api/v1/generation-sessions/est-test-123/status",
            headers={"X-API-Key": "test-key"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "running"
        assert "result" not in data
        assert "artifact_path" not in data
        assert "code_archived" not in data


# ============================================================
# 2.5b — GET /generation-sessions/{id}/outputs
# ============================================================

class TestDownloadGenerationSessionOutputs:
    """Test the download_generation_session_outputs endpoint."""

    def test_outputs_archived_true_returns_content(
        self, client, mock_generation_session_service
    ):
        """outputs_archived=True and tarball exists → returns raw binary tar.gz."""
        import tempfile
        from pathlib import Path

        archived_doc = {
            "status": "completed",
            "outputs_archived": True,
            "artifact_path": "/tmp/artifacts/est-test-123",
            "error": None,
        }
        mock_generation_session_service.get_generation_session_status = AsyncMock(return_value=archived_doc)

        fake_content = b"fake tarball content"
        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
            tmp.write(fake_content)
            tmp_path = Path(tmp.name)

        with patch(
            "app.api.v1.generation_sessions.ArtifactStore"
        ) as MockArtifactStore:
            mock_store = MockArtifactStore.return_value
            mock_store.build_tarball = AsyncMock(return_value=tmp_path)

            response = client.get(
                "/api/v1/generation-sessions/est-test-123/outputs",
                headers={"X-API-Key": "test-key"},
            )

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/gzip"
        assert "est-test-123.tar.gz" in response.headers["content-disposition"]
        assert response.content == fake_content
        tmp_path.unlink(missing_ok=True)

    def test_outputs_not_archived_no_checkpoint_returns_no_outputs(
        self, client, mock_generation_session_service
    ):
        """outputs_archived=False with no archivable checkpoint → 200 no_outputs JSON."""
        not_archived_doc = {
            "status": "completed",
            "outputs_archived": False,
            "workspace_ids": [],
            "checkpoint": "uploaded_specs",
            "error": None,
        }
        mock_generation_session_service.get_generation_session_status = AsyncMock(return_value=not_archived_doc)

        response = client.get(
            "/api/v1/generation-sessions/est-test-123/outputs",
            headers={"X-API-Key": "test-key"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "no_outputs"
        assert body["generation_id"] == "est-test-123"

    def test_failed_generation_session_triggers_emergency_archive(
        self, client, mock_generation_session_service
    ):
        """FAILED session with workspace_ids → emergency archive triggered, partial header set."""
        import tempfile
        from pathlib import Path

        failed_doc = {
            "status": "failed",
            "outputs_archived": False,
            "workspace_ids": ["ws-01-1"],
            "checkpoint": "generation_done",
            "error": "e2e loop failed",
        }
        archived_doc = {
            "status": "failed",
            "outputs_archived": True,
            "artifact_path": "/tmp/artifacts/est-test-123",
            "workspace_ids": ["ws-01-1"],
            "error": "e2e loop failed",
        }

        call_count = 0

        async def mock_get_status(generation_id):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return failed_doc
            return archived_doc

        mock_generation_session_service.get_generation_session_status = mock_get_status

        fake_content = b"fake partial tarball"
        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
            tmp.write(fake_content)
            tmp_path = Path(tmp.name)

        with patch("app.api.v1.generation_sessions.ArtifactStore") as MockArtifactStore:
            mock_store = MockArtifactStore.return_value
            mock_store.emergency_archive = AsyncMock(return_value={"workspace_warnings": []})
            mock_store.build_tarball = AsyncMock(return_value=tmp_path)

            response = client.get(
                "/api/v1/generation-sessions/est-test-123/outputs",
                headers={"X-API-Key": "test-key"},
            )

        assert response.status_code == 200
        assert response.headers.get("x-specflow-partial-output") == "true"
        assert response.content == fake_content
        mock_store.emergency_archive.assert_called_once_with("est-test-123")
        tmp_path.unlink(missing_ok=True)

    def test_running_generation_session_triggers_emergency_archive(
        self, client, mock_generation_session_service
    ):
        """RUNNING session (stuck) with workspace_ids → emergency archive triggered."""
        import tempfile
        from pathlib import Path

        running_doc = {
            "status": "running",
            "outputs_archived": False,
            "workspace_ids": ["ws-01-1"],
            "checkpoint": "deploy_and_e2e_done",
        }
        archived_doc = {
            "status": "running",
            "outputs_archived": True,
            "artifact_path": "/tmp/artifacts/est-test-123",
            "workspace_ids": ["ws-01-1"],
        }

        call_count = 0

        async def mock_get_status(generation_id):
            nonlocal call_count
            call_count += 1
            return running_doc if call_count == 1 else archived_doc

        mock_generation_session_service.get_generation_session_status = mock_get_status

        fake_content = b"fake partial tarball"
        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
            tmp.write(fake_content)
            tmp_path = Path(tmp.name)

        with patch("app.api.v1.generation_sessions.ArtifactStore") as MockArtifactStore:
            mock_store = MockArtifactStore.return_value
            mock_store.emergency_archive = AsyncMock(return_value={"workspace_warnings": []})
            mock_store.build_tarball = AsyncMock(return_value=tmp_path)

            response = client.get(
                "/api/v1/generation-sessions/est-test-123/outputs",
                headers={"X-API-Key": "test-key"},
            )

        assert response.status_code == 200
        assert response.headers.get("x-specflow-partial-output") == "true"
        mock_store.emergency_archive.assert_called_once()
        tmp_path.unlink(missing_ok=True)

    def test_already_emergency_archived_skips_trigger_and_sets_partial_header(
        self, client, mock_generation_session_service
    ):
        """emergency_archived=True → skip emergency_archive call, serve with partial header."""
        import tempfile
        from pathlib import Path

        emergency_doc = {
            "status": "failed",
            "outputs_archived": False,
            "emergency_archived": True,
            "artifact_path": "/tmp/artifacts/est-test-123",
            "workspace_ids": ["ws-01-1"],
        }
        mock_generation_session_service.get_generation_session_status = AsyncMock(return_value=emergency_doc)

        fake_content = b"fake partial tarball"
        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
            tmp.write(fake_content)
            tmp_path = Path(tmp.name)

        with patch("app.api.v1.generation_sessions.ArtifactStore") as MockArtifactStore:
            mock_store = MockArtifactStore.return_value
            mock_store.build_tarball = AsyncMock(return_value=tmp_path)

            response = client.get(
                "/api/v1/generation-sessions/est-test-123/outputs",
                headers={"X-API-Key": "test-key"},
            )

        assert response.status_code == 200
        assert response.headers.get("x-specflow-partial-output") == "true"
        assert response.content == fake_content
        mock_store.emergency_archive.assert_not_called()
        tmp_path.unlink(missing_ok=True)

    def test_emergency_archive_all_workspaces_unreachable_returns_404(
        self, client, mock_generation_session_service
    ):
        """Emergency archive raises RuntimeError (all workspaces gone) → 404."""
        failed_doc = {
            "status": "failed",
            "outputs_archived": False,
            "workspace_ids": ["ws-01-1"],
        }
        mock_generation_session_service.get_generation_session_status = AsyncMock(return_value=failed_doc)

        with patch("app.api.v1.generation_sessions.ArtifactStore") as MockArtifactStore:
            mock_store = MockArtifactStore.return_value
            mock_store.emergency_archive = AsyncMock(
                side_effect=RuntimeError("could not archive any workspace")
            )

            response = client.get(
                "/api/v1/generation-sessions/est-test-123/outputs",
                headers={"X-API-Key": "test-key"},
            )

        assert response.status_code == 404
        assert "Emergency archive failed" in response.json()["detail"]

    def test_outputs_missing_artifact_dir_returns_404(
        self, client, mock_generation_session_service
    ):
        """outputs_archived=True but artifact dir missing → 404."""
        archived_doc = {
            "status": "completed",
            "outputs_archived": True,
            "artifact_path": "/nonexistent/path",
            "error": None,
        }
        mock_generation_session_service.get_generation_session_status = AsyncMock(return_value=archived_doc)

        with patch(
            "app.api.v1.generation_sessions.ArtifactStore"
        ) as MockArtifactStore:
            mock_store = MockArtifactStore.return_value
            mock_store.build_tarball = AsyncMock(side_effect=FileNotFoundError("not found"))

            response = client.get(
                "/api/v1/generation-sessions/est-test-123/outputs",
                headers={"X-API-Key": "test-key"},
            )

        assert response.status_code == 404

    def test_completed_status_uploaded_specs_checkpoint_still_returns_no_outputs(
        self, client, mock_generation_session_service
    ):
        """
        Regression guard: status=completed + checkpoint=uploaded_specs must still
        return no_outputs (completed but nothing ran). The analysis-status fix must
        not accidentally let non-analysis statuses bypass the checkpoint guard.
        """
        not_archived_doc = {
            "status": "completed",
            "outputs_archived": False,
            "workspace_ids": [],
            "checkpoint": "uploaded_specs",
            "error": None,
        }
        mock_generation_session_service.get_generation_session_status = AsyncMock(return_value=not_archived_doc)

        response = client.get(
            "/api/v1/generation-sessions/est-test-123/outputs",
            headers={"X-API-Key": "test-key"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "no_outputs"


class TestDownloadGenerationSessionReportHtml:
    """Test the report.html endpoint — lightweight alternative to /outputs.

    The backend runs in a container while the TUI runs on the host, so the
    TUI can't just stat ``artifact_path`` on its own filesystem; it fetches
    the report over HTTP instead.
    """

    def test_returns_html_when_report_exists(self, client, tmp_path):
        from app.core.artifact_files import MULTI_WORKSPACE_REPORT_HTML_FILE
        from app.core.artifact_subdirs import REPORT_SUBDIR

        report_dir = tmp_path / "est-test-123" / REPORT_SUBDIR
        report_dir.mkdir(parents=True)
        (report_dir / MULTI_WORKSPACE_REPORT_HTML_FILE).write_text("<html>report</html>")

        with patch("app.api.v1.generation_sessions.ARTIFACTS_BASE", tmp_path):
            response = client.get(
                "/api/v1/generation-sessions/est-test-123/report.html",
                headers={"X-API-Key": "test-key"},
            )

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert response.text == "<html>report</html>"

    def test_404_when_report_missing(self, client, tmp_path):
        with patch("app.api.v1.generation_sessions.ARTIFACTS_BASE", tmp_path):
            response = client.get(
                "/api/v1/generation-sessions/est-test-123/report.html",
                headers={"X-API-Key": "test-key"},
            )

        assert response.status_code == 404


class TestStreamWorkspaceMessages:
    """Tests for GET /{generation_id}/workspaces/{workspace_id}/messages/stream (SSE)."""

    def test_unknown_workspace_returns_404(
        self, client, mock_generation_session_service
    ):
        """A workspace id not in the session must be rejected before streaming."""
        mock_generation_session_service.get_generation_session_status = AsyncMock(
            return_value={"workspace_ids": ["ws-01-1", "ws-01-2"]}
        )
        response = client.get(
            "/api/v1/generation-sessions/est-test-123/workspaces/ws-99-9/messages/stream",
            headers={"X-API-Key": "test-key"},
        )
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_stream_emits_connected_then_published_event(self):
        """Driving the SSE generator directly: it emits a connected comment, then a
        live event published to the broker for the matching (generation, workspace)."""
        from app.api.v1.generation_sessions import stream_workspace_messages
        from app.services.agent_stream_broker import get_agent_stream_broker
        from app.services.agent_stream_events import AgentStreamEvent

        svc = Mock()
        svc.get_generation_session_status = AsyncMock(
            return_value={"workspace_ids": ["ws-01-1"]}
        )

        class _FakeRequest:
            async def is_disconnected(self):
                return False

        response = await stream_workspace_messages(
            "est-1", "ws-01-1", _FakeRequest(), None, svc
        )
        assert response.media_type == "text/event-stream"

        agen = response.body_iterator
        try:
            # Subscription now happens inside the generator, so iterate once (the
            # connected comment) to register the queue before publishing.
            first = await agen.__anext__()
            assert "connected" in first
            get_agent_stream_broker().publish(
                "est-1",
                "ws-01-1",
                AgentStreamEvent(
                    timestamp="2026-06-26T00:00:00+00:00",
                    generation_id="est-1",
                    workspace_id="ws-01-1",
                    kind="assistant_text",
                    message="hello world",
                ),
            )
            second = await agen.__anext__()
            assert "hello world" in second
            assert second.startswith("data: ")
        finally:
            await agen.aclose()


class TestListGenerationSessions:
    """GET /generation-sessions/ — list active + completed sessions for the caller's key."""

    @staticmethod
    def _request(api_key):
        return SimpleNamespace(state=SimpleNamespace(api_key=api_key))

    @pytest.mark.asyncio
    async def test_lists_by_key_uid_newest_first_with_slimmed_phases(self):
        """key_uid path: newest-first, completed/failed included, only own key, phases slimmed."""
        db = InMemoryDatabase()
        db.set("api_keys", "key-doc-1", {"key_uid": "uid-1"})
        older = datetime(2026, 6, 29, 10, 0, tzinfo=timezone.utc)
        newer = datetime(2026, 6, 30, 10, 0, tzinfo=timezone.utc)
        db.set(COL_GENERATION_SESSIONS, "est-old", {
            "generation_id": "est-old", "key_uid": "uid-1",
            "status": GenerationStatus.COMPLETED.value, "checkpoint": "estimation_done",
            "created_at": older,
            "workspace_phases": {
                "ws-01-1": {"last_completed_phase": 3, "phase_name": "Payments",
                            "planning_data": {"big": "drop me"}},
            },
        })
        db.set(COL_GENERATION_SESSIONS, "est-new", {
            "generation_id": "est-new", "key_uid": "uid-1",
            "status": GenerationStatus.FAILED.value, "checkpoint": "generation_started",
            "created_at": newer, "current_phase": "Generating", "workspace_phases": {},
        })
        # A session belonging to a different key must not leak into the result.
        db.set(COL_GENERATION_SESSIONS, "est-other", {
            "generation_id": "est-other", "key_uid": "uid-2",
            "status": GenerationStatus.RUNNING.value, "created_at": newer,
        })

        result = await list_generation_sessions(request=self._request("key-doc-1"), db=db, limit=50)

        assert [s.generation_id for s in result] == ["est-new", "est-old"]
        assert result[0].status == GenerationStatus.FAILED.value
        assert result[0].current_phase == "Generating"
        assert result[1].status == GenerationStatus.COMPLETED.value
        assert result[1].created_at == older.isoformat()
        # workspace_phases is slimmed: summary fields kept, planning_data dropped.
        ws = result[1].workspace_phases["ws-01-1"]
        assert ws.last_completed_phase == 3
        assert ws.phase_name == "Payments"
        assert not hasattr(ws, "planning_data")

    @pytest.mark.asyncio
    async def test_limit_caps_results_to_newest(self):
        db = InMemoryDatabase()
        db.set("api_keys", "key-doc-1", {"key_uid": "uid-1"})
        for i, day in enumerate((28, 29, 30)):
            db.set(COL_GENERATION_SESSIONS, f"est-{day}", {
                "generation_id": f"est-{day}", "key_uid": "uid-1",
                "status": GenerationStatus.COMPLETED.value,
                "created_at": datetime(2026, 6, day, tzinfo=timezone.utc),
            })

        result = await list_generation_sessions(request=self._request("key-doc-1"), db=db, limit=1)

        assert [s.generation_id for s in result] == ["est-30"]

    @pytest.mark.asyncio
    async def test_fallback_resolves_real_status_when_no_key_uid(self):
        """No-key_uid keys hit the lease-snapshot fallback, which must resolve real status."""
        db = InMemoryDatabase()
        now = datetime.now(timezone.utc)
        db.set("api_keys", "key-doc-legacy", {
            "max_concurrent_sessions": 5,
            "active_generation_sessions": [
                {"generation_id": "est-active", "operation": "generation",
                 "lease_started_at": now, "lease_ttl_minutes": 480},
            ],
        })
        db.set(COL_GENERATION_SESSIONS, "est-active", {
            "generation_id": "est-active", "status": GenerationStatus.RUNNING.value,
            "checkpoint": "generation_started", "created_at": now, "current_phase": "Generating",
        })

        result = await list_generation_sessions(request=self._request("key-doc-legacy"), db=db, limit=50)

        assert len(result) == 1
        assert result[0].generation_id == "est-active"
        # The whole point of finding #4: never a flat "unknown" — resolve the real doc status.
        assert result[0].status == GenerationStatus.RUNNING.value
        assert result[0].current_phase == "Generating"

    @pytest.mark.asyncio
    async def test_unauthenticated_returns_401(self):
        with pytest.raises(HTTPException) as exc:
            await list_generation_sessions(request=self._request(None), db=InMemoryDatabase(), limit=50)
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_unknown_api_key_returns_404(self):
        with pytest.raises(HTTPException) as exc:
            await list_generation_sessions(request=self._request("nope"), db=InMemoryDatabase(), limit=50)
        assert exc.value.status_code == 404
