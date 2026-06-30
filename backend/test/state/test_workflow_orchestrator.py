"""
Tests for WorkflowOrchestrator.
Covers all test cases from implementation-plan.md section 1.10.
"""
import pytest
from unittest.mock import AsyncMock

from app.services.contract_validator import ContractRejection, RejectionCode
from app.state.workflow_orchestrator import WorkflowOrchestrator
from app.state.generation_session_state_machine import GenerationSessionStateMachine
from app.schemas.generation_workflow_enums import GenerationStatus, GenerationCheckpoint


def make_orchestrator(db, esm=None, archive_svc=None):
    if esm is None:
        esm = AsyncMock(spec=GenerationSessionStateMachine)
        esm.advance_checkpoint = AsyncMock(return_value={})
        esm.fail = AsyncMock(return_value={})
        esm.complete = AsyncMock(return_value={})
    return WorkflowOrchestrator(db, esm, workspace_archive_service=archive_svc)


def make_step(name="step"):
    """Returns an AsyncMock callable for use as a step implementation."""
    return AsyncMock(return_value=None)


class TestFreshRun:
    @pytest.mark.asyncio
    async def test_all_steps_execute_in_order_on_fresh_run(self, db):
        """No checkpoint: all provided steps execute in order."""
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.RUNNING,
            "checkpoint": None,
        })
        esm = AsyncMock(spec=GenerationSessionStateMachine)
        esm.advance_checkpoint = AsyncMock(return_value={})
        esm.complete = AsyncMock(return_value={})
        orch = WorkflowOrchestrator(db, esm)

        step_order = []
        async def make_recording_step(name):
            async def step():
                step_order.append(name)
            return step

        steps = {
            "validate_contract": await make_recording_step("validate_contract"),
            "kb_init": await make_recording_step("kb_init"),
            "generation": await make_recording_step("generation"),
            "deploy_and_e2e": await make_recording_step("deploy_and_e2e"),
            "archive_outputs": await make_recording_step("archive_outputs"),
            "estimation": await make_recording_step("estimation"),
        }

        await orch.run("est-1", steps, triggered_by="test")

        # Order matches GENERATION_WORKFLOW_STEPS exactly.
        assert step_order == [
            "validate_contract", "kb_init", "generation",
            "deploy_and_e2e", "archive_outputs", "estimation",
        ]
        assert esm.advance_checkpoint.call_count == 6
        assert esm.complete.call_count == 1


class TestSkipLogic:
    @pytest.mark.asyncio
    async def test_skips_completed_steps(self, db):
        """With checkpoint past CONTRACT_VALIDATED, validate_contract is skipped; kb_init onwards run."""
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.RUNNING,
            "checkpoint": GenerationCheckpoint.CONTRACT_VALIDATED,
        })
        esm = AsyncMock(spec=GenerationSessionStateMachine)
        esm.advance_checkpoint = AsyncMock(return_value={})
        esm.complete = AsyncMock(return_value={})
        orch = WorkflowOrchestrator(db, esm)

        executed = []
        async def make_step(name):
            async def step():
                executed.append(name)
            return step

        steps = {
            "validate_contract": await make_step("validate_contract"),
            "kb_init": await make_step("kb_init"),
            "generation": await make_step("generation"),
            "deploy_and_e2e": await make_step("deploy_and_e2e"),
            "archive_outputs": await make_step("archive_outputs"),
            "estimation": await make_step("estimation"),
        }

        await orch.run("est-1", steps, triggered_by="test")

        assert "validate_contract" not in executed
        assert "kb_init" in executed
        assert "generation" in executed
        assert "deploy_and_e2e" in executed
        assert "archive_outputs" in executed
        assert "estimation" in executed

    @pytest.mark.asyncio
    async def test_resume_after_generation_done_runs_archive_and_estimation(self, db):
        """With GENERATION_DONE checkpoint, generation is skipped but archive/estimation run."""
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.RUNNING,
            "checkpoint": GenerationCheckpoint.GENERATION_DONE,
        })
        esm = AsyncMock(spec=GenerationSessionStateMachine)
        esm.advance_checkpoint = AsyncMock(return_value={})
        esm.complete = AsyncMock(return_value={})
        orch = WorkflowOrchestrator(db, esm)

        executed = []
        async def make_step(name):
            async def step():
                executed.append(name)
            return step

        steps = {
            "validate_contract": await make_step("validate_contract"),
            "kb_init": await make_step("kb_init"),
            "generation": await make_step("generation"),
            "deploy_and_e2e": await make_step("deploy_and_e2e"),
            "archive_outputs": await make_step("archive_outputs"),
            "estimation": await make_step("estimation"),
        }

        await orch.run("est-1", steps, triggered_by="test")

        # Everything up to and including generation is skipped (already done).
        assert "validate_contract" not in executed
        assert "kb_init" not in executed
        assert "generation" not in executed
        # Archive + estimation must run.
        assert "deploy_and_e2e" in executed
        assert "archive_outputs" in executed
        assert "estimation" in executed
        assert "archive_outputs" in executed

    @pytest.mark.asyncio
    async def test_all_steps_complete_skipped_calls_complete(self, db):
        """With checkpoint=ESTIMATION_DONE, all steps skipped, complete() called."""
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.RUNNING,
            "checkpoint": GenerationCheckpoint.ESTIMATION_DONE,
        })
        esm = AsyncMock(spec=GenerationSessionStateMachine)
        esm.advance_checkpoint = AsyncMock(return_value={})
        esm.complete = AsyncMock(return_value={})
        orch = WorkflowOrchestrator(db, esm)

        executed = []
        async def make_step(name):
            async def step():
                executed.append(name)
            return step

        steps = {
            "validate_contract": await make_step("validate_contract"),
            "generation": await make_step("generation"),
            "archive_outputs": await make_step("archive_outputs"),
        }

        await orch.run("est-1", steps, triggered_by="test")

        assert executed == []
        esm.complete.assert_called_once()


class TestFailure:
    @pytest.mark.asyncio
    async def test_step_failure_calls_fail_with_step_name(self, db):
        """Step failure: fail() called with correct step in triggered_by."""
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.RUNNING,
            "checkpoint": None,
        })
        esm = AsyncMock(spec=GenerationSessionStateMachine)
        esm.advance_checkpoint = AsyncMock(return_value={})
        esm.fail = AsyncMock(return_value={})
        esm.complete = AsyncMock(return_value={})
        orch = WorkflowOrchestrator(db, esm)

        async def failing_sync_specs():
            raise RuntimeError("network error")

        with pytest.raises(RuntimeError):
            await orch.run("est-1", {"validate_contract": failing_sync_specs}, triggered_by="test")

        esm.fail.assert_called_once()
        call_kwargs = esm.fail.call_args
        assert "validate_contract" in call_kwargs.kwargs.get("triggered_by", "")

    @pytest.mark.asyncio
    async def test_step_failure_reraises_exception(self, db):
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.RUNNING,
            "checkpoint": None,
        })
        esm = AsyncMock(spec=GenerationSessionStateMachine)
        esm.fail = AsyncMock(return_value={})
        orch = WorkflowOrchestrator(db, esm)

        async def failing_step():
            raise ValueError("specific error")

        with pytest.raises(ValueError, match="specific error"):
            await orch.run("est-1", {"validate_contract": failing_step}, triggered_by="test")

    @pytest.mark.asyncio
    async def test_step_failure_tolerates_inner_orchestrator_already_failed(self, db):
        """
        Bug regression (2026-02-24): when a step implementation internally runs its
        own WorkflowOrchestrator (e.g. generate_poc contains its own orchestrator.run()),
        the inner orchestrator calls esm.fail() first — the generation is now FAILED.
        The outer orchestrator then catches the re-raised step exception and also
        calls fail(). Without the fix, this second fail() raised InvalidGenerationSessionStateError,
        which replaced the original error in the exception chain and caused asyncio to
        log an unhandled task exception.

        After the fix: the outer orchestrator silently ignores InvalidGenerationSessionStateError
        from fail() and re-raises the ORIGINAL step exception.
        """
        from app.state.exceptions import InvalidGenerationSessionStateError

        db.seed_generation_session("est-1", {
            "status": GenerationStatus.RUNNING,
            "checkpoint": None,
        })
        esm = AsyncMock(spec=GenerationSessionStateMachine)
        esm.advance_checkpoint = AsyncMock(return_value={})
        # Simulate inner orchestrator having already called fail(): our fail() now raises
        esm.fail = AsyncMock(
            side_effect=InvalidGenerationSessionStateError(
                generation_id="est-1",
                current_status="failed",
                attempted_transition="fail",
                allowed_from=["running", "initializing", "pending"],
            )
        )
        orch = WorkflowOrchestrator(db, esm)

        async def step_with_inner_orchestrator():
            # Inner orchestrator ran, called fail(), then re-raised its step error
            raise RuntimeError("planning agent failed: exit code 1")

        # Must propagate the ORIGINAL RuntimeError, not InvalidGenerationSessionStateError
        with pytest.raises(RuntimeError, match="planning agent failed"):
            await orch.run(
                "est-1",
                {"validate_contract": step_with_inner_orchestrator},
                triggered_by="test",
                complete_on_finish=False,
            )

    @pytest.mark.asyncio
    async def test_step_failure_still_calls_fail_on_fresh_generation(self, db):
        """
        Regression guard: when esm.fail() succeeds normally (generation was RUNNING,
        not yet FAILED), it must still be called exactly once.
        """
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.RUNNING,
            "checkpoint": None,
        })
        esm = AsyncMock(spec=GenerationSessionStateMachine)
        esm.fail = AsyncMock(return_value={})
        orch = WorkflowOrchestrator(db, esm)

        async def failing_step():
            raise RuntimeError("agent crashed")

        with pytest.raises(RuntimeError):
            await orch.run("est-1", {"validate_contract": failing_step}, triggered_by="test")

        esm.fail.assert_called_once()


class TestMissingImpl:
    @pytest.mark.asyncio
    async def test_missing_step_silently_skipped(self, db):
        """Step with no implementation is silently skipped."""
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.RUNNING,
            "checkpoint": None,
        })
        esm = AsyncMock(spec=GenerationSessionStateMachine)
        esm.advance_checkpoint = AsyncMock(return_value={})
        esm.complete = AsyncMock(return_value={})
        orch = WorkflowOrchestrator(db, esm)

        # Provide only some steps
        called = []
        async def planning_step():
            called.append("kb_init")

        # No error expected — sync_specs not provided is fine
        await orch.run("est-1", {"kb_init": planning_step}, triggered_by="test")
        assert "kb_init" in called


class TestCompleteCalledOnce:
    @pytest.mark.asyncio
    async def test_complete_called_only_after_all_steps_succeed(self, db):
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.RUNNING,
            "checkpoint": None,
        })
        esm = AsyncMock(spec=GenerationSessionStateMachine)
        esm.advance_checkpoint = AsyncMock(return_value={})
        esm.complete = AsyncMock(return_value={})
        orch = WorkflowOrchestrator(db, esm)

        executed = []
        async def step():
            executed.append(True)

        await orch.run("est-1", {"kb_init": step}, triggered_by="test")

        assert len(executed) == 1
        esm.complete.assert_called_once()

    @pytest.mark.asyncio
    async def test_advance_checkpoint_called_once_per_completed_step(self, db):
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.RUNNING,
            "checkpoint": None,
        })
        esm = AsyncMock(spec=GenerationSessionStateMachine)
        esm.advance_checkpoint = AsyncMock(return_value={})
        esm.complete = AsyncMock(return_value={})
        orch = WorkflowOrchestrator(db, esm)

        async def noop():
            pass

        await orch.run("est-1", {
            "validate_contract": noop,
            "kb_init": noop,
            "generation": noop,
        }, triggered_by="test")

        assert esm.advance_checkpoint.call_count == 3


class TestDeployAndE2E:
    @pytest.mark.asyncio
    async def test_deploy_and_e2e_omitted_silently_skipped(self, db):
        """When deploy_and_e2e has no impl (LOCAL_ONLY path), it is silently skipped."""
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.RUNNING,
            "checkpoint": GenerationCheckpoint.GENERATION_DONE,
        })
        esm = AsyncMock(spec=GenerationSessionStateMachine)
        esm.advance_checkpoint = AsyncMock(return_value={})
        esm.complete = AsyncMock(return_value={})
        orch = WorkflowOrchestrator(db, esm)

        executed = []
        async def estimation_step():
            executed.append("estimation")

        # No deploy_and_e2e in dict — LOCAL_ONLY path
        await orch.run("est-1", {"estimation": estimation_step}, triggered_by="test")

        assert "estimation" in executed
        # advance_checkpoint called once for estimation only
        assert esm.advance_checkpoint.call_count == 1

    @pytest.mark.asyncio
    async def test_retry_from_generation_done_resumes_at_deploy_and_e2e(self, db):
        """
        Retry safety: generation saved at GENERATION_DONE (code on workspace).
        On retry, generation is skipped, deploy_and_e2e runs, generated code is safe.
        """
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.RUNNING,
            "checkpoint": GenerationCheckpoint.GENERATION_DONE,
        })
        esm = AsyncMock(spec=GenerationSessionStateMachine)
        esm.advance_checkpoint = AsyncMock(return_value={})
        esm.complete = AsyncMock(return_value={})
        orch = WorkflowOrchestrator(db, esm)

        executed = []
        async def make_step(name):
            async def step():
                executed.append(name)
            return step

        steps = {
            "generation": await make_step("generation"),
            "deploy_and_e2e": await make_step("deploy_and_e2e"),
            "estimation": await make_step("estimation"),
            "archive_outputs": await make_step("archive_outputs"),
        }

        await orch.run("est-1", steps, triggered_by="test")

        assert "generation" not in executed       # skipped: GENERATION_DONE already reached
        assert "deploy_and_e2e" in executed       # runs: code is safe, workspace ALLOCATED
        assert executed.index("archive_outputs") < executed.index("estimation")
        assert "estimation" in executed
        assert "archive_outputs" in executed

    @pytest.mark.asyncio
    async def test_retry_from_deploy_and_e2e_done_skips_deploy(self, db):
        """Retry from DEPLOY_AND_E2E_DONE skips both generation and deploy_and_e2e."""
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.RUNNING,
            "checkpoint": GenerationCheckpoint.DEPLOY_AND_E2E_DONE,
        })
        esm = AsyncMock(spec=GenerationSessionStateMachine)
        esm.advance_checkpoint = AsyncMock(return_value={})
        esm.complete = AsyncMock(return_value={})
        orch = WorkflowOrchestrator(db, esm)

        executed = []
        async def make_step(name):
            async def step():
                executed.append(name)
            return step

        steps = {
            "generation": await make_step("generation"),
            "deploy_and_e2e": await make_step("deploy_and_e2e"),
            "estimation": await make_step("estimation"),
            "archive_outputs": await make_step("archive_outputs"),
        }

        await orch.run("est-1", steps, triggered_by="test")

        assert "generation" not in executed
        assert "deploy_and_e2e" not in executed   # already done
        assert executed.index("archive_outputs") < executed.index("estimation")
        assert "estimation" in executed
        assert "archive_outputs" in executed


# ---------------------------------------------------------------------------
# Phase 6: deploy_and_e2e step failure triggers esm.fail()
# ---------------------------------------------------------------------------

class TestDeployAndE2EFailure:
    @pytest.mark.asyncio
    async def test_deploy_and_e2e_failure_triggers_fail(self, db):
        """
        Phase 6: When the deploy_and_e2e step raises, the orchestrator must call
        esm.fail(), not esm.complete(). Workspaces stay ALLOCATED per Commandment II.
        """
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.RUNNING,
            "checkpoint": GenerationCheckpoint.GENERATION_DONE,
        })
        esm = AsyncMock(spec=GenerationSessionStateMachine)
        esm.advance_checkpoint = AsyncMock(return_value={})
        esm.fail = AsyncMock(return_value={})
        esm.complete = AsyncMock(return_value={})
        orch = WorkflowOrchestrator(db, esm)

        async def failing_deploy_and_e2e():
            raise RuntimeError("deploy container failed to start")

        steps = {
            "deploy_and_e2e": failing_deploy_and_e2e,
            "generation": make_step("generation"),
            "archive_outputs": make_step("archive_outputs"),
        }

        with pytest.raises(RuntimeError, match="deploy container failed"):
            await orch.run("est-1", steps, triggered_by="test")

        esm.fail.assert_called_once()
        esm.complete.assert_not_called()


class TestContractRejection:
    @pytest.mark.asyncio
    async def test_contract_rejection_propagates_without_fail_or_checkpoint(self, db):
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.RUNNING,
            "checkpoint": GenerationCheckpoint.FILES_UPLOADED.value,
            "state_history": [],
        })
        esm = AsyncMock(spec=GenerationSessionStateMachine)
        esm.advance_checkpoint = AsyncMock(return_value={})
        esm.fail = AsyncMock(return_value={})
        orch = WorkflowOrchestrator(db, esm)

        async def reject_step():
            raise ContractRejection(
                code=RejectionCode.PLAN_MISSING,
                message="Missing plan",
            )

        with pytest.raises(ContractRejection):
            await orch.run(
                "est-1",
                {"validate_contract": reject_step},
                triggered_by="test",
            )

        esm.fail.assert_not_called()
        esm.advance_checkpoint.assert_not_called()
