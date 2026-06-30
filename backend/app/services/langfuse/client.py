"""Process-singleton wrapper around the Langfuse v4 SDK client."""

from __future__ import annotations

import contextvars
import logging
from contextlib import asynccontextmanager
from typing import Any, Optional

try:
    from langfuse import Langfuse as _LangfuseClient
    from langfuse import propagate_attributes as _propagate_attributes
except ImportError:
    _LangfuseClient = None  # type: ignore[assignment,misc]
    _propagate_attributes = None  # type: ignore[assignment]

from app.core.config import settings
from app.core.telemetry_context import TelemetryContext

from .stream_tracer import LangfuseStreamTracer, _NullTracer
from .utils import (
    _PROPAGATED_CONTEXT_KEYS,
    _safe_update,
    _to_propagate_str,
)

logger = logging.getLogger(__name__)


class LangfuseTracer:
    """Module-singleton tracer wrapping the Langfuse v4 SDK."""

    _current_root_obs: contextvars.ContextVar[Optional[Any]] = contextvars.ContextVar(
        "langfuse_current_root_obs", default=None
    )

    def __init__(self) -> None:
        self._client: Optional[Any] = None
        self._propagate_attributes_fn: Optional[Any] = None
        self._redact_tool_io: bool = False

    def init(self) -> None:
        if not settings.langfuse_enabled:
            logger.info(
                "Langfuse tracing disabled — set LANGFUSE_PUBLIC_KEY, "
                "LANGFUSE_SECRET_KEY, and LANGFUSE_BASE_URL to enable"
            )
            return
        if _LangfuseClient is None:
            logger.warning("langfuse package not installed — tracing disabled")
            return
        try:
            self._client = _LangfuseClient(
                public_key=settings.LANGFUSE_PUBLIC_KEY,
                secret_key=settings.LANGFUSE_SECRET_KEY,
                base_url=settings.LANGFUSE_BASE_URL,
                environment=settings.LANGFUSE_ENVIRONMENT,
            )
            self._propagate_attributes_fn = _propagate_attributes
            self._redact_tool_io = settings.LANGFUSE_REDACT_TOOL_IO
            self._log_init_resolution()
        except Exception:
            logger.warning("Langfuse initialization failed — tracing disabled", exc_info=True)
            self._client = None
            self._propagate_attributes_fn = None

    def _log_init_resolution(self) -> None:
        # SDK resolves base_url via LANGFUSE_BASE_URL env > kwarg > LANGFUSE_HOST env;
        # log the resolved value so configured-vs-actual mismatches are visible.
        resolved = getattr(self._client, "_base_url", "<unknown>")
        pk_fingerprint = (settings.LANGFUSE_PUBLIC_KEY or "")[:10] + "…"
        logger.info(
            "Langfuse tracing enabled (configured_base_url=%s resolved_base_url=%s "
            "public_key=%s environment=%s redact_tool_io=%s)",
            settings.LANGFUSE_BASE_URL, resolved, pk_fingerprint,
            settings.LANGFUSE_ENVIRONMENT, self._redact_tool_io,
        )
        if resolved != settings.LANGFUSE_BASE_URL:
            logger.warning(
                "Langfuse SDK resolved a different base_url than configured "
                "(configured=%s resolved=%s) — check LANGFUSE_BASE_URL/LANGFUSE_HOST env vars",
                settings.LANGFUSE_BASE_URL, resolved,
            )

    async def flush_async(self) -> None:
        if self._client is None:
            logger.debug("Langfuse flush skipped (client not initialized)")
            return
        try:
            logger.debug("Langfuse flush starting")
            self._client.flush()
            logger.info("Langfuse flush complete")
        except Exception:
            logger.warning("Langfuse flush failed", exc_info=True)

    def is_enabled(self) -> bool:
        return self._client is not None

    def get_current_root_obs(self) -> Optional[Any]:
        return self._current_root_obs.get()

    def _build_propagate_metadata(self, extra_metadata: Optional[dict]) -> dict[str, str]:
        ctx = TelemetryContext.get_user_context() or {}
        result: dict[str, str] = {}
        for key in _PROPAGATED_CONTEXT_KEYS:
            value = ctx.get(key)
            if value is None or value == "":
                continue
            result[key] = _to_propagate_str(value)
        if extra_metadata:
            for k, v in extra_metadata.items():
                result[_to_propagate_str(k)[:50]] = _to_propagate_str(v)
        return result

    @asynccontextmanager
    async def start_workflow_step_trace(
        self,
        name: str,
        extra_metadata: Optional[dict[str, Any]] = None,
    ):
        if self._client is None:
            logger.debug("Langfuse trace %r skipped (client not initialized)", name)
            yield None
            return

        ctx = TelemetryContext.get_user_context() or {}
        user_email = ctx.get("user_email")
        generation_id = ctx.get("generation_id")
        workspace_ids: list[str] = ctx.get("workspace_ids") or []
        tags = [f"ws:{wid}" for wid in workspace_ids] if workspace_ids else []
        metadata = self._build_propagate_metadata(extra_metadata)

        logger.debug(
            "Langfuse trace opening: name=%s session_id=%s user_id=%s tags=%s metadata_keys=%s",
            name, generation_id, user_email, tags, list(metadata.keys()),
        )

        # `yielded` separates SDK-setup failures (fall back to a single no-op yield)
        # from user-code exceptions (must propagate). Yielding twice on athrow masks
        # the original with RuntimeError("generator didn't stop after athrow()").
        yielded = False
        try:
            with self._client.start_as_current_observation(
                name=name, as_type="span"
            ) as root_obs:
                # propagate_attributes must wrap *inside* the root span so the root
                # itself also gets session_id/user_id.
                with self._propagate_attributes_fn(
                    user_id=user_email or None,
                    session_id=generation_id or None,
                    tags=tags or None,
                    metadata=metadata or None,
                ):
                    token = self._current_root_obs.set(root_obs)
                    try:
                        yielded = True
                        yield root_obs
                    except Exception:
                        _safe_update(root_obs, level="ERROR", status_message="workflow_step_failed")
                        raise
                    finally:
                        self._current_root_obs.reset(token)
        except Exception:
            if yielded:
                raise
            logger.warning("Langfuse: SDK setup failed for trace %r", name, exc_info=True)
            yield None

    def create_generation(
        self,
        name: str,
        model: str,
        input_data: dict[str, Any],
        metadata: dict[str, Any],
        model_parameters: Optional[dict[str, Any]] = None,
    ) -> Optional[Any]:
        root_obs = self._current_root_obs.get()
        if root_obs is None or self._client is None:
            logger.debug(
                "Langfuse generation %r skipped (root_obs=%s client=%s)",
                name,
                "set" if root_obs else "missing",
                "set" if self._client else "missing",
            )
            return None
        try:
            return root_obs.start_observation(
                name=name,
                as_type="generation",
                model=model,
                input=input_data,
                metadata=metadata,
                model_parameters=model_parameters,
            )
        except Exception:
            logger.warning("Langfuse: failed to create generation %r", name, exc_info=True)
            return None

    def make_stream_tracer(self, generation: Optional[Any]) -> Any:
        if generation is None:
            return _NullTracer()
        return LangfuseStreamTracer(
            generation,
            redact_tool_io=self._redact_tool_io,
            get_root_obs=self.get_current_root_obs,
        )


tracer = LangfuseTracer()
