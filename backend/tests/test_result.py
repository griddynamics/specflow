from __future__ import annotations

from types import SimpleNamespace

from llm.result import AgentProgress, AgentResult


def test_structured_output_returns_mapping() -> None:
    result = AgentResult(
        output=None,
        session_id="session",
        is_error=False,
        progress=AgentProgress(num_messages=1),
        final_result=SimpleNamespace(structured_output={"complete": True}),
    )

    assert result.structured_output == {"complete": True}


def test_structured_output_is_none_without_mapping() -> None:
    missing = AgentResult(
        output="text",
        session_id="session",
        is_error=False,
        progress=AgentProgress(),
    )
    invalid = AgentResult(
        output=None,
        session_id="session",
        is_error=False,
        progress=AgentProgress(),
        final_result=SimpleNamespace(structured_output=["not", "a", "mapping"]),
    )

    assert missing.structured_output is None
    assert invalid.structured_output is None
