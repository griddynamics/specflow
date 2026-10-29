from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from llm.tracing.core import ToolEnd, ToolStart, TraceClose, TraceResult, TraceStart
from llm.tracing.langfuse_urls import langfuse_trace_url
from logger.correlation import CorrelationFilter, current_run_id
from logger.logger import DEBUG, ERROR, INFO, WARNING, get_logger, log_event
from logger.payload import summarise_payload

# `src/llm/tracing/` never touches stdlib `logging` directly (test_logging_
# adoption.py's `test_tracing_package_never_touches_stdlib_logging_
# directly`): `logger.logger`'s re-exported level constants above are what
# this module logs at, and `logging.Logger` below is a deferred (`from
# __future__ import annotations`) type-hint-only reference -- never
# evaluated at runtime, so it needs no import of its own.

#: `TraceClose.level` -> the stdlib level its `trace.close` record is logged at.
_CLOSE_LEVELS: dict[str, int] = {
    "default": INFO,
    "warning": WARNING,
    "error": ERROR,
}

#: `TraceStart.metadata` keys that are lifecycle bookkeeping, not
#: caller-supplied data -- excluded from the `metadata` field's user keys.
_START_METADATA_EXCLUDED_KEYS: frozenset[str] = frozenset(
    {"session_id", "langfuse_session_id", "num_messages", "num_tool_uses", "is_error"}
)


class LoggingSink:
    """`TraceSink` that writes every tracing event through the central logger.

    Stateful (`_tool_names` maps an open tool's id to its name so `end_tool`/
    `abandon_tool` can still name a tool the caller never re-supplies a name
    for), so each observer needs its own instance -- never share one across
    concurrent runs. Every method calls `logger.logger.log_event` directly,
    so `funcName` on the emitted record is the method name, and never
    passes a correlation id as an explicit field: `CorrelationFilter`
    injects whatever the wrapping `CompositeTraceSink.start()` bound.

    Payloads (inputs, outputs, reasoning, system messages) are only ever
    logged as `summarise_payload` previews, sizes and hashes; the full
    content lives only in Langfuse.
    """

    def __init__(self, *, logger: Any = None) -> None:
        self._logger = logger or get_logger(__name__)
        # Idempotent: `get_logger(__name__)` returns the same shared Logger
        # for every instance, so guard against piling up one filter per
        # `LoggingSink()` constructed. Needed so a record carries `run_id`/
        # `session_id`/`agent_id` even before `w3-a`'s pipeline attaches its
        # own `CorrelationFilter` to the central queue handler.
        if not any(isinstance(existing, CorrelationFilter) for existing in self._logger.filters):
            self._logger.addFilter(CorrelationFilter())
        self._tool_names: dict[str, str] = {}

    def start(self, event: TraceStart) -> None:
        self._tool_names.clear()

        fields: dict[str, Any] = {
            "model": event.model,
            "trace_url": langfuse_trace_url(current_run_id()),
        }
        if event.metadata:
            fields["sdk_session_id"] = event.metadata.get("session_id")
            user_keys = {
                key: value
                for key, value in event.metadata.items()
                if key not in _START_METADATA_EXCLUDED_KEYS and not key.startswith("capture_")
            }
            if user_keys:
                fields["metadata"] = user_keys
        if event.input is not None:
            fields.update(summarise_payload(event.input))

        log_event(
            self._logger,
            INFO,
            "trace.start",
            **{key: value for key, value in fields.items() if value is not None},
        )

    def update_model(self, model: str) -> None:
        log_event(self._logger, DEBUG, "trace.model", model=model)

    def record_system_message(self, data: Mapping[str, Any]) -> None:
        log_event(
            self._logger,
            DEBUG,
            "trace.system_message",
            **summarise_payload(data, prefix="data"),
        )

    def start_tool(self, event: ToolStart) -> None:
        self._tool_names[event.id] = event.name
        log_event(
            self._logger,
            INFO,
            "tool.start",
            tool_use_id=event.id,
            tool_name=event.name,
            is_server=event.is_server,
            **summarise_payload(event.input),
        )

    def end_tool(self, event: ToolEnd) -> None:
        tool_name = self._tool_names.pop(event.id, None)
        level = ERROR if event.is_error else INFO
        log_event(
            self._logger,
            level,
            "tool.end",
            tool_use_id=event.id,
            tool_name=tool_name,
            is_error=event.is_error,
            **summarise_payload(event.output),
        )

    def abandon_tool(self, tool_id: str) -> None:
        tool_name = self._tool_names.pop(tool_id, None)
        log_event(
            self._logger,
            WARNING,
            "tool.abandoned",
            tool_use_id=tool_id,
            tool_name=tool_name,
        )

    def record_result(self, event: TraceResult) -> None:
        level = ERROR if event.is_error else INFO
        fields: dict[str, Any] = {
            "usage": dict(event.usage),
            "cost": event.cost,
            "num_turns": event.metadata.get("num_turns"),
            "duration_ms": event.metadata.get("duration_ms"),
            "duration_api_ms": event.metadata.get("duration_api_ms"),
            "subtype": event.metadata.get("subtype"),
            "stop_reason": event.metadata.get("stop_reason"),
            "is_error": event.is_error,
        }
        fields.update(summarise_payload(event.output))
        fields.update(summarise_payload(event.reasoning, prefix="reasoning"))
        log_event(self._logger, level, "trace.result", **fields)

    def close(self, event: TraceClose) -> None:
        level = _CLOSE_LEVELS[event.level]
        fields: dict[str, Any] = {"status_message": event.status_message}
        if event.metadata is not None:
            # Only on this path: `event.metadata` is `None` on the plain
            # result-close (session.py's `result()` sends its own
            # `trace.close` with no metadata), so these three keys must be
            # entirely absent there, not present-with-null.
            fields["sdk_session_id"] = event.metadata.get("session_id")
            fields["num_messages"] = event.metadata.get("num_messages")
            fields["num_tool_uses"] = event.metadata.get("num_tool_uses")
        log_event(self._logger, level, "trace.close", **fields)
