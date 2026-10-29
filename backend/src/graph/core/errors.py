"""Every expected failure of the library is a ``GraphError`` (docs/graph/architecture.md "Errors").

An error carries a stable ``code`` and fields that hold ids, counts and codes only, never document
text, prompt text or secrets, so it is safe to log and to show in a build result.
"""

from __future__ import annotations

from typing import Literal, override

FieldValue = str | int | float | bool | None

LLMErrorReason = Literal["malformed", "refused", "overflow", "truncated", "exhausted", "fatal"]


class GraphError(Exception):
    """Base error: ``code`` names the failure, ``fields`` add ids and counts."""

    def __init__(self, code: str, **fields: FieldValue) -> None:
        self.code = code
        self.fields: dict[str, FieldValue] = dict(fields)
        details = " ".join(f"{key}={value}" for key, value in sorted(self.fields.items()))
        super().__init__(f"{code} {details}".rstrip())


class ConfigurationError(GraphError):
    """Invalid settings or options, unsupported Neo4j, rejected Neo4j credentials."""


class OntologyError(ConfigurationError):
    """An invalid ontology; ``problems`` lists every problem found."""

    def __init__(self, problems: list[str] | tuple[str, ...], **fields: FieldValue) -> None:
        self.problems: tuple[str, ...] = tuple(problems)
        super().__init__("ontology_invalid", problems=len(self.problems), **fields)

    @override
    def __str__(self) -> str:
        return "\n".join([super().__str__(), *(f"- {problem}" for problem in self.problems)])


class CorpusError(GraphError):
    """The corpus breaks its contract (missing lineage id, version or markdown)."""


class OwnershipError(GraphError):
    """A write targets a type its owner does not own."""


class LLMError(GraphError):
    """A model call failed; ``reason`` says how."""

    def __init__(self, reason: LLMErrorReason, **fields: FieldValue) -> None:
        self.reason: LLMErrorReason = reason
        super().__init__(f"llm_{reason}", **fields)


class LLMFatalError(LLMError):
    """The endpoint rejected the credentials or the model, or the endpoint is wrong."""

    def __init__(self, **fields: FieldValue) -> None:
        super().__init__("fatal", **fields)


class InfrastructureUnavailableError(GraphError):
    """The LLM endpoint, Neo4j or a port backend is unreachable."""


class GraphWriteError(GraphError):
    """A Neo4j write failed; persist resumes where it stopped."""


class ConformanceError(GraphError):
    """An internal consistency check failed: a bug, not bad data."""
