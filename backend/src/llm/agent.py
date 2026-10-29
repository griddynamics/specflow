from __future__ import annotations

from collections.abc import AsyncIterable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from claude_agent_sdk import query

from llm.captured import CapturedException
from llm.result import AgentCleanupError, AgentObserverError, AgentResult
from llm.settings import AgentSettings, _build_sdk_options
from llm.stream import (
    MessageObserver,
    StreamState,
    close_message_observer,
    close_stream,
    consume_stream_with_timeout,
)

AgentInput = str | AsyncIterable[dict[str, Any]]


class Agent:
    """General-purpose async Claude Agent SDK session."""

    def __init__(
        self,
        *,
        model: str,
        system_prompt: str | None = None,
        cwd: str | Path | None = None,
        timeout: float = 3600.0,
        message_observer: MessageObserver | None = None,
        settings: AgentSettings | None = None,
    ) -> None:
        if not model.strip():
            raise ValueError("model must not be empty")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.model = model
        self.system_prompt = system_prompt
        self.cwd = cwd
        self.timeout = timeout
        self.message_observer = message_observer
        self.settings = settings or AgentSettings()

    async def run(
        self,
        prompt_or_messages: AgentInput,
        *,
        resume_session_id: str | None = None,
        output_format: Mapping[str, Any] | None = None,
    ) -> AgentResult:
        """Run one prompt or SDK-supported message stream."""

        if resume_session_id is not None and not resume_session_id.strip():
            raise ValueError("resume_session_id must not be empty")

        settings = self.settings if output_format is None else replace(self.settings, output_format=output_format)
        options = _build_sdk_options(
            model=self.model,
            system_prompt=self.system_prompt,
            cwd=self.cwd,
            resume_session_id=resume_session_id,
            fallback_model=settings.fallback_model,
            subagents=settings.subagents,
            settings=settings,
        )
        stream = query(prompt=prompt_or_messages, options=options)
        state = StreamState(session_id=resume_session_id)
        primary_error: BaseException | None = None
        try:
            await consume_stream_with_timeout(
                stream,
                state,
                self.message_observer,
                self.timeout,
            )
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            await _cleanup_agent_run(
                stream=stream,
                observer=self.message_observer,
                state=state,
                primary_error=primary_error,
            )

        return state.to_result()


def _observer_close_status(
    primary_error: BaseException | None,
    state: StreamState,
) -> tuple[bool, str]:
    if primary_error is not None:
        return True, "agent_run_failed"
    if state.final_result is None:
        return True, "stream_ended_without_result"
    return state.final_result.is_error, "agent_result"


def _note_secondary_failure(
    error: BaseException,
    *,
    cleanup_error: Exception | None,
    observer_error: Exception | None,
) -> None:
    if cleanup_error is not None:
        error.add_note(f"SDK stream cleanup also failed: {type(cleanup_error).__name__}: {cleanup_error}")
    if observer_error is not None:
        error.add_note(f"Message observer cleanup also failed: {type(observer_error).__name__}: {observer_error}")


async def _cleanup_agent_run(
    *,
    stream: Any,
    observer: MessageObserver | None,
    state: StreamState,
    primary_error: BaseException | None,
) -> None:
    with CapturedException() as close_capture:
        await close_stream(stream)
    cleanup_error = close_capture.error
    is_error, status_message = _observer_close_status(primary_error, state)
    with CapturedException() as observer_capture:
        await close_message_observer(
            observer,
            is_error=is_error,
            status_message=status_message,
        )
    observer_error = observer_capture.error

    if primary_error is not None:
        _note_secondary_failure(
            primary_error,
            cleanup_error=cleanup_error,
            observer_error=observer_error,
        )
        return
    if cleanup_error is not None:
        error = AgentCleanupError(
            "Agent run completed but SDK stream cleanup failed",
            progress=state.progress,
            session_id=state.session_id,
            partial_output=state.output,
            final_result=state.final_result,
        )
        _note_secondary_failure(
            error,
            cleanup_error=None,
            observer_error=observer_error,
        )
        raise error from cleanup_error
    if observer_error is not None:
        raise AgentObserverError(
            "Agent run completed but message observer cleanup failed",
            progress=state.progress,
            session_id=state.session_id,
            partial_output=state.output,
            final_result=state.final_result,
        ) from observer_error
