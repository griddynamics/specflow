"""Workflow execution metrics collection using context variables."""

import json
import logging
from dataclasses import asdict
from contextvars import ContextVar
from functools import wraps
from typing import Any, Callable, Dict, Optional, Tuple

from app.schemas.model_token_usage import FLAT_AGGREGATE_MODEL_NAME, ModelTokenUsage
from app.schemas.workflow_stats import AgentQueryMetrics, WorkflowExecutionStats

# Use main logger configuration (configured via configure_logging() in main.py)
logger = logging.getLogger("api.workflow_stats")

# Context variable - each async task gets its own instance
_workflow_stats: ContextVar[Optional[WorkflowExecutionStats]] = ContextVar(
    'workflow_stats', default=None
)


def get_workflow_stats() -> Optional[WorkflowExecutionStats]:
    """Get current workflow stats from context. Never raises."""
    try:
        return _workflow_stats.get()
    except Exception as e:
        logger.warning(f"Failed to get workflow stats from context: {e}", exc_info=True)
        return None


def set_workflow_stats(stats: Optional[WorkflowExecutionStats]) -> None:
    """Set workflow stats in current context. Never raises."""
    try:
        _workflow_stats.set(stats)
    except Exception as e:
        logger.warning(f"Failed to set workflow stats in context: {e}", exc_info=True)


def record_agent_query_metrics(metrics: AgentQueryMetrics) -> None:
    """Record metrics to current workflow stats. Never raises."""
    try:
        stats = get_workflow_stats()
        if stats:
            stats.add_query(metrics)
    except Exception as e:
        logger.warning(f"Failed to record agent query metrics: {e}", exc_info=True)


def workflow_metrics(
    workflow_name: str,
    add_to_result: bool = False,
    workspace_name: Optional[str] = None
):
    """
    Decorator to collect workflow execution metrics.
    
    Args:
        workflow_name: Name of the workflow for identification
        add_to_result: If True, add stats to return value. If False, stats available via context.
        workspace_name: Optional workspace name (for nested workflows)
    
    Usage:
        @workflow_metrics("spec_analysis")
        async def spec_analysis_workflow(...):
            ...
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> Any:
            # Create stats object
            stats = None
            try:
                stats = WorkflowExecutionStats(
                    workflow_name=workflow_name,
                    workspace_name=workspace_name
                )
                set_workflow_stats(stats)
            except Exception as e:
                logger.warning(
                    f"Failed to initialize workflow stats for {workflow_name}: {e}",
                    exc_info=True
                )
                # Continue without stats - call function normally
                return await func(*args, **kwargs)
            
            # Execute workflow
            try:
                result = await func(*args, **kwargs)
                
                # Mark stats as completed
                if stats:
                    try:
                        stats.mark_completed()
                        # Log workflow stats
                        try:
                            stats_dict = stats.to_dict()
                            logger.info(
                                f"Workflow metrics for '{workflow_name}': {json.dumps(stats_dict, indent=2)}"
                            )
                        except Exception as e:
                            logger.warning(
                                f"Failed to log workflow stats: {e}",
                                exc_info=True
                            )
                    except Exception as e:
                        logger.warning(
                            f"Failed to mark workflow stats as completed: {e}",
                            exc_info=True
                        )
                
                # Add stats to result if requested
                if add_to_result and stats:
                    try:
                        result = _add_stats_to_result(result, stats)
                    except Exception as e:
                        logger.warning(
                            f"Failed to add stats to result: {e}",
                            exc_info=True
                        )
                        # Return original result if stats addition fails
                
                return result
                
            except Exception as workflow_error:
                # Workflow failed - still try to mark stats (but don't fail if it does)
                if stats:
                    try:
                        stats.mark_completed()
                        # Log workflow stats even on error
                        try:
                            stats_dict = stats.to_dict()
                            logger.info(
                                f"Workflow metrics for '{workflow_name}' (failed): {json.dumps(stats_dict, indent=2)}"
                            )
                        except Exception as e:
                            logger.warning(
                                f"Failed to log workflow stats after error: {e}",
                                exc_info=True
                            )
                    except Exception as e:
                        logger.warning(
                            f"Failed to mark workflow stats after error: {e}",
                            exc_info=True
                        )
                # Re-raise original error
                raise workflow_error
            finally:
                # Clean up context
                try:
                    set_workflow_stats(None)
                except Exception as e:
                    logger.warning(
                        f"Failed to clean up workflow stats context: {e}",
                        exc_info=True
                    )
        
        return wrapper
    return decorator


def _add_stats_to_result(result: Any, stats: WorkflowExecutionStats) -> Any:
    """
    Add stats to result in a non-breaking way.
    
    Strategy:
    1. If result is a dict, add 'stats' key
    2. If result is a Pydantic model, try to add stats attribute or wrap in dict
    3. If result is a dataclass, try to add stats attribute
    4. Otherwise, wrap in dict
    """
    try:
        stats_dict = stats.to_dict()
    except Exception as e:
        logger.warning(f"Failed to serialize stats: {e}", exc_info=True)
        return result  # Return original if serialization fails
    
    # Case 1: Result is already a dict
    if isinstance(result, dict):
        result["stats"] = stats_dict
        return result
    
    # Case 2: Result is a Pydantic model
    # Check if it's a Pydantic v2 model (has model_dump) or v1 (has dict)
    if hasattr(result, "__class__"):
        # Pydantic v2
        if hasattr(result, "model_dump"):
            try:
                # Try to add stats as a field (if model allows extra fields)
                if hasattr(result, "model_config") and hasattr(result.model_config, "get"):
                    if result.model_config.get("extra") == "allow":
                        # Try to set attribute directly
                        try:
                            result.stats = stats_dict
                            return result
                        except Exception:
                            pass
                # Otherwise, create a new dict with the model's data + stats
                result_dict = result.model_dump()
                result_dict["stats"] = stats_dict
                return result_dict
            except Exception as e:
                logger.warning(f"Failed to add stats to Pydantic v2 model: {e}", exc_info=True)
                # Fall through to wrapping
        # Pydantic v1
        elif hasattr(result, "dict"):
            try:
                # Try to add stats as a field (if model allows extra fields)
                if hasattr(result, "__config__") and hasattr(result.__config__, "extra"):
                    if result.__config__.extra == "allow":
                        try:
                            result.stats = stats_dict
                            return result
                        except Exception:
                            pass
                # Otherwise, create a new dict with the model's data + stats
                result_dict = result.dict()
                result_dict["stats"] = stats_dict
                return result_dict
            except Exception as e:
                logger.warning(f"Failed to add stats to Pydantic v1 model: {e}", exc_info=True)
                # Fall through to wrapping
    
    # Case 3: Result is a dataclass
    if hasattr(result, "__dataclass_fields__"):
        try:
            # Convert dataclass to dict and add stats
            result_dict = asdict(result)
            result_dict["stats"] = stats_dict
            return result_dict
        except Exception as e:
            logger.warning(f"Failed to convert dataclass to dict: {e}", exc_info=True)
            # Fall through to wrapping
    
    # Case 4: Wrap in dict (safest fallback)
    return {
        "result": result,
        "stats": stats_dict
    }


def extract_stats_from_result(result: Any) -> Tuple[Any, Optional[dict]]:
    """
    Extract stats from workflow result safely.
    
    The workflow_metrics decorator may:
    1. Add stats as a key in dict: {"result": ..., "stats": {...}}
    2. Add stats as an attribute: result.stats
    3. Wrap result in dict: {"result": result, "stats": {...}}
    
    Args:
        result: The workflow result (could be dict, Pydantic model, dataclass, etc.)
    
    Returns:
        Tuple of (cleaned_result, stats_dict) where stats_dict is None if not found
    """
    stats = None
    cleaned_result = result
    
    try:
        # Case 1: Result is a dict with "stats" key
        if isinstance(result, dict):
            if "stats" in result:
                stats = result.get("stats")
                # Extract the actual result (could be "result" or "results" key)
                cleaned_result = result.get("result") or result.get("results") or result
                # If we extracted a different key, remove stats from cleaned_result if it's still a dict
                if isinstance(cleaned_result, dict) and cleaned_result is not result:
                    # Don't modify the original, but stats is already extracted
                    pass
        
        # Case 2: Result has stats attribute (Pydantic model, dataclass, etc.)
        elif hasattr(result, "stats"):
            try:
                stats = result.stats
                # Keep the original result (stats is just an attribute)
                cleaned_result = result
            except Exception:
                pass
        
        # Case 3: Result is wrapped in dict with "result" key (decorator fallback)
        # This is already handled in Case 1
        
    except Exception as e:
        logger.warning(f"Failed to extract stats from result: {e}", exc_info=True)
        # Return original result if extraction fails
        cleaned_result = result
        stats = None
    
    return cleaned_result, stats


def extract_workspace_codegen_usage(result: Any) -> Dict[str, ModelTokenUsage]:
    """
    Extract per-workspace cumulative LLM usage from a generate_app workflow result with stats.

    Aggregates each workspace's agent_query rows (turns + four token buckets).
    Keys match ``WorkspaceSettings.name`` (workspace id), e.g. ``ws-01-1``.
    """
    _, stats = extract_stats_from_result(result)
    out: Dict[str, ModelTokenUsage] = {}
    if not stats or not isinstance(stats, dict):
        return out
    workspaces = stats.get("workspaces", {})
    for ws_name, ws_stats in workspaces.items():
        if not isinstance(ws_stats, dict):
            continue
        acc = ModelTokenUsage(model_name=FLAT_AGGREGATE_MODEL_NAME)
        for q in ws_stats.get("queries", []):
            if not isinstance(q, dict):
                continue
            usage = q.get("usage") or {}
            nt = int(q.get("num_turns") or 0)
            acc = acc + ModelTokenUsage.from_sdk_usage(usage, nt)
        if not acc.is_empty():
            out[ws_name] = acc
    return out


def build_response_with_stats(result, result_key: str = "result"):
    """
    Extract stats from workflow result and build response with stats included.
    
    Args:
        result: The workflow result (could be dict, Pydantic model, dataclass, etc.)
        result_key: Key to use when wrapping non-dict results (default: "result", use "results" for lists)
    
    Returns:
        Response dict or object with stats included if available
    """
    cleaned_result, stats = extract_stats_from_result(result)
    
    # Include stats in response if available
    if stats:
        if isinstance(cleaned_result, dict):
            cleaned_result["stats"] = stats
            return cleaned_result
        else:
            # Wrap in dict if result is not a dict
            return {
                result_key: cleaned_result,
                "stats": stats
            }
    logger.info(f"Workflow stats: {stats}")
    return cleaned_result