"""
PostHog telemetry service for tracking user events.

Provides a singleton telemetry instance that can be used throughout the application
to capture events to PostHog. Gracefully degrades if PostHog is not configured.
"""

import logging
import os
from typing import Any, Dict, List, Optional
from contextlib import contextmanager

from app.core.config import settings
from app.core.telemetry_context import TelemetryContext
from app.services.agent_metrics import tool_usage_telemetry_props

logger = logging.getLogger(__name__)


class PostHogTelemetry:
    """
    PostHog telemetry client wrapper.
    
    Provides a singleton instance for capturing events to PostHog.
    Gracefully handles cases where PostHog is not configured.
    """
    
    def __init__(self):
        """Initialize PostHog client if API key is configured."""
        self._client = None
        self._initialized = False
        
        if settings.POSTHOG_ENABLED and settings.POSTHOG_API_KEY:
            try:
                from posthog import Posthog
                
                # Verify API key format (project API keys typically start with 'phc_' or 'phx_')
                api_key = settings.POSTHOG_API_KEY
                if not api_key.startswith(('phc_', 'phx_')):
                    logger.warning(
                        f"PostHog API key format may be incorrect. "
                        f"Project API keys typically start with 'phc_' or 'phx_'. "
                        f"Current key starts with: {api_key[:4] if len(api_key) >= 4 else 'unknown'}"
                    )
                
                posthog_host = settings.POSTHOG_HOST
                self._client = Posthog(
                    project_api_key=api_key,
                    host=posthog_host
                )
                self._initialized = True
                logger.info(
                    f"PostHog telemetry initialized - "
                    f"API endpoint: {posthog_host} "
                    f"(Note: Dashboard URL is different - use https://us.posthog.com/ to view events)"
                )
            except ImportError:
                logger.warning("posthog package not installed, telemetry disabled")
            except Exception as e:
                logger.warning(f"Failed to initialize PostHog: {e}", exc_info=True)
        else:
            logger.debug("PostHog telemetry disabled (API key not configured or disabled)")
    
    def is_enabled(self) -> bool:
        """Check if telemetry is enabled and initialized."""
        return self._initialized and self._client is not None
    
    def capture_event(
        self,
        distinct_id: str,
        event: str,
        properties: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Capture an event to PostHog.
        
        Args:
            distinct_id: User identifier (email)
            event: Event name
            properties: Optional event properties dict
        """
        if not self.is_enabled():
            return
        
        try:
            properties = properties or {}
            
            # Capture event
            self._client.capture(
                distinct_id=distinct_id,
                event=event,
                properties=properties
            )
            
            logger.debug(f"Captured event: {event} for user: {distinct_id}")
        except Exception as e:
            # Never interrupt application flow due to telemetry failures
            logger.warning(f"Failed to capture PostHog event '{event}': {e}", exc_info=True)
    
    def identify_user(
        self,
        distinct_id: str,
        properties: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Identify a user in PostHog.
        
        Sets person properties by capturing an event with $set properties.
        This is the recommended approach for PostHog Python SDK v3.0+.
        
        Args:
            distinct_id: User identifier (email)
            properties: Optional user properties dict
        """
        if not self.is_enabled():
            return
        
        try:
            properties = properties or {}
            
            # Identify user by capturing an event with $set properties
            # This sets person properties in PostHog person profiles
            self._client.capture(
                distinct_id=distinct_id,
                event="user_identified",
                properties={
                    "$set": properties
                }
            )
            
            logger.debug(f"Identified user: {distinct_id}")
        except Exception as e:
            # Never interrupt application flow due to telemetry failures
            logger.warning(f"Failed to identify user '{distinct_id}' in PostHog: {e}", exc_info=True)
    
    def capture_agent_query_event(
        self,
        model: str,
        provider_name: str,
        workspace_path: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_write_tokens: int = 0,
        cache_read_tokens: int = 0,
        latency_seconds: float = 0.0,
        trace_id: Optional[str] = None,
        cost_usd: float = 0.0,
        num_turns: int = 0,
        is_error: bool = False,
        error_type: Optional[str] = None,
        tool_usage_breakdown: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Capture a $ai_generation PostHog LLM-analytics event for a single agent_query call.

        Reads user/generation/workflow/phase_name context from TelemetryContext automatically.
        All metric parameters default to zero so error paths can omit them.

        Args:
            model: Model name used for the call.
            provider_name: "openrouter" or "anthropic".
            workspace_path: Workspace path string; basename used as workspace_name fallback.
            input_tokens: Non-cached prompt token count (0 if unavailable).
            output_tokens: Completion token count (0 if unavailable).
            cache_write_tokens: Cache-write (creation) tokens from the SDK (0 if unavailable).
            cache_read_tokens: Cache-read tokens from the SDK (0 if unavailable).
            latency_seconds: API latency in seconds (0 if unavailable).
            trace_id: Session/trace ID; falls back to TelemetryContext.get_session_id().
            cost_usd: USD cost (0 if unavailable).
            num_turns: Agent turn count (0 if unavailable).
            is_error: True for timeout / no-messages / SDK error results.
            error_type: Short label e.g. "timeout", "no_messages" (omitted on success).
            tool_usage_breakdown: Optional per-call tool/MCP aggregates from ClaudeCodeSdkAgentMetrics.
        """
        if not self.is_enabled():
            return
        try:
            user_email = TelemetryContext.get_user_email() or "anonymous"
            # SSOT for workspace dimension: filesystem path for this agent_query (not TelemetryContext),
            # so parallel asyncio tasks cannot mix workspace labels when ContextVar dict was shared.
            workspace_fs_name = os.path.basename(workspace_path.rstrip("/\\"))
            effective_trace_id = trace_id or TelemetryContext.get_session_id()

            props: Dict[str, Any] = {
                "$ai_model": model,
                "$ai_provider": provider_name,
                "$ai_input_tokens": input_tokens,
                "$ai_output_tokens": output_tokens,
                # PostHog LLM analytics canonical cache field names
                "$ai_cache_creation_input_tokens": cache_write_tokens,
                "$ai_cache_read_input_tokens": cache_read_tokens,
                # Legacy aliases (same values) for existing dashboards
                "$ai_cache_write_tokens": cache_write_tokens,
                "$ai_cache_read_tokens": cache_read_tokens,
                "$ai_latency": latency_seconds,
                "$ai_trace_id": effective_trace_id,
                # Authoritative paid amount (OpenRouter catalog or SDK on direct Anthropic)
                "$ai_cost_usd": cost_usd,
                "$ai_total_cost_usd": cost_usd,
                "$ai_num_turns": num_turns,
                "$ai_is_error": is_error,
                "workspace_name": workspace_fs_name,
                "duration_seconds": round(latency_seconds, 2),
            }
            props["model_name"] = model

            generation_id = TelemetryContext.get_generation_id()
            if generation_id:
                props["generation_id"] = generation_id

            workflow = TelemetryContext.get_workflow()
            if workflow is not None:
                props["workflow"] = workflow.to_stored_string()

            phase_name = TelemetryContext.get_phase_name()
            if phase_name:
                props["phase_name"] = phase_name

            if error_type:
                props["error_type"] = error_type

            props.update(TelemetryContext.get_mcp_props())

            if tool_usage_breakdown is not None:
                props.update(tool_usage_telemetry_props(tool_usage_breakdown))

            self.capture_event(distinct_id=user_email, event="$ai_generation", properties=props)

            logger.debug(
                f"Captured $ai_generation: model={model}, "
                f"tokens={input_tokens}+{output_tokens} "
                f"(cache_write={cache_write_tokens}, cache_read={cache_read_tokens}), "
                f"cost=${cost_usd}, workspace={workspace_fs_name}, is_error={is_error}"
            )
        except Exception as e:
            logger.warning(f"Failed to capture agent query telemetry: {e}", exc_info=True)

    def capture_kb_init_event(
        self,
        *,
        generation_id: str,
        workspace_name: str,
        status: str,
        generated_files: list[str],
        duration_seconds: float = 0.0,
        error_message: Optional[str] = None,
    ) -> None:
        """Capture a kb_init_completed PostHog event with file manifest.

        Emitted after the KB init agent finishes (success or failure) so that
        operators can see what Rosetta produced without digging into logs.

        Args:
            generation_id: Current generation ID.
            workspace_name: Workspace where KB init ran.
            status: "success", "skipped", or "failed".
            generated_files: List of relative paths (CLAUDE.md + outputs_dir docs) created by KB init.
            duration_seconds: Wall-clock time the agent ran.
            error_message: Error string when status == "failed".
        """
        if not self.is_enabled():
            return
        try:
            user_email = TelemetryContext.get_user_email() or "anonymous"

            props: Dict[str, Any] = {
                "generation_id": generation_id,
                "workspace_name": workspace_name,
                "status": status,
                "generated_files": generated_files,
                "generated_files_count": len(generated_files),
                "duration_seconds": round(duration_seconds, 2),
            }
            if error_message:
                props["error_message"] = error_message[:500]

            workflow = TelemetryContext.get_workflow()
            if workflow is not None:
                props["workflow"] = workflow.to_stored_string()

            props.update(TelemetryContext.get_mcp_props())

            self.capture_event(
                distinct_id=user_email,
                event="kb_init_completed",
                properties=props,
            )

            logger.info(
                f"Captured kb_init_completed: status={status}, "
                f"files={len(generated_files)}, duration={duration_seconds:.1f}s, "
                f"generation={generation_id}"
            )
        except Exception as e:
            logger.warning(f"Failed to capture kb_init telemetry: {e}", exc_info=True)

    def capture_spec_check_event(
        self,
        *,
        event: str,
        generation_id: str,
        workspace_id: str,
        readiness: Optional[str] = None,
        duration_seconds: float = 0.0,
        error_message: Optional[str] = None,
        total_tokens: int = 0,
    ) -> None:
        """Emit spec_check_completed or spec_check_failed from the async spec-analysis task."""
        if not self.is_enabled():
            return
        try:
            user_email = TelemetryContext.get_user_email() or "anonymous"
            props: Dict[str, Any] = {
                "generation_id": generation_id,
                "workspace_id": workspace_id,
            }
            if readiness is not None:
                props["readiness"] = readiness
            props["duration_seconds"] = round(duration_seconds, 2)
            if error_message:
                props["error_message"] = error_message[:500]
            if total_tokens:
                props["total_tokens"] = total_tokens
            props.update(TelemetryContext.get_mcp_props())
            self.capture_event(distinct_id=user_email, event=event, properties=props)
            logger.info("Captured %s: generation=%s", event, generation_id)
        except Exception as e:
            logger.warning("Failed to capture spec check telemetry: %s", e, exc_info=True)

    def _capture_milestone(
        self,
        *,
        event: str,
        generation_id: str,
        extra_props: Dict[str, Any],
        distinct_id: Optional[str] = None,
        include_workflow: bool = True,
    ) -> None:
        if not self.is_enabled():
            return
        try:
            did = distinct_id or TelemetryContext.get_user_email() or "anonymous"
            props: Dict[str, Any] = {"generation_id": generation_id, **extra_props}
            if include_workflow:
                workflow = TelemetryContext.get_workflow()
                if workflow is not None:
                    props["workflow"] = workflow.to_stored_string()
            props.update(TelemetryContext.get_mcp_props())
            self.capture_event(distinct_id=did, event=event, properties=props)
        except Exception as e:
            logger.warning("Failed to capture %s: %s", event, e, exc_info=True)

    def capture_spec_check_triggered(
        self,
        *,
        generation_id: str,
        workspace_id: str,
        user_email: Optional[str] = None,
    ) -> None:
        """Fires when spec analysis is queued (pairs with spec_check_completed / failed)."""
        self._capture_milestone(
            event="spec_check_triggered",
            generation_id=generation_id,
            extra_props={"workspace_id": workspace_id},
            distinct_id=user_email,
            include_workflow=False,
        )

    def capture_kb_init_triggered(
        self,
        *,
        generation_id: str,
        workspace_name: str,
    ) -> None:
        """Fires when the KB init agent is about to run (pairs with kb_init_completed)."""
        self._capture_milestone(
            event="kb_init_triggered",
            generation_id=generation_id,
            extra_props={"workspace_name": workspace_name},
        )

    def capture_workspace_coding_completed(
        self,
        *,
        generation_id: str,
        workspace_ids: List[str],
        duration_seconds: float,
    ) -> None:
        """Fires after parallel workspace code-generation phases finish (pre-deploy/P10Y)."""
        self._capture_milestone(
            event="workspace_coding_completed",
            generation_id=generation_id,
            extra_props={
                "workspace_ids": workspace_ids,
                "workspace_count": len(workspace_ids),
                "duration_seconds": round(duration_seconds, 2),
            },
        )

    def capture_planning_event(
        self,
        *,
        event: str,
        generation_id: str,
        workspace_name: str,
        status: Optional[str] = None,
        phase_count: Optional[int] = None,
        duration_seconds: float = 0.0,
        error_message: Optional[str] = None,
    ) -> None:
        """Capture a planning PostHog event (planning_triggered or planning_completed).

        Args:
            event: "planning_triggered" or "planning_completed".
            generation_id: Current generation ID.
            workspace_name: Primary workspace name.
            status: "success" or "failed" (omit for triggered).
            phase_count: Number of phases parsed from the plan (on success).
            duration_seconds: Wall-clock time the agent ran (completed only).
            error_message: Error string when status == "failed".
        """
        if not self.is_enabled():
            return
        try:
            user_email = TelemetryContext.get_user_email() or "anonymous"

            props: Dict[str, Any] = {
                "generation_id": generation_id,
                "workspace_name": workspace_name,
            }
            if status is not None:
                props["status"] = status
            if phase_count is not None:
                props["phase_count"] = phase_count
            props["duration_seconds"] = round(duration_seconds, 2)
            if error_message:
                props["error_message"] = error_message[:500]

            workflow = TelemetryContext.get_workflow()
            if workflow is not None:
                props["workflow"] = workflow.to_stored_string()

            props.update(TelemetryContext.get_mcp_props())

            self.capture_event(distinct_id=user_email, event=event, properties=props)

            logger.info(
                f"Captured {event}: generation={generation_id}, "
                f"status={status}, phase_count={phase_count}, "
                f"duration={duration_seconds:.1f}s"
            )
        except Exception as e:
            logger.warning(f"Failed to capture planning telemetry: {e}", exc_info=True)

    def shutdown(self) -> None:
        """Shutdown PostHog client and flush pending events."""
        if self.is_enabled():
            try:
                self._client.shutdown()
                logger.info("PostHog telemetry shutdown")
            except Exception as e:
                logger.warning(f"Error during PostHog shutdown: {e}")
    
    @contextmanager
    def context(self):
        """Context manager for automatic cleanup."""
        try:
            yield self
        finally:
            # PostHog client handles flushing automatically, but we can add cleanup here if needed
            pass


# Singleton instance
telemetry = PostHogTelemetry()
