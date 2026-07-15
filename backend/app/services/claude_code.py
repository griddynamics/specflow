import asyncio
from collections.abc import AsyncIterator
import contextlib
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import shutil
from typing import Any, Dict, FrozenSet, List, Optional

from claude_agent_sdk import (
    AgentDefinition,
    AssistantMessage,
    ClaudeAgentOptions,
    Message,
    ResultMessage,
    TextBlock,
    query,
)


from app.core.config import SUPPORTED_MCPS, settings
from app.core.ttl_config import GenerationLifecyclePolicy
from app.core.logging import create_agent_logger, format_json_to_log, log_agent_options
from app.core.mcp_selection import McpSelector
from app.core.rosetta_kb import rosetta_plugin_root
from app.schemas.generation_workflow_enums import WorkflowStepName
from app.core.tool_usage import (
    BASH_DEFAULT_TIMEOUT_MS,
    BASH_MAX_TIMEOUT_MS,
    bash_usage,
    DEPLOY_EXTRA_TOOLS,
    disallowed_tools,
    get_workspace_rm_bash_allowlist,
    skill_sources,
    skill_usage,
)
from app.prompts.agents_claude_code import (
    generate_deploy_phase_agent_template,
    generate_phase_agent_template,
    generate_planning_agent_template,
    is_agent_complete,
    resume_prompt,
    todo_validator_prompt,
)
from app.schemas.agent import AgentErrorType, AgentResult
from app.services.model_routing import (
    apply_model_fallback_if_routing_failure,
    classify_error,
    get_fallback_model,
)
from app.schemas.deploy_context import DeployGithubContext
from app.schemas.planning import PlanningResult
from app.schemas.specification import GenerateAppRequest, SpecReadiness
from app.schemas.workflow_stats import AgentQueryMetrics
from app.schemas.workspace import WorkspaceSettings
from app.services.agent_hooks import get_bash_guard_hooks
from app.services.providers.base import BaseProvider
from app.services.generation_session import GenerationSessionService
from app.services.skip_mode_mock import (
    generate_mock_implementation_plan,
    get_mock_phase_count,
    is_skip_mode_enabled,
    persist_skip_mode_mock_agent_query_totals,
    setup_skip_mode_workspace_commits,
)
from app.agents_sandboxing.claude_env_vars import build_redacted_env_overlay
from app.database.factory import get_database
from app.state.cancellation import raise_if_cancelled
from app.state.db_adapter import StateMachineDBAdapter
from app.state.transitions import TriggeredBy
from app.state.workspace_models import set_workspace_model_override
from app.services.github_auth import github_cli_env_for_generation
from app.core.telemetry_context import TelemetryContext
from app.schemas.telemetry_workflow import PhaseKind, TelemetryWorkflowLabel
from app.schemas.llm_tier import WorkflowName
from app.schemas.model_token_usage import ModelTokenUsage
from app.services.agent_metrics import ClaudeCodeSdkAgentMetrics
from app.services.agent_stream_broker import build_stream_publisher_from_context
from app.services.langfuse import tracer
from app.services.openrouter_pricing import resolve_agent_query_cost_usd
from app.services.telemetry import telemetry
from app.services.workflow_stats import record_agent_query_metrics
from app.services.workspace_manager import WorkspaceManager

logger = logging.getLogger(__name__)

_AGENT_CALL_TIMEOUT_SECONDS = GenerationLifecyclePolicy.AGENT_PHASE_TIMEOUT_SECONDS

# tool_call_failure aborts immediately after 1; other errors tolerate 2 consecutive.
_MAX_CONSECUTIVE_PHASE_ERRORS = 2

class WorkspaceAbortedError(Exception):
    """
    Raised by execute_all_phases when a workspace should be abandoned.
    Caught by ParallelAgentExecutor per-workspace; other workspaces continue.
    """

def get_system_context_allowed_tools(workspace_path: str) -> List[str]:
    """
    Get system context allowed tools for agents with standards directory.
    
    Standards are located at {workspace_path}/standards/ in each workspace.
    This function returns the allowed tools list with the correct absolute path.
    
    Args:
        workspace_path: The workspace root path (e.g., "/workspaces/workspaceName")
        
    Returns:
        List of allowed tool strings for reading standards files
    """
    standards_dir = f"{workspace_path}/{settings.STANDARDS_DIR_NAME}"
    return [
        f"Read({standards_dir}/**/*.md)",
        f"Glob({standards_dir}/**/*.md)",
    ]

def workspace_usage(workspace_path: str) -> List[str]:
    """
    Get workspace-specific allowed tools for reading workspace files.
    
    Args:
        workspace_path: The workspace root path (e.g., "/workspaces/workspaceName")
        
    Returns:
        List of allowed tool strings for reading workspace files
    """
    return [
        f"Read({workspace_path}/**)",
        f"Write({workspace_path}/**)",
        f"Edit({workspace_path}/**)",
        f"Glob({workspace_path}/**)",
    ]

def get_common_allowed_tools(workspace_path: str) -> List[str]:
    """
    Assemble common allowed tools for agents working in a workspace.

    Combines bash tools (including git), skills, system context (standards),
    and workspace access. All agents get git via Bash(git:*) in bash_usage.

    Args:
        workspace_path: The workspace root path (e.g., "/workspaces/workspaceName")

    Returns:
        Combined list of allowed tool strings
    """
    tools = bash_usage + skill_usage
    tools += get_system_context_allowed_tools(workspace_path)
    tools += workspace_usage(workspace_path)
    return tools

def setup_workspace_cache_directories(workspace_path: str) -> Dict[str, str]:
    """
    Redirect all tool caches and data directories to ``/workspaces/caches/<workspace_name>/``
    so they never land inside the workspace git repo (preventing accidental commits)
    and never land on the pod's ephemeral disk.

    Redirect strategy — two layers:

    1. **XDG base directories** (``XDG_CACHE_HOME``, ``XDG_DATA_HOME``,
       ``XDG_CONFIG_HOME``) act as a catch-all for every XDG-compliant tool
       (uv, pip, Cargo, Rustup, Flutter's Linux config, etc.).
    2. **Explicit per-tool vars** cover tools that ignore XDG (npm, Yarn,
       Composer, Go, Gradle, Maven, Android, NuGet) and provide clarity in logs.

    The vars are built as separate collections so the makedirs step stays honest:
    only ``managed_cache_dirs`` (directories this function owns) is created on disk;
    out-of-band paths, scalar flags, and telemetry opt-outs are set-only. They are
    summed into the returned env at the point of use.

    Args:
        workspace_path: The workspace root path (e.g., "/workspaces/ws-01-1")

    Returns:
        Dictionary of environment variables ready to merge into the agent env.
    """
    workspace_name = Path(workspace_path).name
    cache_root = os.path.join(settings.WORKSPACE_BASE_PATH, "caches", workspace_name)
    cache_base = os.path.join(cache_root, ".cache")
    data_base = os.path.join(cache_root, ".local", "share")
    config_base = os.path.join(cache_root, ".config")
    android_sdk_root = os.path.join(settings.WORKSPACE_BASE_PATH, "caches", "common", "android")

    # Directories this function owns and creates via makedirs.
    managed_cache_dirs = {
        # XDG bases redirect ~/.cache, ~/.local/share, ~/.config off the ~1 GB pod rootfs onto NFS,
        # isolating state per workspace and preventing rootfs exhaustion under concurrent agents.
        "XDG_CACHE_HOME": cache_base,
        "XDG_DATA_HOME": data_base,
        "XDG_CONFIG_HOME": config_base,

        # Python / UV
        "UV_CACHE_DIR": os.path.join(cache_base, "uv"),
        "UV_PYTHON_INSTALL_DIR": os.path.join(data_base, "uv", "python"),
        "PIP_CACHE_DIR": os.path.join(cache_base, "pip"),

        # npm requires lowercase env vars: npm_config_<key>
        "npm_config_cache": os.path.join(cache_base, "npm"),
        "YARN_CACHE_FOLDER": os.path.join(cache_base, "yarn"),

        # PHP
        "COMPOSER_CACHE_DIR": os.path.join(cache_base, "composer"),

        # Go
        "GOMODCACHE": os.path.join(cache_base, "go", "pkg", "mod"),
        "GOPATH": os.path.join(cache_root, ".go"),

        # Rust
        "CARGO_HOME": os.path.join(cache_root, ".cargo"),
        "RUSTUP_HOME": os.path.join(cache_root, ".rustup"),

        # JVM (Maven repo is a JVM flag in scalar_flags, not a dir)
        "GRADLE_USER_HOME": os.path.join(cache_base, "gradle"),

        # Android per-workspace homes — AVD state and emulator locks must not bleed across workspaces.
        "ANDROID_USER_HOME": os.path.join(cache_root, ".android"),
        "ANDROID_AVD_HOME": os.path.join(cache_root, ".android", "avd"),
        "ANDROID_EMULATOR_HOME": os.path.join(cache_root, ".android"),

        "PUB_CACHE": os.path.join(cache_base, "pub"),

        # .NET
        "NUGET_PACKAGES": os.path.join(cache_base, "nuget"),
    }

    # Paths set in the env but NOT created by makedirs — either provisioned out-of-band
    # (Android SDK by init-mobile-sdk.sh; Flutter per-workspace copy by the wrapper scripts)
    # or a scalar JVM flag. Kept separate so the makedirs loop only sees real directories.
    external_paths = {
        # Shared read-only Android SDK; MUST match the Dockerfile ENV.
        "ANDROID_SDK_ROOT": android_sdk_root,
        "ANDROID_HOME": android_sdk_root,
        # Flutter self-mutates $FLUTTER_ROOT/bin/cache and can't relocate it, so a shared
        # copy would race across concurrent workspaces. The flutter/dart wrappers copy the
        # shared template into this per-workspace path on first use.
        "FLUTTER_ROOT": os.path.join(cache_root, "flutter"),
    }
    scalar_flags = {
        "MAVEN_OPTS": f"-Dmaven.repo.local={os.path.join(cache_base, 'maven', 'repository')}",
    }
    # Disable analytics for every CLI handed to agents: the sandbox has no egress (dead
    # latency + noisy errors), and analytics state written under $HOME can exhaust the pod rootfs.
    telemetry_opt_outs = {
        "DO_NOT_TRACK": "1",
        "FLUTTER_NO_ANALYTICS": "1",
        "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
        "DOTNET_NOLOGO": "1",
        "NEXT_TELEMETRY_DISABLED": "1",
        "GATSBY_TELEMETRY_DISABLED": "1",
        "ASTRO_TELEMETRY_DISABLED": "1",
        "STORYBOOK_DISABLE_TELEMETRY": "1",
        "HOMEBREW_NO_ANALYTICS": "1",
    }

    for path in managed_cache_dirs.values():
        os.makedirs(path, exist_ok=True)

    return {**managed_cache_dirs, **external_paths, **scalar_flags, **telemetry_opt_outs}


async def clear_workspace_caches(workspace_ids: List[str]) -> None:
    """Delete the cache directories for the given workspaces after a completed run.

    Non-fatal: logs and continues if a directory is missing or deletion fails.

    Args:
        workspace_ids: List of workspace IDs (e.g. ["ws-01-1", "ws-01-2"]).
    """
    for ws_id in workspace_ids:
        cache_root = Path(settings.WORKSPACE_BASE_PATH) / "caches" / ws_id
        if cache_root.exists():
            try:
                await asyncio.to_thread(shutil.rmtree, cache_root)
                logger.info("Cleared cache for workspace %s at %s", ws_id, cache_root)
            except Exception as exc:
                logger.warning(
                    "Failed to clear cache for workspace %s at %s: %s",
                    ws_id, cache_root, exc,
                )


def setup_rosetta_plugin_env() -> Dict[str, str]:
    """Return ``CLAUDE_PLUGIN_ROOT`` for the bundled Rosetta plugin in provisioned-plugin mode.

    In provisioned-plugin mode ``WorkspaceManager.provision_rosetta_plugin`` copies the plugin's
    agents/skills/commands into each workspace ``.claude/`` and merges its hooks into
    ``.claude/settings.json``. The plugin's own files (hook scripts, rules/, templates/) stay in
    the read-only image at ``ROSETTA_PLUGIN_PATH``; pointing ``CLAUDE_PLUGIN_ROOT`` there lets the
    merged hooks' ``${CLAUDE_PLUGIN_ROOT}`` resolve. Hooks run as CLI subprocesses (not the
    agent's sandboxed tools), so the read-only ``/opt`` path is reachable; this env var is just a
    string and does not widen the agent's file-tool sandbox.

    Returns an empty dict in any other mode (live MCP, or no usable plugin on disk), so the env
    var is simply not set. The path is resolved through ``app.core.rosetta_kb`` — the same single
    source of truth that gates provisioning — so the env var is set exactly when the plugin was
    provisioned. The ``is_dir`` stat is intentionally not cached: it is cheap, the path can be
    mounted lazily, and a stale cache would desync this from provisioning.
    """
    plugin_root = rosetta_plugin_root(settings)
    return {"CLAUDE_PLUGIN_ROOT": plugin_root} if plugin_root else {}


def setup_claude_code_tmpdir() -> Dict[str, str]:
    """Ensure the shared Claude Code temp directory exists and return its env var.

    Claude Code writes internal temp files to CLAUDE_CODE_TMPDIR. Pointing it at
    the persistent NFS volume (rather than ephemeral container storage) prevents
    data loss on container restarts and avoids filling the container's rootfs.

    Returns:
        {"CLAUDE_CODE_TMPDIR": "<path>"} ready to merge into the agent env.
    """
    tmpdir = settings.CLAUDE_CODE_TMPDIR_PATH
    os.makedirs(tmpdir, exist_ok=True)
    return {"CLAUDE_CODE_TMPDIR": tmpdir}


def setup_claude_code_max_output_tokens() -> Dict[str, str]:
    """Return the CLAUDE_CODE_MAX_OUTPUT_TOKENS env var when configured.

    Claude Code honors this env var to cap tool-call output tokens.
    Keeping it below the provider hard cap (64 k) prevents hard failures.

    Returns:
        {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "<n>"} or {} when unset.
    """
    max_tokens = settings.CLAUDE_CODE_MAX_OUTPUT_TOKENS
    if max_tokens is None:
        return {}
    return {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(max_tokens)}


def setup_claude_code_bash_timeouts() -> Dict[str, str]:
    """Forward per-Bash-call timeouts to the spawned Claude Code CLI.

    BASH_DEFAULT_TIMEOUT_MS is applied when the agent does not specify a
    timeout; BASH_MAX_TIMEOUT_MS is the hard ceiling. When a Bash call exceeds
    its budget the CLI terminates the subprocess and returns an error to the
    agent, so a hung dev server / watch mode / `gh run watch` no longer
    consumes the entire 5 h phase wall-clock.
    """
    return {
        "BASH_DEFAULT_TIMEOUT_MS": str(BASH_DEFAULT_TIMEOUT_MS),
        "BASH_MAX_TIMEOUT_MS": str(BASH_MAX_TIMEOUT_MS),
    }



async def simple_query():
    options = ClaudeAgentOptions(
        system_prompt="You are a helpful assistant",
        max_turns=1
    )

    async for message in query(prompt="Hello Claude", options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    return(block.text)

# We need to design a more resilient agent query that can use session_id from agent_query to start session again if it fails or ends prematurely
# There can be still TODOs not finalized by the agent, but still it can exit. We need to at least once resume with another call and add a message on top to check TODOs, and continue or decide to end the session.
NO_RESUMING_RESULT = "Original run only, no resuming happened"




async def _resume_iteration(
    resume_count: int,
    max_resume_attempts: int,
    current_session_id: Optional[str],
    outputs_dir: str,
    spec_path: str,
    system_prompt: str,
    workspace_path: str,
    subagents: dict[str, AgentDefinition],
    max_turns: int,
    allowed_tools: list[str],
    disallowed_tools: list[str],
    model: str,
    logger: logging.Logger,
    max_buffer_size: int,
    provider: Optional[BaseProvider],
    qa_results: str,
    phase_number: Optional[int] = None,
    mcp_servers: Optional[Dict] = None,
    phase_kind: PhaseKind = PhaseKind.GENERATION,
    phase_num: int = 0,
    extra_env: Optional[Dict[str, str]] = None,
) -> tuple[Optional[AgentResult], str, bool]:
    """
    Execute a single resume iteration.
    
    Returns:
        Tuple of (resumed_result, qa_results, should_break)
        - resumed_result: The result from the resume attempt, or None if failed
        - qa_results: DISABLED - Updated QA results string
        - should_break: True if the loop should break (completion or failure)
    """
    # DISABLING QA AGENT DUE TO VERBOSITY AND TOKEN CONSUMPTION
    qa_results = ""

    # logger.info(f"Running QA agent with session_id: {current_session_id}")
    # qa_result: AgentResult = await agent_query(
    #     system_prompt=qa_agent_prompt(outputs_dir=outputs_dir),
    #     workspace_path=workspace_path,
    #     # Deliberately not resuming session, we want fresh context
    #     session_id=None,
    #     max_turns=max_turns,
    #     allowed_tools=allowed_tools,
    #     disallowed_tools=disallowed_tools,
    #     model=model,
    #     logger=logger,
    #     max_buffer_size=max_buffer_size,
    #     provider=provider,
    # )

    # if qa_result.result is None or qa_result.session_id is None:
    #     logger.warning(f"QA attempt {resume_count} returned None result or session_id, ignoring")
    # else:
    #     qa_results = qa_result.result

    # logger.info(f"QA attempt {resume_count} returned session_id: {qa_result.session_id}")
    # current_session_id = qa_result.session_id if qa_result.session_id else current_session_id

    logger.info(f"Attempting resume {resume_count}/{max_resume_attempts}")
    TelemetryContext.set_workflow(TelemetryWorkflowLabel.phase(phase_kind, phase_num, "resume"))
    resumed_result: AgentResult = await agent_query(
        system_prompt=resume_prompt(
            dev_system_prompt=system_prompt,
            workspace_root=workspace_path,
            outputs_dir=outputs_dir,
            spec_path=spec_path,
            qa_results=qa_results,
            phase_number=phase_number,
        ),
        workspace_path=workspace_path,
        subagents=subagents,
        # SUPER IMPORTANT: we want fresh context as this is very long horizon task and agents tend to cheat and finish early
        session_id=None,
        max_turns=max_turns,
        allowed_tools=allowed_tools,
        disallowed_tools=disallowed_tools,
        model=model,
        logger=logger,
        max_buffer_size=max_buffer_size,
        provider=provider,
        mcp_servers=mcp_servers,
        extra_env=extra_env,
    )
    
    if resumed_result.result is None or resumed_result.session_id is None:
        logger.error(f"Resume attempt {resume_count} returned None result or session_id, stopping")

    # Check if the agent indicates completion
    # Look for indicators that work is complete
    TelemetryContext.set_workflow(TelemetryWorkflowLabel.phase(phase_kind, phase_num, "validator"))
    validator_result: AgentResult = await agent_query(
        system_prompt=todo_validator_prompt(
            workspace_root=workspace_path,
            outputs_dir=outputs_dir,
            spec_path=spec_path,
            phase_number=phase_number,
        ),
        workspace_path=workspace_path,
        # Deliberately not resuming session, we want fresh context
        session_id=None,
        max_turns=max_turns,
        allowed_tools=allowed_tools,
        disallowed_tools=disallowed_tools,
        model=model,
        logger=logger,
        max_buffer_size=max_buffer_size,
        provider=provider,
        extra_env=extra_env,
    )
    
    if validator_result.result is None or validator_result.session_id is None:
        logger.warning(f"Validator attempt {resume_count} returned None result or session_id, exiting resume loop")
        return AgentResult(result=None, session_id=None), qa_results, True

    # Log validator result for debugging
    validator_result_preview = (validator_result.result[:200] + "...") if validator_result.result and len(validator_result.result) > 200 else validator_result.result
    logger.info(f"Validator result (attempt {resume_count}): {validator_result_preview}")

    if is_agent_complete(validator_result):
        logger.info(f"Agent indicates completion after {resume_count} resume(s)")
        return resumed_result, qa_results, True
    
    logger.warning(
        f"Validator indicates work is still incomplete after resume attempt {resume_count}. "
        f"Validator said: {validator_result_preview}. "
        f"This may cause additional resume iterations."
    )
    logger.info(f"Resume attempt {resume_count} completed, returned session_id: {resumed_result.session_id}")
    return resumed_result, qa_results, False

async def agent_query_with_resume(
    system_prompt: str,
    workspace_path: str,
    outputs_dir: str,
    spec_path: str,
    model: str,
    subagents: dict[str, AgentDefinition] = None,
    session_id: str = None,
    max_turns: int = 200,
    allowed_tools: list[str] = bash_usage + skill_usage,
    disallowed_tools: list[str] = disallowed_tools,
    logger: logging.Logger = None,
    max_buffer_size: int = 1024 * 1024 * 10,
    max_resume_attempts: int = 2,
    provider: Optional[BaseProvider] = None,
    phase_number: Optional[int] = None,
    mcp_servers: Optional[Dict] = None,
    phase_kind: PhaseKind = PhaseKind.GENERATION,
    extra_env: Optional[Dict[str, str]] = None,
) -> AgentResult:
    """
    Query the Claude Code agent with automatic resume capability for incomplete sessions.
    
    This function wraps agent_query and provides resilience by:
    1. Running the initial query
    2. Running validator query to check if there are incomplete TODOs
    3. Automatically resuming the session to complete remaining work up to (max_resume_attempts) iterations
    
    Args:
        system_prompt: The system prompt to use for the agent.
        workspace_path: The path to the workspace to use for the agent.
        outputs_dir: The path to the outputs directory to use for the agent.
        spec_path: The path to the specification directory to use for the agent.
        subagents: The subagents to use for the agent.
        session_id: Optional session ID to resume from.
        max_turns: The maximum number of turns to allow the agent per query.
        allowed_tools: The tools to allow the agent to use.
        disallowed_tools: The tools to disallow the agent to use.
        model: The model to use for the agent (legacy parameter, prefer provider).
        logger: The logger to use for logging.
        max_buffer_size: Maximum buffer size for the agent.
        max_resume_attempts: Maximum number of times to resume the session (default: 2).
        provider: Optional provider instance for multi-provider support.
        phase_kind: Codegen vs deploy loop; combined with ``phase_number`` and agent role
            into ``TelemetryWorkflowLabel`` for telemetry.
    
    Returns:
        The final result from the agent after all resume attempts.
    """
    if logger is None:
        logger = logging.getLogger(__name__)

    phase_num = phase_number if phase_number is not None else 0
    qa_results = ""
    result: Optional[AgentResult] = None

    active_model = model
    _fallback_candidate = get_fallback_model(active_model)

    # Initial query
    logger.info("Starting initial agent query with validator follow-up")
    try:
        TelemetryContext.set_workflow(TelemetryWorkflowLabel.phase(phase_kind, phase_num, "coding"))
        result = await agent_query(
            system_prompt=system_prompt,
            workspace_path=workspace_path,
            subagents=subagents,
            session_id=session_id if session_id else None,
            max_turns=max_turns,
            allowed_tools=allowed_tools,
            disallowed_tools=disallowed_tools,
            model=active_model,
            logger=logger,
            max_buffer_size=max_buffer_size,
            provider=provider,
            mcp_servers=mcp_servers,
            extra_env=extra_env,
        )
        
        if result.result is None or result.session_id is None:
            logger.error(f"Initial agent query returned None or session_id is None: {result}")
        
        TelemetryContext.set_workflow(TelemetryWorkflowLabel.phase(phase_kind, phase_num, "validator"))
        validator_result: AgentResult = await agent_query(
            system_prompt=todo_validator_prompt(
                workspace_root=workspace_path,
                outputs_dir=outputs_dir,
                spec_path=spec_path,
                phase_number=phase_number,
            ),
            workspace_path=workspace_path,
            # Deliberately not resuming session, we want fresh context
            session_id=None,
            max_turns=max_turns,
            allowed_tools=allowed_tools,
            disallowed_tools=disallowed_tools,
            model=active_model,
            logger=logger,
            max_buffer_size=max_buffer_size,
            provider=provider,
            extra_env=extra_env,
        )
        
        if validator_result.result is None or validator_result.session_id is None:
            logger.warning("Initial validator attempt returned None result, we have to continue to at least one resume iteration")
        else:
            # Log validator result for debugging
            validator_result_preview = (validator_result.result[:200] + "...") if validator_result.result and len(validator_result.result) > 200 else validator_result.result
            logger.info(f"Initial validator result: {validator_result_preview}")
            
            if is_agent_complete(validator_result):
                logger.info("Agent indicates completion after initial query, no need to resume")
                return result
            else:
                logger.warning(
                    f"Agent indicates incomplete after initial query. Validator said: {validator_result_preview}. "
                    f"This will trigger resume loop (max {max_resume_attempts} attempts)."
                )
        
    except Exception as e:
        logger.error(f"[AGENT CRASH] Initial agent query failed with exception: {e}", exc_info=True)
        err_str = str(e)
        result = AgentResult(result=err_str, session_id=None, is_error=True)
        active_model, _fallback_candidate = apply_model_fallback_if_routing_failure(
            err_str, active_model, _fallback_candidate, logger, active_model
        )
    
    resume_count = 0
    current_session_id = result.session_id if result else None
    logger.info(f"Initial agent query returned session_id: {current_session_id}")
    
    # Initialize with the initial result; will be updated in the loop
    resumed_result: Optional[AgentResult] = result
    
    # Attempt to resume if needed
    while resume_count < max_resume_attempts:
        resume_count += 1
        logger.info(f"Starting resume iteration {resume_count}/{max_resume_attempts} for phase {phase_number}")

        try:
            resumed_result, qa_results, should_break = await _resume_iteration(
                resume_count=resume_count,
                max_resume_attempts=max_resume_attempts,
                current_session_id=current_session_id,
                outputs_dir=outputs_dir,
                spec_path=spec_path,
                system_prompt=system_prompt,
                workspace_path=workspace_path,
                subagents=subagents,
                max_turns=max_turns,
                allowed_tools=allowed_tools,
                disallowed_tools=disallowed_tools,
                model=active_model,
                logger=logger,
                max_buffer_size=max_buffer_size,
                provider=provider,
                qa_results=qa_results,
                phase_number=phase_number,
                mcp_servers=mcp_servers,
                phase_kind=phase_kind,
                phase_num=phase_num,
                extra_env=extra_env,
            )
            
            if should_break:
                logger.info(f"Resume loop breaking after {resume_count} iteration(s) - work marked as complete")
                break
        except Exception as e:
            logger.error(f"[AGENT CRASH] Resume iteration {resume_count} failed with exception: {e}", exc_info=True)
            err_str = str(e)
            active_model, _fallback_candidate = apply_model_fallback_if_routing_failure(
                err_str, active_model, _fallback_candidate, logger, f"resume {resume_count}"
            )

    if resume_count >= max_resume_attempts:
        logger.warning(
            f"Reached maximum resume attempts ({max_resume_attempts}) for phase {phase_number}. "
            f"This may indicate the agent is stuck or the validator is too strict. "
            f"Check PROGRESS.md and TODOs.md to verify actual completion status."
        )
    
    final = resumed_result if resumed_result else AgentResult(result=None, session_id=None, is_error=True)
    if active_model != model:
        final.active_model = active_model
    return final

async def agent_query(
    system_prompt: str,
    workspace_path: str,
    model: str,
    subagents: dict[str, AgentDefinition] = None,
    session_id: str = None,
    max_turns: int = 200,
    allowed_tools: list[str] = bash_usage + skill_usage,
    disallowed_tools: list[str] = disallowed_tools,
    logger: logging.Logger = None,
    max_buffer_size: int = 1024 * 1024 * 10,
    provider: Optional[BaseProvider] = None,
    mcp_servers: Optional[Dict] = None,
    extra_env: Optional[Dict[str, str]] = None,
) -> AgentResult:
    """
    Query the Claude Code agent with the given system prompt and options.
    
    Args:
        system_prompt: The system prompt to use for the agent.
        workspace_path: The path to the workspace to use for the agent. It is expected to contain specification docs and source code.
        subagents: The subagents to use for the agent.
        session_id: Optional session ID to resume from.
        max_turns: The maximum number of turns to allow the agent.
        allowed_tools: The tools to allow the agent to use.
        disallowed_tools: The tools to disallow the agent to use.
        model: The model to use for the agent (legacy parameter, prefer provider).
        logger: Logger instance for logging.
        max_buffer_size: Maximum buffer size for the agent.
        provider: Optional provider instance for multi-provider support.
        
    Returns:
        AgentResult containing the result and session_id.
    """
    if logger is None:
        logger = logging.getLogger(__name__)
    
    # Check for SKIP_MODE - useful for testing workflows without triggering real agent execution
    if os.getenv("SKIP_AGENT_EXECUTION", "").lower() in ("true", "1", "yes"):
        logger.warning("[SKIP_MODE] Agent execution skipped due to SKIP_AGENT_EXECUTION environment variable")
        final_model = model
        if provider:
            final_model = provider.transform_model_name(model)
        await persist_skip_mode_mock_agent_query_totals(
            model=final_model or "",
            workspace_path=str(workspace_path),
            logger=logger,
        )
        return AgentResult(result="SKIP_MODE", session_id=None)

    # Determine provider configuration
    env_config: Optional[Dict[str, str]] = None
    final_model = model
    
    if provider:
        # Multi-provider mode
        env_config = provider.get_environment_config()
        final_model = provider.transform_model_name(model)
        logger.info(
            f"Using provider with model: {final_model}, "
            f"{os.linesep}base_url: {provider.get_base_url()}, "
        )
    else:
        # Legacy mode - use default Anthropic
        logger.debug(f"Using Anthropic provider with model: {model}")

    # Set up per-workspace cache directories to avoid conflicts between concurrent workspaces
    if env_config is None:
        env_config = {}
    
    # Add workspace-specific cache directories to environment
    cache_env = setup_workspace_cache_directories(workspace_path)
    env_config.update(cache_env)

    # CLAUDE_PLUGIN_ROOT for the bundled Rosetta plugin (plugin mode); empty otherwise.
    env_config.update(setup_rosetta_plugin_env())

    env_config.update(setup_claude_code_tmpdir())
    env_config.update(setup_claude_code_max_output_tokens())
    env_config.update(setup_claude_code_bash_timeouts())
    if extra_env:
        env_config.update(extra_env)

    # See docs/agents/env-vars-leak.md.
    agent_env = {**build_redacted_env_overlay(), **env_config}

    fallback_model = get_fallback_model(final_model)

    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        max_turns=max_turns,
        permission_mode="acceptEdits",
        model=final_model,
        fallback_model=fallback_model,
        max_buffer_size=max_buffer_size,
        # Constraint on the working directory
        cwd=workspace_path,

        # Constraints on tool usage
        # Important - limit paths to specific workspace to avoid jailbreaking to external file system
        allowed_tools=allowed_tools,
        disallowed_tools=disallowed_tools,

        # Sources
        setting_sources=skill_sources,

        # Delegation to workers - parallelizable
        agents=subagents,

        mcp_servers=mcp_servers if mcp_servers else None,
        # Rosetta is delivered by copying the bundled plugin into each workspace's .claude/
        # (see WorkspaceManager.provision_rosetta_plugin), discovered via setting_sources=
        # ["project"]; the SDK plugins= loader is intentionally unused so nothing is read
        # from /opt or ~/.claude at agent runtime.
        plugins=[],
        tools=None,

        # Resuming session if required to continue with context
        resume=session_id,

        # Multi-provider environment configuration + per-workspace cache directories.
        # see build_redacted_env_overlay
        env=agent_env,

        # PreToolUse Bash guard — denies dev-server / watch-mode / backgrounded
        # commands before they ever spawn. See app/services/agent_hooks.py.
        hooks=get_bash_guard_hooks(),

        # Capture subprocess stderr so startup errors (MCP failures, bad session, etc.)
        # are visible in logs instead of being swallowed as "Check stderr output for details"
        stderr=lambda line: logger.warning("[claude-cli stderr] %s", line),
    )

    validate_models_and_tools(options, logger)

    logger.info(
        "%s",
        log_agent_options(options, extra={
            "event": "agent_query_start",
            "num_subagents": len(subagents) if subagents else 0,
        }),
    )

    query_stream: AsyncIterator[Message] = query(prompt=system_prompt, options=options)

    stream_metrics = ClaudeCodeSdkAgentMetrics() if telemetry.is_enabled() else None

    # Best-effort live message tap for the TUI workspace drill-in. Derived from
    # TelemetryContext (generation_id + workspace name + workflow); None when that
    # identity is unknown (one-off / test calls). Publishing is non-blocking and
    # cannot affect generation — see StreamPublisher.publish_nowait.
    stream_publisher = build_stream_publisher_from_context()

    workflow = TelemetryContext.get_workflow()
    lf_gen_name = f"agent_query:{workflow.to_stored_string() if workflow else 'unknown'}"
    lf_subagent_registry: Optional[Dict[str, Dict[str, Any]]] = None
    if subagents:
        lf_subagent_registry = {
            name: {
                "description": (agent_def.description or "")[:500],
                "model": agent_def.model,
                "tool_count": len(agent_def.tools) if agent_def.tools else 0,
                "skill_count": len(agent_def.skills) if agent_def.skills else 0,
                "prompt_preview": (agent_def.prompt or "")[:500],
            }
            for name, agent_def in subagents.items()
        }
    lf_generation = tracer.create_generation(
        name=lf_gen_name,
        model=final_model or "",
        input_data={
            "system_prompt": system_prompt[:5_000],
            "max_turns": max_turns,
        },
        metadata={
            "workspace_path": str(workspace_path),
            "workspace_name": TelemetryContext.get_workspace_name(),
            "provider": "openrouter" if provider else "anthropic",
            "phase_name": TelemetryContext.get_phase_name(),
            "subagents_available": list(subagents.keys()) if subagents else [],
            "subagent_registry": lf_subagent_registry,
            **TelemetryContext.get_mcp_props(),
        },
        model_parameters={
            "max_turns": max_turns,
            "permission_mode": "acceptEdits",
            "max_buffer_size": max_buffer_size,
        },
    )
    lf_tracer = tracer.make_stream_tracer(lf_generation)

    try:
        messages: List[ResultMessage] = await asyncio.wait_for(
            process_query_stream(query_stream, logger, stream_metrics, lf_tracer, stream_publisher),
            timeout=_AGENT_CALL_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        lf_tracer.finalize_pending()
        lf_tracer.end_generation_on_error("timeout")
        logger.error(
            f"[AGENT TIMEOUT] agent_query exceeded {_AGENT_CALL_TIMEOUT_SECONDS // 3600}h limit — "
            f"model={final_model}, workspace={workspace_path}. "
            f"A subprocess is likely hanging (dev server, watch process, or stuck test). "
            f"Returning empty result so the phase can be retried."
        )
        telemetry.capture_agent_query_event(
            final_model, "openrouter" if provider else "anthropic", str(workspace_path),
            is_error=True, error_type="timeout",
            tool_usage_breakdown=stream_metrics.get_metrics() if stream_metrics is not None else None,
        )
        return AgentResult(result=None, session_id=None)
    except Exception:
        lf_tracer.finalize_pending()
        lf_tracer.end_generation_on_error("exception")
        raise
    finally:
        # Guarantee the SDK query generator — and its Claude Code CLI subprocess — is
        # torn down on every exit path, including task.cancel() (CancelledError) during a
        # user cancellation, where the stream would otherwise linger until GC. aclose()
        # on an already-exhausted stream is a harmless no-op.
        if hasattr(query_stream, "aclose"):
            with contextlib.suppress(Exception):
                await query_stream.aclose()

    if messages:
        logger.info(f"Agent returned {len(messages)} messages.")
        result_message = messages[-1]
        usage = result_message.usage or {}
        try:
            cost_breakdown = resolve_agent_query_cost_usd(
                use_catalog_pricing=provider.use_catalog_pricing() if provider else False,
                model=final_model,
                usage=usage,
                sdk_reported_usd=result_message.total_cost_usd,
            )
            cost_usd = cost_breakdown.cost_usd
        except Exception as exc:
            logger.warning("Cost resolution failed, using SDK fallback: %s", exc)
            cost_usd = float(result_message.total_cost_usd or 0.0)
            cost_breakdown = None
        lf_tracer.complete_with_billing(cost_usd)
        # Backstop: close any open tool spans and guarantee the generation is
        # ended even if complete_with_billing() could not finalize (e.g. no
        # ResultMessage ever reached the tracer). Idempotent once ended.
        lf_tracer.finalize_pending()

        logger.info(
            "%s",
            format_json_to_log({
                "duration_ms": result_message.duration_ms,
                "duration_api_ms": result_message.duration_api_ms,
                "is_error": result_message.is_error,
                "num_turns": result_message.num_turns,
                "session_id": result_message.session_id,
                "total_cost_usd": cost_usd,
                "cost_source": cost_breakdown.source if cost_breakdown else "sdk_direct",
                "sdk_reported_cost_usd": result_message.total_cost_usd,
                "usage": usage,
            }),
        )

        tool_usage_breakdown = stream_metrics.get_metrics() if stream_metrics is not None else None

        # Record metrics to workflow stats context if available (wrapped in try-except)
        try:
            metrics = AgentQueryMetrics(
                duration_ms=result_message.duration_ms,
                duration_api_ms=result_message.duration_api_ms,
                is_error=result_message.is_error,
                num_turns=result_message.num_turns,
                session_id=result_message.session_id,
                total_cost_usd=cost_usd,
                usage=usage,
                timestamp=datetime.now(timezone.utc).isoformat(),
                tool_usage_breakdown=tool_usage_breakdown,
            )
            record_agent_query_metrics(metrics)
        except Exception as e:
            # Never interrupt workflow - just log warning
            logger.warning(f"Failed to record metrics for agent_query: {e}", exc_info=True)

        # Track LLM Analytics to PostHog
        result_error_type = None
        if result_message.is_error and result_message.result:
            result_error_type = classify_error(result_message.result, api_error_status=result_message.api_error_status)
            if result_error_type == AgentErrorType.TOOL_CALL_FAILURE:
                logger.error(
                    f"[TOOL-CALL FAILURE] Agent completed with tool-call error. "
                    f"Model={final_model}, provider={'openrouter' if provider else 'anthropic'}. "
                    f"Result: {result_message.result[:500]}"
                )
        api_ms = result_message.duration_api_ms or 0
        wall_ms = result_message.duration_ms or 0
        latency_s = (api_ms / 1000.0) if api_ms > 0 else (wall_ms / 1000.0)

        telemetry.capture_agent_query_event(
            final_model, "openrouter" if provider else "anthropic", str(workspace_path),
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cache_write_tokens=usage.get("cache_creation_input_tokens", 0),
            cache_read_tokens=usage.get("cache_read_input_tokens", 0),
            latency_seconds=latency_s,
            trace_id=result_message.session_id,
            cost_usd=cost_usd,
            num_turns=result_message.num_turns or 0,
            is_error=result_message.is_error,
            error_type=result_error_type,
            tool_usage_breakdown=tool_usage_breakdown,
        )

        if not result_message.is_error:
            persist = TelemetryContext.get_agent_query_totals_handler()
            eid = TelemetryContext.get_generation_id()
            if persist and eid:
                wf_l = TelemetryContext.get_workflow()
                workflow_key = wf_l.to_stored_string() if wf_l else "unknown"
                ws_name = TelemetryContext.get_workspace_name()
                if not ws_name:
                    ws_name = os.path.basename(str(workspace_path).rstrip("/")) or "_"
                delta = ModelTokenUsage.from_sdk(
                    final_model, usage, result_message.num_turns or 0
                )
                if not delta.is_empty():
                    try:
                        await persist(
                            eid,
                            workflow_key,
                            ws_name,
                            delta,
                            float(cost_usd),
                        )
                    except Exception as persist_exc:
                        logger.warning(
                            "Failed to persist agent query totals: %s",
                            persist_exc,
                            exc_info=True,
                        )

        return AgentResult(
            result=result_message.result,
            session_id=result_message.session_id,
            is_error=result_message.is_error,
        )
    else:
        lf_tracer.finalize_pending()
        lf_tracer.end_generation_on_error("no_messages")
        logger.error("No messages returned from agent")
        telemetry.capture_agent_query_event(
            final_model, "openrouter" if provider else "anthropic", str(workspace_path),
            is_error=True, error_type="no_messages",
            tool_usage_breakdown=stream_metrics.get_metrics() if stream_metrics is not None else None,
        )
        return AgentResult(result=None, session_id=None)

async def process_query_stream(
    query_stream: AsyncIterator[Message],
    logger: logging.Logger,
    stream_metrics: Optional[ClaudeCodeSdkAgentMetrics] = None,
    lf_tracer: Optional[Any] = None,
    stream_publisher: Optional[Any] = None,
) -> List[ResultMessage]:
    messages = []
    try:
        async for message in query_stream:
            logger.debug(str(message))
            if stream_metrics is not None:
                stream_metrics.push(message)
            if lf_tracer is not None:
                lf_tracer.push(message)
            if stream_publisher is not None:
                # Best-effort live-tail tap: non-blocking, swallows its own errors,
                # never affects metrics/tracing/result handling below.
                stream_publisher.publish_nowait(message)

            if isinstance(message, ResultMessage):
                messages.append(message)

    except Exception as e:
        error_msg = str(e)
        if messages and messages[-1].is_error and messages[-1].result:
            # The SDK's trailing ProcessError/synthesized exception text can be lossy: when its
            # own `errors` list is empty it falls back to the bare result subtype (e.g. "success"),
            # discarding the actual diagnostic already carried on the ResultMessage we collected
            # above (e.g. "API returned an empty or malformed response"). Prefer that text so
            # classify_error / model-routing-failure fallback downstream sees the real reason.
            error_msg = messages[-1].result
        error_type = classify_error(error_msg)
        if error_type == AgentErrorType.TOOL_CALL_FAILURE:
            logger.error(
                f"[TOOL-CALL FAILURE] Model rejected Anthropic tool-call format. "
                f"This typically happens when a non-Anthropic model (GPT/Gemini) is used via OpenRouter "
                f"and the tool-call translation fails. "
                f"Check that the model supports tool use and that OpenRouter's format translation is working. "
                f"Error: {error_msg}"
            )
        else:
            logger.error(f"Error during query execution: {error_msg}")
        raise Exception(error_msg) from e

    return messages

def validate_models_and_tools(options: ClaudeAgentOptions, logger: logging.Logger):
    """
    Validation of models and tools
    1. Verify that subagent models are the same as parent - log warning if not because this will miss token cache
    2. Verify that tools are allowed for the agent - that tools like Read, Write, Edit, Bash, are not given with full access like "Write", it should be "Write(specflow/**/*)"
    Args:
        options: The options to validate.
    Returns:
        None
    """
    # Collect list of subagents that have different models
    if options.agents:
        subagents_with_different_models = []
        for subagent_name, subagent in options.agents.items():
            if subagent.model != options.model:
                subagents_with_different_models.append(subagent_name)

        if subagents_with_different_models:
            logger.warning(f"Subagents {subagents_with_different_models} have different models from parent model {options.model}")

    if options.allowed_tools:
        for tool in options.allowed_tools:
            if tool in ["Read", "Write", "Edit", "Bash"]:
                logger.warning(f"Tool {tool} is not constrained for the agent - it should be given with limited access like \"Write(specflow/**/*)\"")
    

async def execute_all_phases(
    planning_data: PlanningResult,
    workspace: WorkspaceSettings,
    manager: WorkspaceManager,
    request: GenerateAppRequest,
    logger: logging.Logger,
    start_phase: Optional[int] = None,
    end_phase: Optional[int] = None,
    generation_session_service: Optional[GenerationSessionService] = None,
    workspace_id: Optional[str] = None,
    integration_readiness: SpecReadiness = SpecReadiness.LOCAL_ONLY,
    is_deployment: bool = False,
    phase_prefix: str = "",
    deploy_github_context: Optional[DeployGithubContext] = None,
    enabled_mcps: FrozenSet[str] = frozenset(),
):
    """
    Execute all phases for a workspace, optionally starting from a specific phase.
    
    Args:
        planning_data: Planning result with phase information
        workspace: Workspace settings
        manager: Workspace manager
        request: Generation request
        logger: Logger instance
        start_phase: Optional starting phase number (1-based, inclusive). If None, starts from phase 1.
        end_phase: Optional ending phase number (1-based, inclusive). If None, executes all phases.
        generation_session_service: Optional generation service for checkpoint updates
        workspace_id: Optional workspace ID for checkpoint updates
        integration_readiness: Spec readiness level; controls deploy artifact expectations in prompt.
        is_deployment: If True, uses generate_deploy_phase_agent_template instead of the standard
            phase template, injecting deploy context into the prompt.
        phase_prefix: Log prefix string prepended to phase identifiers (e.g. "deploy-").
        deploy_github_context: DeployGithubContext with github_repo, github_ref, and
            deploy_workflow to inject into the deploy phase prompt. Only used when
            is_deployment=True.
        enabled_mcps: Generation-level subset of SUPPORTED_MCPS for Playwright/Figma agent MCPs.
            Each phase may further restrict via PhaseInfo.applicable_agent_mcps (intersection).

    Returns:
        List of phase results
    """
    all_results = []
    workspace_name = workspace.workspace_path.name if hasattr(workspace.workspace_path, 'name') else str(workspace.workspace_path).split('/')[-1]

    db_adapter = generation_session_service.db_adapter if generation_session_service else None

    # Determine phase range
    phase_start = start_phase if start_phase is not None else 1
    phase_end = end_phase if end_phase is not None else planning_data.phase_count
    
    # Validate range
    if phase_start < 1:
        phase_start = 1
    if phase_end > planning_data.phase_count:
        phase_end = planning_data.phase_count
    if phase_start > phase_end:
        logger.warning(
            f"[{workspace_name}] Invalid phase range: start={phase_start}, end={phase_end}. "
            f"All phases may already be completed."
        )
        return all_results

    logger.info(
        f"[{workspace_name}] Executing phases {phase_start} to {phase_end} "
        f"(out of {planning_data.phase_count} total phases)"
    )

    step = WorkflowStepName.DEPLOY_AND_E2E if is_deployment else WorkflowStepName.GENERATION
    selector = McpSelector(manager.settings, enabled_mcps, logger)

    # Resolve GH_TOKEN once for all deploy phases — same PAT as git clone.
    # Fail early with a clear message rather than letting agents hit auth errors mid-run.
    deploy_extra_env: Optional[Dict[str, str]] = None
    if is_deployment and request.generation_id:
        try:
            deploy_extra_env = github_cli_env_for_generation(
                get_database(), request.generation_id
            )
        except Exception as e:
            logger.error(
                f"[{workspace_name}] Cannot resolve GH_TOKEN for deploy phases "
                f"(generation_id={request.generation_id}): {e}. "
                "Deploy agents will not have gh CLI access — aborting deploy run.",
                exc_info=True,
            )
            raise

    consecutive_errors = 0
    for phase_num in range(phase_start, phase_end + 1):
        # Cooperative cancellation: stop between phases if the user cancelled
        # (cross-pod-safe fallback to the local task.cancel()). raise_if_cancelled
        # no-ops when db_adapter is None (only in DB-less unit tests; every real run
        # wires a generation_session_service).
        await raise_if_cancelled(db_adapter, request.generation_id)

        phase_info = planning_data.phases[phase_num - 1]

        if phase_info.applicable_agent_mcps is None:
            phase_mcps = enabled_mcps
        else:
            phase_mcps = (
                frozenset(phase_info.applicable_agent_mcps) & enabled_mcps & SUPPORTED_MCPS
            )
        phase_mcp = selector.for_step(step, phase_mcps=phase_mcps)

        logger.info(f"[{workspace_name}] === Phase {phase_num}/{planning_data.phase_count}: {phase_info.name} ===")
        TelemetryContext.set_phase_name(phase_info.name or "")

        workspace_root = workspace.get_isolated_root()
        if is_deployment:
            phase_prompt = generate_deploy_phase_agent_template(
                model=workspace.model,
                phase_number=phase_num,
                phase_info=phase_info,
                workspace_root=workspace_root,
                spec_path=request.spec_path,
                outputs_dir=request.outputs_dir,
                integration_readiness=integration_readiness,
                workspace_id=workspace_id,
                generation_id=request.generation_id,
                github_repo=deploy_github_context.github_repo if deploy_github_context else None,
                github_ref=deploy_github_context.github_ref if deploy_github_context else None,
                deploy_workflow=deploy_github_context.deploy_workflow if deploy_github_context else None,
                enabled_mcps=phase_mcps,
            )
        else:
            phase_prompt = generate_phase_agent_template(
                model=workspace.model,
                phase_number=phase_num,
                phase_info=phase_info,
                workspace_root=workspace_root,
                spec_path=request.spec_path,
                outputs_dir=request.outputs_dir,
                integration_readiness=integration_readiness,
                enabled_mcps=phase_mcps,
            )

        phase_kind_label = "deployE2E" if is_deployment else "coding"
        lf_trace_name = f"{workspace_name}:{phase_kind_label}:phase{phase_num}"
        async with tracer.start_workflow_step_trace(
            name=lf_trace_name,
            extra_metadata={
                "phase_num": phase_num,
                "phase_name": phase_info.name or "",
                "is_deployment": is_deployment,
                "workspace_id": workspace_id,
                "workspace_name": workspace_name,
            },
        ):
            phase_result: AgentResult = await phase_agent_fn(
                workspace=workspace,
                manager=manager,
                phase_num=phase_num,
                phase_prompt=phase_prompt,
                request=request,
                log=logger,
                phase_prefix=phase_prefix,
                extra_allowed_tools=DEPLOY_EXTRA_TOOLS if is_deployment else [],
                phase_mcp_servers=phase_mcp.servers or None,
                phase_mcp_tools=phase_mcp.allowed_tools,
                phase_kind=PhaseKind.DEPLOY if is_deployment else PhaseKind.GENERATION,
                extra_env=deploy_extra_env,
                db_adapter=db_adapter,
            )
        TelemetryContext.set_phase_name("")  # clear so validators don't inherit it
        logger.info(f"[{workspace_name}] Phase {phase_num} execution completed")

        # Detect workspace-level abort conditions before checkpointing.
        if phase_result.is_error:
            error_type = classify_error(phase_result.result or "")
            if error_type == AgentErrorType.TOOL_CALL_FAILURE:
                raise WorkspaceAbortedError(
                    f"[{workspace_name}] Phase {phase_num} failed with tool-call incompatibility "
                    f"(model={workspace.model}). Aborting workspace — other workspaces continue. "
                    f"Error: {phase_result.result}"
                )
            if error_type == AgentErrorType.CONNECTION_ERROR:
                raise WorkspaceAbortedError(
                    f"[{workspace_name}] Phase {phase_num} lost connection to the API "
                    f"(model={workspace.model}). Aborting workspace — other workspaces continue. "
                    f"Error: {phase_result.result}"
                )
            consecutive_errors += 1
            logger.error(
                f"[{workspace_name}] Phase {phase_num} reported is_error=True "
                f"({consecutive_errors}/{_MAX_CONSECUTIVE_PHASE_ERRORS} consecutive errors). "
                f"Result: {(phase_result.result or '')[:200]}"
            )
            if consecutive_errors >= _MAX_CONSECUTIVE_PHASE_ERRORS:
                raise WorkspaceAbortedError(
                    f"[{workspace_name}] Aborting after {consecutive_errors} consecutive phase errors "
                    f"(last at phase {phase_num}, model={workspace.model})."
                )
        else:
            consecutive_errors = 0

        # Update checkpoint after each phase completes - CRITICAL for resumption
        if generation_session_service and workspace_id and request.generation_id:
            try:
                phase_kind = "deploy phase" if is_deployment else "phase"
                logger.info(
                    f"[{workspace_name}] Updating checkpoint: workspace_id={workspace_id}, "
                    f"generation_id={request.generation_id}, completed_{phase_kind}={phase_num}"
                )
                if is_deployment:
                    await generation_session_service.update_deployment_workspace_phase(
                        generation_id=request.generation_id,
                        workspace_id=workspace_id,
                        completed_phase=phase_num,
                    )
                else:
                    await generation_session_service.update_workspace_phase(
                        generation_id=request.generation_id,
                        workspace_id=workspace_id,
                        completed_phase=phase_num,
                    )
                logger.info(
                    f"[{workspace_name}] Successfully updated checkpoint to {phase_kind} {phase_num}"
                )
            except Exception as e:
                raise WorkspaceAbortedError(
                    f"[{workspace_name}] Failed to write {phase_kind} {phase_num} checkpoint to Firestore: {e}. "
                    f"Aborting workspace — cannot track progress, retry would resume from wrong phase."
                ) from e
        else:
            logger.warning(
                f"[{workspace_name}] Cannot update checkpoint: "
                f"generation_session_service={generation_session_service is not None}, "
                f"workspace_id={workspace_id}, "
                f"generation_id={request.generation_id if request else None}"
            )
                
        all_results.append({
            "phase": phase_num,
            "phase_name": phase_info.name,
            "results": phase_result
        })
    
    # Only run git commits janitor if all phases are complete
    if phase_end == planning_data.phase_count:
        logger.info(f"[{workspace_name}] All phases completed, running git commits janitor")
        
        # Commit any uncommitted agent work and push so origin/main is up to date
        # before archiving. This replaces the old LLM janitor agent.
        if is_skip_mode_enabled():
            logger.warning(f"[{workspace_name}] SKIP_MODE enabled - setting up mock commits")
            success = setup_skip_mode_workspace_commits(
                workspace_path=workspace.get_isolated_root(),
                outputs_dir=request.outputs_dir,
                logger=logger,
            )
            if not success:
                logger.error(f"[{workspace_name}] Failed to set up mock commits")
        else:
            logger.info(f"[{workspace_name}] Committing any outstanding agent work")
            await manager.commit_and_push_outstanding(
                workspace,
                commit_message="SKIP_janitor_finalize",
            )
    else:
        logger.info(f"[{workspace_name}] Phases {phase_start}-{phase_end} completed, more phases remaining")

    return all_results

async def _apply_model_override_if_switched(
    result: AgentResult,
    workspace: WorkspaceSettings,
    workspace_model: str,
    request: GenerateAppRequest,
    db_adapter: Optional[StateMachineDBAdapter],
    logger: logging.Logger,
) -> None:
    if not result.active_model or result.active_model == workspace_model:
        return
    workspace.model = result.active_model
    logger.warning(
        "[MODEL FALLBACK] Persisting model switch %s -> %s for workspace %s"
        " — all remaining phases on this workspace will also use %s",
        workspace_model, result.active_model, workspace.name, result.active_model,
    )
    if db_adapter is None:
        # In production db_adapter is always set (generate_app_workflow requires
        # generation_session_service). None only occurs in tests or one-off callers
        # that don't pass generation_session_service — warn so it's visible in logs.
        logger.warning(
            "[MODEL FALLBACK] db_adapter unavailable — model override %s -> %s for workspace %s"
            " is in-memory only; crash-retry will reuse the original model",
            workspace_model, result.active_model, workspace.name,
        )
        return
    try:
        await set_workspace_model_override(
            generation_id=request.generation_id,
            workspace_id=workspace.name,
            model=result.active_model,
            triggered_by=TriggeredBy.MODEL_FALLBACK,
            db=db_adapter,
        )
    except Exception as persist_err:
        # ERROR (not warning): if Firestore is unavailable, the override is not
        # durable — a crash-retry would re-assign the broken model with no
        # recovery. The in-memory workspace.model update above still protects
        # the current run, but this needs operator attention.
        logger.error(
            "[MODEL FALLBACK] Failed to persist model override to Firestore"
            " — crash-retry may reuse broken model %s for workspace %s: %s",
            workspace_model, workspace.name, persist_err,
            exc_info=True,
        )


async def phase_agent_fn(
    workspace: WorkspaceSettings,
    manager: WorkspaceManager,
    phase_num: int,
    phase_prompt: str,
    request: GenerateAppRequest,
    log: logging.Logger,
    phase_prefix: str = "",
    extra_allowed_tools: Optional[List[str]] = None,
    phase_mcp_servers: Optional[Dict] = None,
    phase_mcp_tools: Optional[List[str]] = None,
    phase_kind: PhaseKind = PhaseKind.GENERATION,
    extra_env: Optional[Dict[str, str]] = None,
    db_adapter=None,
):
            workspace_name = workspace.workspace_path.name if hasattr(workspace.workspace_path, 'name') else str(workspace.workspace_path).split('/')[-1]
            agent_logger = create_agent_logger(f"{workspace_name}-{phase_prefix}phase{phase_num}", generation_id=request.generation_id)
            TelemetryContext.set_workspace_name(workspace_name)

            isolated_root = workspace.get_isolated_root()

            # Check if SKIP_MODE is enabled - if so, skip phase execution
            if is_skip_mode_enabled():
                agent_logger.warning(f"[SKIP_MODE] Phase {phase_num} execution skipped for workspace {workspace_name}")
                await persist_skip_mode_mock_agent_query_totals(
                    model=workspace.model,
                    workspace_path=isolated_root,
                    logger=agent_logger,
                    workflow=TelemetryWorkflowLabel.phase(phase_kind, phase_num, "coding"),
                )
                return AgentResult(result=f"[SKIP_MODE] Phase {phase_num} skipped", session_id=None)

            provider_instance = manager.get_provider(workspace)
            workspace_model = workspace.model

            # Workflow telemetry is set per sub-call inside agent_query_with_resume (coding / validator / resume).

            try:
                mcp_tool_list = phase_mcp_tools or []
                common_tools = get_common_allowed_tools(isolated_root)
                base_phase_tools = common_tools + (extra_allowed_tools or []) + mcp_tool_list
                # By design there is no programmatic coding subagent: Rosetta is always-on
                # (the plugin is baked into the image and provisioned into every workspace, or
                # the live MCP is enabled), so the phase agent delegates to the Rosetta agents
                # (engineer/architect/reviewer/...) in .claude/agents/, discovered via
                # setting_sources=["project"]. See WorkspaceManager.provision_rosetta_plugin and
                # the coding-flow guidance in generate_production_agent_template. If Rosetta is
                # unavailable (KB DISABLED, or a non-fatal KB-init failure), the phase prompt's
                # explicit fallback has the agent implement the phase directly.
                result: AgentResult = await agent_query_with_resume(
                    system_prompt=phase_prompt,
                    phase_number=phase_num,
                    phase_kind=phase_kind,
                    model=workspace_model,
                    workspace_path=isolated_root,
                    outputs_dir=request.outputs_dir,
                    spec_path=request.spec_path,
                    session_id=None,  # Fresh session for each phase
                    logger=agent_logger,
                    max_buffer_size=1024 * 1024 * 100,
                    allowed_tools=base_phase_tools,
                    max_resume_attempts=2,  # Each phase can have 1 resume attempt - QA + 1 retry of phase + validator, and second try if something failed but shouldnt get here unless something crashed
                    max_turns=300,
                    provider=provider_instance,
                    mcp_servers=phase_mcp_servers,
                    extra_env=extra_env,
                )
                await _apply_model_override_if_switched(
                    result=result,
                    workspace=workspace,
                    workspace_model=workspace_model,
                    request=request,
                    db_adapter=db_adapter,
                    logger=agent_logger,
                )
                return result
            except Exception as e:
                log.error(
                    f"Error executing phase {phase_num} for workspace {workspace.workspace_path}:\n"
                    f"Error type: {type(e).__name__}\n"
                    f"Error message: {str(e)}\n"
                    f"Provider: {workspace.provider}\n"
                    f"Model: {workspace.model}",
                    exc_info=True
                )
                if hasattr(e, '__cause__') and e.__cause__:
                    log.error(f"Underlying cause: {e.__cause__}", exc_info=e.__cause__)
                if hasattr(e, 'stderr'):
                    log.error(f"stderr output:\n{e.stderr}")
                return AgentResult(result=str(e), session_id=None, is_error=True)

async def planning_agent_fn(
    primary_workspace: WorkspaceSettings,
    provider_instance: BaseProvider,
    request: GenerateAppRequest,
    logger: logging.Logger,
    model: Optional[str] = None,
    mcp_servers: Optional[Dict] = None,
    mcp_allowed_tools: Optional[List[str]] = None,
    enabled_mcps: FrozenSet[str] = frozenset(),
):
    logger.info("=== Step 0: Planning ===")
    logger.info(f"Running planning step in workspace: {primary_workspace.workspace_path}")

    workspace_name = (
        primary_workspace.workspace_path.name
        if hasattr(primary_workspace.workspace_path, "name")
        else str(primary_workspace.workspace_path).split("/")[-1]
    )
    TelemetryContext.set_workspace_name(workspace_name)

    # Check if SKIP_MODE is enabled - if so, generate mock side effects
    if is_skip_mode_enabled():
        logger.warning("[SKIP_MODE] Planning agent execution skipped - generating mock side effects")

        workspace_root = primary_workspace.get_isolated_root()
        # Create markdown file only; JSON will be created by REPARSE_PLAN conversion agent
        # Pass full path so mock file is created in the workspace, not current directory
        full_outputs_path = str(Path(workspace_root) / request.outputs_dir)
        generate_mock_implementation_plan(
            outputs_dir=full_outputs_path,
            phase_count=get_mock_phase_count(),
            logger=logger
        )

        logger.info(
            "[SKIP_MODE] Created stub IMPLEMENTATION_PLAN.md in %s — returning None for REPARSE_PLAN to handle",
            full_outputs_path
        )
        await persist_skip_mode_mock_agent_query_totals(
            model=model or primary_workspace.model,
            workspace_path=primary_workspace.get_isolated_root(),
            logger=logger,
            workflow=TelemetryWorkflowLabel.plain(WorkflowName.PLANNING),
        )
        return None

    # Normal execution path
    planning_logger = create_agent_logger(f"{workspace_name}-planning", generation_id=request.generation_id)
    planning_model = model or primary_workspace.model

    planning_prompt = generate_planning_agent_template(
        model=planning_model,
        spec_path=request.spec_path,
        outputs_dir=request.outputs_dir,
        enabled_mcps=enabled_mcps,
    )
    
    # Set workflow context for telemetry
    TelemetryContext.set_workflow(TelemetryWorkflowLabel.plain(WorkflowName.PLANNING))

    mcp_tools = mcp_allowed_tools or []

    planning_result: AgentResult = await agent_query(
        system_prompt=planning_prompt,
        model=planning_model,
        workspace_path=primary_workspace.get_isolated_root(),
        logger=planning_logger,
        max_buffer_size=1024 * 1024 * 100,
        allowed_tools=(
            get_common_allowed_tools(primary_workspace.get_isolated_root())
            + get_workspace_rm_bash_allowlist(primary_workspace.get_isolated_root())
            + mcp_tools
        ),
        max_turns=100,
        provider=provider_instance,
        mcp_servers=mcp_servers,
    )

    if planning_result.result is None:
        raise RuntimeError("Planning agent returned no result")

    # Planning agent writes markdown only; conversion agent (REPARSE_PLAN step) produces JSON.
    logger.info("Planning agent complete — conversion agent will produce JSON from markdown")
    return None
