"""Regression tests for process_query_stream's exception-path error text handling."""

import asyncio
import logging

import pytest
from claude_agent_sdk import ResultMessage

from app.services.claude_code import process_query_stream


def _result_message(is_error, result):
    return ResultMessage(
        subtype="success",
        duration_ms=100,
        duration_api_ms=0,
        is_error=is_error,
        num_turns=1,
        session_id="sess-1",
        result=result,
    )


def test_prefers_result_message_text_over_lossy_trailing_exception():
    """A ResultMessage's `result` (e.g. "malformed response") must survive even when the
    SDK's trailing ProcessError/synthesized exception text is generic (e.g. "success"),
    since classify_error / model-routing fallback downstream key off that text.
    """

    async def _stream():
        yield _result_message(
            is_error=True,
            result="API Error: API returned an empty or malformed response (HTTP 200)",
        )
        raise Exception("Claude Code returned an error result: success")

    with pytest.raises(Exception) as excinfo:
        asyncio.run(process_query_stream(_stream(), logging.getLogger("test")))

    assert "malformed response" in str(excinfo.value)
    assert str(excinfo.value) != "Claude Code returned an error result: success"


def test_no_result_message_preserves_original_exception_text():
    async def _stream():
        if False:
            yield _result_message(is_error=False, result=None)
        raise Exception("connection reset")

    with pytest.raises(Exception) as excinfo:
        asyncio.run(process_query_stream(_stream(), logging.getLogger("test")))

    assert str(excinfo.value) == "connection reset"


def test_ignores_last_result_message_when_it_was_not_an_error():
    async def _stream():
        yield _result_message(is_error=False, result="all good")
        raise Exception("connection reset")

    with pytest.raises(Exception) as excinfo:
        asyncio.run(process_query_stream(_stream(), logging.getLogger("test")))

    assert str(excinfo.value) == "connection reset"
