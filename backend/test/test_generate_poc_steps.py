"""Unit tests for generate_poc workflow step ordering."""

import pytest
from unittest.mock import AsyncMock, Mock, patch

from app.core.config import Settings
from app.schemas.generation_workflow_enums import GenerationStatus
from app.schemas.specification import GenerateAppRequest
from app.state.generation_session_state_machine import GenerationSessionStateMachine


class TestGeneratePocStepOrdering:
    """Tests for generate_app_workflow step ordering."""

    def test_kb_init_and_sync_is_imported(self) -> None:
        """Scenario: workspace prep must run via generate_poc (KB init + file sync)."""
        from app.workflows.generate_poc import run_kb_init_and_sync_workspaces

        assert run_kb_init_and_sync_workspaces is not None
        assert callable(run_kb_init_and_sync_workspaces)

    @pytest.mark.asyncio
    @patch("app.workflows.generate_poc.build_workflow_context", new_callable=AsyncMock)
    async def test_kb_init_runs_via_orchestrator_path(
        self, mock_build_ctx: AsyncMock,
    ) -> None:
        """Regression: kb_init must execute when generation_session_service + generation_id
        are provided (orchestrator path).  Uses FakeDB + mocked ESM so no
        Firestore emulator is required."""
        from test.state.conftest import FakeDB
        from app.workflows.generate_poc import generate_app_workflow

        call_order: list[str] = []

        mock_ctx = Mock()
        mock_ctx.generation_result = None
        mock_build_ctx.return_value = mock_ctx

        # In-memory DB seeded with a fresh generation (no checkpoint yet)
        fake_db = FakeDB()
        fake_db.seed_generation_session("est-123", {
            "status": GenerationStatus.RUNNING,
            "checkpoint": None,
        })

        esm = AsyncMock(spec=GenerationSessionStateMachine)
        esm.advance_checkpoint = AsyncMock(return_value={})
        esm.fail = AsyncMock(return_value={})

        mock_generation_session_service = Mock()
        mock_generation_session_service._db_adapter = fake_db
        mock_generation_session_service._esm = esm
        mock_generation_session_service.get_workspace_repo_url = AsyncMock(return_value=None)

        request = Mock(spec=GenerateAppRequest)
        request.generation_id = "est-123"
        settings = Mock(spec=Settings)
        logger = Mock()

        with (
            patch(
                "app.workflows.generate_poc.run_contract_validator",
                new_callable=AsyncMock,
                side_effect=lambda ctx: call_order.append("validate_contract"),
            ),
            patch(
                "app.workflows.generate_poc.run_kb_init_and_sync_workspaces",
                new_callable=AsyncMock,
                side_effect=lambda ctx: call_order.append("kb_init"),
            ),
            patch(
                "app.workflows.generate_poc.run_generation_phases",
                new_callable=AsyncMock,
                side_effect=lambda ctx: call_order.append("generation"),
            ),
            patch(
                "app.workflows.generate_poc.run_archive_outputs_step",
                new_callable=AsyncMock,
                side_effect=lambda ctx: call_order.append("archive_outputs"),
            ),
        ):
            await generate_app_workflow(
                request=request,
                settings=settings,
                logger=logger,
                generation_session_service=mock_generation_session_service,
                workspace_ids=["ws1"],
            )

        assert "validate_contract" in call_order, (
            "validate_contract was not executed via orchestrator path — "
            "check that 'validate_contract' is registered in GENERATION_WORKFLOW_STEPS"
        )
        # validate_contract runs first, then kb_init.
        assert call_order[0] == "validate_contract"
        assert call_order[1] == "kb_init"
