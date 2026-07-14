"""
Parallel execution helper for running multiple agents across different workspaces.
"""

import asyncio
from dataclasses import dataclass
import logging
from typing import Awaitable, Callable, List, Optional

from app.schemas.agent import AgentErrorType, AgentResult
from app.schemas.workspace import WorkspaceSettings
from app.services.claude_code import agent_query
from app.services.model_routing import classify_error
from app.services.workspace_manager import WorkspaceManager
from app.state.exceptions import GenerationCancelledError


@dataclass
class ParallelAgentResult:
    """Result from parallel agent execution."""
    workspace_name: str
    workspace_settings: WorkspaceSettings
    result: Optional[AgentResult]
    success: bool
    error: Optional[str] = None
    duration_ms: Optional[float] = None


@dataclass
class ParallelGenerationResult:
    """Result from parallel generation execution."""
    workspace_name: str
    workspace_settings: WorkspaceSettings
    estimation: Optional[any]  # WorkspaceEstimation type (avoiding circular import)
    success: bool
    error: Optional[str] = None
    duration_ms: Optional[float] = None
    warning: Optional[str] = None  # For non-fatal issues like missing commits


class ParallelAgentExecutor:
    """
    Executes agents in parallel across multiple workspaces.
    
    Each workspace can have different:
    - Provider (Anthropic, OpenRouter, etc.)
    - Model (Claude, Gemini, GPT-4, etc.)
    - Configuration (API keys, base URLs, etc.)
    """
    
    def __init__(self, workspace_manager: WorkspaceManager, logger: Optional[logging.Logger] = None):
        """
        Initialize parallel executor.
        
        Args:
            workspace_manager: WorkspaceManager instance
            logger: Optional logger instance
        """
        self.workspace_manager = workspace_manager
        self.logger = logger or logging.getLogger(__name__)
    
    async def execute_parallel(
        self,
        workspaces: List[WorkspaceSettings],
        agent_fn: Callable[[WorkspaceSettings, WorkspaceManager, logging.Logger], Awaitable[AgentResult]],
        workspace_names: Optional[List[str]] = None,
    ) -> List[ParallelAgentResult]:
        """
        Execute agent function in parallel across multiple workspaces.
        
        Args:
            workspaces: List of workspace settings
            agent_fn: Async function that takes (workspace_settings, workspace_manager, logger)
            workspace_names: Optional list of names for workspaces (for logging)
            
        Returns:
            List of ParallelAgentResult instances
        """
        if workspace_names is None:
            workspace_names = [f"workspace-{i}" for i in range(1, len(workspaces) + 1)]
        
        self.logger.info(f"Starting parallel execution across {len(workspaces)} workspaces")
        
        tasks = []
        for i, (workspace, name) in enumerate(zip(workspaces, workspace_names)):
            task = self._execute_single(workspace, name, agent_fn)
            tasks.append(task)
        
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # return_exceptions=True absorbs a cooperative cancellation into the results
        # list — surface it so the entire generation aborts (session already CANCELLED).
        for r in results:
            if isinstance(r, GenerationCancelledError):
                raise r

        # Count successes and failures
        successes = sum(1 for r in results if isinstance(r, ParallelAgentResult) and r.success)
        failures = len(results) - successes
        
        self.logger.info(
            f"Parallel execution complete: {successes} succeeded, {failures} failed"
        )
        
        return [r if isinstance(r, ParallelAgentResult) else 
                ParallelAgentResult(
                    workspace_name=f"workspace-{i}",
                    workspace_settings=workspaces[i],
                    result=None,
                    success=False,
                    error=str(r)
                )
                for i, r in enumerate(results)]
    
    async def _execute_single(
        self,
        workspace: WorkspaceSettings,
        name: str,
        agent_fn: Callable[[WorkspaceSettings, WorkspaceManager, logging.Logger], Awaitable[AgentResult]],
    ) -> ParallelAgentResult:
        """
        Execute agent function for a single workspace.
        
        Args:
            workspace: Workspace settings
            name: Workspace name for logging
            agent_fn: Agent function to execute
            
        Returns:
            ParallelAgentResult instance
        """
        import time
        start_time = time.time()
        
        try:
            self.logger.info(
                f"[{name}] Starting execution with provider={workspace.provider}, "
                f"model={workspace.model}"
            )
            
            result = await agent_fn(workspace, self.workspace_manager, self.logger)
            
            duration_ms = (time.time() - start_time) * 1000
            
            self.logger.info(
                f"[{name}] Execution completed successfully in {duration_ms:.0f}ms"
            )
            
            return ParallelAgentResult(
                workspace_name=name,
                workspace_settings=workspace,
                result=result,
                success=True,
                duration_ms=duration_ms
            )

        except GenerationCancelledError:
            # User cancellation must abort the whole fan-out, not degrade to a
            # per-workspace failure result. Re-raise so execute_parallel propagates it.
            raise

        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            error_msg = str(e)
            error_type = classify_error(error_msg)

            if error_type == AgentErrorType.TOOL_CALL_FAILURE:
                self.logger.error(
                    f"[{name}] [TOOL-CALL FAILURE] Workspace failed due to tool-call incompatibility "
                    f"(model={workspace.model}, provider={workspace.provider}). "
                    f"Other workspaces will continue. "
                    f"Error: {error_msg}",
                    exc_info=True,
                )
            else:
                self.logger.error(
                    f"[{name}] Execution failed after {duration_ms:.0f}ms: {error_msg}",
                    exc_info=True,
                )

            return ParallelAgentResult(
                workspace_name=name,
                workspace_settings=workspace,
                result=None,
                success=False,
                error=str(e),
                duration_ms=duration_ms
            )


# Helper function for common use case
async def execute_query_parallel(
    workspaces: List[WorkspaceSettings],
    workspace_manager: WorkspaceManager,
    system_prompt: str,
    logger: logging.Logger,
    **kwargs
) -> List[ParallelAgentResult]:
    """
    Execute agent_query in parallel across multiple workspaces.
    
    This is a convenience function for the common case of running
    the same query across multiple workspaces with different providers/models.
    
    Args:
        workspaces: List of workspace settings
        workspace_manager: WorkspaceManager instance
        system_prompt: System prompt for all agents
        logger: Logger instance
        **kwargs: Additional arguments to pass to agent_query
        
    Returns:
        List of ParallelAgentResult instances
        
    Example:
        >>> workspaces = [
        ...     WorkspaceSettings(name="ws1", workspace_path="ws1", provider="anthropic", model="claude-sonnet-4-0"),
        ...     WorkspaceSettings(name="ws2", workspace_path="ws2", provider="openrouter", model="gemini-2.5-pro"),
        ...     WorkspaceSettings(name="ws3", workspace_path="ws3", provider="openrouter", model="gpt-4o"),
        ... ]
        >>> results = await execute_query_parallel(
        ...     workspaces=workspaces,
        ...     workspace_manager=manager,
        ...     system_prompt="Analyze this code",
        ...     logger=logger
        ... )
    """
    executor = ParallelAgentExecutor(workspace_manager, logger)
    
    async def agent_fn(workspace: WorkspaceSettings, manager: WorkspaceManager, log: logging.Logger):
        # Get provider instance for this workspace
        provider_instance = manager.get_provider(workspace)
        try:
            result: AgentResult =  await agent_query(
                system_prompt=system_prompt,
                workspace_path=str(workspace.full_workspace_path),
                provider=provider_instance,
                model=workspace.model,  # Use workspace-specific model
                logger=log,
                **kwargs
            )
            return result.result
        except Exception as e:
            # Log full error details including stack trace
            log.error(
                f"Error executing agent query for workspace {workspace.workspace_path}:\n"
                f"Error type: {type(e).__name__}\n"
                f"Error message: {str(e)}\n"
                f"Provider: {workspace.provider}\n"
                f"Model: {workspace.model}",
                exc_info=True  # This captures the full stack trace
            )
            # Log stderr if available in the exception
            if hasattr(e, '__cause__') and e.__cause__:
                log.error(f"Underlying cause: {e.__cause__}", exc_info=e.__cause__)
            if hasattr(e, 'stderr'):
                log.error(f"stderr output:\n{e.stderr}")
            raise  # Re-raise to let the outer handler capture it properly
    
    return await executor.execute_parallel(
        workspaces=workspaces,
        agent_fn=agent_fn
    )


# Generation-specific parallel execution
async def execute_generation_parallel(
    workspaces: List[WorkspaceSettings],
    workspace_manager: WorkspaceManager,
    generation_fn: Callable,
    generation_args: dict,
    logger: logging.Logger,
) -> List[ParallelGenerationResult]:
    """
    Execute generation function in parallel across multiple workspaces.
    
    This function handles generation-specific requirements:
    - Graceful handling of missing commit files
    - Workspace-specific P10Y configuration
    - Better error messages for generation failures
    
    Args:
        workspaces: List of workspace settings
        workspace_manager: WorkspaceManager instance
        generation_fn: Generation function that takes (workspace, **kwargs) and returns WorkspaceEstimation | None
        generation_args: Dict of arguments to pass to generation_fn (e.g., request, client, settings, logger)
        logger: Logger instance
        
    Returns:
        List of ParallelGenerationResult instances
        
    Example:
        >>> results = await execute_generation_parallel(
        ...     workspaces=workspaces,
        ...     workspace_manager=manager,
        ...     generation_fn=_process_single_workspace,
        ...     generation_args={"request": request, "client": client, "settings": settings, "logger": logger},
        ...     logger=logger
        ... )
    """
    import time
    
    logger.info(f"Starting parallel generation across {len(workspaces)} workspaces")
    
    async def process_workspace(workspace: WorkspaceSettings) -> ParallelGenerationResult:
        """Process generation for a single workspace."""
        start_time = time.time()
        workspace_name = workspace.name or str(workspace.workspace_path)
        
        try:
            logger.info(
                f"[{workspace_name}] Starting generation with provider={workspace.provider}, "
                f"model={workspace.model}"
            )
            
            # Check if workspace has P10Y configuration
            if not workspace.p10y_repository_id:
                warning_msg = f"No P10Y repository ID configured for workspace {workspace_name}"
                logger.warning(f"[{workspace_name}] {warning_msg}")
                duration_ms = (time.time() - start_time) * 1000
                return ParallelGenerationResult(
                    workspace_name=workspace_name,
                    workspace_settings=workspace,
                    estimation=None,
                    success=False,
                    warning=warning_msg,
                    duration_ms=duration_ms
                )
            
            # Call the generation function
            generation_result = await generation_fn(workspace=workspace, **generation_args)
            
            duration_ms = (time.time() - start_time) * 1000
            
            if generation_result is None:
                # Generation returned None - likely due to missing commits
                warning_msg = "No estimation generated (possibly missing commits)"
                logger.warning(f"[{workspace_name}] {warning_msg}")
                return ParallelGenerationResult(
                    workspace_name=workspace_name,
                    workspace_settings=workspace,
                    estimation=None,
                    success=False,
                    warning=warning_msg,
                    duration_ms=duration_ms
                )
            
            logger.info(
                f"[{workspace_name}] Generation completed successfully in {duration_ms:.0f}ms: "
                f"{generation_result.total_hours:.1f} hours"
            )
            
            return ParallelGenerationResult(
                workspace_name=workspace_name,
                workspace_settings=workspace,
                estimation=generation_result,
                success=True,
                duration_ms=duration_ms
            )
            
        except FileNotFoundError as e:
            # Handle missing commit files gracefully
            duration_ms = (time.time() - start_time) * 1000
            error_msg = f"Missing required files: {e}"
            logger.warning(
                f"[{workspace_name}] {error_msg}",
                exc_info=False  # Don't need full stack trace for missing files
            )
            return ParallelGenerationResult(
                workspace_name=workspace_name,
                workspace_settings=workspace,
                estimation=None,
                success=False,
                error=error_msg,
                warning="Commit files not found - workspace may not have completed code generation",
                duration_ms=duration_ms
            )

        except GenerationCancelledError:
            # User cancellation aborts the whole estimation fan-out (gather uses
            # return_exceptions=False, so this propagates out immediately).
            raise

        except Exception as e:
            # Handle other errors
            duration_ms = (time.time() - start_time) * 1000
            error_msg = str(e)
            logger.error(
                f"[{workspace_name}] Generation failed after {duration_ms:.0f}ms: {error_msg}",
                exc_info=True
            )
            return ParallelGenerationResult(
                workspace_name=workspace_name,
                workspace_settings=workspace,
                estimation=None,
                success=False,
                error=error_msg,
                duration_ms=duration_ms
            )
    
    # Execute all generations in parallel
    tasks = [process_workspace(ws) for ws in workspaces]
    results = await asyncio.gather(*tasks, return_exceptions=False)
    
    # Count successes and failures
    successes = sum(1 for r in results if r.success)
    failures = len(results) - successes
    warnings = sum(1 for r in results if r.warning and not r.success)
    
    logger.info(
        f"Parallel generation complete: {successes} succeeded, {failures} failed "
        f"({warnings} with warnings)"
    )
    
    # Log summary of each result
    for result in results:
        if result.success:
            logger.info(f"  ✓ {result.workspace_name}: {result.estimation.total_hours:.1f} hours")
        elif result.warning:
            logger.warning(f"  ⚠ {result.workspace_name}: {result.warning}")
        else:
            logger.error(f"  ✗ {result.workspace_name}: {result.error}")
    
    return results
