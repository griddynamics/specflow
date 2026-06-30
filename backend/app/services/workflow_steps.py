"""
Named service functions called by WorkflowOrchestrator.
Each function does exactly one thing and is independently testable.

WorkflowContext holds all mutable state shared between step functions.
"""
import asyncio
from dataclasses import dataclass, field
import hashlib
import json
import logging
from pathlib import Path
import time
from typing import Any, Dict, Final, FrozenSet, List, Optional

from app.core.artifact_subdirs import ANALYSIS_SUBDIR, PLANNING_SUBDIR
from app.core.config import (
    Settings,
    WORKSPACE_DEFAULT_BRANCH,
    WORKSPACE_DEPLOY_WORKFLOW,
)
from app.core.logging import create_agent_logger
from app.core.mcp_config import (
    enabled_mcps_to_parameter_string,
    parse_mcp_servers_enabled_string,
    resolve_enabled_mcps,
)
from app.core.mcp_selection import McpSelector
from app.core.notifications import (
    GenerationSessionNotificationKind,
    build_coding_complete_pre_deploy_response,
    notifications,
)
from app.core.telemetry_context import TelemetryContext
from app.schemas.telemetry_workflow import TelemetryWorkflowLabel
from app.services.langfuse import tracer
from app.prompts.agents_claude_code import (
    E2E_PHASES_FILE,
    E2E_TEST_PLAN_FILE,
    IMPLEMENTATION_PLAN_FILE,
    PLANNING_PHASES_FILE,
    SPEC_COMPLETENESS_FILE,
)
from app.prompts.knowledge_base_agent import kb_init_agent_template
from app.prompts.plan_conversion_agent import plan_conversion_agent_template
from app.schemas.deploy_context import DeployGithubContext
from app.schemas.llm_tier import (
    WORKFLOW_TIER_MAP,
    WorkflowName,
    assign_model_to_workspace,
    parse_and_validate_models,
    resolve_model,
)
from app.schemas.planning import PhaseInfo, PlanType, PlanningResult
from app.schemas.specification import GenerateAppRequest
from app.schemas.specification import SpecReadiness
from app.schemas.workflow_usage_metrics import WORKFLOW_USAGE_METRICS_FIELD
from app.schemas.workspace_model_usage_store import (
    SESSION_TOTAL_USD_COST_FIELD,
    WORKSPACE_MODEL_USAGE_SUBCOLLECTION,
    WORKSPACE_TOTAL_USD_COST_FIELD,
)
from app.schemas.workspace import WorkspaceSettings
from app.services.artifact_store import ArtifactStore
from app.services.claude_code import (
    agent_query,
    execute_all_phases,
    get_common_allowed_tools,
)
from app.services.generation_session_output_protection import protect_workspace_after_generation
from app.services.parallel_executor import ParallelAgentExecutor
from app.services.planning_parser import (
    construct_planning_result,
    parse_applicable_agent_mcps,
    parse_planning_output,
)
from app.services.skip_mode_mock import (
    is_skip_mode_enabled,
    persist_skip_mode_mock_agent_query_totals,
    write_mock_planning_phases_json,
)
from app.services.telemetry import telemetry
from app.services.workflow_stats import (
    WorkflowExecutionStats,
    get_workflow_stats,
    set_workflow_stats,
)
from app.services.workspace_manager import WorkspaceManager
from app.state.db_adapter import COL_GENERATION_SESSIONS
from app.state.workspace_models import load_workspace_model_overrides, resolve_workspace_model
from app.services.contract_validator import (
    normalize_contract_files,
    parse_integration_readiness_from_file,
    validate_preconverted_plan_contract,
)
from app.services.mcp_prune import prune_enabled_mcps_keyword_only


def _planning_result_from_dict(planning_dict: dict) -> PlanningResult:
    """Reconstruct a PlanningResult from a stored planning_data dict.

    Intentionally lenient (uses .get defaults) because the source is our own Firestore
    document and must tolerate schema drift across versions. Agent-JSON parsing uses
    the strict `construct_planning_result` path instead.
    """
    return PlanningResult(
        phase_count=planning_dict.get(
            "phase_count", len(planning_dict.get("phases", []))
        ),
        phases=[
            PhaseInfo(
                number=p.get("number"),
                name=p.get("name"),
                description=p.get("description", ""),
                estimated_commits=p.get("estimated_commits", 0),
                applicable_agent_mcps=parse_applicable_agent_mcps(p),
            )
            for p in planning_dict.get("phases", [])
        ],
        plan_file_path=planning_dict.get("plan_file_path", ""),
        plan_markdown_checksum=planning_dict.get("plan_markdown_checksum"),
    )


def _compute_markdown_checksum(content: str) -> str:
    """Compute SHA256 checksum of markdown content."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _read_markdown_file(workspace_path: Path, outputs_dir: str, filename: str) -> Optional[str]:
    """Read a markdown file from the workspace, returning None when absent."""
    md_path = workspace_path / outputs_dir / PLANNING_SUBDIR / filename
    try:
        return md_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def _resolve_workspace_id(
    workspace: WorkspaceSettings,
    workspaces: List[WorkspaceSettings],
    workspace_ids: List[str],
) -> Optional[str]:
    """Return the allocation ID for a workspace by identity or name+path match."""
    for idx, ws in enumerate(workspaces):
        if ws == workspace or (
            ws.name == workspace.name and ws.workspace_path == workspace.workspace_path
        ):
            return workspace_ids[idx] if idx < len(workspace_ids) else None
    return None


@dataclass
class WorkflowContext:
    """Shared mutable state passed between workflow step functions."""
    request: Any  # GenerateAppRequest
    settings: Any  # Settings
    logger: Any   # logging.Logger
    workspace_ids: List[str]
    workspaces: List[WorkspaceSettings]
    workspace_manager: WorkspaceManager
    primary_workspace: Optional[WorkspaceSettings] = None
    extra_workspaces: Optional[List[WorkspaceSettings]] = None
    planning_data: Optional[PlanningResult] = None
    workspace_phases_data: dict = field(default_factory=dict)
    # Deploy loop resume state — loaded from workspace_phases_deployment in Firestore.
    # None means the deploy loop has not started yet on this generation.
    workspace_phases_deployment_data: dict = field(default_factory=dict)
    e2e_planning_data: Optional[PlanningResult] = None
    generation_result: Optional[Any] = None
    generation_session_service: Optional[Any] = None
    llm_overrides: Dict[str, str] = field(default_factory=dict)  # LLMTier.value → model
    # NOT_READY is the safe default: uninitialized state is explicit.
    # build_workflow_context() always sets this via parse_integration_readiness_from_file().
    # LOCAL_ONLY as a default would silently mask cases where that call was skipped or failed.
    integration_readiness: SpecReadiness = SpecReadiness.NOT_READY
    # MCP servers enabled for this generation (subset of SUPPORTED_MCPS).
    enabled_mcps: FrozenSet[str] = field(default_factory=frozenset)


def _is_first_deploy_loop_entry(ctx: WorkflowContext) -> bool:
    """True on the first entry to deploy/QA; False when resuming after partial progress."""
    if not ctx.workspace_phases_deployment_data:
        return True
    for wid in ctx.workspace_ids:
        info = ctx.workspace_phases_deployment_data.get(wid) or {}
        if info.get("last_completed_phase", 0) > 0:
            return False
    return True


async def _notify_coding_complete_before_deploy_if_applicable(ctx: WorkflowContext) -> None:
    """
    Send the same rich email/Slack as the final completion notification, with milestone copy,
    before the deploy & QA phase loop (INTEGRATION_TESTS_READY only).
    """
    if not ctx.generation_session_service:
        return
    if not _is_first_deploy_loop_entry(ctx):
        return
    eid = ctx.request.generation_id
    try:
        est_doc = await ctx.generation_session_service.get_generation_session_status(eid)
    except Exception as exc:
        ctx.logger.warning(
            "Could not load generation for pre-deploy notification (non-fatal): %s",
            exc,
        )
        return
    if not isinstance(est_doc, dict):
        return
    raw_params = est_doc.get("parameters")
    params: Dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
    recipient = params.get("notification_email") or est_doc.get("user_email")
    if not recipient:
        return

    ws_costs: Dict[str, float] = {}
    try:
        rows = await ctx.generation_session_service.db_adapter.list_subcollection(
            COL_GENERATION_SESSIONS,
            eid,
            WORKSPACE_MODEL_USAGE_SUBCOLLECTION,
        )
        for row in rows:
            wid = row.get("_id")
            if wid:
                ws_costs[str(wid)] = float(row.get(WORKSPACE_TOTAL_USD_COST_FIELD) or 0.0)
    except Exception as exc:
        ctx.logger.warning(
            "Could not load workspace LLM cost subdocs for pre-deploy notify (%s): %s",
            eid,
            exc,
        )

    raw_session = est_doc.get(SESSION_TOTAL_USD_COST_FIELD)
    try:
        session_total: Optional[float] = float(raw_session) if raw_session is not None else None
    except (TypeError, ValueError):
        session_total = None
    if session_total is None and ws_costs:
        session_total = sum(ws_costs.values())

    try:
        result = build_coding_complete_pre_deploy_response(
            workspace_ids=ctx.workspace_ids,
            workflow_usage_metrics=est_doc.get(WORKFLOW_USAGE_METRICS_FIELD),
            total_usd_cost=session_total,
            workspace_llm_cost_usd=ws_costs or None,
        )
        notifications.notify_generation_session_complete(
            generation_id=eid,
            workspace_ids=ctx.workspace_ids,
            result=result,
            spec_path=ctx.request.spec_path,
            recipient_email=recipient,
            db=None,
            notification_kind=GenerationSessionNotificationKind.CODING_COMPLETE_PRE_DEPLOY,
        )
    except Exception as exc:
        ctx.logger.error(
            "Pre-deploy milestone notification failed for %s (non-fatal): %s",
            eid,
            exc,
            exc_info=True,
        )


async def build_workflow_context(
    request: GenerateAppRequest,
    settings: Settings,
    logger: logging.Logger,
    workspace_ids: Optional[List[str]],
    generation_session_service=None,
    llm_overrides: Optional[Dict[str, str]] = None,
    enabled_mcps: Optional[FrozenSet[str]] = None,
) -> WorkflowContext:
    """
    Build a WorkflowContext from call arguments.

    If generation_session_service is provided, pre-loads
    workspace_phases_data and reconstructs planning_data from Firestore
    so that subsequent steps have it even when the planning step is skipped
    by the orchestrator.
    """
    if not workspace_ids:
        raise RuntimeError("No workspace_ids provided. Workspaces must be allocated from pool first.")

    resolved_overrides = llm_overrides or {}
    
    # For multi-workspace: get full model list for round-robin assignment
    generation_tier_value = resolve_model(
        WORKFLOW_TIER_MAP[WorkflowName.GENERATE_APP],
        settings,
        resolved_overrides,
        return_first=False,  # Keep full comma-separated list
    )

    # Validate models via OpenRouter API (important for multi-model scenarios)
    # This catches typos early when assigning different models to different workspaces
    generation_models = await parse_and_validate_models(
        generation_tier_value,
        settings,
        logger,
    )

    workspace_base_path = Path(settings.WORKSPACE_BASE_PATH)

    # On crash-retry, a prior run may have persisted per-workspace model overrides on the
    # generation session (workspace_model_overrides map).  Read once before the loop.
    _db_adapter = generation_session_service.db_adapter if generation_session_service else None
    _session_model_overrides: Dict[str, str] = await load_workspace_model_overrides(
        request.generation_id, _db_adapter, logger
    )

    workspaces = []
    for idx, workspace_id in enumerate(workspace_ids):
        workspace_path = workspace_base_path / workspace_id
        workspace_model = resolve_workspace_model(
            workspace_id,
            assign_model_to_workspace(idx, generation_models),
            _session_model_overrides,
            logger,
        )
        logger.info(f"  Workspace: {workspace_id} -> {workspace_path}, model: {workspace_model}")

        workspace = WorkspaceSettings(
            name=workspace_id,
            workspace_path=str(workspace_path),
            provider=settings.DEFAULT_PROVIDER,
            model=workspace_model,  # Each workspace gets different model
        )
        workspaces.append(workspace)

    workspace_manager = WorkspaceManager(settings, logger)
    primary_workspace = workspaces[0]
    extra_workspaces = workspaces[1:] if len(workspaces) > 1 else []

    # Pre-load checkpoint data so subsequent steps have planning_data even
    # when the planning step is skipped by the orchestrator (resumption).
    workspace_phases_data: dict = {}
    planning_data: Optional[PlanningResult] = None
    workspace_phases_deployment_data: dict = {}
    e2e_planning_data: Optional[PlanningResult] = None

    resolved_enabled_mcps: FrozenSet[str]
    if enabled_mcps is not None:
        resolved_enabled_mcps = enabled_mcps
    else:
        resolved_enabled_mcps = parse_mcp_servers_enabled_string(settings.MCP_SERVERS_ENABLED)

    if generation_session_service:
        try:
            est_doc = await generation_session_service.get_generation_session_status(request.generation_id)

            # Lift retry_count into TelemetryContext so Langfuse trace metadata
            # carries the current attempt number (0 on first run, N after N retries).
            TelemetryContext.set_retry_count(int(est_doc.get("retry_count") or 0))

            if enabled_mcps is None:
                resolved_enabled_mcps = resolve_enabled_mcps(
                    form_value=None,
                    generation_session_parameters=est_doc.get("parameters"),
                    settings=settings,
                )

            # Generation phases resume state
            workspace_phases_data = est_doc.get("workspace_phases", {})
            for wid in workspace_ids:
                planning_dict = workspace_phases_data.get(wid, {}).get("planning_data")
                if planning_dict:
                    planning_data = _planning_result_from_dict(planning_dict)
                    logger.info(
                        f"Restored planning_data from checkpoint "
                        f"({planning_data.phase_count} phases)"
                    )
                    break


            # Deploy loop resume state
            workspace_phases_deployment_data = est_doc.get("workspace_phases_deployment") or {}
            for wid in workspace_ids:
                deploy_planning_dict = workspace_phases_deployment_data.get(wid, {}).get("planning_data")
                if deploy_planning_dict:
                    e2e_planning_data = _planning_result_from_dict(deploy_planning_dict)
                    logger.info(
                        f"Restored e2e_planning_data from checkpoint "
                        f"({e2e_planning_data.phase_count} deploy phases)"
                    )
                    break

        except Exception as e:
            raise RuntimeError(
                f"Failed to load checkpoint data for generation {request.generation_id}: {e}. "
                f"Cannot determine resume point — aborting to prevent restarting from phase 1 "
                f"and re-running already-completed work."
            ) from e

    # Read integration_readiness from specification_completeness.md (written by spec completeness flow).
    # The two flows are separate and communicate only via files on disk.
    completeness_file = Path(primary_workspace.get_isolated_root()) / request.outputs_dir / ANALYSIS_SUBDIR / SPEC_COMPLETENESS_FILE
    integration_readiness = parse_integration_readiness_from_file(completeness_file)
    logger.info(f"Read integration_readiness={integration_readiness.value} from {completeness_file}")

    eid_for_usage = request.generation_id
    if generation_session_service is not None:
        TelemetryContext.merge_generation_id(eid_for_usage)
        TelemetryContext.set_agent_query_totals_handler(
            generation_session_service.add_agent_query_token_usage
        )

    return WorkflowContext(
        request=request,
        settings=settings,
        logger=logger,
        workspace_ids=workspace_ids,
        workspaces=workspaces,
        workspace_manager=workspace_manager,
        primary_workspace=primary_workspace,
        extra_workspaces=extra_workspaces,
        planning_data=planning_data,
        workspace_phases_data=workspace_phases_data,
        workspace_phases_deployment_data=workspace_phases_deployment_data,
        e2e_planning_data=e2e_planning_data,
        generation_session_service=generation_session_service,
        llm_overrides=resolved_overrides,
        integration_readiness=integration_readiness,
        enabled_mcps=resolved_enabled_mcps,
    )


def _collect_rosetta_file_manifest(workspace_root: str, rosetta_dir: str) -> list[str]:
    """Walk rosetta/ directory and return a sorted list of relative file paths."""
    workspace_root_path = Path(workspace_root)
    rosetta_path = workspace_root_path / rosetta_dir
    if not rosetta_path.exists():
        return []
    files: list[str] = []
    for item in rosetta_path.rglob("*"):
        if item.is_file():
            files.append(str(item.relative_to(workspace_root_path)))
    return sorted(files)


# Root project-instructions file the provisioned-plugin KB-init agent writes (outside outputs_dir).
_ROOT_CLAUDE_MD: Final[str] = "CLAUDE.md"


def _snapshot_outputs_dir_files(workspace_root: str, outputs_dir: str) -> set[str]:
    """Relative paths of files currently under ``workspace_root/outputs_dir`` (empty if absent)."""
    root = Path(workspace_root)
    base = root / outputs_dir
    if not base.exists():
        return set()
    return {str(p.relative_to(root)) for p in base.rglob("*") if p.is_file()}


def _collect_provisioned_plugin_manifest(
    workspace_root: str, outputs_dir: str, pre_existing: set[str]
) -> list[str]:
    """KB artifacts a provisioned-plugin KB-init run actually produced.

    Unlike MCP mode (which stages output under its own rosetta/ dir), the provisioned-plugin agent
    writes ``CLAUDE.md`` at the workspace root and docs under ``outputs_dir`` — but ``outputs_dir``
    was already populated with the user's uploaded specs/plans before KB init. Report only the
    root ``CLAUDE.md`` plus files under ``outputs_dir`` that did NOT exist before the run, so the
    manifest reflects KB output rather than pre-existing uploads.
    """
    root = Path(workspace_root)
    new_under_outputs = _snapshot_outputs_dir_files(workspace_root, outputs_dir) - pre_existing
    files = set(new_under_outputs)
    if (root / _ROOT_CLAUDE_MD).is_file():
        files.add(_ROOT_CLAUDE_MD)
    return sorted(files)


def _generate_mock_rosetta_artifacts(
    workspace_root: str, rosetta_dir: str, logger: logging.Logger
) -> None:
    """Create minimal mock rosetta/ artifacts for SKIP_MODE.

    Produces the same directory structure a real KB init agent would,
    so the downstream unpack/sync flow is exercised end-to-end.
    """
    root = Path(workspace_root) / rosetta_dir
    agents_dir = root / "agents"
    docs_dir = root / "docs"
    agents_dir.mkdir(parents=True, exist_ok=True)
    docs_dir.mkdir(parents=True, exist_ok=True)

    (root / "CLAUDE.md").write_text(
        "# CLAUDE.md\n\n[SKIP_MODE] Mock project instructions.\n"
    )
    (agents_dir / "default.md").write_text(
        "---\ndescription: Default agent\n---\n\n[SKIP_MODE] Mock subagent.\n"
    )
    (docs_dir / "CONTEXT.md").write_text(
        "# Context\n\n[SKIP_MODE] Mock project context.\n"
    )
    logger.info(f"[SKIP_MODE] Created mock rosetta artifacts under {root}")


async def run_kb_init_and_sync_workspaces(ctx: WorkflowContext) -> None:
    """Run KB init on the primary workspace, then sync specs/outputs/rosetta and src to all workspaces.

    MCP upload targets the primary workspace only (``sync_to_all=False``). Extra workspaces must
    receive specs, plans, ``rosetta/``, and optional ``src/`` before parallel codegen.
    """
    await run_kb_init_agent(ctx)
    await sync_specs_to_workspaces(ctx)
    await sync_code_to_workspaces(ctx)


async def run_kb_init_agent(ctx: WorkflowContext) -> None:
    """Run KB initialization agent on primary workspace via Rosetta MCP.

    Writes output to rosetta/ folder in the primary workspace.
    The sync step will later unpack these to SDK discovery paths.
    Non-fatal: if this fails, workflow continues without KB artifacts.
    Emits a kb_init_completed PostHog event with the generated file manifest.
    """
    generation_id = getattr(ctx.request, "generation_id", "unknown")
    workspace_name = ctx.primary_workspace.workspace_path.name
    ws_root = ctx.primary_workspace.get_isolated_root()
    mcp = McpSelector(ctx.settings, ctx.enabled_mcps, ctx.logger).for_kb_init(ws_root)
    if mcp.is_empty:
        ctx.logger.info(
            "KB init skipped: Rosetta MCP disabled and no ROSETTA_PLUGIN_PATH configured"
        )
        telemetry.capture_kb_init_event(
            generation_id=generation_id,
            workspace_name=workspace_name,
            status="skipped",
            generated_files=[],
        )
        return

    rosetta_dir = ctx.settings.ROSETTA_OUTPUT_DIR
    uses_provisioned_plugin = mcp.uses_provisioned_plugin
    # Bind a manifest collector here (before the try) so both the success and except paths read it.
    # MCP mode stages output under rosetta/, so the manifest is that whole tree. Provisioned-plugin
    # mode writes docs to their FINAL locations (root CLAUDE.md + outputs_dir), where outputs_dir
    # already holds the user's uploaded specs/plans — so snapshot it first and report only what KB
    # init newly produced, otherwise the telemetry manifest is inflated with pre-existing uploads.
    if uses_provisioned_plugin:
        outputs_dir = ctx.request.outputs_dir
        outputs_pre_existing = _snapshot_outputs_dir_files(ws_root, outputs_dir)
        def collect_manifest() -> list[str]:
            return _collect_provisioned_plugin_manifest(ws_root, outputs_dir, outputs_pre_existing)
    else:
        def collect_manifest() -> list[str]:
            return _collect_rosetta_file_manifest(ws_root, rosetta_dir)

    if is_skip_mode_enabled():
        # SKIP_MODE exercises the rosetta/ staging path (mock + collect) regardless of mode; it
        # does NOT run the real provision/unpack, so the mock manifest is always read from rosetta/.
        _generate_mock_rosetta_artifacts(ws_root, rosetta_dir, ctx.logger)
        generated_files = _collect_rosetta_file_manifest(ws_root, rosetta_dir)
        ctx.logger.warning(
            f"[SKIP_MODE] KB init agent skipped — created {len(generated_files)} "
            f"mock artifacts under {rosetta_dir}/"
        )
        TelemetryContext.set_workspace_name(workspace_name)
        TelemetryContext.set_workflow(TelemetryWorkflowLabel.plain(WorkflowName.KB_INIT))
        kb_init_model = resolve_model(
            WORKFLOW_TIER_MAP[WorkflowName.KB_INIT],
            ctx.settings,
            ctx.llm_overrides,
            return_first=True,
        )
        await persist_skip_mode_mock_agent_query_totals(
            model=kb_init_model,
            workspace_path=ws_root,
            logger=ctx.logger,
            workflow=TelemetryWorkflowLabel.plain(WorkflowName.KB_INIT),
        )
        telemetry.capture_kb_init_event(
            generation_id=generation_id,
            workspace_name=workspace_name,
            status="success",
            generated_files=generated_files,
            duration_seconds=0.0,
        )
        return

    t0 = time.monotonic()
    try:
        telemetry.capture_kb_init_triggered(
            generation_id=generation_id,
            workspace_name=workspace_name,
        )
        
        kb_init_model = resolve_model(
            WORKFLOW_TIER_MAP[WorkflowName.KB_INIT],
            ctx.settings,
            ctx.llm_overrides,
            return_first=True,
        )
        kb_init_prompt = kb_init_agent_template(
            rosetta_output_dir=rosetta_dir,
            model=kb_init_model,
            use_plugin=uses_provisioned_plugin,
            outputs_dir=ctx.request.outputs_dir,
        )
        # Provisioned-plugin mode: copy the bundled Rosetta plugin into the primary workspace
        # .claude/ so the KB init agent discovers init-workspace-flow (and the rest of the
        # toolset) via setting_sources=["project"]. No-op in MCP mode. Extra workspaces are
        # provisioned later in prepare_parallel_workspaces.
        ctx.workspace_manager.provision_rosetta_plugin(ctx.primary_workspace)
        provider_instance = ctx.workspace_manager.get_provider(ctx.primary_workspace)
        # Required for the TUI live-message stream: the StreamPublisher keys events
        # on (generation_id, workspace_name) from TelemetryContext. Without this,
        # build_stream_publisher_from_context() returns None and KB-init emits no
        # live events (codegen phases set this in phase_agent_fn; KB init must too).
        TelemetryContext.set_workspace_name(workspace_name)
        TelemetryContext.set_workflow(TelemetryWorkflowLabel.plain(WorkflowName.KB_INIT))

        async with tracer.start_workflow_step_trace(
            name="kb_init",
            extra_metadata={"workspace_name": workspace_name, "generation_id": generation_id},
        ):
            await agent_query(
                system_prompt=kb_init_prompt,
                workspace_path=ws_root,
                model=kb_init_model,
                mcp_servers=mcp.servers,
                allowed_tools=get_common_allowed_tools(ws_root) + mcp.allowed_tools,
                max_turns=100,
                logger=create_agent_logger(
                    f"{workspace_name}-kb-init", generation_id=generation_id,
                ),
                provider=provider_instance,
            )
        duration = time.monotonic() - t0
        generated_files = collect_manifest()
        ctx.logger.info(
            f"KB init agent completed successfully: "
            f"{len(generated_files)} files in {duration:.1f}s"
        )
        for f in generated_files:
            ctx.logger.info(f"  KB artifact: {f}")

        telemetry.capture_kb_init_event(
            generation_id=generation_id,
            workspace_name=workspace_name,
            status="success",
            generated_files=generated_files,
            duration_seconds=duration,
        )
    except Exception as e:
        duration = time.monotonic() - t0
        generated_files = collect_manifest()
        ctx.logger.error(f"KB init agent failed (non-fatal): {e}", exc_info=True)

        telemetry.capture_kb_init_event(
            generation_id=generation_id,
            workspace_name=workspace_name,
            status="failed",
            generated_files=generated_files,
            duration_seconds=duration,
            error_message=str(e),
        )


async def sync_specs_to_workspaces(ctx: WorkflowContext) -> None:
    """Prepare workspace directories and sync spec files to all workspaces."""
    ctx.workspace_manager.prepare_parallel_workspaces(
        primary_workspace=ctx.primary_workspace,
        extra_workspaces=ctx.extra_workspaces,
        spec_path=ctx.request.spec_path,
        outputs_dir=ctx.request.outputs_dir,
        standards_source=ctx.settings.STANDARDS_SOURCE_PATH,
        src_dir=ctx.request.src_dir,
    )


async def sync_code_to_workspaces(ctx: WorkflowContext) -> None:
    """Sync customer source code from primary workspace to extra workspaces.

    Must run after sync_specs_to_workspaces, which clears the src directory on
    extra workspaces. This step restores it from the primary workspace.
    No-op when there are no extra workspaces or no src directory on primary.
    """
    for extra_ws in ctx.extra_workspaces:
        ctx.workspace_manager.sync_directories(
            source_ws=ctx.primary_workspace,
            target_ws=extra_ws,
            directories=[ctx.request.src_dir],
        )


async def sync_plan_to_workspaces(ctx: WorkflowContext) -> None:
    """Sync implementation plan from primary workspace to extra workspaces."""
    if not ctx.extra_workspaces or not ctx.planning_data:
        return
    ctx.workspace_manager.sync_plan_to_workspaces(
        primary_workspace=ctx.primary_workspace,
        extra_workspaces=ctx.extra_workspaces,
        plan_file_path=ctx.planning_data.plan_file_path,
    )


# File naming per plan type. Single source of truth used by reparse helpers.
_MARKDOWN_FILE_BY_PLAN: Dict[PlanType, str] = {
    PlanType.IMPLEMENTATION: IMPLEMENTATION_PLAN_FILE,
    PlanType.E2E: E2E_TEST_PLAN_FILE,
}
_JSON_FILE_BY_PLAN: Dict[PlanType, str] = {
    PlanType.IMPLEMENTATION: PLANNING_PHASES_FILE,
    PlanType.E2E: E2E_PHASES_FILE,
}


async def _update_plan_firestore(
    ctx: WorkflowContext,
    plan_type: PlanType,
    new_plan: PlanningResult,
) -> None:
    """Persist new planning data to ctx and Firestore for the given plan type."""
    if plan_type == PlanType.IMPLEMENTATION:
        ctx.planning_data = new_plan
        written = await ctx.generation_session_service.update_planning_data(
            ctx.request.generation_id,
            planning_data=new_plan,
            total_phases=new_plan.phase_count,
        )
        if written is False:
            ctx.logger.info(
                "contract_validator: %s plan set in ctx (%d phases); Firestore skipped — no workspace_ids yet",
                plan_type.value, new_plan.phase_count,
            )
            return
    else:
        ctx.e2e_planning_data = new_plan
        written = await ctx.generation_session_service.save_e2e_plan(
            ctx.request.generation_id,
            planning_data=new_plan,
            total_phases=new_plan.phase_count,
        )
        if not written:
            ctx.logger.info(
                "contract_validator: %s plan set in ctx (%d phases); Firestore skipped — no workspace_ids yet",
                plan_type.value, new_plan.phase_count,
            )
            return
    ctx.logger.info(
        "contract_validator: persisted %s plan to Firestore (%d phases)",
        plan_type.value, new_plan.phase_count,
    )


def _load_planning_result_from_json(
    ctx: WorkflowContext,
    plan_type: PlanType,
    markdown_checksum: str,
) -> PlanningResult:
    """Parse the JSON file the conversion agent wrote, using the provided checksum.

    Raises RuntimeError if the JSON is missing or malformed.
    """
    md_file = _MARKDOWN_FILE_BY_PLAN[plan_type]
    json_file = _JSON_FILE_BY_PLAN[plan_type]
    workspace_root = Path(ctx.primary_workspace.workspace_path)
    json_path = workspace_root / ctx.request.outputs_dir / PLANNING_SUBDIR / json_file

    try:
        data = json.loads(json_path.read_text())
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Plan conversion agent did not create {json_path}. "
            "Agent may have failed to write the file."
        ) from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Failed to parse {plan_type.value} phases JSON at {json_path}: {exc}"
        ) from exc

    result = construct_planning_result(
        data=data,
        outputs_dir=ctx.request.outputs_dir,
        plan_filename=md_file,
        logger=ctx.logger,
        markdown_checksum=markdown_checksum,
    )
    if result is None:
        raise RuntimeError(
            f"Failed to construct PlanningResult from {plan_type.value} JSON at {json_path}"
        )

    ctx.logger.info("Loaded %s phases JSON from %s", plan_type.value, json_path)
    return result


async def _invoke_plan_conversion_agent(
    ctx: WorkflowContext,
    plans_to_convert: Dict[PlanType, str],
) -> Dict[PlanType, PlanningResult]:
    """Run the conversion agent for the given plan types and load the JSON it wrote.

    ``plans_to_convert`` maps each plan type to its freshly-computed markdown checksum
    so `_load_planning_result_from_json` can stamp the new `PlanningResult` without
    re-reading the markdown.
    """
    workspace_root = str(ctx.primary_workspace.workspace_path)
    plan_types = list(plans_to_convert.keys())

    prompt = plan_conversion_agent_template(
        workspace_root_dir=workspace_root,
        outputs_dir=ctx.request.outputs_dir,
        plan_types=plan_types,
    )
    ctx.logger.info(
        "Invoking plan conversion agent for: %s",
        ", ".join(pt.value for pt in plan_types),
    )

    conversion_model = resolve_model(
        WORKFLOW_TIER_MAP[WorkflowName.MARKDOWN_TO_JSON_CONVERTER],
        ctx.settings,
        ctx.llm_overrides,
        return_first=True,
    )
    TelemetryContext.set_workflow(TelemetryWorkflowLabel.plain(WorkflowName.MARKDOWN_TO_JSON_CONVERTER))
    conversion_logger = create_agent_logger(
        "plan-converter", generation_id=ctx.request.generation_id
    )
    provider_instance = ctx.workspace_manager.get_provider(ctx.primary_workspace)
    async with tracer.start_workflow_step_trace("plan_conversion"):
        result = await agent_query(
            system_prompt=prompt,
            workspace_path=workspace_root,
            model=conversion_model,
            logger=conversion_logger,
            allowed_tools=get_common_allowed_tools(workspace_root),
            max_turns=30,
            provider=provider_instance,
        )

    if not result.result:
        raise RuntimeError("Plan conversion agent returned no result")

    if result.result == "SKIP_MODE":
        ctx.logger.info("Plan conversion agent was skipped (SKIP_MODE) — writing stub JSON files")
        write_mock_planning_phases_json(
            workspace_root=workspace_root,
            outputs_dir=ctx.request.outputs_dir,
            plan_types=plan_types,
            logger=ctx.logger,
        )
    else:
        ctx.logger.debug("Plan conversion agent response: %s", result.result[:500])

    return {
        plan_type: _load_planning_result_from_json(ctx, plan_type, checksum)
        for plan_type, checksum in plans_to_convert.items()
    }


async def run_contract_validator(ctx: WorkflowContext) -> None:
    """First step of generate_app_workflow.

    Reads the user-uploaded files from the primary workspace, fuzzy-matches them to
    canonical paths via the deterministic ``normalize_contract_files`` helper, then
    converts the markdown plans to JSON and writes ``planning_data`` (and optionally
    ``e2e_planning_data``) to Firestore.

    On a ContractRejection, this function re-raises — the orchestrator propagates it
    without ``fail()``; the API handler calls ``reject_contract()`` (see CLAUDE.md).

    This step replaces the old PLANNING + REPARSE_PLAN + SYNC_PLAN steps. The backend
    no longer has a planning agent: the user produces the markdown locally via
    ``run_planning``.
    """
    if not ctx.primary_workspace or not ctx.primary_workspace.workspace_path:
        raise RuntimeError(
            "contract_validator: no primary workspace allocated — cannot validate uploaded files."
        )

    # in production the service is always threaded through. Treat its
    # absence-with-a-generation_id as a wiring bug (single precondition) rather than
    # scattering None-guards at each persistence call (:644/:657/:824).
    if ctx.request.generation_id and ctx.generation_session_service is None:
        raise RuntimeError(
            "contract_validator: generation_id is set but generation_session_service is None — "
            "wiring bug; the validator must be able to persist plan and MCP-prune data."
        )

    workspace_root = Path(ctx.primary_workspace.workspace_path)
    outputs_dir = ctx.request.outputs_dir

    # Step 1: deterministic file normalization. Raises ContractRejection on missing
    # or ambiguous files; propagate to reject_contract() via the orchestrator.
    canonical_paths = normalize_contract_files(workspace_root, outputs_dir, ctx.logger)
    validate_preconverted_plan_contract(canonical_paths)
    ctx.logger.info(
        "contract_validator: canonical files resolved: %s",
        {k: str(v.relative_to(workspace_root)) for k, v in canonical_paths.items()},
    )

    # Bug 1 (PR255): re-read integration readiness from the NORMALIZED analysis file.
    # build_workflow_context took an early snapshot before normalization; a misplaced
    # completeness file (e.g. at outputs_dir root) is moved to canonical by
    # normalize_contract_files above. run_deploy_and_e2e reads ctx.integration_readiness
    # as its no-op guard, so it must reflect ground truth, not the pre-validation snapshot.
    ctx.integration_readiness = parse_integration_readiness_from_file(canonical_paths["analysis"])
    ctx.logger.info(
        "contract_validator: integration_readiness (from normalized file) = %s",
        ctx.integration_readiness.value,
    )

    # Step 1b: keyword-only spec-driven MCP prune (session-wide ceiling for codegen).
    effective_mcps, prune_reasons, should_persist = prune_enabled_mcps_keyword_only(
        settings=ctx.settings,
        logger=ctx.logger,
        workspace_root=workspace_root,
        spec_path=ctx.request.spec_path,
        outputs_dir=outputs_dir,
        candidate_enabled_mcps=ctx.enabled_mcps,
    )
    if should_persist and effective_mcps != ctx.enabled_mcps:
        ctx.enabled_mcps = effective_mcps
        if ctx.request.generation_id:
            ctx.generation_session_service.merge_generation_session_parameters(
                ctx.request.generation_id,
                {
                    "mcp_servers_enabled": enabled_mcps_to_parameter_string(effective_mcps),
                },
            )
        ctx.logger.info(
            "contract_validator: mcp keyword prune updated enablement to %s (%s)",
            sorted(effective_mcps),
            prune_reasons,
        )

    # Step 2: convert markdown to JSON for every plan we found. The conversion agent
    # reads markdown, writes JSON files into the workspace, and we load + validate them.
    plans_to_convert: Dict[PlanType, str] = {}

    plan_md = canonical_paths["plan"].read_text(encoding="utf-8")
    plans_to_convert[PlanType.IMPLEMENTATION] = _compute_markdown_checksum(plan_md)

    if "e2e_plan" in canonical_paths:
        e2e_md = canonical_paths["e2e_plan"].read_text(encoding="utf-8")
        plans_to_convert[PlanType.E2E] = _compute_markdown_checksum(e2e_md)

    impl_results = await _invoke_plan_conversion_agent(
        ctx, {PlanType.IMPLEMENTATION: plans_to_convert[PlanType.IMPLEMENTATION]}
    )

    conversion_results: Dict[PlanType, PlanningResult] = dict(impl_results)

    if PlanType.E2E in plans_to_convert:
        e2e_results = await _invoke_plan_conversion_agent(
            ctx, {PlanType.E2E: plans_to_convert[PlanType.E2E]}
        )
        conversion_results.update(e2e_results)

    impl_plan = conversion_results.get(PlanType.IMPLEMENTATION)
    if impl_plan is None or impl_plan.phase_count == 0:
        raise RuntimeError(
            "Plan conversion produced no implementation phases after preflight passed"
        )

    # Step 3: persist to Firestore + ctx. _update_plan_firestore handles both
    # implementation and e2e plans, and tolerates missing workspace_ids by setting ctx
    # without writing Firestore.
    for plan_type, new_plan in conversion_results.items():
        await _update_plan_firestore(ctx, plan_type, new_plan)


async def run_generation_phases(ctx: WorkflowContext) -> None:
    """
    Run parallel code generation phases across all workspaces.
    Stores phase results in ctx.generation_result.
    """
    gen_t0 = time.monotonic()
    executor = ParallelAgentExecutor(ctx.workspace_manager, ctx.logger)

    async def agent_fn(workspace: WorkspaceSettings, manager: WorkspaceManager, logger: logging.Logger):
        parent_stats = get_workflow_stats()
        workspace_stats = None
        try:
            workspace_stats = WorkflowExecutionStats(
                workflow_name="workspace_phases",
                workspace_name=workspace.name or str(workspace.workspace_path),
            )
            set_workflow_stats(workspace_stats)
        except Exception as e:
            logger.warning(f"Failed to initialize workspace stats: {e}", exc_info=True)

        try:
            workspace_id = _resolve_workspace_id(workspace, ctx.workspaces, ctx.workspace_ids)

            start_phase = None
            end_phase = None
            if workspace_id and ctx.workspace_phases_data.get(workspace_id):
                phase_info = ctx.workspace_phases_data[workspace_id]
                last_completed = phase_info.get("last_completed_phase", 0)
                total_phases = phase_info.get(
                    "total_phases",
                    ctx.planning_data.phase_count if ctx.planning_data else 0,
                )
                if last_completed < total_phases:
                    start_phase = last_completed + 1
                    end_phase = total_phases
                    logger.info(
                        f"Workspace {workspace_id}: resuming from phase {start_phase} "
                        f"(last completed: {last_completed}, total: {total_phases})"
                    )
                else:
                    logger.info(f"Workspace {workspace_id}: all phases already completed, skipping")
                    return []

            result = await execute_all_phases(
                planning_data=ctx.planning_data,
                workspace=workspace,
                manager=manager,
                request=ctx.request,
                logger=logger,
                start_phase=start_phase,
                end_phase=end_phase,
                generation_session_service=ctx.generation_session_service,
                workspace_id=workspace_id,
                integration_readiness=ctx.integration_readiness,
                enabled_mcps=ctx.enabled_mcps,
            )

            if parent_stats and workspace_stats:
                try:
                    set_workflow_stats(parent_stats)
                    parent_stats.add_workspace_stats(
                        workspace.name or str(workspace.workspace_path),
                        workspace_stats,
                    )
                    workspace_stats.mark_completed()
                except Exception as e:
                    logger.warning(f"Failed to merge workspace stats: {e}", exc_info=True)

            return result
        finally:
            try:
                set_workflow_stats(parent_stats)
            except Exception:
                pass

    ctx.generation_result = await executor.execute_parallel(
        workspaces=ctx.workspaces, agent_fn=agent_fn
    )
    try:
        eid = getattr(ctx.request, "generation_id", None) or "unknown"
        telemetry.capture_workspace_coding_completed(
            generation_id=eid,
            workspace_ids=list(ctx.workspace_ids),
            duration_seconds=time.monotonic() - gen_t0,
        )
    except Exception as exc:
        ctx.logger.warning(
            "workspace_coding_completed telemetry failed (non-fatal): %s",
            exc,
            exc_info=True,
        )


async def _build_deploy_github_context(generation_session_service, workspace_id: str) -> DeployGithubContext:
    """Return deploy context for the agent prompt.

    repo_url is read from Firestore via GenerationSessionService (no transformation).
    Branch is always WORKSPACE_DEFAULT_BRANCH — all SpecFlow workspaces push generated code to that branch.
    """
    github_repo: Optional[str] = None
    if generation_session_service and workspace_id:
        github_repo = await generation_session_service.get_workspace_repo_url(workspace_id)
    return DeployGithubContext(
        github_repo=github_repo,
        github_ref=WORKSPACE_DEFAULT_BRANCH,
        deploy_workflow=WORKSPACE_DEPLOY_WORKFLOW,
    )


async def run_deploy_and_e2e(ctx: WorkflowContext) -> None:
    """
    Run the deploy → e2e test → fix loop using e2e-test-plan.md.

    Only called when integration_readiness == INTEGRATION_TESTS_READY.
    The generated code (Dockerfile, k8s manifests, CI/CD workflows, e2e tests)
    already exists on the workspace filesystems from the generation phases.

    Phase structure is driven by e2e-test-plan.md, written by the planning agent
    alongside IMPLEMENTATION_PLAN.md. Phases are executed with the same phase agent
    template used for application code generation.

    On retry, ctx.e2e_planning_data and ctx.workspace_phases_deployment_data are
    pre-loaded by build_workflow_context from Firestore, enabling resume from the
    last completed deploy phase without re-reading the file from disk.
    """
    # Bug 1 (PR255): the step is always registered (see generate_poc.py); this no-op
    # guard is the single decision point for whether deploy/e2e runs. By now
    # run_contract_validator has set ctx.integration_readiness from the NORMALIZED
    # analysis file, so a LOCAL_ONLY spec returns in O(1) — equivalent to never
    # registering the step — while an INTEGRATION_TESTS_READY spec proceeds even if the
    # completeness file was originally misplaced.
    if ctx.integration_readiness != SpecReadiness.INTEGRATION_TESTS_READY:
        ctx.logger.info(
            "deploy_and_e2e skipped — integration_readiness=%s (not INTEGRATION_TESTS_READY)",
            ctx.integration_readiness.value,
        )
        return

    ctx.logger.info("=== Deploy & E2E Test Loop (INTEGRATION_TESTS_READY) ===")

    if is_skip_mode_enabled():
        ctx.logger.info("[SKIP_MODE] deploy_and_e2e skipped")
        return

    await _notify_coding_complete_before_deploy_if_applicable(ctx)

    # Use planning data from Firestore if available (retry/resume path).
    # Fall back to parsing the file on first run.
    if ctx.e2e_planning_data is not None:
        e2e_planning_data = ctx.e2e_planning_data
        ctx.logger.info(
            f"e2e plan restored from Firestore: {e2e_planning_data.phase_count} phases"
        )
    else:
        e2e_plan_filename = E2E_TEST_PLAN_FILE
        e2e_plan_path = (
            Path(ctx.primary_workspace.get_isolated_root())
            / ctx.request.outputs_dir
            / PLANNING_SUBDIR
            / e2e_plan_filename
        )
        try:
            e2e_plan_text = e2e_plan_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(
                f"e2e-test-plan.md not found at {e2e_plan_path}. "
                "The planning agent must write this file when INTEGRATION_TESTS_READY."
            ) from exc

        e2e_planning_data = parse_planning_output(
            agent_result=e2e_plan_text,
            outputs_dir=ctx.request.outputs_dir,
            plan_filename=e2e_plan_filename,
            logger=ctx.logger,
        )
        if e2e_planning_data is None:
            raise RuntimeError(
                f"Failed to parse phase JSON from {e2e_plan_path}. "
                "Ensure the planning agent embedded the JSON structure in e2e-test-plan.md."
            )

        ctx.logger.info(
            f"e2e plan loaded: {e2e_planning_data.phase_count} phases from {e2e_plan_path}"
        )

    # Initialize workspace_phases_deployment in Firestore (no-op if already set on retry).
    if ctx.generation_session_service:
        await ctx.generation_session_service.update_deployment_planning_data(
            generation_id=ctx.request.generation_id,
            planning_data=e2e_planning_data,
            total_phases=e2e_planning_data.phase_count,
        )

    executor = ParallelAgentExecutor(ctx.workspace_manager, ctx.logger)

    async def agent_fn(
        workspace: WorkspaceSettings,
        manager: WorkspaceManager,
        logger: logging.Logger,
    ):
        workspace_id = _resolve_workspace_id(workspace, ctx.workspaces, ctx.workspace_ids)

        # Build per-workspace deploy context from Firestore (repo_url differs per workspace).
        # Branch is always WORKSPACE_DEFAULT_BRANCH; repo_url is stored in the workspace document.
        deploy_github_context = await _build_deploy_github_context(ctx.generation_session_service, workspace_id)
        if not deploy_github_context.github_repo:
            raise ValueError(
                f"Cannot start deploy phases for workspace {workspace_id}: "
                f"repo_url is not set in Firestore. "
                f"The workspace must be linked to a GitHub repository before deployment."
            )
        logger.info(
            f"Deploy context for {workspace_id}: repo={deploy_github_context.github_repo}, "
            f"ref={deploy_github_context.github_ref}"
        )

        # Resume logic: skip already-completed deploy phases.
        start_phase = None
        end_phase = None
        if workspace_id and ctx.workspace_phases_deployment_data.get(workspace_id):
            phase_info = ctx.workspace_phases_deployment_data[workspace_id]
            last_completed = phase_info.get("last_completed_phase", 0)
            total_phases = phase_info.get("total_phases", e2e_planning_data.phase_count)
            if last_completed >= total_phases:
                logger.info(
                    f"Workspace {workspace_id}: all deploy phases already completed, skipping"
                )
                return []
            if last_completed > 0:
                start_phase = last_completed + 1
                end_phase = total_phases
                logger.info(
                    f"Workspace {workspace_id}: resuming deploy from phase {start_phase} "
                    f"(last completed: {last_completed}, total: {total_phases})"
                )

        return await execute_all_phases(
            planning_data=e2e_planning_data,
            workspace=workspace,
            manager=manager,
            request=ctx.request,
            logger=logger,
            start_phase=start_phase,
            end_phase=end_phase,
            integration_readiness=ctx.integration_readiness,
            generation_session_service=ctx.generation_session_service,
            workspace_id=workspace_id,
            deploy_github_context=deploy_github_context,
            is_deployment=True,
            phase_prefix="deploy-",
            enabled_mcps=ctx.enabled_mcps,
        )

    await executor.execute_parallel(workspaces=ctx.workspaces, agent_fn=agent_fn)


async def run_archive_outputs_step(ctx: WorkflowContext) -> None:
    """
    Orchestrator step ``archive_outputs``: protect generated outputs after generation/deploy,
    before any P10Y work (STEEL XI). Advances checkpoint to OUTPUTS_ARCHIVED via orchestrator.

    Uses ``WorkflowContext.workspaces`` (same identities/models as generation). Not used by
    the P10Y workflow.
    """
    if ctx.generation_session_service is None:
        raise RuntimeError(
            "run_archive_outputs_step requires generation_session_service"
        )

    generation_id = ctx.request.generation_id
    git_archive = ctx.generation_session_service._archive_svc
    artifact_store = ArtifactStore(ctx.generation_session_service.db_adapter)

    # return_exceptions=True ensures every workspace archive is attempted even if one fails.
    # Without it the first exception propagates immediately, leaving remaining archives as
    # orphaned asyncio tasks that the orchestrator can never observe.
    results = await asyncio.gather(
        *[
            protect_workspace_after_generation(
                ws,
                generation_id=generation_id,
                workspace_manager=ctx.workspace_manager,
                git_archive=git_archive,
                artifact_store=artifact_store,
            )
            for ws in ctx.workspaces
        ],
        return_exceptions=True,
    )

    failures = [
        (ctx.workspaces[i].name or str(ctx.workspaces[i].workspace_path), exc)
        for i, exc in enumerate(results)
        if isinstance(exc, BaseException)
    ]
    if failures:
        for ws_name, exc in failures:
            ctx.logger.error(
                "Archive failed for workspace %s (generation %s): %s",
                ws_name, generation_id, exc, exc_info=exc,
            )
        first_ws, first_exc = failures[0]
        raise RuntimeError(
            f"{len(failures)} workspace archive(s) failed for generation {generation_id} "
            f"(first: {first_ws})"
        ) from first_exc

    # Re-archive the flat planning/ subdir from the primary workspace so that
    # build_tarball_from_dir (used by download_outputs) serves the latest plan.
    # The workspace snapshot already has the correct file under ws-01-1/docs/planning/,
    # but the flat planning/ artifact was last written by run_planning and is stale
    # when the user edits the plan before calling run_generation.
    primary_ws = ctx.primary_workspace or (ctx.workspaces[0] if ctx.workspaces else None)
    if primary_ws:
        planning_path = Path(primary_ws.workspace_path) / ctx.request.outputs_dir / PLANNING_SUBDIR
        if planning_path.exists():
            await artifact_store.archive_planning(generation_id, planning_path)


