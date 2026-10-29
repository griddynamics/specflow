from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    ServerToolUseBlock,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
)

import llm.agent as agent_module
from llm import (
    Agent,
    AgentCleanupError,
    AgentErrorKind,
    AgentObserverError,
    AgentRunError,
    AgentSettings,
    AgentStreamError,
    AgentTimeoutError,
)
from llm.environment import REDACTED_ENV_VALUE


class FakeStream:
    def __init__(
        self,
        messages: list[object],
        *,
        error: Exception | None = None,
        close_error: Exception | None = None,
    ) -> None:
        self._messages = iter(messages)
        self._error = error
        self._close_error = close_error
        self.closed = False

    def __aiter__(self) -> FakeStream:
        return self

    async def __anext__(self) -> object:
        try:
            return next(self._messages)
        except StopIteration as stop:
            if self._error is not None:
                error, self._error = self._error, None
                raise error from stop
            raise StopAsyncIteration from stop

    async def aclose(self) -> None:
        self.closed = True
        if self._close_error is not None:
            raise self._close_error


class BlockingStream:
    def __init__(self, messages: list[object] | None = None) -> None:
        self._messages = iter(messages or [])
        self.started = asyncio.Event()
        self.closed = False

    def __aiter__(self) -> BlockingStream:
        return self

    async def __anext__(self) -> object:
        try:
            return next(self._messages)
        except StopIteration:
            pass
        self.started.set()
        await asyncio.Event().wait()
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.closed = True


class ManagedObserver:
    def __init__(self, *, close_error: Exception | None = None) -> None:
        self.messages: list[object] = []
        self.closes: list[tuple[bool, str]] = []
        self.close_error = close_error

    def __call__(self, message: object) -> None:
        self.messages.append(message)

    async def close(self, *, is_error: bool, status_message: str) -> None:
        self.closes.append((is_error, status_message))
        if self.close_error is not None:
            raise self.close_error


def result_message(
    *,
    result: str | None = "done",
    is_error: bool = False,
    session_id: str = "session-1",
    usage: dict[str, Any] | None = None,
    errors: list[str] | None = None,
) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=100,
        duration_api_ms=80,
        is_error=is_error,
        num_turns=1,
        session_id=session_id,
        result=result,
        usage=usage,
        errors=errors,
    )


def assistant_message(
    *blocks: object,
    session_id: str | None = None,
) -> AssistantMessage:
    return AssistantMessage(
        content=list(blocks),
        model="test-model",
        session_id=session_id,
    )


def install_stream(
    monkeypatch: pytest.MonkeyPatch,
    stream: FakeStream | BlockingStream,
) -> dict[str, object]:
    captured: dict[str, object] = {}

    def fake_query(*, prompt: object, options: object) -> object:
        captured["prompt"] = prompt
        captured["options"] = options
        return stream

    monkeypatch.setattr(agent_module, "query", fake_query)
    return captured


@pytest.mark.asyncio
async def test_success_uses_final_result_and_tracks_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    final = result_message(result="final", usage={"input_tokens": 3})
    stream = FakeStream(
        [
            assistant_message(
                TextBlock(text="fallback"),
                ToolUseBlock(id="tool-1", name="Read", input={}),
            ),
            final,
        ]
    )
    install_stream(monkeypatch, stream)

    result = await Agent(model="test-model").run("hello")

    assert result.output == "final"
    assert result.session_id == "session-1"
    assert result.is_error is False
    assert result.error is None
    assert result.usage == {"input_tokens": 3}
    assert result.progress.num_messages == 2
    assert result.progress.num_tool_uses == 1
    assert result.final_result is final
    assert stream.closed


@pytest.mark.asyncio
async def test_error_result_is_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = FakeStream([result_message(result="request failed", is_error=True)])
    install_stream(monkeypatch, stream)

    result = await Agent(model="test-model").run("hello")

    assert result.output == "request failed"
    assert result.is_error is True
    assert result.error is not None
    assert result.error.kind is AgentErrorKind.RESULT
    assert result.error.message == "request failed"
    assert stream.closed


@pytest.mark.asyncio
async def test_error_result_uses_sdk_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = FakeStream(
        [
            result_message(
                result=None,
                is_error=True,
                errors=["first diagnostic", "second diagnostic"],
            )
        ]
    )
    install_stream(monkeypatch, stream)

    result = await Agent(model="test-model").run("hello")

    assert result.error is not None
    assert result.error.message == "first diagnostic; second diagnostic"


@pytest.mark.asyncio
async def test_streamed_text_falls_back_when_final_text_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = FakeStream(
        [
            assistant_message(TextBlock(text="available text")),
            result_message(result=None),
        ]
    )
    install_stream(monkeypatch, stream)

    result = await Agent(model="test-model").run("hello")

    assert result.output == "available text"
    assert result.session_id == "session-1"
    assert result.is_error is False
    assert stream.closed


@pytest.mark.asyncio
async def test_stream_ending_without_result_is_error_but_preserves_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = FakeStream(
        [
            assistant_message(
                TextBlock(text="available text"),
                session_id="partial-session",
            )
        ]
    )
    install_stream(monkeypatch, stream)

    result = await Agent(model="test-model").run("hello")

    assert result.output == "available text"
    assert result.session_id == "partial-session"
    assert result.is_error is True
    assert result.error is not None
    assert result.error.kind is AgentErrorKind.NO_RESULT
    assert result.final_result is None
    assert stream.closed


@pytest.mark.asyncio
async def test_empty_stream_returns_typed_no_result_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = FakeStream([])
    install_stream(monkeypatch, stream)

    result = await Agent(model="test-model").run(
        "hello",
        resume_session_id="resumed-session",
    )

    assert result.output is None
    assert result.session_id == "resumed-session"
    assert result.is_error is True
    assert result.error is not None
    assert result.error.kind is AgentErrorKind.NO_RESULT
    assert stream.closed


@pytest.mark.asyncio
async def test_string_input_and_resume_are_passed_to_sdk(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    stream = FakeStream([result_message()])
    captured = install_stream(monkeypatch, stream)
    agent = Agent(
        model="test-model",
        system_prompt="system",
        cwd=tmp_path,
    )

    await agent.run("hello", resume_session_id="resume-me")

    assert captured["prompt"] == "hello"
    options = captured["options"]
    assert options.model == "test-model"
    assert options.system_prompt == "system"
    assert options.cwd == tmp_path
    assert options.resume == "resume-me"
    assert all(value == REDACTED_ENV_VALUE for value in options.env.values())


@pytest.mark.asyncio
async def test_message_stream_input_is_passed_to_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def messages() -> AsyncIterable[dict[str, Any]]:
        yield {"type": "user", "message": {"role": "user", "content": "hello"}}

    prompt_stream = messages()
    stream = FakeStream([result_message()])
    captured = install_stream(monkeypatch, stream)

    await Agent(model="test-model").run(prompt_stream)

    assert captured["prompt"] is prompt_stream
    await prompt_stream.aclose()


@pytest.mark.asyncio
async def test_explicit_gateway_environment_overrides_sdk_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = FakeStream([result_message()])
    captured = install_stream(monkeypatch, stream)

    await Agent(
        model="z-ai/glm-5",
        settings=AgentSettings(
            extra_env={
                "ANTHROPIC_AUTH_TOKEN": "test-key",
                "ANTHROPIC_API_KEY": "",
                "ANTHROPIC_BASE_URL": "https://gateway.example/anthropic",
            }
        ),
    ).run("hello")

    options = captured["options"]
    assert options.model == "z-ai/glm-5"
    expected_environment = {
        "ANTHROPIC_AUTH_TOKEN": "test-key",
        "ANTHROPIC_API_KEY": "",
        "ANTHROPIC_BASE_URL": "https://gateway.example/anthropic",
    }
    assert {name: options.env[name] for name in expected_environment} == expected_environment


@pytest.mark.asyncio
async def test_error_result_text_survives_lossy_trailing_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = FakeStream(
        [
            result_message(
                result="API returned an empty or malformed response",
                is_error=True,
            )
        ],
        error=Exception("Agent returned an error result: success"),
    )
    install_stream(monkeypatch, stream)

    with pytest.raises(AgentStreamError, match="malformed response") as exc_info:
        await Agent(model="test-model").run("hello")

    assert exc_info.value.progress.num_messages == 1
    assert exc_info.value.session_id == "session-1"
    assert exc_info.value.partial_output == "API returned an empty or malformed response"
    assert exc_info.value.final_result is not None
    assert stream.closed


@pytest.mark.asyncio
async def test_original_exception_survives_without_error_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = FakeStream([], error=Exception("connection reset"))
    install_stream(monkeypatch, stream)

    with pytest.raises(AgentStreamError, match="connection reset"):
        await Agent(model="test-model").run("hello")

    assert stream.closed


@pytest.mark.asyncio
async def test_success_result_does_not_replace_trailing_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = FakeStream(
        [result_message(result="all good")],
        error=Exception("connection reset"),
    )
    install_stream(monkeypatch, stream)

    with pytest.raises(AgentStreamError, match="connection reset"):
        await Agent(model="test-model").run("hello")

    assert stream.closed


@pytest.mark.asyncio
async def test_timeout_closes_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = BlockingStream(
        [
            assistant_message(
                TextBlock(text="partial"),
                ToolUseBlock(id="tool-1", name="Read", input={}),
                session_id="session-before-timeout",
            )
        ]
    )
    install_stream(monkeypatch, stream)

    with pytest.raises(AgentTimeoutError, match="timed out") as exc_info:
        await Agent(model="test-model", timeout=0.01).run("hello")

    assert isinstance(exc_info.value, AgentRunError)
    assert exc_info.value.progress.num_messages == 1
    assert exc_info.value.progress.num_tool_uses == 1
    assert exc_info.value.session_id == "session-before-timeout"
    assert exc_info.value.partial_output == "partial"
    assert stream.closed


@pytest.mark.asyncio
async def test_cancellation_closes_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = BlockingStream()
    install_stream(monkeypatch, stream)
    task = asyncio.create_task(Agent(model="test-model").run("hello"))
    await stream.started.wait()

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed


@pytest.mark.asyncio
async def test_observer_receives_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages = [
        assistant_message(TextBlock(text="hello")),
        result_message(),
    ]
    stream = FakeStream(messages)
    install_stream(monkeypatch, stream)
    observed: list[object] = []

    async def observe(message: object) -> None:
        observed.append(message)

    await Agent(model="test-model", message_observer=observe).run("hello")

    assert observed == messages


@pytest.mark.asyncio
async def test_managed_observer_is_closed_after_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages = [result_message()]
    install_stream(monkeypatch, FakeStream(messages))
    observer = ManagedObserver()

    await Agent(model="test-model", message_observer=observer).run("hello")

    assert observer.messages == messages
    assert observer.closes == [(False, "agent_result")]


@pytest.mark.asyncio
async def test_managed_observer_is_closed_after_incomplete_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_stream(monkeypatch, FakeStream([]))
    observer = ManagedObserver()

    result = await Agent(model="test-model", message_observer=observer).run("hello")

    assert result.is_error is True
    assert observer.closes == [(True, "stream_ended_without_result")]


@pytest.mark.asyncio
async def test_managed_observer_is_closed_after_stream_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_stream(
        monkeypatch,
        FakeStream([], error=RuntimeError("transport failed")),
    )
    observer = ManagedObserver()

    with pytest.raises(AgentStreamError, match="transport failed"):
        await Agent(model="test-model", message_observer=observer).run("hello")

    assert observer.closes == [(True, "agent_run_failed")]


@pytest.mark.asyncio
async def test_managed_observer_close_failure_is_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_stream(monkeypatch, FakeStream([result_message()]))
    observer = ManagedObserver(close_error=RuntimeError("close observer failed"))

    with pytest.raises(AgentObserverError, match="observer cleanup failed"):
        await Agent(model="test-model", message_observer=observer).run("hello")


@pytest.mark.asyncio
async def test_observer_failure_is_distinct_from_stream_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = FakeStream(
        [
            assistant_message(
                TextBlock(text="partial"),
                session_id="observer-session",
            )
        ]
    )
    install_stream(monkeypatch, stream)

    async def observe(message: object) -> None:
        raise RuntimeError("observer failed")

    with pytest.raises(AgentObserverError, match="observer failed") as exc_info:
        await Agent(model="test-model", message_observer=observe).run("hello")

    assert exc_info.value.partial_output == "partial"
    assert exc_info.value.session_id == "observer-session"
    assert isinstance(exc_info.value.__cause__, RuntimeError)
    assert stream.closed


@pytest.mark.asyncio
async def test_session_id_is_captured_from_system_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = FakeStream(
        [
            SystemMessage(
                subtype="init",
                data={"session_id": "system-session"},
            )
        ]
    )
    install_stream(monkeypatch, stream)

    result = await Agent(model="test-model").run("hello")

    assert result.session_id == "system-session"
    assert result.is_error is True


@pytest.mark.asyncio
async def test_server_tool_uses_count_as_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = FakeStream(
        [
            assistant_message(
                ServerToolUseBlock(
                    id="server-tool-1",
                    name="web_search",
                    input={},
                )
            ),
            result_message(),
        ]
    )
    install_stream(monkeypatch, stream)

    result = await Agent(model="test-model").run("hello")

    assert result.progress.num_tool_uses == 1


@pytest.mark.asyncio
async def test_cleanup_failure_after_completion_is_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = FakeStream(
        [result_message()],
        close_error=RuntimeError("close failed"),
    )
    install_stream(monkeypatch, stream)

    with pytest.raises(AgentCleanupError) as exc_info:
        await Agent(model="test-model").run("hello")

    assert exc_info.value.session_id == "session-1"
    assert exc_info.value.partial_output == "done"
    assert isinstance(exc_info.value.__cause__, RuntimeError)


@pytest.mark.asyncio
async def test_cleanup_failure_does_not_mask_stream_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = FakeStream(
        [],
        error=RuntimeError("stream failed"),
        close_error=RuntimeError("close failed"),
    )
    install_stream(monkeypatch, stream)

    with pytest.raises(AgentStreamError, match="stream failed") as exc_info:
        await Agent(model="test-model").run("hello")

    assert any("cleanup also failed" in note for note in exc_info.value.__notes__)


def test_timeout_must_be_positive() -> None:
    with pytest.raises(ValueError, match="timeout must be positive"):
        Agent(model="test-model", timeout=0)


def test_model_must_not_be_empty() -> None:
    with pytest.raises(ValueError, match="model must not be empty"):
        Agent(model=" ")


@pytest.mark.asyncio
async def test_resume_session_id_must_not_be_empty() -> None:
    with pytest.raises(ValueError, match="resume_session_id must not be empty"):
        await Agent(model="test-model").run("hello", resume_session_id=" ")
