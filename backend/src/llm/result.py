from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from claude_agent_sdk import ResultMessage


class AgentErrorKind(StrEnum):
    """Kinds of non-exceptional agent failures."""

    RESULT = "result"
    NO_RESULT = "no_result"


@dataclass(frozen=True, slots=True)
class AgentProgress:
    """Work observed while consuming an agent message stream."""

    num_messages: int = 0
    num_tool_uses: int = 0


@dataclass(frozen=True, slots=True)
class AgentError:
    """Failure reported by a completed agent stream."""

    kind: AgentErrorKind
    message: str


@dataclass(frozen=True, slots=True)
class AgentResult:
    """Typed outcome of one agent run."""

    output: str | None
    session_id: str | None
    is_error: bool
    progress: AgentProgress
    usage: Mapping[str, Any] = field(default_factory=dict)
    error: AgentError | None = None
    final_result: ResultMessage | None = None

    @property
    def structured_output(self) -> Mapping[str, Any] | None:
        if self.final_result is None:
            return None
        structured = self.final_result.structured_output
        return structured if isinstance(structured, Mapping) else None


class AgentRunError(RuntimeError):
    """Base exception for an agent run that did not complete."""

    def __init__(
        self,
        message: str,
        *,
        progress: AgentProgress,
        session_id: str | None = None,
        partial_output: str | None = None,
        final_result: ResultMessage | None = None,
    ) -> None:
        super().__init__(message)
        self.progress = progress
        self.session_id = session_id
        self.partial_output = partial_output
        self.final_result = final_result


class AgentStreamError(AgentRunError):
    """Raised when the SDK message stream fails."""


class AgentObserverError(AgentRunError):
    """Raised when a caller-provided message observer fails."""


class AgentTimeoutError(AgentRunError):
    """Raised when an agent run exceeds its configured timeout."""


class AgentCleanupError(AgentRunError):
    """Raised when an otherwise completed run cannot release its SDK stream."""
