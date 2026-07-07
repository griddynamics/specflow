"""
Multi-workspace P10Y estimation workflow.

This workflow runs P10Y estimation across all configured workspaces,
aggregates results, and generates comparative analysis.
"""

from dataclasses import replace
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional

from app.core.config import Settings
from app.core.telemetry_context import TelemetryContext
from app.services.langfuse import tracer
from app.prompts.agents_claude_code import estimation_report_agent_template
from app.schemas.agent import AgentResult
from app.schemas.estimate import (
    ComparativeAnalysis,
    EstimationSummary,
    MultiWorkspaceEstimationResponse,
    RiskAssessment,
    SkippedWorkspaceP10Y,
    WorkspaceEstimation,
)
from app.schemas.workflow_usage_metrics import (
    WORKFLOW_USAGE_METRICS_FIELD,
    aggregate_model_usage_by_workspace,
)
from app.schemas.workspace_model_usage_store import (
    SESSION_TOTAL_USD_COST_FIELD,
    WORKSPACE_MODEL_USAGE_SUBCOLLECTION,
    WORKSPACE_TOTAL_USD_COST_FIELD,
)
from app.schemas.llm_tier import (
    WORKFLOW_TIER_MAP,
    WorkflowName,
    assign_model_to_workspace,
    parse_model_list,
    resolve_model,
)
from app.schemas.specification import GenerationWorkflowRequest
from app.schemas.telemetry_workflow import TelemetryWorkflowLabel
from app.schemas.workspace import WorkspaceSettings
from app.core.artifact_files import MULTI_WORKSPACE_REPORT_HTML_FILE, MULTI_WORKSPACE_REPORT_MD_FILE
from app.core.notifications import render_generation_session_report_html
from app.services.artifact_store import ArtifactStore
from app.services.claude_code import agent_query
from app.services.p10y.estimation_report_generator import format_multi_workspace_report
from app.services.p10y.multi_workspace_estimation import (
    calculate_estimation_statistics,
    estimate_single_workspace,
    generate_component_comparison,
    generate_insights,
    identify_high_variance_components,
)
from app.services.p10y.p10y_api_client import P10YInternalAPIClient
from app.services.p10y.p10y_lib import (
    fetch_and_filter_commit_stats,
    load_code_generation_metadata,
    trigger_and_poll_p10y_metrics,
)
from app.services.p10y.risk_model import RiskModel
from app.services.parallel_executor import (
    ParallelGenerationResult,
    execute_generation_parallel,
)
from app.services.skip_mode_mock import (
    create_mock_workspace_estimation,
    is_skip_mode_enabled,
)
from app.services.workflow_stats import workflow_metrics
from app.state.workspace_models import load_workspace_model_overrides, resolve_workspace_model
from app.services.workspace_manager import WorkspaceManager
from app.state.db_adapter import COL_GENERATION_SESSIONS, StateMachineDBAdapter


async def _process_single_workspace(
    workspace: WorkspaceSettings,
    request: GenerationWorkflowRequest,
    client: P10YInternalAPIClient,
    settings: Settings,
    logger: logging.Logger,
) -> WorkspaceEstimation | None:
    """
    Process estimation for a single workspace.
    
    Args:
        workspace: Workspace configuration
        request: Estimation request
        client: P10Y API client
        settings: Application settings
        logger: Logger instance
    
    Returns:
        WorkspaceEstimation or None if processing fails
    """
    logger.info(f"Processing estimation for workspace: {workspace.name}")

    # SKIP_MODE: bypass all P10Y API calls and return mock estimation
    if is_skip_mode_enabled():
        return create_mock_workspace_estimation(workspace, logger)

    # Skip workspace if P10Y repository ID is not configured
    if not workspace.p10y_repository_id:
        logger.warning(
            f"Workspace {workspace.name} has no P10Y repository ID configured, skipping"
        )
        return None
    
    try:
        # Load commit metadata from git (SKIP_* subjects excluded); no JSON sidecar file.
        # Git flush + artifact protection run only in the generation workflow (STEEL XI).
        code_generation_metadata = await load_code_generation_metadata(
            str(workspace.full_workspace_path), logger
        )
        
        if not code_generation_metadata or not code_generation_metadata.commits:
            logger.warning(
                f"No commits found in git history for workspace {workspace.name}, skipping "
                f"(repo {workspace.full_workspace_path})"
            )
            return None
        
        # Full SHAs from local git only — P10Y may list historical commits for the same repo id
        allowed_commit_shas = [
            c.sha for c in code_generation_metadata.commits if c.sha is not None
        ]
        count_of_commits = len(allowed_commit_shas)
        
        logger.info(
            f"Workspace {workspace.name} has {count_of_commits} commits"
        )
        
        # Trigger P10Y metrics and poll for completion
        await trigger_and_poll_p10y_metrics(
            client=client,
            repository_id=workspace.p10y_repository_id,
            organisation_id=settings.P10Y_ORGANISATION_ID,
            workspace_name=workspace.name,
            logger=logger,
        )
        
        # Fetch and filter commit stats
        filtered_commit_stats_data = await fetch_and_filter_commit_stats(
            client=client,
            repository_id=workspace.p10y_repository_id,
            organisation_id=settings.P10Y_ORGANISATION_ID,
            allowed_commit_shas=allowed_commit_shas,
            workspace_name=workspace.name,
            logger=logger,
        )
        
        # Run estimation for this workspace (pass metadata so estimate_single_workspace
        # uses the same snapshot that was used to build the P10Y allowlist)
        workspace_estimation = await estimate_single_workspace(
            workspace=workspace,
            filtered_commit_stats_data=filtered_commit_stats_data,
            code_generation_metadata=code_generation_metadata,
            logger=logger,
            multiplier=2.0,  # Standard productivity multiplier
        )
        
        if workspace_estimation:
            logger.info(
                f"Workspace {workspace.name} estimation: "
                f"{workspace_estimation.total_hours:.1f} hours "
                f"({workspace_estimation.commits_count} commits)"
            )
        
        return workspace_estimation
        
    except Exception as e:
        logger.error(
            f"Failed to process workspace {workspace.name}: {e}",
            exc_info=True
        )
        return None


def _aggregate_p10y_commit_coverage_pct(
    workspace_estimations: List[WorkspaceEstimation],
    logger: logging.Logger,
) -> Optional[float]:
    """Percentage of eligible commits that had P10Y scores (successful workspaces only)."""
    if not workspace_estimations:
        return None
    eligible = 0
    scored = 0
    for ws in workspace_estimations:
        if ws.p10y_scored_commits is not None:
            eligible += ws.commits_count
            scored += ws.p10y_scored_commits
        else:
            logger.warning(
                "workspace_estimation missing p10y_scored_commits (commits_count=%d); "
                "excluding from coverage calculation",
                ws.commits_count,
            )
    if eligible <= 0:
        return None
    return (scored / eligible) * 100.0


def _skipped_workspaces_from_parallel(
    parallel_results: List[ParallelGenerationResult],
) -> List[SkippedWorkspaceP10Y]:
    out: List[SkippedWorkspaceP10Y] = []
    for pr in parallel_results:
        if pr.success:
            continue
        reason = (pr.error or pr.warning or "P10Y estimation unavailable").strip()
        out.append(SkippedWorkspaceP10Y(workspace_name=pr.workspace_name, reason=reason))
    return out


def _build_comparative_analysis(
    workspace_estimations: List[WorkspaceEstimation],
    logger: logging.Logger,
) -> tuple[EstimationSummary, ComparativeAnalysis]:
    """
    Build statistical summary and comparative analysis.
    
    Args:
        workspace_estimations: List of workspace estimation results
        logger: Logger instance
    
    Returns:
        Tuple of (EstimationSummary, ComparativeAnalysis)
    """
    if not workspace_estimations:
        summary = EstimationSummary(
            average_hours=0.0,
            std_deviation=0.0,
            min_hours=0.0,
            max_hours=0.0,
            coefficient_of_variation=0.0,
            variance_assessment="high",
        )
        comparative_analysis = ComparativeAnalysis(
            component_comparison={},
            high_variance_components=[],
            insights=[
                "No workspace produced hour estimates from P10Y in this run; "
                "check skipped workspaces and P10Y service availability.",
            ],
        )
        return summary, comparative_analysis

    # Calculate statistics
    summary = calculate_estimation_statistics(workspace_estimations)
    
    logger.info(
        f"Estimation summary: "
        f"avg={summary.average_hours:.1f}h, "
        f"std={summary.std_deviation:.1f}h, "
        f"cv={summary.coefficient_of_variation*100:.1f}%, "
        f"variance={summary.variance_assessment}"
    )
    
    # Generate component comparison
    component_comparison = generate_component_comparison(workspace_estimations)
    
    # Identify high variance components
    high_variance_components = identify_high_variance_components(component_comparison)
    
    if high_variance_components:
        logger.info(
            f"High variance components: {', '.join(high_variance_components)}"
        )
    
    # Generate insights
    insights = generate_insights(
        workspace_estimations, summary, high_variance_components
    )
    
    # Build comparative analysis
    comparative_analysis = ComparativeAnalysis(
        component_comparison=component_comparison,
        high_variance_components=high_variance_components,
        insights=insights,
    )
    
    return summary, comparative_analysis


def _write_structured_report(
    report_content: str,
    report_path: str,
    logger: logging.Logger,
) -> None:
    """Write the structured markdown report to disk, logging a warning on any filesystem error.

    The report is observability output, not a safety invariant.  Losing it must
    not kill an estimation after all generation work is done.
    """
    try:
        os.makedirs(os.path.dirname(report_path), exist_ok=True)
        with open(report_path, "w") as f:
            f.write(report_content)
        logger.info(f"Structured report saved to: {report_path}")
    except OSError:
        logger.warning(
            "Failed to write structured report to %s — continuing without it",
            report_path, exc_info=True,
        )


async def _generate_ai_report(
    workspace_estimations: List[WorkspaceEstimation],
    summary: EstimationSummary,
    comparative_analysis: ComparativeAnalysis,
    outputs_dir: str,
    settings: Settings,
    logger: logging.Logger,
    llm_overrides: Optional[Dict[str, str]] = None,
    generation_id: Optional[str] = None,
) -> str:
    """
    Generate AI-powered comprehensive estimation report.
    
    Args:
        workspace_estimations: List of workspace estimation results
        summary: Statistical summary
        comparative_analysis: Comparative analysis
        outputs_dir: Output directory for the report
        settings: Application settings
        logger: Logger instance
    
    Returns:
        Report summary text
    """
    if not workspace_estimations:
        logger.info("Skipping AI estimation report — no workspace results to analyze")
        return "skipped_no_workspace_results"

    # Format workspace results
    workspace_summaries = []
    for ws_est in workspace_estimations:
        workspace_summaries.append(
            f"### {ws_est.workspace_name}\n"
            f"- Total Hours: {ws_est.total_hours:.1f}\n"
            f"- Commits: {ws_est.commits_count}\n"
            f"- Components: {len(ws_est.component_breakdown)}\n"
            f"- New Work: {ws_est.estimation_metrics.new_work:.1f}\n"
            f"- Refactor: {ws_est.estimation_metrics.refactor:.1f}\n"
            f"- Quality Score: {ws_est.estimation_metrics.quality_score:.2f}\n"
        )
    
    # Format component comparison
    component_comparison_text = []
    for comp_name, comp_comp in sorted(comparative_analysis.component_comparison.items()):
        component_comparison_text.append(
            f"**{comp_name}**: avg={comp_comp.average:.1f}h, "
            f"std={comp_comp.std_deviation:.1f}h, "
            f"variance={comp_comp.variance_percentage:.1f}%"
        )
    
    # Construct full output path using primary workspace (first in list)
    # Report generation agent runs in the context of the primary workspace
    # For testing or edge cases where no workspaces are provided, require explicit WORKSPACE_DIR
    if workspace_estimations:
        primary_workspace_path = workspace_estimations[0].workspace_path
    elif settings.WORKSPACE_DIR:
        primary_workspace_path = settings.WORKSPACE_DIR
    else:
        raise RuntimeError(
            "_generate_ai_report: No workspace path available. "
            "Provide workspace_summaries or set WORKSPACE_PATH environment variable."
        )
    
    full_outputs_dir = f"{primary_workspace_path}/{outputs_dir}"
    
    # Single-workspace workflows always use first model from list  
    final_model = resolve_model(
        WORKFLOW_TIER_MAP[WorkflowName.ESTIMATION_P10Y_MULTI_WORKSPACE],
        settings,
        llm_overrides,
        return_first=True,
    )

    # Create workspace manager and get provider using primary workspace
    workspace_manager = WorkspaceManager(settings, logger)
    workspace_settings = workspace_manager.create_workspace_settings(
        workspace_path=primary_workspace_path,
        provider=settings.DEFAULT_PROVIDER,
        model=final_model,  # Use first model from list
    )
    provider_instance = workspace_manager.get_provider(workspace_settings)

    # Set workflow context for telemetry
    TelemetryContext.set_workflow(TelemetryWorkflowLabel.plain(WorkflowName.ESTIMATION_P10Y_MULTI_WORKSPACE))
    TelemetryContext.set_workspace_name(workspace_settings.name or "")
    if generation_id:
        TelemetryContext.merge_generation_id(generation_id)

    async with tracer.start_workflow_step_trace("estimation_report"):
        result: AgentResult = await agent_query(
            system_prompt=estimation_report_agent_template(
                summary=summary,
                workspace_summaries=workspace_summaries,
                component_comparison_text=component_comparison_text,
                comparative_analysis=comparative_analysis,
                full_outputs_dir=full_outputs_dir,
                model=final_model,
            ),
            workspace_path=primary_workspace_path,
            logger=logger,
            model=final_model,  # Use first model from list
            provider=provider_instance,
        )


    return result.result


@workflow_metrics("multi_workspace_estimation_p10y")
async def multi_workspace_estimation_p10y_workflow(
    request: GenerationWorkflowRequest,
    settings: Settings,
    logger: logging.Logger,
    workspace_ids: Optional[List[str]] = None,
    db_adapter: Optional[StateMachineDBAdapter] = None,
    llm_overrides: Optional[Dict[str, str]] = None,
) -> MultiWorkspaceEstimationResponse:
    """
    Run P10Y estimation across allocated workspaces and generate comparative analysis.
    
    Uses workspace pool model - workspaces are allocated from pool and cloned to /workspaces/{workspace_id}.
    
    Args:
        request: Estimation request with spec_path and outputs_dir
        settings: Application settings
        logger: Logger instance
        workspace_ids: List of workspace IDs allocated from pool (e.g., ["ws-01-1", "ws-01-2", "ws-01-3"])
        db_adapter: Optional state-machine DB adapter (same instance as GenerationSessionService) for
            workspace docs (P10Y repo IDs) and ArtifactStore wiring
        llm_overrides: Optional per-request model overrides
    Returns:
        MultiWorkspaceEstimationResponse with aggregated results and analysis
    """
    logger.info(
        f"Starting multi-workspace P10Y estimation - "
        f"outputs_dir: {request.outputs_dir}"
    )
    
    # Validate workspace_ids are provided
    if not workspace_ids or len(workspace_ids) == 0:
        raise RuntimeError("No workspace_ids provided. Workspaces must be allocated from pool first.")
    
    # Create workspace settings from allocated workspace_ids
    # Workspace paths are constructed as WORKSPACE_BASE_PATH/{workspace_id}
    workspace_base_path = Path(settings.WORKSPACE_BASE_PATH)
    workspaces = []

    models = parse_model_list(
        resolve_model(
            WORKFLOW_TIER_MAP[WorkflowName.GENERATE_APP],
            settings,
            llm_overrides,
            return_first=False,
        )
    )

    # Read per-workspace model overrides once from the generation session (not workspace docs).
    _session_model_overrides: Dict[str, str] = await load_workspace_model_overrides(
        request.generation_id, db_adapter, logger
    )

    for i, workspace_id in enumerate(workspace_ids):
        workspace_path = workspace_base_path / workspace_id

        # Try to get P10Y repository ID from workspace doc
        p10y_repository_id = None
        if db_adapter:
            try:
                ws_doc = await db_adapter.get_workspace(workspace_id)
                if ws_doc:
                    p10y_repository_id = ws_doc.get("p10y_repository_id")
            except Exception as e:
                logger.warning(f"Failed to get workspace document for {workspace_id}: {e}")

        workspace_model = resolve_workspace_model(
            workspace_id,
            assign_model_to_workspace(i, models),
            _session_model_overrides,
            logger,
        )

        workspace = WorkspaceSettings(
            name=workspace_id,
            workspace_path=str(workspace_path),
            provider=settings.DEFAULT_PROVIDER,
            model=workspace_model,
            p10y_repository_id=p10y_repository_id,
        )
        workspaces.append(workspace)
        logger.info(
            f"  {i+1}. {workspace_id} ({workspace.provider}/{workspace.model}) -> {workspace_path} "
            f"[P10Y repo: {workspace.p10y_repository_id}]"
        )
    
    # P10Y API client setup (shared across workspaces, but with different repository IDs)
    client = P10YInternalAPIClient(
        base_url=settings.P10Y_BASE_URL,
        api_key=settings.P10Y_API_KEY
    )
    
    # Create workspace manager (needed for parallel executor)
    workspace_manager = WorkspaceManager(settings, logger)
    
    # Process workspaces in parallel (data collection only, no AI agents)
    logger.info("Executing estimations in parallel...")
    parallel_results = await execute_generation_parallel(
        workspaces=workspaces,
        workspace_manager=workspace_manager,
        generation_fn=_process_single_workspace,
        generation_args={
            "request": request,
            "client": client,
            "settings": settings,
            "logger": logger,
        },
        logger=logger,
    )
    
    # Extract successful estimations
    workspace_estimations: List[WorkspaceEstimation] = []
    for result in parallel_results:
        if result.success and result.estimation:
            workspace_estimations.append(result.estimation)

    skipped_workspaces = _skipped_workspaces_from_parallel(parallel_results)
    aggregate_cov = _aggregate_p10y_commit_coverage_pct(workspace_estimations, logger)

    session_llm_cost_total: Optional[float] = None

    # Attach per-workspace codegen usage from Firestore nested metrics (after generate_app)
    if request.generation_id and db_adapter and workspace_estimations:
        try:
            est_doc = await db_adapter.get_generation_session(request.generation_id)
        except Exception as e:
            logger.warning(
                "Could not load estimation %s for workspace usage metrics: %s",
                request.generation_id,
                e,
            )
            est_doc = None
        if est_doc:
            by_ws = aggregate_model_usage_by_workspace(
                est_doc.get(WORKFLOW_USAGE_METRICS_FIELD)
            )
            for ws_est in workspace_estimations:
                ws_agg = by_ws.get(ws_est.workspace_name)
                if ws_agg and not ws_agg.is_empty():
                    model_name = (ws_est.model_usage.model_name if ws_est.model_usage else "") or ""
                    ws_est.model_usage = replace(ws_agg, model_name=model_name)

            raw_st = est_doc.get(SESSION_TOTAL_USD_COST_FIELD)
            session_llm_cost_total = float(raw_st) if raw_st is not None else None
            try:
                rows = await db_adapter.list_subcollection(
                    COL_GENERATION_SESSIONS,
                    request.generation_id,
                    WORKSPACE_MODEL_USAGE_SUBCOLLECTION,
                )
                by_cost = {
                    str(r["_id"]): float(r.get(WORKSPACE_TOTAL_USD_COST_FIELD) or 0.0)
                    for r in rows
                    if r.get("_id")
                }
                for ws_est in workspace_estimations:
                    c = by_cost.get(ws_est.workspace_name)
                    if c is not None:
                        ws_est.total_usd_cost = c
                if session_llm_cost_total is None and by_cost:
                    session_llm_cost_total = sum(by_cost.values())
            except Exception as cost_exc:
                logger.warning(
                    "Could not load workspace LLM USD costs for generation %s: %s",
                    request.generation_id,
                    cost_exc,
                )

    if not workspace_estimations:
        logger.warning(
            "No workspace produced P10Y-backed estimates (%s workspace(s) skipped). "
            "Completing with empty aggregates — estimation run will not fail solely on P10Y.",
            len(skipped_workspaces),
        )
    else:
        logger.info(
            f"Successfully estimated {len(workspace_estimations)} out of "
            f"{len(workspaces)} workspaces"
        )
    
    # Build comparative analysis
    summary, comparative_analysis = _build_comparative_analysis(
        workspace_estimations, logger
    )
    
    # Calculate risk assessment using RiskModel (degenerate inputs must not abort reports)
    logger.info("Calculating risk assessment...")
    risk_model = RiskModel()
    estimates = [ws.total_hours for ws in workspace_estimations]
    try:
        risk_result = risk_model.evaluate(estimates)
    except ValueError as exc:
        logger.warning(
            "Risk model skipped (degenerate workspace hour totals): %s",
            exc,
        )
        mu = float(summary.average_hours) if workspace_estimations else 0.0
        risk_result = {
            "status": "Unknown",
            "instability_ratio": 0.0,
            "rejection_threshold": 1.0,
            "base_component": 0.0,
            "var_component": 0.0,
            "size_component": 0.0,
            "total_buffer_pct": 0.0,
            "final_estimate": mu,
        }

    # Create RiskAssessment object
    risk_assessment = RiskAssessment(
        status=risk_result["status"],
        instability_ratio=risk_result["instability_ratio"],
        rejection_threshold=risk_result["rejection_threshold"],
        base_component=risk_result["base_component"],
        var_component=risk_result["var_component"],
        size_component=risk_result["size_component"],
        total_buffer_pct=risk_result["total_buffer_pct"],
        final_estimate=risk_result["final_estimate"],
    )
    
    # Add risk assessment to summary
    summary.risk_assessment = risk_assessment
    
    logger.info(
        f"Risk assessment: status={risk_assessment.status}, "
        f"buffer={risk_assessment.total_buffer_pct*100:.1f}%, "
        f"final_estimate={risk_assessment.final_estimate:.1f}h"
    )

    # Build response now — every field is resolved, and both the markdown and
    # HTML reports below render from it.
    response = MultiWorkspaceEstimationResponse(
        summary=summary,
        workspace_estimations=workspace_estimations,
        comparative_analysis=comparative_analysis,
        timestamp=datetime.now(timezone.utc).isoformat(),
        skipped_workspaces=skipped_workspaces,
        aggregate_p10y_commit_coverage_pct=aggregate_cov,
        total_usd_cost=session_llm_cost_total,
    )

    # Generate structured markdown report using report generator
    logger.info("Generating structured markdown report...")
    structured_report = format_multi_workspace_report(
        workspace_estimations=workspace_estimations,
        summary=summary,
        comparative_analysis=comparative_analysis,
        skipped_workspaces=skipped_workspaces,
        aggregate_p10y_commit_coverage_pct=aggregate_cov,
    )

    # Save structured report to file in primary workspace
    primary_workspace_path = workspaces[0].workspace_path
    full_outputs_dir = f"{primary_workspace_path}/{request.outputs_dir}"
    structured_report_path = f"{full_outputs_dir}/{MULTI_WORKSPACE_REPORT_MD_FILE}"
    _write_structured_report(structured_report, structured_report_path, logger)

    # Save the same report as HTML, regardless of whether email/Slack notifiers
    # are configured — local quickstart users have neither, so this is the only
    # place the HTML report is produced. Non-essential observability output: a
    # failure here must never abort an estimation whose work is already done.
    # The renderer only reads (a plain db.get("workspaces", id)); db_adapter is
    # the async state-machine wrapper, so pass its read-only view — without a
    # database handle the Variants section silently drops out.
    logger.info("Generating HTML report...")
    try:
        html_content, _plain_content = render_generation_session_report_html(
            generation_id=request.generation_id,
            workspace_ids=workspace_ids,
            result=response,
            spec_path=request.spec_path,
            db=db_adapter.read_only_db if db_adapter else None,
        )
        html_report_path = f"{full_outputs_dir}/{MULTI_WORKSPACE_REPORT_HTML_FILE}"
        _write_structured_report(html_content, html_report_path, logger)
    except Exception as e:
        logger.warning(
            "Failed to render/write HTML report — continuing without it: %s",
            e, exc_info=True,
        )

    # Generate AI-powered comprehensive report (optional, additional analysis)
    logger.info("Generating AI-powered comprehensive report...")
    report_summary = await _generate_ai_report(
        workspace_estimations=workspace_estimations,
        summary=summary,
        comparative_analysis=comparative_analysis,
        outputs_dir=request.outputs_dir,
        settings=settings,
        logger=logger,
        llm_overrides=llm_overrides,
        generation_id=request.generation_id,
    )
    logger.info(f"AI report generated: {report_summary}")
    
    # Workspace filesystem snapshots are archived before P10Y (see archive_outputs pipeline).
    # Copy the combined comparison report under report/ after reports exist.
    generation_id = request.generation_id
    
    if generation_id:
        artifact_store = ArtifactStore(db_adapter)
        # Archive combined report (written to primary workspace / outputs_dir)
        combined_report_dir = Path(workspaces[0].workspace_path) / request.outputs_dir
        try:
            await artifact_store.archive_report(
                generation_id=generation_id,
                report_source_path=combined_report_dir,
            )
        except Exception as e:
            logger.error(
                f"Failed to archive combined report for estimation {generation_id}: {e}",
                exc_info=True,
            )

    logger.info("Multi-workspace estimation completed successfully")

    return response
