"""
Integration tests for email notification functionality.

Tests email generation for generation completion and resend email endpoint.
Mocks SMTP client to avoid actually sending emails.
"""

from datetime import datetime, timezone
from unittest.mock import Mock, patch, MagicMock, AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import require_generation_session_owner
from app.api.v1.generation_sessions import (
    router as generation_sessions_router,
    get_workspace_pool,
    get_generation_session_service,
    get_generation_session_retry_service,
    _reconstruct_generation_session_response,
    _multi_workspace_result_to_store,
)
from app.database.dependencies import get_db
from app.core.notifications import EmailNotifier, notifications
from app.database.memory import InMemoryDatabase
from app.schemas.estimate import (
    MultiWorkspaceEstimationResponse,
    EstimationSummary,
    WorkspaceEstimation,
    ComparativeAnalysis,
    ComponentEstimation,
    EstimationMetrics,
    ComponentComparison,
    RiskAssessment,
    SimplifiedEstimationResponse,
    SkippedWorkspaceP10Y,
)
from app.schemas.model_token_usage import ModelTokenUsage
from app.services.generation_session import GenerationSessionNotFoundError


@pytest.fixture
def mock_db():
    """Create in-memory database for testing."""
    return InMemoryDatabase()


@pytest.fixture
def mock_workspace_pool():
    """Create a mock WorkspacePoolService."""
    mock_service = Mock()
    return mock_service


@pytest.fixture
def mock_generation_session_service():
    """Create a mock GenerationSessionService."""
    mock_service = Mock()
    mock_service.get_generation_session_status = AsyncMock()
    mock_service.complete_generation_session = AsyncMock()
    mock_service.update_completed_generation_session_result = AsyncMock()
    mock_service.db_adapter = Mock()
    return mock_service


@pytest.fixture
def mock_retry_service():
    """Create a mock GenerationSessionRetryService."""
    mock_service = Mock()
    return mock_service


@pytest.fixture
def app(test_app, mock_workspace_pool, mock_generation_session_service, mock_retry_service, mock_db):
    """Create FastAPI app with generations router and mocked dependencies."""
    # Include router
    test_app.include_router(generation_sessions_router, prefix="/api/v1")
    
    # Override dependencies
    def override_get_workspace_pool():
        return mock_workspace_pool
    
    def override_get_generation_session_service():
        return mock_generation_session_service
    
    def override_get_generation_session_retry_service():
        return mock_retry_service
    
    def override_get_db():
        return mock_db
    
    test_app.dependency_overrides[get_workspace_pool] = override_get_workspace_pool
    test_app.dependency_overrides[get_generation_session_service] = override_get_generation_session_service
    test_app.dependency_overrides[get_generation_session_retry_service] = override_get_generation_session_retry_service
    test_app.dependency_overrides[get_db] = override_get_db

    # Override ownership check (tests run without auth middleware)
    async def override_require_generation_session_owner():
        return None
    test_app.dependency_overrides[require_generation_session_owner] = override_require_generation_session_owner
    
    yield test_app
    
    # Cleanup
    test_app.dependency_overrides.clear()


@pytest.fixture
def client(app):
    """Create test client."""
    return TestClient(app)


@pytest.fixture
def sample_workspace_docs(mock_db):
    """Create sample workspace documents in database."""
    now = datetime.now(timezone.utc)
    workspaces = [
        {
            "repo_url": "https://github.com/testuser/repo1",
            "p10y_repository_id": 1,
            "workspace_pool": "default",
            "set_number": 1,
            "status": "available",
            "clean_verified": True,
            "last_cleaned_at": now,
        },
        {
            "repo_url": "https://github.com/testuser/repo2",
            "p10y_repository_id": 2,
            "workspace_pool": "default",
            "set_number": 1,
            "status": "available",
            "clean_verified": True,
            "last_cleaned_at": now,
        },
        {
            "repo_url": "https://github.com/testuser/repo3",
            "p10y_repository_id": 3,
            "workspace_pool": "default",
            "set_number": 1,
            "status": "available",
            "clean_verified": True,
            "last_cleaned_at": now,
        },
    ]
    
    workspace_ids = ["ws-01-1", "ws-01-2", "ws-01-3"]
    for ws_id, ws_doc in zip(workspace_ids, workspaces):
        mock_db.set("workspaces", ws_id, ws_doc)
    
    return workspace_ids, workspaces


@pytest.fixture
def sample_estimation_result():
    """Create a sample MultiWorkspaceEstimationResponse."""
    risk_assessment = RiskAssessment(
        status="Approved",
        instability_ratio=0.15,
        rejection_threshold=0.5,
        base_component=0.1,
        var_component=0.05,
        size_component=0.0,
        total_buffer_pct=0.15,
        final_estimate=115.0,
    )
    
    summary = EstimationSummary(
        average_hours=100.0,
        std_deviation=10.0,
        min_hours=90.0,
        max_hours=110.0,
        coefficient_of_variation=0.1,
        variance_assessment="low",
        risk_assessment=risk_assessment,
    )
    
    # Create workspace generations with component breakdown
    workspace_estimations = []
    models = [
        "anthropic/claude-sonnet-4.5",
        "google/gemini-2.5-pro",
        "anthropic/claude-sonnet-4.5",
    ]
    for i, ws_name in enumerate(["ws-01-1", "ws-01-2", "ws-01-3"], 1):
        component_breakdown = {
            "auth": ComponentEstimation(
                component_name="auth",
                hours=20.0 + i,
                new_work=15.0,
                refactor=3.0,
                rework=2.0 + i,
                quality_score=0.85,
            ),
            "api": ComponentEstimation(
                component_name="api",
                hours=30.0 + i,
                new_work=25.0,
                refactor=4.0,
                rework=1.0 + i,
                quality_score=0.90,
            ),
        }
        
        estimation_metrics = EstimationMetrics(
            new_work=40.0,
            refactor=7.0,
            rework=3.0 + i,
            removed_work=0.0,
            quality_score=0.875,
            effective_output=47.0,
            total_output=50.0,
        )
        
        ws_est = WorkspaceEstimation(
            workspace_name=ws_name,
            workspace_path=f"/workspaces/{ws_name}",
            total_hours=50.0 + i,
            total_effective_output=47.0,
            component_breakdown=component_breakdown,
            estimation_metrics=estimation_metrics,
            commits_count=10 + i,
            model_usage=ModelTokenUsage(
                model_name=models[i - 1],
                num_turns=12 + i,
                input_tokens=1_500_000 * i,
                output_tokens=500_000 * i,
                cache_write_tokens=1000 * i,
                cache_read_tokens=500 * i,
            ),
        )
        workspace_estimations.append(ws_est)
    
    # Create comparative analysis
    component_comparison = {
        "auth": ComponentComparison(
            component_name="auth",
            hours_by_workspace={
                "ws-01-1": 21.0,
                "ws-01-2": 22.0,
                "ws-01-3": 23.0,
            },
            average=22.0,
            std_deviation=0.82,
            variance_percentage=3.7,
        ),
        "api": ComponentComparison(
            component_name="api",
            hours_by_workspace={
                "ws-01-1": 31.0,
                "ws-01-2": 32.0,
                "ws-01-3": 33.0,
            },
            average=32.0,
            std_deviation=0.82,
            variance_percentage=2.6,
        ),
    }
    
    comparative_analysis = ComparativeAnalysis(
        component_comparison=component_comparison,
        high_variance_components=[],
        insights=["Low variance across workspaces", "Consistent estimates"],
    )
    
    return MultiWorkspaceEstimationResponse(
        summary=summary,
        workspace_estimations=workspace_estimations,
        comparative_analysis=comparative_analysis,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


@pytest.fixture
def mock_email_config():
    """Create mock email config."""
    config = Mock()
    config.username = "test@example.com"
    config.password = "test_password"
    return config


class TestEmailNotificationGeneration:
    """Tests for email content generation."""
    
    @patch('app.core.notifications.smtplib.SMTP_SSL')
    def test_notify_generation_session_complete_with_full_data(
        self, mock_smtp_class, mock_db, sample_workspace_docs, sample_estimation_result, mock_email_config
    ):
        """Test email notification with full generation data."""
        workspace_ids, _ = sample_workspace_docs
        
        # Mock SMTP
        mock_smtp_instance = MagicMock()
        mock_smtp_class.return_value.__enter__.return_value = mock_smtp_instance
        
        # Create email notifier
        email_notifier = EmailNotifier(mock_email_config)
        
        # Call notify_generation_session_complete
        email_notifier.notify_generation_session_complete(
            generation_id="est-test-123",
            workspace_ids=workspace_ids,
            result=sample_estimation_result,
            spec_path="specs/test.md",
            recipient_email="recipient@example.com",
            db=mock_db
        )
        
        # Verify SMTP was called
        mock_smtp_class.assert_called_once_with('smtp.gmail.com', 465)
        mock_smtp_instance.login.assert_called_once_with("test@example.com", "test_password")
        assert mock_smtp_instance.send_message.called
        
        # Get the sent message
        sent_message = mock_smtp_instance.send_message.call_args[0][0]
        
        # Verify email headers
        assert sent_message['Subject'] == "SpecFlow Iteration Complete: specs/test.md"
        assert sent_message['From'] == "test@example.com"
        assert sent_message['To'] == "recipient@example.com"
        
        # Verify email content (both plain and HTML)
        # EmailMessage with multipart/alternative has payload as list
        # Get plain text content (first part)
        plain_content = ""
        html_content = ""
        if sent_message.is_multipart():
            for part in sent_message.get_payload():
                content_type = part.get_content_type()
                if content_type == 'text/plain':
                    plain_content = part.get_payload(decode=True).decode()
                elif content_type == 'text/html':
                    html_content = part.get_payload(decode=True).decode()
                    assert html_content.strip() != ""
        else:
            # Single part message
            plain_content = sent_message.get_payload(decode=True).decode()
            html_content = ""
        
        # Check that variant links are included
        assert "https://github.com/testuser/repo1/tree/archive/est-test-123" in html_content
        assert "https://github.com/testuser/repo2/tree/archive/est-test-123" in html_content
        assert "https://github.com/testuser/repo3/tree/archive/est-test-123" in html_content
        
        # Check that section is named "Variants"
        assert "Variants" in html_content
        
        # Check model info is included in variants
        assert "anthropic/claude-sonnet-4.5" in html_content
        assert "google/gemini-2.5-pro" in html_content
        assert "Agent turns:" in html_content
        assert "Tokens (cumulative):" in html_content
        assert "input:" in html_content
        assert "cache write:" in html_content
        
        # Check that component breakdown is included
        assert "Component Complexity Metrics Breakdown" in html_content
        assert "auth" in html_content
        assert "api" in html_content
        
        # Check summary information (P10Y variance — not approval/rejection labels)
        assert "115.0" in html_content  # Final estimate
        assert "Variance (P10Y)" in html_content
        assert "low (CV 10.0%)" in html_content
        
        # Check header says "SpecFlow Iteration Complete"
        assert "SpecFlow Iteration Complete" in html_content
        
        # Verify plain text content
        assert "est-test-123" in plain_content
        assert "VARIANTS:" in plain_content
        assert "SpecFlow ITERATION COMPLETE" in plain_content
        assert "COMPONENT BREAKDOWN" in plain_content
        assert "model: anthropic/claude-sonnet-4.5" in plain_content

    @patch("app.core.notifications.smtplib.SMTP_SSL")
    def test_notify_generation_session_pre_deploy_milestone_subject_and_header(
        self, mock_smtp_class, mock_db, mock_email_config
    ):
        """Pre-deploy milestone uses deployment-starting subject and milestone HTML title."""
        from app.core.notifications import build_coding_complete_pre_deploy_response

        mock_smtp_instance = MagicMock()
        mock_smtp_class.return_value.__enter__.return_value = mock_smtp_instance

        result = build_coding_complete_pre_deploy_response(
            workspace_ids=["ws-01-1"],
            workflow_usage_metrics={},
        )
        email_notifier = EmailNotifier(mock_email_config)
        email_notifier.notify_generation_session_complete(
            generation_id="est-pre",
            workspace_ids=["ws-01-1"],
            result=result,
            spec_path="specs/pre.md",
            recipient_email="r@example.com",
            db=mock_db,
            notification_kind="coding_complete_pre_deploy",
        )
        sent_message = mock_smtp_instance.send_message.call_args[0][0]
        assert sent_message["Subject"] == "SpecFlow: Coding complete — deployment starting: specs/pre.md"
        html_content = ""
        for part in sent_message.get_payload():
            if part.get_content_type() == "text/html":
                html_content = part.get_payload(decode=True).decode()
        assert "Coding complete" in html_content
        assert "deployment" in html_content.lower()
    
    @patch('app.core.notifications.smtplib.SMTP_SSL')
    def test_notify_generation_session_complete_without_component_breakdown(
        self, mock_smtp_class, mock_db, sample_workspace_docs, mock_email_config
    ):
        """Test email notification without component breakdown."""
        workspace_ids, _ = sample_workspace_docs
        
        # Create result without component breakdown
        risk_assessment = RiskAssessment(
            status="Approved",
            instability_ratio=0.15,
            rejection_threshold=0.5,
            base_component=0.1,
            var_component=0.05,
            size_component=0.0,
            total_buffer_pct=0.15,
            final_estimate=100.0,
        )
        
        summary = EstimationSummary(
            average_hours=100.0,
            std_deviation=10.0,
            min_hours=90.0,
            max_hours=110.0,
            coefficient_of_variation=0.1,
            variance_assessment="low",
            risk_assessment=risk_assessment,
        )
        
        # Workspace generation without component breakdown
        estimation_metrics = EstimationMetrics(
            new_work=40.0,
            refactor=7.0,
            rework=3.0,
            removed_work=0.0,
            quality_score=0.875,
            effective_output=47.0,
            total_output=50.0,
        )
        
        workspace_est = WorkspaceEstimation(
            workspace_name="workspace-1",
            workspace_path="/workspaces/workspace-1",
            total_hours=50.0,
            total_effective_output=47.0,
            component_breakdown={},  # Empty breakdown
            estimation_metrics=estimation_metrics,
            commits_count=10,
        )
        
        result = MultiWorkspaceEstimationResponse(
            summary=summary,
            workspace_estimations=[workspace_est],
            comparative_analysis=ComparativeAnalysis(
                component_comparison={},
                high_variance_components=[],
                insights=[],
            ),
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        
        # Mock SMTP
        mock_smtp_instance = MagicMock()
        mock_smtp_class.return_value.__enter__.return_value = mock_smtp_instance
        
        # Create email notifier
        email_notifier = EmailNotifier(mock_email_config)
        
        # Call notify_generation_session_complete
        email_notifier.notify_generation_session_complete(
            generation_id="est-test-456",
            workspace_ids=workspace_ids[:1],  # Only one workspace
            result=result,
            spec_path="specs/test2.md",
            recipient_email="recipient2@example.com",
            db=mock_db
        )
        
        # Get the sent message
        sent_message = mock_smtp_instance.send_message.call_args[0][0]
        
        # Get HTML content from multipart payload
        html_content = ""
        if sent_message.is_multipart():
            for part in sent_message.get_payload():
                if part.get_content_type() == 'text/html':
                    html_content = part.get_payload()
                    assert html_content.strip() != ""
                    break
        
        # Component breakdown section should not be present
        # (it should be omitted when empty)
        assert "Component Complexity Metrics Breakdown" not in html_content
    
    @patch('app.core.notifications.smtplib.SMTP_SSL')
    def test_notify_generation_session_complete_with_missing_workspace(
        self, mock_smtp_class, mock_db, mock_email_config, sample_estimation_result
    ):
        """Test email notification when workspace document is missing."""
        # Use workspace IDs that don't exist in database
        workspace_ids = ["ws-nonexistent-1", "ws-nonexistent-2", "ws-nonexistent-3"]
        
        # Mock SMTP
        mock_smtp_instance = MagicMock()
        mock_smtp_class.return_value.__enter__.return_value = mock_smtp_instance
        
        # Create email notifier
        email_notifier = EmailNotifier(mock_email_config)
        
        # Call notify_generation_session_complete - should not fail
        email_notifier.notify_generation_session_complete(
            generation_id="est-test-789",
            workspace_ids=workspace_ids,
            result=sample_estimation_result,
            spec_path="specs/test3.md",
            recipient_email="recipient3@example.com",
            db=mock_db
        )
        
        # Verify email was still sent (without repo links)
        assert mock_smtp_instance.send_message.called
        
        sent_message = mock_smtp_instance.send_message.call_args[0][0]
        
        # Get HTML content from multipart payload
        html_content = ""
        if sent_message.is_multipart():
            for part in sent_message.get_payload():
                if part.get_content_type() == 'text/html':
                    html_content = part.get_payload()
                    assert html_content.strip() != ""
                    break
        
        # Repository links section may be empty or missing
        # But email should still be sent successfully


class TestMultiWorkspaceResultSerialization:
    """Stored Firestore result shape for email resend / P10Y metadata."""

    def test_multi_workspace_result_to_store_includes_p10y_fields(
        self, sample_estimation_result
    ):
        """_multi_workspace_result_to_store persists skipped_workspaces and coverage %."""
        fresh = MultiWorkspaceEstimationResponse(
            summary=sample_estimation_result.summary,
            workspace_estimations=sample_estimation_result.workspace_estimations,
            comparative_analysis=sample_estimation_result.comparative_analysis,
            timestamp=sample_estimation_result.timestamp,
            skipped_workspaces=[
                SkippedWorkspaceP10Y(workspace_name="ws-01-2", reason="P10Y timeout")
            ],
            aggregate_p10y_commit_coverage_pct=71.25,
            total_usd_cost=3.45,
        )
        simplified = SimplifiedEstimationResponse(
            status="Approved",
            final_estimate_hours=115.0,
        )
        stored = _multi_workspace_result_to_store(fresh, simplified)
        assert stored["final_estimate_hours"] == 115.0
        assert len(stored["skipped_workspaces"]) == 1
        assert stored["skipped_workspaces"][0]["workspace_name"] == "ws-01-2"
        assert stored["aggregate_p10y_commit_coverage_pct"] == 71.25
        assert stored["total_usd_cost"] == 3.45


class TestReconstructGenerationResponse:
    """Tests for reconstructing generation response from stored data."""
    
    def test_reconstruct_with_full_data(self, sample_estimation_result):
        """Test reconstructing response from full stored data."""
        # Convert result to dict (as it would be stored)
        stored_result = {
            "status": "Approved",
            "final_estimate_hours": 115.0,
            "summary": sample_estimation_result.summary.model_dump(),
            "workspace_estimations": [ws.model_dump() for ws in sample_estimation_result.workspace_estimations],
            "comparative_analysis": sample_estimation_result.comparative_analysis.model_dump(),
            "timestamp": sample_estimation_result.timestamp,
        }
        
        # Reconstruct
        reconstructed = _reconstruct_generation_session_response(stored_result)
        
        # Verify reconstruction
        assert isinstance(reconstructed, MultiWorkspaceEstimationResponse)
        assert reconstructed.summary.average_hours == 100.0
        assert reconstructed.summary.risk_assessment.status == "Approved"
        assert len(reconstructed.workspace_estimations) == 3
        assert len(reconstructed.comparative_analysis.component_comparison) == 2
        
        # Verify component breakdown
        assert "auth" in reconstructed.workspace_estimations[0].component_breakdown
        assert "api" in reconstructed.workspace_estimations[0].component_breakdown

    def test_reconstruct_includes_skipped_workspaces_and_p10y_coverage(
        self, sample_estimation_result
    ):
        """Stored results from P10Y best-effort runs round-trip skipped_workspaces + coverage."""
        ws_dump = [ws.model_dump() for ws in sample_estimation_result.workspace_estimations]
        if ws_dump:
            ws_dump[0]["p10y_scored_commits"] = 7
        stored_result = {
            "status": "Approved",
            "final_estimate_hours": 115.0,
            "summary": sample_estimation_result.summary.model_dump(),
            "workspace_estimations": ws_dump,
            "comparative_analysis": sample_estimation_result.comparative_analysis.model_dump(),
            "timestamp": sample_estimation_result.timestamp,
            "skipped_workspaces": [
                {"workspace_name": "ws-01-2", "reason": "no P10Y scores"},
            ],
            "aggregate_p10y_commit_coverage_pct": 88.0,
        }
        reconstructed = _reconstruct_generation_session_response(stored_result)
        assert len(reconstructed.skipped_workspaces) == 1
        assert reconstructed.skipped_workspaces[0].workspace_name == "ws-01-2"
        assert reconstructed.aggregate_p10y_commit_coverage_pct == 88.0
        assert reconstructed.workspace_estimations[0].p10y_scored_commits == 7
    
    def test_reconstruct_legacy_stored_result_returns_minimal_response(self):
        """Legacy payloads (no workspace_estimations / comparative_analysis) return a minimal
        response rather than raising so that resend-email works for old completions."""
        stored_result = {
            "status": "Approved",
            "final_estimate_hours": 100.0,
            "summary": {
                "average_hours": 100.0,
                "std_deviation": 10.0,
                "min_hours": 90.0,
                "max_hours": 110.0,
                "coefficient_of_variation": 0.1,
                "variance_assessment": "low",
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        result = _reconstruct_generation_session_response(stored_result)
        assert result.summary.average_hours == 100.0
        assert result.workspace_estimations == []
        assert result.comparative_analysis.component_comparison == {}


class TestResendEmailEndpoint:
    """Tests for resend email endpoint."""
    
    @patch('app.core.notifications.smtplib.SMTP_SSL')
    def test_resend_email_success(
        self, mock_smtp_class, client, mock_generation_session_service, mock_db,
        sample_workspace_docs, sample_estimation_result, mock_email_config
    ):
        """Test successful email resend."""
        workspace_ids, _ = sample_workspace_docs
        generation_id = "est-test-resend"
        
        # Setup stored result
        stored_result = {
            "status": "Approved",
            "final_estimate_hours": 115.0,
            "summary": sample_estimation_result.summary.model_dump(),
            "workspace_estimations": [ws.model_dump() for ws in sample_estimation_result.workspace_estimations],
            "comparative_analysis": sample_estimation_result.comparative_analysis.model_dump(),
            "timestamp": sample_estimation_result.timestamp,
        }
        
        # Mock generation document
        est_doc = {
            "user_email": "user@example.com",
            "status": "completed",
            "workspace_ids": workspace_ids,
            "parameters": {"spec_path": "specs/test.md"},
            "result": stored_result,
        }
        
        mock_generation_session_service.get_generation_session_status = AsyncMock(return_value=est_doc)
        
        # Mock SMTP
        mock_smtp_instance = MagicMock()
        mock_smtp_class.return_value.__enter__.return_value = mock_smtp_instance
        
        # Ensure EmailNotifier is in notifications list
        email_notifier = EmailNotifier(mock_email_config)
        # Clear existing notifiers and add our mock email notifier
        original_notifiers = notifications.notifiers[:]
        notifications.notifiers = [email_notifier]
        
        try:
            # Mock settings
            with patch('app.api.v1.generation_sessions.settings') as mock_est_settings:
                mock_est_settings.NOTIFY_EMAIL_USERNAME = None
                # Make request with recipient_email (bypasses request.state requirement)
                response = client.post(
                    f"/api/v1/generation-sessions/{generation_id}/resend-email",
                    data={"recipient_email": "recipient@example.com"}
                )
        finally:
            # Restore original notifiers
            notifications.notifiers = original_notifiers
        
        # Verify response
        assert response.status_code == 200
        data = response.json()
        assert data["generation_id"] == generation_id
        assert data["email_sent"] is True
        assert data["recipient"] == "recipient@example.com"
        
        # Verify email was sent
        assert mock_smtp_instance.send_message.called
    
    def test_resend_email_not_completed(self, client, mock_generation_session_service):
        """Test resend email fails for non-completed generation."""
        generation_id = "est-test-pending"
        
        # Mock generation document with pending status
        est_doc = {
            "user_email": "user@example.com",
            "status": "pending",
            "workspace_ids": [],
            "parameters": {},
            "result": None,
        }
        
        mock_generation_session_service.get_generation_session_status = AsyncMock(return_value=est_doc)
        
        # Mock settings to avoid AttributeError
        with patch('app.api.v1.generation_sessions.settings') as mock_settings:
            mock_settings.NOTIFY_EMAIL_USERNAME = None
            # Make request
            response = client.post(
                f"/api/v1/generation-sessions/{generation_id}/resend-email",
                data={"recipient_email": "recipient@example.com"}
            )
        
        # Verify error response
        assert response.status_code == 400
        assert "Only completed sessions" in response.json()["detail"]
    
    def test_resend_email_no_result_data(self, client, mock_generation_session_service):
        """Test resend email fails when no result data exists."""
        generation_id = "est-test-no-result"
        
        # Mock generation document without result
        est_doc = {
            "user_email": "user@example.com",
            "status": "completed",
            "workspace_ids": [],
            "parameters": {},
            "result": None,  # No result data
        }
        
        mock_generation_session_service.get_generation_session_status = AsyncMock(return_value=est_doc)
        
        # Mock settings to avoid AttributeError
        with patch('app.api.v1.generation_sessions.settings') as mock_settings:
            mock_settings.NOTIFY_EMAIL_USERNAME = None
            # Make request
            response = client.post(
                f"/api/v1/generation-sessions/{generation_id}/resend-email",
                data={"recipient_email": "recipient@example.com"}
            )
        
        # Verify error response
        assert response.status_code == 400
        assert "no stored result data" in response.json()["detail"]
    
    def test_resend_email_not_found(self, client, mock_generation_session_service):
        """Test resend email fails for non-existent generation."""
        generation_id = "est-nonexistent"

        mock_generation_session_service.get_generation_session_status = AsyncMock(
            side_effect=GenerationSessionNotFoundError(f"Generation {generation_id} not found")
        )
        
        # Mock settings to avoid AttributeError
        with patch('app.api.v1.generation_sessions.settings') as mock_settings:
            mock_settings.NOTIFY_EMAIL_USERNAME = None
            # Make request
            response = client.post(
                f"/api/v1/generation-sessions/{generation_id}/resend-email",
                data={"recipient_email": "recipient@example.com"}
            )
        
        # Verify error response
        assert response.status_code == 404
    
    @patch("app.api.v1.generation_sessions.create_agent_logger")
    @patch("app.api.v1.generation_sessions.multi_workspace_estimation_p10y_workflow", new_callable=AsyncMock)
    @patch("app.core.notifications.smtplib.SMTP_SSL")
    def test_resend_email_recalculate_p10y_success(
        self,
        mock_smtp_class,
        mock_p10y_workflow,
        mock_create_agent_logger,
        client,
        mock_generation_session_service,
        sample_workspace_docs,
        sample_estimation_result,
        mock_email_config,
    ):
        """recalculate_p10y re-runs P10Y, patches stored result, then sends email."""
        workspace_ids, _ = sample_workspace_docs
        generation_id = "est-recalc-p10y"

        stored_result = {
            "status": "Approved",
            "final_estimate_hours": 100.0,
            "summary": sample_estimation_result.summary.model_dump(),
            "workspace_estimations": [
                ws.model_dump() for ws in sample_estimation_result.workspace_estimations
            ],
            "comparative_analysis": sample_estimation_result.comparative_analysis.model_dump(),
            "timestamp": sample_estimation_result.timestamp,
        }

        est_doc = {
            "user_email": "user@example.com",
            "status": "completed",
            "workspace_ids": workspace_ids,
            "parameters": {
                "spec_path": "specifications",
                "outputs_dir": "specflow",
                "session_id": "sess-1",
                "LLM_MEDIUM": "anthropic/claude-sonnet-4.6",
            },
            "result": stored_result,
        }
        mock_generation_session_service.get_generation_session_status = AsyncMock(return_value=est_doc)

        fresh = MultiWorkspaceEstimationResponse(
            summary=sample_estimation_result.summary,
            workspace_estimations=sample_estimation_result.workspace_estimations,
            comparative_analysis=sample_estimation_result.comparative_analysis,
            timestamp="2026-04-09T12:00:00+00:00",
            skipped_workspaces=[
                SkippedWorkspaceP10Y(workspace_name="ws-01-2", reason="recalc test")
            ],
            aggregate_p10y_commit_coverage_pct=90.0,
        )
        mock_p10y_workflow.return_value = fresh
        mock_create_agent_logger.return_value = MagicMock()

        mock_smtp_instance = MagicMock()
        mock_smtp_class.return_value.__enter__.return_value = mock_smtp_instance
        email_notifier = EmailNotifier(mock_email_config)
        original_notifiers = notifications.notifiers[:]
        notifications.notifiers = [email_notifier]

        try:
            with patch("app.api.v1.generation_sessions.settings") as mock_est_settings:
                mock_est_settings.NOTIFY_EMAIL_USERNAME = None
                response = client.post(
                    f"/api/v1/generation-sessions/{generation_id}/resend-email",
                    data={
                        "recipient_email": "recipient@example.com",
                        "recalculate_p10y": "true",
                    },
                )
        finally:
            notifications.notifiers = original_notifiers

        assert response.status_code == 202
        data = response.json()
        assert data["email_sent"] is False
        assert "triggered" in data["message"].lower()
        mock_p10y_workflow.assert_awaited_once()
        call_kw = mock_p10y_workflow.call_args.kwargs
        assert call_kw["workspace_ids"] == workspace_ids
        assert call_kw["request"].generation_id == generation_id

        mock_generation_session_service.update_completed_generation_session_result.assert_awaited_once()
        patch_payload = mock_generation_session_service.update_completed_generation_session_result.await_args.args[1]
        assert patch_payload["aggregate_p10y_commit_coverage_pct"] == 90.0
        assert len(patch_payload["skipped_workspaces"]) == 1

        assert mock_smtp_instance.send_message.called

    def test_resend_email_recalculate_p10y_requires_workspace_ids(
        self, client, mock_generation_session_service, sample_estimation_result
    ):
        """recalculate_p10y fails fast when generation has no workspace_ids."""
        generation_id = "est-no-ws"
        stored_result = {
            "status": "Approved",
            "final_estimate_hours": 100.0,
            "summary": sample_estimation_result.summary.model_dump(),
            "workspace_estimations": [],
            "comparative_analysis": sample_estimation_result.comparative_analysis.model_dump(),
            "timestamp": sample_estimation_result.timestamp,
        }
        est_doc = {
            "user_email": "user@example.com",
            "status": "completed",
            "workspace_ids": [],
            "parameters": {"spec_path": "specs/a.md", "outputs_dir": "specflow"},
            "result": stored_result,
        }
        mock_generation_session_service.get_generation_session_status = AsyncMock(return_value=est_doc)

        with patch("app.api.v1.generation_sessions.settings") as mock_settings:
            mock_settings.NOTIFY_EMAIL_USERNAME = None
            response = client.post(
                f"/api/v1/generation-sessions/{generation_id}/resend-email",
                data={"recipient_email": "r@example.com", "recalculate_p10y": "true"},
            )

        assert response.status_code == 400
        assert "workspace_ids" in response.json()["detail"].lower()
        mock_generation_session_service.update_completed_generation_session_result.assert_not_called()

    @patch("app.api.v1.generation_sessions.create_agent_logger")
    @patch("app.api.v1.generation_sessions.multi_workspace_estimation_p10y_workflow", new_callable=AsyncMock)
    def test_resend_email_recalculate_p10y_workflow_error(
        self,
        mock_p10y_workflow,
        mock_create_agent_logger,
        client,
        mock_generation_session_service,
        sample_workspace_docs,
        sample_estimation_result,
    ):
        """P10Y recalculation failure is logged; 202 is returned, email is not sent."""
        workspace_ids, _ = sample_workspace_docs
        generation_id = "est-p10y-fail"
        stored_result = {
            "status": "Approved",
            "final_estimate_hours": 100.0,
            "summary": sample_estimation_result.summary.model_dump(),
            "workspace_estimations": [
                ws.model_dump() for ws in sample_estimation_result.workspace_estimations
            ],
            "comparative_analysis": sample_estimation_result.comparative_analysis.model_dump(),
            "timestamp": sample_estimation_result.timestamp,
        }
        est_doc = {
            "user_email": "user@example.com",
            "status": "completed",
            "workspace_ids": workspace_ids,
            "parameters": {"spec_path": "s", "outputs_dir": "g"},
            "result": stored_result,
        }
        mock_generation_session_service.get_generation_session_status = AsyncMock(return_value=est_doc)
        mock_p10y_workflow.side_effect = RuntimeError("P10Y service unavailable")
        mock_create_agent_logger.return_value = MagicMock()

        with patch("app.api.v1.generation_sessions.settings") as mock_settings:
            mock_settings.NOTIFY_EMAIL_USERNAME = None
            response = client.post(
                f"/api/v1/generation-sessions/{generation_id}/resend-email",
                data={"recipient_email": "r@example.com", "recalculate_p10y": "true"},
            )

        assert response.status_code == 202
        assert "triggered" in response.json()["message"].lower()
        mock_generation_session_service.update_completed_generation_session_result.assert_not_called()

    @patch("app.api.v1.generation_sessions.create_agent_logger")
    @patch("app.api.v1.generation_sessions.multi_workspace_estimation_p10y_workflow", new_callable=AsyncMock)
    @patch("app.core.notifications.smtplib.SMTP_SSL")
    def test_resend_email_recalculate_p10y_result_store_fails_email_still_sent(
        self,
        mock_smtp_class,
        mock_p10y_workflow,
        mock_create_agent_logger,
        client,
        mock_generation_session_service,
        sample_workspace_docs,
        sample_estimation_result,
        mock_email_config,
    ):
        """If persisting the recalculated result fails, email is still sent with fresh P10Y data."""
        workspace_ids, _ = sample_workspace_docs
        generation_id = "est-recalc-store-fail"

        est_doc = {
            "user_email": "user@example.com",
            "status": "completed",
            "workspace_ids": workspace_ids,
            "parameters": {"spec_path": "specifications", "outputs_dir": "specflow"},
            "result": {"status": "Approved", "final_estimate_hours": 100.0},
        }
        mock_generation_session_service.get_generation_session_status = AsyncMock(return_value=est_doc)

        fresh = MultiWorkspaceEstimationResponse(
            summary=sample_estimation_result.summary,
            workspace_estimations=sample_estimation_result.workspace_estimations,
            comparative_analysis=sample_estimation_result.comparative_analysis,
            timestamp="2026-04-09T12:00:00+00:00",
        )
        mock_p10y_workflow.return_value = fresh
        mock_create_agent_logger.return_value = MagicMock()
        mock_generation_session_service.update_completed_generation_session_result = AsyncMock(
            side_effect=RuntimeError("Firestore unavailable")
        )

        mock_smtp_instance = MagicMock()
        mock_smtp_class.return_value.__enter__.return_value = mock_smtp_instance
        email_notifier = EmailNotifier(mock_email_config)
        original_notifiers = notifications.notifiers[:]
        notifications.notifiers = [email_notifier]

        try:
            with patch("app.api.v1.generation_sessions.settings") as mock_settings:
                mock_settings.NOTIFY_EMAIL_USERNAME = None
                response = client.post(
                    f"/api/v1/generation-sessions/{generation_id}/resend-email",
                    data={"recipient_email": "r@example.com", "recalculate_p10y": "true"},
                )
        finally:
            notifications.notifiers = original_notifiers

        assert response.status_code == 202
        # update failed but email should still be sent with fresh data
        mock_generation_session_service.update_completed_generation_session_result.assert_awaited_once()
        assert mock_smtp_instance.send_message.called
