"""Shared generation-workflow core. ``run_generation_and_estimation`` is the post-RUNNING
sequence used by the initial run, retry, and boot-resume paths; ``rerun_generation_session``
re-fires a session from its persisted parameters (manual retry + boot recovery). Also hosts
the result helpers + the shared workflow-exception handler (here, the lower-level module, so
the API imports them — no cycle). See docs/backend/graceful-shutdown-recovery.md.
"""
import logging
from typing import Any, Dict

from app.core.config import settings
from app.core.logging import create_agent_logger
from app.core.notifications import notifications
from app.core.telemetry_context import TelemetryContext
from app.schemas.estimate import (
    MultiWorkspaceEstimationResponse,
    SimplifiedEstimationResponse,
)
from app.schemas.generation_workflow_enums import GenerationCheckpoint
from app.schemas.llm_tier import LLMTier
from app.schemas.specification import GenerateAppRequest, GenerationWorkflowRequest
from app.services.claude_code import clear_workspace_caches
from app.services.contract_validator import ContractRejection
from app.services.generation_session import GenerationSessionService
from app.services.generation_task_registry import GenerationTaskRegistry
from app.state.api_key_session_concurrency import OperationKind, SessionBeginOutcome
from app.state.exceptions import (
    GenerationCancelledError,
    InvalidGenerationSessionStateError as SMInvalidGenerationSessionStateError,
)
from app.state.transitions import TriggeredBy
from app.workflows.generate_poc import generate_app_workflow
from app.workflows.multi_workspace_estimation_p10y import (
    multi_workspace_estimation_p10y_workflow,
)

logger = logging.getLogger(__name__)


def _build_simplified_response(
    result: MultiWorkspaceEstimationResponse,
) -> SimplifiedEstimationResponse:
    """Extract simplified status/hours from a multi-workspace result."""
    if result.summary.risk_assessment:
        return SimplifiedEstimationResponse(
            status=result.summary.risk_assessment.status,
            final_estimate_hours=result.summary.risk_assessment.final_estimate,
        )
    return SimplifiedEstimationResponse(
        status="Unknown",
        final_estimate_hours=result.summary.average_hours,
    )


def _multi_workspace_result_to_store(
    result: MultiWorkspaceEstimationResponse,
    simplified: SimplifiedEstimationResponse,
) -> Dict[str, Any]:
    """Serialize multi-workspace P10Y output for Firestore ``result`` (email resend / APIs)."""
    return {
        "status": simplified.status,
        "final_estimate_hours": simplified.final_estimate_hours,
        "summary": result.summary.model_dump(),
        "workspace_estimations": [ws.model_dump() for ws in result.workspace_estimations],
        "comparative_analysis": result.comparative_analysis.model_dump(),
        "timestamp": result.timestamp,
        "skipped_workspaces": [s.model_dump() for s in result.skipped_workspaces],
        "aggregate_p10y_commit_coverage_pct": result.aggregate_p10y_commit_coverage_pct,
        "total_usd_cost": result.total_usd_cost,
    }


async def _handle_workflow_exception(
    exc: Exception,
    generation_id: str,
    generation_session_service: GenerationSessionService,
) -> None:
    """Shared error handling for every workflow entry point (initial run + retry + boot recovery).

    A ``ContractRejection`` is a pre-flight rejection, NOT a failure: roll the session
    back to PENDING via ``reject_generation_session`` (no ``failed_at``, workspaces
    released cleanly) so the user can fix their uploaded files and retry. Any other
    exception is a real failure → ``fail_generation_session``.

    A ``GenerationCancelledError`` (raised by cooperative cancellation checks) is NOT a
    failure: the session has already been transitioned to CANCELLED by the cancel endpoint,
    so this is a silent no-op — no fail(), no reject(), no notification (the user knows).

    Centralizing here is the structural fix — entry points can no longer diverge when a new exception type is introduced.
    """
    if isinstance(exc, GenerationCancelledError):
        logger.info(
            "Generation session %s cancelled by user — workflow stopped (no-op)", generation_id
        )
        return

    if isinstance(exc, ContractRejection):
        logger.info(
            "Generation session %s rejected by contract validator: %s", generation_id, exc
        )
        try:
            await generation_session_service.reject_generation_session(
                generation_id=generation_id,
                reason=str(exc),
                triggered_by=TriggeredBy.VALIDATE_CONTRACT,
            )
        except Exception as reject_exc:
            logger.error(
                "reject_contract failed for %s — failing session to release resources: %s",
                generation_id, reject_exc, exc_info=True,
            )
            try:
                await generation_session_service.fail_generation_session(
                    generation_id,
                    f"contract_reject_cleanup_failed: {reject_exc}; original: {exc}",
                )
            except SMInvalidGenerationSessionStateError:
                pass
    else:
        logger.error(
            "Generation session workflow failed for %s: %s", generation_id, exc, exc_info=True
        )
        try:
            await generation_session_service.fail_generation_session(generation_id, str(exc))
        except SMInvalidGenerationSessionStateError:
            pass  # already FAILED by the orchestrator — no-op


async def run_generation_and_estimation(
    *,
    generation_id: str,
    generation_session_service: GenerationSessionService,
    agent_logger,
    workspace_ids: list,
    spec_path: str,
    outputs_dir: str,
    src_dir: str,
    session_id,
    llm_overrides: dict,
    recipient_email: str | None = None,
    enabled_mcps: list | None = None,
    notify_spec_path: str | None = None,
) -> None:
    """Core, post-RUNNING workflow shared by the initial run and the retry/resume paths.

    Runs app generation (+ archive_outputs) → P10Y → checkpoint → notify → complete →
    clear caches. The caller owns the session→RUNNING transition, task-slot/registry
    scaffolding, and the ``_handle_workflow_exception`` wrapper; this function never catches.

    ``enabled_mcps`` is passed to ``generate_app_workflow`` only when provided (initial run);
    ``notify_spec_path`` overrides the spec path shown in the completion email (defaults to
    ``spec_path``).
    """
    app_request = GenerateAppRequest(
        spec_path=spec_path,
        outputs_dir=outputs_dir,
        src_dir=src_dir,
        session_id=session_id,
        generation_id=generation_id,
    )
    gen_kwargs = {
        "generation_session_service": generation_session_service,
        "workspace_ids": workspace_ids,
        "llm_overrides": llm_overrides,
    }
    if enabled_mcps is not None:
        gen_kwargs["enabled_mcps"] = enabled_mcps
    app_result = await generate_app_workflow(app_request, settings, agent_logger, **gen_kwargs)
    logger.info("App generation completed for %s: %s", generation_id, app_result)

    workflow_request = GenerationWorkflowRequest(
        spec_path=spec_path,
        outputs_dir=outputs_dir,
        session_id=session_id,
        generation_id=generation_id,
    )
    result = await multi_workspace_estimation_p10y_workflow(
        request=workflow_request,
        settings=settings,
        logger=agent_logger,
        workspace_ids=workspace_ids,
        db_adapter=generation_session_service.db_adapter,
        llm_overrides=llm_overrides,
    )

    await generation_session_service.update_checkpoint(
        generation_id,
        checkpoint=GenerationCheckpoint.ESTIMATION_DONE,
    )

    simplified = _build_simplified_response(result)
    result_to_store = _multi_workspace_result_to_store(result, simplified)

    if recipient_email:
        try:
            notifications.notify_generation_session_complete(
                generation_id=generation_id,
                workspace_ids=workspace_ids,
                result=result,
                spec_path=notify_spec_path or spec_path,
                recipient_email=recipient_email,
                db=generation_session_service.db,
            )
        except Exception as notify_err:
            logger.error(
                "Notification failed for generation session %s (non-fatal): %s",
                generation_id, notify_err, exc_info=True,
            )

    await generation_session_service.complete_generation_session(
        generation_id, result=result_to_store
    )
    await clear_workspace_caches(workspace_ids)
    logger.info("Generation session %s completed successfully", generation_id)


async def rerun_generation_session(
    generation_id: str,
    *,
    generation_session_service: GenerationSessionService,
    task_registry: GenerationTaskRegistry,
    user_email: str | None = None,
) -> None:
    """Re-fire a generation session from its persisted parameters.

    Allocates (reuses) workspaces, runs app generation + P10Y, notifies, and completes —
    resuming from the saved checkpoint via the inner orchestrators. The caller is
    responsible for having already transitioned the session out of FAILED (the HTTP
    retry route and the boot-recovery job both call ``reset_for_retry`` first).

    Registers the session in ``active_generation_sessions`` (so the TUI shows the
    resumed run, exactly as the initial-run path does) and wraps the workflow in
    ``task_slot`` so the slot is always released. All failures route through the
    single shared handler.
    """
    agent_logger = create_agent_logger(
        workspace_name="retry-workflow",
        generation_id=generation_id,
    )
    session = generation_session_service.api_key_sessions

    session_doc = await generation_session_service.get_generation_session_status(generation_id)
    parameters = session_doc.get("parameters", {})
    normalized_spec_path = parameters.get("spec_path")
    normalized_outputs_dir = parameters.get("outputs_dir")
    normalized_src_dir = parameters.get("src_dir", "src")
    session_id_val = parameters.get("session_id")
    recipient_email = parameters.get("notification_email")
    llm_overrides = {
        tier.value: parameters[tier.value]
        for tier in LLMTier
        if tier.value in parameters and parameters[tier.value]
    }

    try:
        task_registry.register_current_task(generation_id)
        TelemetryContext.set_user_context(
            user_email=user_email,
            generation_id=generation_id,
        )
        TelemetryContext.merge_generation_id(generation_id)

        if not normalized_spec_path or not normalized_outputs_dir:
            raise ValueError(
                f"Generation session {generation_id} missing required parameters: "
                f"spec_path={normalized_spec_path}, outputs_dir={normalized_outputs_dir}"
            )

        # Re-register the session in active_generation_sessions so the TUI shows the
        # resumed run. The initial run does this in the HTTP endpoint via try_begin;
        # the rerun path (manual retry + boot recovery) has no request api_key, so
        # resolve it from the generation. Best-effort: a full/missing slot must never
        # block resuming sacred code, so we never raise here.
        begin = await session.try_begin_for_generation(
            generation_id=generation_id,
            operation=OperationKind.GENERATION,
        )
        if begin is not None and begin.outcome == SessionBeginOutcome.ALREADY_HELD:
            try:
                await session.refresh_lease(
                    generation_id=generation_id,
                    operation=OperationKind.GENERATION,
                )
            except Exception:
                # Lease refresh is housekeeping for TUI TTL — must not fail a resume.
                logger.warning(
                    "rerun_generation_session: lease refresh failed for %s — continuing",
                    generation_id,
                    exc_info=True,
                )

        async with session.task_slot(generation_id=generation_id):
            # PENDING → INITIALIZING → RUNNING + workspace allocation (reuses existing ws).
            workspace_ids = await generation_session_service.start_generation_session(generation_id)
            logger.info("Allocated workspaces %s for rerun %s", workspace_ids, generation_id)

            await run_generation_and_estimation(
                generation_id=generation_id,
                generation_session_service=generation_session_service,
                agent_logger=agent_logger,
                workspace_ids=workspace_ids,
                spec_path=normalized_spec_path,
                outputs_dir=normalized_outputs_dir,
                src_dir=normalized_src_dir,
                session_id=session_id_val,
                llm_overrides=llm_overrides,
                recipient_email=recipient_email,
            )

    except Exception as e:
        # Single exit path — ContractRejection → reject (PENDING), else → fail (FAILED).
        # GenerationCancelledError is handled as a silent no-op there.
        await _handle_workflow_exception(e, generation_id, generation_session_service)
    finally:
        task_registry.deregister_task(generation_id)
        TelemetryContext.set_agent_query_totals_handler(None)
