from __future__ import annotations

from contextlib import suppress

from claude_agent_sdk import (
    AssistantMessage,
    Message,
    ResultMessage,
    ServerToolResultBlock,
    ServerToolUseBlock,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from llm.tracing.session import TraceSession


class ClaudeTraceObserver:
    """Translate Claude Agent SDK messages into a generic tracing session."""

    def __init__(self, session: TraceSession) -> None:
        self.session = session

    @property
    def enabled(self) -> bool:
        return self.session.enabled

    @property
    def closed(self) -> bool:
        return self.session.closed

    @property
    def error_count(self) -> int:
        return self.session.error_count

    def __call__(self, message: Message) -> None:
        if not self.enabled:
            return
        with suppress(Exception):
            if self.closed:
                self.session.begin_next_run()
            session_id = getattr(message, "session_id", None)
            if not session_id and isinstance(message, SystemMessage):
                session_id = message.data.get("session_id")
            model = message.model if isinstance(message, AssistantMessage) else None
            self.session.message(session_id=session_id, model=model)
            self._dispatch(message)
            return
        self.session.record_failure()

    def close(
        self,
        *,
        is_error: bool = False,
        status_message: str = "stream_ended_without_result",
    ) -> None:
        self.session.close(is_error=is_error, status_message=status_message)

    def _dispatch(self, message: Message) -> None:
        if isinstance(message, SystemMessage):
            self.session.system_message(message.data)
        elif isinstance(message, AssistantMessage):
            self._assistant(message)
        elif isinstance(message, UserMessage):
            self._user(message)
        elif isinstance(message, ResultMessage):
            self.session.result(
                is_error=message.is_error,
                usage=message.usage,
                cost=message.total_cost_usd,
                output=message.result,
                metadata={
                    "subtype": message.subtype,
                    "num_turns": message.num_turns,
                    "duration_ms": message.duration_ms,
                    "duration_api_ms": message.duration_api_ms,
                    "stop_reason": message.stop_reason,
                },
            )

    def _assistant(self, message: AssistantMessage) -> None:
        for block in message.content:
            if isinstance(block, TextBlock):
                self.session.assistant_text(block.text)
            elif isinstance(block, ThinkingBlock):
                self.session.reasoning(block.thinking)
            elif isinstance(block, (ToolUseBlock, ServerToolUseBlock)):
                self.session.tool_started(
                    tool_id=block.id,
                    name=block.name,
                    is_server=isinstance(block, ServerToolUseBlock),
                    tool_input=block.input,
                )

    def _user(self, message: UserMessage) -> None:
        if not isinstance(message.content, list):
            return
        for block in message.content:
            if isinstance(block, (ToolResultBlock, ServerToolResultBlock)):
                self.session.tool_finished(
                    tool_id=block.tool_use_id,
                    output=block.content,
                    is_error=bool(getattr(block, "is_error", False)),
                )
