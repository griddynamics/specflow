"""Tests for run_contract_validator plan-conversion rejection codes."""
from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.core.artifact_files import IMPLEMENTATION_PLAN_FILE, SPEC_COMPLETENESS_FILE
from app.core.artifact_subdirs import ANALYSIS_SUBDIR, PLANNING_SUBDIR
from app.schemas.planning import PhaseInfo, PlanningResult, PlanType
from app.schemas.specification import SpecReadiness
from app.services.contract_validator import ContractRejection, RejectionCode
from app.services.workflow_steps import WorkflowContext, run_contract_validator

_VALID_ANALYSIS = "# Part F\n\nLOCAL_ONLY\n"
_MINIMAL_PLAN = "# Plan\n\n## Phase 1\nDo something.\n- task 1\n"


def _setup_workspace(tmp_path: Path, outputs_dir: str = "docs") -> Path:
    root = tmp_path
    (root / outputs_dir / ANALYSIS_SUBDIR).mkdir(parents=True)
    (root / outputs_dir / PLANNING_SUBDIR).mkdir(parents=True)
    (root / outputs_dir / ANALYSIS_SUBDIR / SPEC_COMPLETENESS_FILE).write_text(_VALID_ANALYSIS)
    (root / outputs_dir / PLANNING_SUBDIR / IMPLEMENTATION_PLAN_FILE).write_text(_MINIMAL_PLAN)
    return root


def _make_ctx(workspace_root: Path, outputs_dir: str = "docs") -> WorkflowContext:
    primary = Mock()
    primary.workspace_path = workspace_root
    request = Mock()
    request.outputs_dir = outputs_dir
    request.generation_id = "est-test"
    ctx = WorkflowContext(
        request=request,
        settings=Mock(),
        logger=logging.getLogger("test_run_contract_validator"),
        workspace_ids=["ws-1"],
        workspaces=[primary],
        workspace_manager=Mock(),
        primary_workspace=primary,
        generation_session_service=AsyncMock(),
    )
    ctx.generation_session_service.update_planning_data = AsyncMock(return_value=True)
    ctx.generation_session_service.save_e2e_plan = AsyncMock(return_value={})
    return ctx


class TestRunContractValidatorStructuralRejections:
    """Structural plan problems are caught deterministically by the preflight
    (``validate_preconverted_plan_contract``) inside ``run_contract_validator`` and
    surface as a user ``ContractRejection`` — before the markdown→JSON conversion agent
    runs. This is the reuse-path mirror of the pre-allocation preflight in
    ``TestGenerationContractPreflight``."""

    @pytest.mark.asyncio
    async def test_plan_with_no_phase_heading_rejected(self, tmp_path):
        root = _setup_workspace(tmp_path)
        (root / "docs" / PLANNING_SUBDIR / IMPLEMENTATION_PLAN_FILE).write_text(
            "# Plan\n\nNo phases yet.\n"
        )
        ctx = _make_ctx(root)

        with pytest.raises(ContractRejection) as exc_info:
            await run_contract_validator(ctx)

        assert exc_info.value.code == RejectionCode.PLAN_NO_PHASES

    @pytest.mark.asyncio
    async def test_e2e_plan_with_no_round_heading_rejected(self, tmp_path):
        root = _setup_workspace(tmp_path)
        (root / "docs" / ANALYSIS_SUBDIR / SPEC_COMPLETENESS_FILE).write_text(
            "# Part F\n\nINTEGRATION_TESTS_READY\n"
        )
        (root / "docs" / PLANNING_SUBDIR / "e2e-test-plan.md").write_text("# E2E\n")
        ctx = _make_ctx(root)

        with pytest.raises(ContractRejection) as exc_info:
            await run_contract_validator(ctx)

        assert exc_info.value.code == RejectionCode.E2E_PLAN_UNPARSEABLE


class TestRunContractValidatorPostPreflightConversion:
    """Once the deterministic preflight passes, a plan-conversion agent failure is a
    workflow/internal failure (``RuntimeError`` → ``fail()`` → retry), NOT a user
    ``ContractRejection``. We trust specflow-produced plans: a structurally valid plan
    that still fails conversion is an infrastructure failure (transient agent/IO), not a
    user-fixable contract error, so retry_generation resumes from checkpoint and re-runs
    conversion rather than bouncing the user back to fix files."""

    @pytest.mark.asyncio
    async def test_conversion_runtime_error_propagates(self, tmp_path):
        root = _setup_workspace(tmp_path)
        ctx = _make_ctx(root)

        with patch(
            "app.services.workflow_steps._invoke_plan_conversion_agent",
            new_callable=AsyncMock,
            side_effect=RuntimeError("agent did not write JSON"),
        ):
            with pytest.raises(RuntimeError, match="agent did not write JSON"):
                await run_contract_validator(ctx)

    @pytest.mark.asyncio
    async def test_zero_phases_after_preflight_is_runtime_error(self, tmp_path):
        root = _setup_workspace(tmp_path)
        ctx = _make_ctx(root)
        empty_plan = PlanningResult(
            phase_count=0,
            phases=[],
            plan_file_path="docs/planning/IMPLEMENTATION_PLAN.md",
        )

        with patch(
            "app.services.workflow_steps._invoke_plan_conversion_agent",
            new_callable=AsyncMock,
            return_value={PlanType.IMPLEMENTATION: empty_plan},
        ):
            with pytest.raises(RuntimeError, match="no implementation phases after preflight"):
                await run_contract_validator(ctx)


class TestRunContractValidatorIntegrationReadiness:
    """PR255 Bug 1: ctx.integration_readiness is re-read from the NORMALIZED file,
    overriding the stale pre-validation snapshot from build_workflow_context."""

    @pytest.mark.asyncio
    async def test_readiness_updated_from_normalized_file(self, tmp_path):
        root = _setup_workspace(tmp_path)
        (root / "docs" / ANALYSIS_SUBDIR / SPEC_COMPLETENESS_FILE).write_text(
            "# Part F\n\nINTEGRATION_TESTS_READY\n"
        )
        # Structurally valid e2e plan so the preflight passes and the test exercises
        # readiness propagation (not E2E_PLAN_UNPARSEABLE).
        (root / "docs" / PLANNING_SUBDIR / "e2e-test-plan.md").write_text(
            "# E2E\n\n## Round 1\n\nVerify the happy path.\n"
        )
        ctx = _make_ctx(root)
        # Stale snapshot: build_workflow_context saw the misplaced file as LOCAL_ONLY.
        ctx.integration_readiness = SpecReadiness.LOCAL_ONLY

        impl_plan = PlanningResult(
            phase_count=1,
            phases=[PhaseInfo(number=1, name="P1", description="d", estimated_commits=1)],
            plan_file_path="docs/planning/IMPLEMENTATION_PLAN.md",
        )
        e2e_plan = PlanningResult(
            phase_count=1,
            phases=[PhaseInfo(number=1, name="R1", description="d", estimated_commits=1)],
            plan_file_path="docs/planning/e2e-test-plan.md",
        )

        async def conversion_side_effect(ctx_arg, plans_to_convert):
            result = {PlanType.IMPLEMENTATION: impl_plan}
            if PlanType.E2E in plans_to_convert:
                result[PlanType.E2E] = e2e_plan
            return result

        with patch(
            "app.services.workflow_steps._invoke_plan_conversion_agent",
            new_callable=AsyncMock,
            side_effect=conversion_side_effect,
        ):
            await run_contract_validator(ctx)

        assert ctx.integration_readiness == SpecReadiness.INTEGRATION_TESTS_READY


class TestRunContractValidatorServicePrecondition:
    """PR255 Bug 6: with a generation_id set, a None service is a wiring bug, not a
    silent skip. The validator must persist plan/MCP data, so it fails fast."""

    @pytest.mark.asyncio
    async def test_missing_service_with_generation_id_raises(self, tmp_path):
        root = _setup_workspace(tmp_path)
        ctx = _make_ctx(root)
        ctx.generation_session_service = None

        with pytest.raises(RuntimeError, match="generation_session_service is None"):
            await run_contract_validator(ctx)
