from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager, nullcontext, suppress
from dataclasses import dataclass
from typing import Any, Final, Protocol, cast, override

from langfuse import Langfuse, propagate_attributes
from langfuse.types import TraceContext

import logger.logger as central_logger
from llm.tracing.claude import ClaudeTraceObserver
from llm.tracing.composite import CompositeTraceSink
from llm.tracing.core import (
    ToolEnd,
    ToolStart,
    TraceCapture,
    TraceClose,
    TraceResult,
    TraceSink,
    TraceStart,
)
from llm.tracing.langfuse_urls import langfuse_settings_from_environment, langfuse_trace_url
from llm.tracing.session import TraceSession
from logger.correlation import current_run_id, new_id

_TRACE_ID_RE = re.compile(r"[0-9a-f]{32}")
_FLUSH_DRAIN_TIMEOUT_SECONDS: Final = 5.0


class LangfuseObservation(Protocol):
    def start_observation(self, **kwargs: Any) -> LangfuseObservation: ...

    def update(self, **kwargs: Any) -> Any: ...

    def end(self) -> Any: ...


class LangfuseClient(Protocol):
    def start_observation(self, **kwargs: Any) -> LangfuseObservation: ...


class LangfuseFlushClient(LangfuseClient, Protocol):
    def flush(self) -> Any: ...
    def auth_check(self) -> bool: ...


def _langfuse_session_id(metadata: Mapping[str, Any] | None) -> str | None:
    if metadata is None:
        return None
    for key in ("langfuse_session_id", "session_id"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _session_context(session_id: str | None) -> AbstractContextManager[Any]:
    if session_id is None:
        return nullcontext()
    return propagate_attributes(session_id=session_id)


class LangfuseSink:
    """Map generic tracing events to Langfuse observations."""

    def __init__(self, client: LangfuseClient, *, adopt_run_id: bool = False) -> None:
        self.client = client
        self.adopt_run_id = adopt_run_id
        self.trace_id: str | None = None
        self._generation: LangfuseObservation | None = None
        self._tools: dict[str, LangfuseObservation] = {}
        self._session_id: str | None = None

    @property
    def trace_url(self) -> str | None:
        return langfuse_trace_url(self.trace_id)

    def start(self, event: TraceStart) -> None:
        self._session_id = _langfuse_session_id(event.metadata)
        self.trace_id = self._resolve_trace_id()
        kwargs: dict[str, Any] = {
            "name": event.name,
            "as_type": "generation",
            "metadata": {
                **event.metadata,
                "integration": "llm.LangfuseMessageObserver",
            },
        }
        if event.model is not None:
            kwargs["model"] = event.model
        if event.input is not None:
            kwargs["input"] = dict(event.input)
        kwargs["trace_context"] = TraceContext(trace_id=self.trace_id)
        with _session_context(self._session_id):
            self._generation = self.client.start_observation(**kwargs)

    def _resolve_trace_id(self) -> str:
        """A fresh id, unless `adopt_run_id` and a valid one is bound (O18)."""

        if self.adopt_run_id:
            bound_run_id = current_run_id()
            if bound_run_id is not None and _TRACE_ID_RE.fullmatch(bound_run_id):
                return bound_run_id
        return new_id()

    def update_model(self, model: str) -> None:
        self._require_generation().update(model=model)

    def record_system_message(self, data: Mapping[str, Any]) -> None:
        self._require_generation().update(metadata={"system_message": dict(data)})

    def start_tool(self, event: ToolStart) -> None:
        kwargs: dict[str, Any] = {
            "name": event.name,
            "as_type": "tool",
            "metadata": {
                "tool_use_id": event.id,
                "server_tool": event.is_server,
            },
        }
        if event.input is not None:
            kwargs["input"] = event.input
        with _session_context(self._session_id):
            self._tools[event.id] = self._require_generation().start_observation(**kwargs)

    def end_tool(self, event: ToolEnd) -> None:
        observation = self._tools.pop(event.id, None)
        if observation is None:
            return
        kwargs: dict[str, Any] = {}
        if event.output is not None:
            kwargs["output"] = event.output
        if event.is_error:
            kwargs.update(level="ERROR", status_message="tool_result_error")
        try:
            if kwargs:
                observation.update(**kwargs)
        finally:
            observation.end()

    def abandon_tool(self, tool_id: str) -> None:
        observation = self._tools.pop(tool_id, None)
        if observation is None:
            return
        try:
            observation.update(
                level="WARNING",
                status_message="stream_ended_with_open_tool",
            )
        finally:
            observation.end()

    def record_result(self, event: TraceResult) -> None:
        metadata = {
            **event.metadata,
            "sdk_session_id": event.metadata.get("session_id"),
        }
        if event.reasoning is not None:
            metadata["reasoning"] = event.reasoning
        kwargs: dict[str, Any] = {
            "metadata": metadata,
            "usage_details": dict(event.usage),
        }
        if event.cost is not None:
            kwargs["cost_details"] = {"total": event.cost}
        if event.output is not None:
            kwargs["output"] = event.output
        if event.is_error:
            kwargs.update(level="ERROR", status_message="agent_result_error")
        self._require_generation().update(**kwargs)

    def close(self, event: TraceClose) -> None:
        generation = self._require_generation()
        try:
            kwargs: dict[str, Any] = {}
            if event.metadata is not None:
                kwargs["metadata"] = {
                    **event.metadata,
                    "sdk_session_id": event.metadata.get("session_id"),
                }
            if event.level != "default":
                kwargs.update(
                    level={"warning": "WARNING", "error": "ERROR"}[event.level],
                    status_message=event.status_message,
                )
            if kwargs:
                generation.update(**kwargs)
        finally:
            generation.end()

    def _require_generation(self) -> LangfuseObservation:
        if self._generation is None:
            raise RuntimeError("Langfuse trace has not started")
        return self._generation


class LangfuseMessageObserver(ClaudeTraceObserver):
    """Trace Claude Agent SDK messages with a Langfuse sink.

    With no `extra_sinks`, this is exactly today's single-`LangfuseSink`
    behaviour, unchanged. Given `extra_sinks`, the Langfuse sink (when
    `client` is not `None`) and every extra sink are fanned out through a
    `CompositeTraceSink`, so a run also reaches e.g. the central logger's
    `LoggingSink` -- with `adopt_run_id=True` on the Langfuse sink, so its
    `trace_id` matches the `run_id` the composite binds for the run.
    """

    def __init__(
        self,
        client: LangfuseClient | None,
        *,
        name: str = "llm.agent",
        model: str | None = None,
        prompt: Any = None,
        system_prompt: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        capture: TraceCapture | None = None,
        extra_sinks: Sequence[TraceSink] = (),
    ) -> None:
        self.client = client
        sink: TraceSink | None
        if extra_sinks:
            langfuse_sink = LangfuseSink(client, adopt_run_id=True) if client is not None else None
            sinks: tuple[TraceSink, ...] = ((langfuse_sink,) if langfuse_sink is not None else ()) + tuple(extra_sinks)
            sink = CompositeTraceSink(sinks)
        else:
            sink = LangfuseSink(client) if client is not None else None
        super().__init__(
            TraceSession(
                sink,
                name=name,
                model=model,
                prompt=prompt,
                system_prompt=system_prompt,
                metadata=metadata,
                capture=capture,
            )
        )

    @property
    @override
    def error_count(self) -> int:
        sink = self.session.sink
        composite_failures = sink.failure_count if isinstance(sink, CompositeTraceSink) else 0
        return self.session.error_count + composite_failures

    @property
    def trace_id(self) -> str | None:
        sink = self.session.sink
        if sink is None:
            return None
        trace_id: str | None = getattr(sink, "trace_id", None)
        if trace_id is not None:
            return trace_id
        run_id: str | None = getattr(sink, "run_id", None)
        return run_id

    @classmethod
    def from_default_client(cls, **kwargs: Any) -> LangfuseMessageObserver:
        """Use the configured Langfuse client, or remain disabled if unavailable."""

        with suppress(Exception):
            client = require_authenticated_langfuse_client()
            return cls(client, **kwargs)
        return cls(None, **kwargs)


def require_authenticated_langfuse_client() -> LangfuseFlushClient:
    """Return a Langfuse client after a successful auth check."""

    langfuse_settings = langfuse_settings_from_environment()
    public_key = langfuse_settings.public_key.get_secret_value().strip() if langfuse_settings.public_key else ""
    secret_key = langfuse_settings.secret_key.get_secret_value().strip() if langfuse_settings.secret_key else ""
    if not public_key or not secret_key:
        raise RuntimeError(
            "Langfuse authentication failed; configure LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, and LANGFUSE_BASE_URL"
        )
    client_kwargs: dict[str, Any] = {
        "public_key": public_key,
        "secret_key": secret_key,
    }
    base_url = (langfuse_settings.base_url or "").strip()
    if base_url:
        client_kwargs["base_url"] = base_url
    client = Langfuse(**client_kwargs)
    auth_check = getattr(client, "auth_check", None)
    if callable(auth_check) and not auth_check():
        raise RuntimeError(
            "Langfuse authentication failed; configure its public key, secret key, and base URL environment variables"
        )
    return cast(LangfuseFlushClient, client)


@dataclass(frozen=True, slots=True)
class LangfuseRegistryEvidence:
    """Outcome of one multi-observer Langfuse application run."""

    passed: bool
    enabled: bool
    observer_count: int
    error_count: int
    leftover_open_count: int


class LangfuseTraceRegistry:
    """Create observers for one application run and flush once at the end.

    Matches the SpecFlow harness: each ``Agent.run`` closes its observer, and
    the process flushes Langfuse once after every observer exists. ``evidence()``
    snapshots current observers without flushing so a later judge can see them.
    ``session_id`` is the Langfuse Sessions grouping key for every observer in
    this run.
    """

    def __init__(
        self,
        client: LangfuseFlushClient,
        *,
        model: str | None = None,
        session_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self.client = client
        self.model = model
        self.session_id = session_id.strip() if session_id and session_id.strip() else None
        self.metadata = dict(metadata or {})
        self._observers: list[LangfuseMessageObserver] = []
        self._closed = False
        self._leftover_open_count = 0
        # id()s of extra sinks already handed to an observer -- each extra
        # sink is stateful (e.g. `LoggingSink._tool_names`), so reusing one
        # across observers would corrupt it between unrelated runs.
        self._extra_sink_ids: set[int] = set()

    def observer(
        self,
        name: str,
        metadata: Mapping[str, Any] | None = None,
        *,
        prompt: Any = None,
        system_prompt: str | None = None,
        capture: TraceCapture | None = None,
        extra_sinks: Sequence[TraceSink] = (),
    ) -> LangfuseMessageObserver:
        if self._closed:
            raise RuntimeError("cannot create observers after close_and_flush")
        for sink in extra_sinks:
            sink_id = id(sink)
            if sink_id in self._extra_sink_ids:
                raise ValueError(
                    f"extra sink {sink!r} was already given to a previous "
                    "observer; each observer needs its own sink instance"
                )
            self._extra_sink_ids.add(sink_id)
        merged = {**self.metadata, **dict(metadata or {})}
        if self.session_id is not None:
            merged["langfuse_session_id"] = self.session_id
        observer = LangfuseMessageObserver(
            self.client,
            name=name,
            model=self.model,
            prompt=prompt,
            system_prompt=system_prompt,
            metadata=merged,
            capture=capture,
            extra_sinks=extra_sinks,
        )
        self._observers.append(observer)
        return observer

    def evidence(self) -> LangfuseRegistryEvidence:
        """Snapshot observer health without closing or flushing."""

        error_count = sum(observer.error_count for observer in self._observers)
        leftover_open_count = (
            self._leftover_open_count if self._closed else sum(1 for observer in self._observers if not observer.closed)
        )
        enabled = bool(self._observers) and all(observer.enabled for observer in self._observers)
        return LangfuseRegistryEvidence(
            passed=enabled and error_count == 0 and leftover_open_count == 0,
            enabled=enabled,
            observer_count=len(self._observers),
            error_count=error_count,
            leftover_open_count=leftover_open_count,
        )

    def close_and_flush(
        self,
        *,
        status_message: str = "trace_registry_cleanup",
    ) -> LangfuseRegistryEvidence:
        """Close leftover observers, then flush once.

        Already-closed observers (the normal ``Agent.run`` path) are left
        untouched. A second call is a no-op besides returning the same evidence.
        """

        if not self._closed:
            self._leftover_open_count = sum(1 for observer in self._observers if not observer.closed)
            for observer in self._observers:
                if observer.closed:
                    continue
                observer.close(
                    is_error=True,
                    status_message=status_message,
                )
            try:
                self.client.flush()
            finally:
                # Drain the central logger's own queue too, even if the
                # Langfuse client's flush raised (thread_safety.md
                # §Shutdown). Looked up via the module (not a bound
                # `from logger.logger import flush`) so a test's
                # `monkeypatch.setattr("logger.logger.flush", ...)` is
                # honored.
                central_logger.flush(timeout=_FLUSH_DRAIN_TIMEOUT_SECONDS)
            self._closed = True
        return self.evidence()
