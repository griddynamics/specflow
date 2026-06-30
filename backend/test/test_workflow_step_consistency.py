"""
Consistency tests for workflow step registration.

These tests catch the class of bug where a new step is added to
generate_app_workflow but not registered in GENERATION_WORKFLOW_STEPS,
or where a new checkpoint is added to GENERATION_WORKFLOW_STEPS but not
added to CHECKPOINT_ORDER in the enum.

Add a new step? These tests will fail until you:
  1. Add it to GENERATION_WORKFLOW_STEPS (workflow_orchestrator.py)
  2. Add its checkpoint to CHECKPOINT_ORDER (generation_workflow_enums.py)
"""

import pytest
from unittest.mock import AsyncMock, Mock, patch

from app.schemas.generation_workflow_enums import CHECKPOINT_ORDER, GenerationStatus
from app.state.workflow_orchestrator import GENERATION_WORKFLOW_STEPS


class TestOrchestratorCheckpointCoverage:
    """Every checkpoint used by GENERATION_WORKFLOW_STEPS must be in CHECKPOINT_ORDER."""

    def test_all_step_checkpoints_in_checkpoint_order(self) -> None:
        """Scenario: a new step is added to GENERATION_WORKFLOW_STEPS with a new
        checkpoint, but CHECKPOINT_ORDER is not updated.  Without this test the
        state machine silently skips order validation for that checkpoint."""
        missing = [
            step.advances_to
            for step in GENERATION_WORKFLOW_STEPS
            if step.advances_to not in CHECKPOINT_ORDER
        ]
        assert not missing, (
            f"Checkpoints used by GENERATION_WORKFLOW_STEPS but missing from "
            f"CHECKPOINT_ORDER: {missing}. "
            f"Add them to CHECKPOINT_ORDER in generation_workflow_enums.py."
        )

    def test_checkpoint_order_matches_workflow_step_order(self) -> None:
        """Checkpoints must appear in CHECKPOINT_ORDER in the same relative order
        as their steps appear in GENERATION_WORKFLOW_STEPS (excluding legacy
        GENERATION_STARTED which has no corresponding step)."""
        step_checkpoints = [step.advances_to for step in GENERATION_WORKFLOW_STEPS]
        # Filter CHECKPOINT_ORDER to only those that correspond to actual steps
        order_filtered = [cp for cp in CHECKPOINT_ORDER if cp in step_checkpoints]
        assert order_filtered == step_checkpoints, (
            f"Checkpoint order mismatch.\n"
            f"  GENERATION_WORKFLOW_STEPS order: {step_checkpoints}\n"
            f"  CHECKPOINT_ORDER (filtered):     {order_filtered}\n"
            f"Ensure CHECKPOINT_ORDER in generation_workflow_enums.py matches "
            f"the step sequence in GENERATION_WORKFLOW_STEPS."
        )


class TestWorkflowStepRegistration:
    """Every step key in generate_app_workflow must be registered in
    GENERATION_WORKFLOW_STEPS, otherwise the orchestrator silently drops it."""

    @pytest.mark.asyncio
    @patch("app.workflows.generate_poc.build_workflow_context", new_callable=AsyncMock)
    async def test_all_generate_poc_steps_registered_in_orchestrator(
        self, mock_build_ctx: AsyncMock,
    ) -> None:
        """Scenario: a step is added to generate_app_workflow's steps dict but
        not to GENERATION_WORKFLOW_STEPS.  The orchestrator iterates
        GENERATION_WORKFLOW_STEPS and does a dict lookup — any key not in
        GENERATION_WORKFLOW_STEPS is silently ignored."""
        from test.state.conftest import FakeDB
        from app.workflows.generate_poc import generate_app_workflow

        mock_ctx = Mock()
        mock_ctx.generation_result = None
        mock_build_ctx.return_value = mock_ctx

        fake_db = FakeDB()
        fake_db.seed_generation_session("est-consistency", {
            "status": GenerationStatus.RUNNING,
            "checkpoint": None,
        })

        esm = AsyncMock()
        esm.advance_checkpoint = AsyncMock(return_value={})
        esm.fail = AsyncMock(return_value={})

        mock_generation_session_service = Mock()
        mock_generation_session_service._db = fake_db
        mock_generation_session_service._esm = esm

        captured: dict = {}

        async def capturing_run(generation_id, step_implementations, **kwargs):
            captured["step_implementations"] = step_implementations
            # Run all provided steps so the test is end-to-end
            for name, impl in step_implementations.items():
                await impl()

        with (
            patch("app.workflows.generate_poc.run_contract_validator", new_callable=AsyncMock),
            patch("app.workflows.generate_poc.run_kb_init_and_sync_workspaces", new_callable=AsyncMock),
            patch("app.workflows.generate_poc.run_generation_phases", new_callable=AsyncMock),
            patch("app.workflows.generate_poc.run_archive_outputs_step", new_callable=AsyncMock),
            patch(
                "app.workflows.generate_poc.WorkflowOrchestrator.run",
                side_effect=capturing_run,
            ),
        ):
            request = Mock()
            request.generation_id = "est-consistency"

            await generate_app_workflow(
                request=request,
                settings=Mock(),
                logger=Mock(),
                generation_session_service=mock_generation_session_service,
                workspace_ids=["ws1"],
            )

        assert captured, "WorkflowOrchestrator.run was never called — orchestrator path not taken"

        registered_names = {step.name for step in GENERATION_WORKFLOW_STEPS}
        provided_names = set(captured["step_implementations"].keys())

        unregistered = provided_names - registered_names
        assert not unregistered, (
            f"Steps passed to orchestrator but missing from GENERATION_WORKFLOW_STEPS: "
            f"{unregistered}. "
            f"Add them to GENERATION_WORKFLOW_STEPS in workflow_orchestrator.py."
        )
