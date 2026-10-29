from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from llm.judge import (
    DEFAULT_JUDGE_SCHEMA,
    JudgeError,
    run_structured_judge,
)
from llm.result import AgentProgress, AgentResult


class FakeJudgeAgent:
    def __init__(self, result: AgentResult) -> None:
        self.result = result
        self.prompts: list[Any] = []
        self.output_formats: list[Any] = []

    async def run(
        self,
        prompt: Any,
        *,
        output_format: dict[str, Any] | None = None,
    ) -> AgentResult:
        self.prompts.append(prompt)
        self.output_formats.append(output_format)
        return self.result


def structured_result(payload: dict[str, Any]) -> AgentResult:
    return AgentResult(
        output=None,
        session_id="judge-1",
        is_error=False,
        progress=AgentProgress(num_messages=1),
        final_result=SimpleNamespace(structured_output=payload),
    )


@pytest.mark.asyncio
async def test_structured_judge_sends_criteria_and_returns_mapping() -> None:
    agent = FakeJudgeAgent(
        structured_result(
            {
                "passed": True,
                "score": 91,
                "criteria": [],
                "risks": [],
            }
        )
    )

    verdict = await run_structured_judge(
        agent,
        criteria=("Tests passed.", "CLI flow passed."),
        evidence={"workspace_checks": {"passed": True}},
        workspace_files={"README.md": "demo"},
    )

    prompt = json.loads(agent.prompts[0])
    assert verdict["passed"] is True
    assert verdict["score"] == 91
    assert prompt["criteria"] == ["Tests passed.", "CLI flow passed."]
    assert prompt["deterministic_evidence"] == {"workspace_checks": {"passed": True}}
    assert prompt["workspace_snapshot"] == {"README.md": "demo"}
    assert agent.output_formats == [{"type": "json_schema", "schema": DEFAULT_JUDGE_SCHEMA}]


@pytest.mark.asyncio
async def test_structured_judge_rejects_error_results() -> None:
    agent = FakeJudgeAgent(
        AgentResult(
            output=None,
            session_id="judge-1",
            is_error=True,
            progress=AgentProgress(num_messages=1),
            final_result=SimpleNamespace(structured_output={"passed": True}),
        )
    )

    with pytest.raises(JudgeError, match="structured output"):
        await run_structured_judge(
            agent,
            criteria=("Tests passed.",),
            evidence={},
        )


@pytest.mark.asyncio
async def test_structured_judge_requires_non_empty_criteria() -> None:
    agent = FakeJudgeAgent(structured_result({"passed": True}))

    with pytest.raises(ValueError, match="criteria"):
        await run_structured_judge(
            agent,
            criteria=("  ",),
            evidence={},
        )
