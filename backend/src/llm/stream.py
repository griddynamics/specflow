from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from inspect import isawaitable

from claude_agent_sdk import (
    AssistantMessage,
    Message,
    ResultMessage,
    ServerToolUseBlock,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
)

from llm.result import (
    AgentError,
    AgentErrorKind,
    AgentObserverError,
    AgentProgress,
    AgentResult,
    AgentRunError,
    AgentStreamError,
    AgentTimeoutError,
)

MessageObserver = Callable[[Message], Awaitable[None] | None]


@dataclass(slots=True)
class StreamState:
    num_messages: int = 0
    num_tool_uses: int = 0
    text_parts: list[str] = field(default_factory=list)
    final_result: ResultMessage | None = None
    session_id: str | None = None

    @property
    def progress(self) -> AgentProgress:
        return AgentProgress(
            num_messages=self.num_messages,
            num_tool_uses=self.num_tool_uses,
        )

    @property
    def streamed_text(self) -> str | None:
        return "".join(self.text_parts) or None

    @property
    def output(self) -> str | None:
        if self.final_result is not None and self.final_result.result:
            return self.final_result.result
        return self.streamed_text

    def observe(self, message: Message) -> None:
        self.num_messages += 1
        session_id = getattr(message, "session_id", None)
        if not session_id and isinstance(message, SystemMessage):
            session_id = message.data.get("session_id")
        if isinstance(session_id, str) and session_id:
            self.session_id = session_id

        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    self.text_parts.append(block.text)
                elif isinstance(block, (ToolUseBlock, ServerToolUseBlock)):
                    self.num_tool_uses += 1
        elif isinstance(message, ResultMessage):
            self.final_result = message
            self.session_id = message.session_id

    def to_result(self) -> AgentResult:
        if self.final_result is None:
            no_result_error = AgentError(
                kind=AgentErrorKind.NO_RESULT,
                message="Agent stream ended without a result message",
            )
            return AgentResult(
                output=self.streamed_text,
                session_id=self.session_id,
                is_error=True,
                progress=self.progress,
                error=no_result_error,
            )

        error: AgentError | None = None
        if self.final_result.is_error:
            error = AgentError(
                kind=AgentErrorKind.RESULT,
                message=_result_error_message(self.final_result),
            )
        return AgentResult(
            output=self.output,
            session_id=self.session_id,
            is_error=self.final_result.is_error,
            progress=self.progress,
            usage=dict(self.final_result.usage or {}),
            error=error,
            final_result=self.final_result,
        )


async def consume_stream(
    stream: AsyncIterator[Message],
    state: StreamState,
    observer: MessageObserver | None = None,
) -> None:
    """Consume one SDK stream into typed state."""

    try:
        async for message in stream:
            state.observe(message)
            if observer is not None:
                try:
                    observed = observer(message)
                    if isawaitable(observed):
                        await observed
                except Exception as exc:
                    detail = str(exc) or type(exc).__name__
                    raise AgentObserverError(
                        f"Message observer failed: {detail}",
                        progress=state.progress,
                        session_id=state.session_id,
                        partial_output=state.output,
                        final_result=state.final_result,
                    ) from exc
    except AgentRunError:
        raise
    except Exception as exc:
        failure_message = str(exc) or type(exc).__name__
        if state.final_result is not None and state.final_result.is_error:
            failure_message = _result_diagnostic(state.final_result) or failure_message
        raise AgentStreamError(
            failure_message,
            progress=state.progress,
            session_id=state.session_id,
            partial_output=state.output,
            final_result=state.final_result,
        ) from exc


async def consume_stream_with_timeout(
    stream: AsyncIterator[Message],
    state: StreamState,
    observer: MessageObserver | None,
    timeout_seconds: float,
) -> None:
    """Consume an SDK stream within the configured timeout."""

    try:
        await asyncio.wait_for(
            consume_stream(stream, state, observer),
            timeout=timeout_seconds,
        )
    except TimeoutError as exc:
        raise AgentTimeoutError(
            f"Agent run timed out after {timeout_seconds:g} seconds",
            progress=state.progress,
            session_id=state.session_id,
            partial_output=state.output,
            final_result=state.final_result,
        ) from exc


async def close_stream(stream: AsyncIterator[Message]) -> None:
    """Close an SDK stream when its implementation supports explicit cleanup."""

    close = getattr(stream, "aclose", None)
    if close is None:
        return
    await close()


async def close_message_observer(
    observer: MessageObserver | None,
    *,
    is_error: bool,
    status_message: str,
) -> None:
    """Close an observer that exposes the optional run-lifecycle method."""

    close = getattr(observer, "close", None)
    if close is None:
        return
    closed = close(is_error=is_error, status_message=status_message)
    if isawaitable(closed):
        await closed


def _result_error_message(result: ResultMessage) -> str:
    return _result_diagnostic(result) or f"Agent returned an error result: {result.subtype}"


def _result_diagnostic(result: ResultMessage) -> str | None:
    if result.result:
        return result.result
    if result.errors:
        return "; ".join(result.errors)
    return result.terminal_reason or result.stop_reason
