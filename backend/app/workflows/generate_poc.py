import logging
from typing import Any, Dict, FrozenSet, List, Optional

from app.core.config import Settings
from app.schemas.generation_workflow_enums import WorkflowStepName
from app.schemas.specification import GenerateAppRequest
from app.services.generation_session import GenerationSessionService
from app.services.workflow_stats import workflow_metrics
from app.services.workflow_steps import (
    build_workflow_context,
    run_archive_outputs_step,
    run_contract_validator,
    run_deploy_and_e2e,
    run_generation_phases,
    run_kb_init_and_sync_workspaces,
)
from app.state import WorkflowOrchestrator


@workflow_metrics("generate_app")
async def generate_app_workflow(
    request: GenerateAppRequest,
    settings: Settings,
    logger: logging.Logger,
    generation_session_service: GenerationSessionService,
    workspace_ids: Optional[List[str]] = None,
    llm_overrides: Optional[Dict[str, str]] = None,
    enabled_mcps: Optional[FrozenSet[str]] = None,
):
    """Entry point for app generation, bound to a generation session.

    Steps (in order):
      1. validate_contract — fuzzy-match user-uploaded analysis + plan markdown to
         canonical paths, then convert markdown to JSON. Hard-fails the run with
         ContractRejection if required files are missing or unparseable.
      2. kb_init — Rosetta KB initialization on the primary workspace, then sync specs,
         outputs, and src to all allocated workspaces.
      3. generation — parallel codegen across all allocated workspaces.
      4. deploy_and_e2e — only when integration_readiness == INTEGRATION_TESTS_READY.
      5. archive_outputs — Steel Commandment XI: persist before P10Y.

    There is no longer a backend planning agent. Users produce
    `<outputs_dir>/planning/IMPLEMENTATION_PLAN.md` locally via `run_planning`
    before calling `run_generation`; the validator converts it to JSON.
    """
    ctx = await build_workflow_context(
        request, settings, logger, workspace_ids, generation_session_service,
        llm_overrides=llm_overrides,
        enabled_mcps=enabled_mcps,
    )
    # Bug 1 (PR255): DEPLOY_AND_E2E is ALWAYS registered. The build-time
    # ctx.integration_readiness here is a pre-validation snapshot and may be stale (e.g.
    # a misplaced completeness file the validator later normalizes). run_deploy_and_e2e
    # no-ops at its own entry unless run_contract_validator has confirmed
    # INTEGRATION_TESTS_READY from the normalized file — the validator is the single
    # source of truth. The orchestrator iterates a fixed step list and cannot register
    # steps late, so the guard lives in the step, not here.
    steps: Dict[str, Any] = {
        WorkflowStepName.VALIDATE_CONTRACT.value: lambda: run_contract_validator(ctx),
        WorkflowStepName.KB_INIT.value: lambda: run_kb_init_and_sync_workspaces(ctx),
        WorkflowStepName.GENERATION.value: lambda: run_generation_phases(ctx),
        WorkflowStepName.DEPLOY_AND_E2E.value: lambda: run_deploy_and_e2e(ctx),
    }
    # Steel XI: output protection must run before P10Y (advances OUTPUTS_ARCHIVED).
    steps[WorkflowStepName.ARCHIVE_OUTPUTS.value] = lambda: run_archive_outputs_step(ctx)

    orchestrator = WorkflowOrchestrator(
        db=generation_session_service._db_adapter,
        generation_session_state_machine=generation_session_service._esm,
    )
    await orchestrator.run(
        generation_id=request.generation_id,
        step_implementations=steps,
        triggered_by="generate_app",
        complete_on_finish=False,
    )

    return ctx.generation_result
