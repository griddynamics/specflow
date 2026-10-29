from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from llm import (
    AgentError,
    AgentErrorKind,
    AgentProgress,
    AgentResult,
    GitHeadProgressDetector,
)


def failure(tool_uses: int) -> AgentResult:
    return AgentResult(
        output=None,
        session_id=None,
        is_error=True,
        progress=AgentProgress(num_tool_uses=tool_uses),
        error=AgentError(AgentErrorKind.RESULT, "failed"),
    )


def test_git_head_progress_detector_uses_commit_or_tool_floor() -> None:
    detector = GitHeadProgressDetector(Path("/workspace/repo"), min_tool_uses=20)

    assert detector.saved_progress("one", "two", failure(0))
    assert detector.saved_progress("one", "one", failure(20))
    assert not detector.saved_progress("one", "one", failure(19))


@pytest.mark.parametrize(
    ("min_tool_uses", "error"),
    [
        (-1, ValueError),
        (False, TypeError),
        (1.5, TypeError),
    ],
)
def test_git_head_progress_detector_validates_tool_floor(
    min_tool_uses: object,
    error: type[Exception],
) -> None:
    with pytest.raises(error):
        GitHeadProgressDetector(
            Path("/workspace/repo"),
            min_tool_uses=min_tool_uses,
        )


@pytest.mark.asyncio
async def test_git_head_detector_is_unavailable_when_git_cannot_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unavailable(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError

    monkeypatch.setattr(asyncio, "create_subprocess_exec", unavailable)
    detector = GitHeadProgressDetector(Path("/workspace/repo"))

    assert await detector.snapshot() is None
