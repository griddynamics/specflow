"""
Telemetry decorator for tracking API endpoint events.

Provides a non-invasive decorator pattern for adding PostHog telemetry
to FastAPI endpoints with minimal code changes.
"""

import logging
import inspect
from functools import wraps
from typing import Callable, Any, Optional, Dict
from datetime import datetime, timezone

from fastapi import Request

from app.core.telemetry_context import TelemetryContext
from app.services.telemetry import telemetry

logger = logging.getLogger(__name__)


def extract_user_context(request: Optional[Request]) -> tuple[Optional[str], Optional[Dict[str, Any]]]:
    """
    Extract user context from FastAPI request.
    
    Args:
        request: FastAPI Request object
        
    Returns:
        Tuple of (user_email, user_properties)
    """
    if not request:
        return None, None
    
    user_email = getattr(request.state, "user_email", None)
    user_name = getattr(request.state, "user_name", None)
    
    user_properties = {}
    if user_name:
        user_properties["user_name"] = user_name
    
    return user_email, user_properties


def extract_event_properties(
    func: Callable,
    args: tuple,
    kwargs: dict,
    request: Optional[Request]
) -> Dict[str, Any]:
    """
    Extract event properties from function arguments and request.
    
    Args:
        func: The decorated function
        args: Function positional arguments
        kwargs: Function keyword arguments
        request: FastAPI Request object
        
    Returns:
        Dictionary of event properties
    """
    properties: Dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    
    # Extract endpoint information from request
    if request:
        properties["endpoint"] = request.url.path
        properties["http_method"] = request.method
        
        # Try to extract generation_id from path parameters
        # FastAPI path params are available in request.path_params
        if hasattr(request, "path_params") and "generation_id" in request.path_params:
            properties["generation_id"] = request.path_params["generation_id"]
    
    # Get function parameter names and match args to parameters
    sig = inspect.signature(func)
    param_names = list(sig.parameters.keys())
    
    # Match positional args to parameter names
    bound_args = {}
    for i, arg_value in enumerate(args):
        if i < len(param_names):
            param_name = param_names[i]
            # Skip 'self' and other special parameters
            if param_name not in ['self', 'cls']:
                bound_args[param_name] = arg_value
    
    # Merge bound args with kwargs (kwargs take precedence)
    all_params = {**bound_args, **kwargs}
    
    # Extract common parameters
    if "generation_id" in all_params:
        properties["generation_id"] = all_params["generation_id"]
    
    if "spec_path" in all_params:
        properties["spec_path"] = all_params["spec_path"]
    
    if "outputs_dir" in all_params:
        properties["outputs_dir"] = all_params["outputs_dir"]
    
    if "session_id" in all_params and all_params["session_id"]:
        properties["session_id"] = all_params["session_id"]
    
    if "max_retries" in all_params:
        properties["max_retries"] = all_params["max_retries"]
    
    # For sync endpoint, try to extract generation_id from params JSON
    if "params" in all_params and all_params["params"]:
        try:
            import json
            params_dict = json.loads(all_params["params"])
            if "generation_id" in params_dict:
                properties["generation_id"] = params_dict["generation_id"]
            if "spec_path" in params_dict:
                properties["spec_path"] = params_dict["spec_path"]
            if "workflow_type" in params_dict:
                properties["workflow_type"] = params_dict["workflow_type"]
        except (json.JSONDecodeError, TypeError):
            pass
    
    if "sync_to_all" in all_params:
        properties["sync_to_all"] = all_params["sync_to_all"]
    
    return properties


def find_request_in_args(args: tuple, kwargs: dict) -> Optional[Request]:
    """
    Find Request object in function arguments.
    
    Args:
        args: Function positional arguments
        kwargs: Function keyword arguments
        
    Returns:
        Request object if found, None otherwise
    """
    # Check kwargs first (most common case)
    if "request" in kwargs:
        req = kwargs["request"]
        if isinstance(req, Request):
            return req
    
    # Check args
    for arg in args:
        if isinstance(arg, Request):
            return arg
    
    return None


def track_event(event_name: str):
    """
    Decorator to track API endpoint events to PostHog.
    
    Usage:
        @track_event(event_name="spec_check_triggered")
        async def analyze_specification(request: Request, ...):
            ...
    
    Args:
        event_name: Name of the event to track
        
    Returns:
        Decorator function
    """
    def decorator(func: Callable) -> Callable:
        # Check if function is async
        is_async = inspect.iscoroutinefunction(func)
        
        if is_async:
            @wraps(func)
            async def async_wrapper(*args, **kwargs):
                try:
                    # Find request object
                    request = find_request_in_args(args, kwargs)
                    
                    # Extract user context
                    user_email, user_properties = extract_user_context(request)
                    
                    # Extract event properties
                    event_properties = extract_event_properties(func, args, kwargs, request)
                    
                    # Add user_id to event properties for easier filtering in PostHog
                    if user_email:
                        event_properties["user_id"] = user_email
                    
                    # Set telemetry context for this request (for LLM analytics)
                    if user_email:
                        TelemetryContext.set_user_context(
                            user_email=user_email,
                            generation_id=event_properties.get("generation_id"),
                            session_id=event_properties.get("session_id")
                        )
                    
                    # Identify user if this is first event (PostHog handles deduplication)
                    if user_email:
                        telemetry.identify_user(
                            distinct_id=user_email,
                            properties=user_properties
                        )
                    
                    # Capture event
                    distinct_id = user_email or "anonymous"
                    telemetry.capture_event(
                        distinct_id=distinct_id,
                        event=event_name,
                        properties=event_properties
                    )
                except Exception as e:
                    # Never interrupt application flow due to telemetry failures
                    logger.warning(f"Failed to capture telemetry event '{event_name}': {e}", exc_info=True)
                
                # Execute original function
                try:
                    result = await func(*args, **kwargs)
                    return result
                finally:
                    # Clear context after request completes
                    try:
                        TelemetryContext.clear_context()
                    except Exception:
                        pass
            
            return async_wrapper
        else:
            @wraps(func)
            def sync_wrapper(*args, **kwargs):
                try:
                    # Find request object
                    request = find_request_in_args(args, kwargs)
                    
                    # Extract user context
                    user_email, user_properties = extract_user_context(request)
                    
                    # Extract event properties
                    event_properties = extract_event_properties(func, args, kwargs, request)
                    
                    # Add user_id to event properties for easier filtering in PostHog
                    if user_email:
                        event_properties["user_id"] = user_email
                    
                    # Set telemetry context for this request (for LLM analytics)
                    if user_email:
                        TelemetryContext.set_user_context(
                            user_email=user_email,
                            generation_id=event_properties.get("generation_id"),
                            session_id=event_properties.get("session_id")
                        )
                    
                    # Identify user if this is first event (PostHog handles deduplication)
                    if user_email:
                        telemetry.identify_user(
                            distinct_id=user_email,
                            properties=user_properties
                        )
                    
                    # Capture event
                    distinct_id = user_email or "anonymous"
                    telemetry.capture_event(
                        distinct_id=distinct_id,
                        event=event_name,
                        properties=event_properties
                    )
                except Exception as e:
                    # Never interrupt application flow due to telemetry failures
                    logger.warning(f"Failed to capture telemetry event '{event_name}': {e}", exc_info=True)
                
                # Execute original function
                result = func(*args, **kwargs)
                
                # Clear context after request
                try:
                    TelemetryContext.clear_context()
                except Exception:
                    pass
                
                return result
            
            return sync_wrapper
    
    return decorator
