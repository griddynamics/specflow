"""Pydantic inputs and results of the agents queue (SPECS sections 11 and 12).

Workflow-sandbox safe: only pydantic, uuid and typing are imported.
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

from pydantic import BaseModel, Field

from temporal.documents.models import MarkFailedInput

__all__ = [
    "CHUNK_LIMIT",
    "DocumentChunk",
    "DocumentRunContext",
    "MarkFailedInput",
    "ReadChunkInput",
    "SaveSummarizationInput",
    "SummarizeDocumentInput",
    "SummarizeDocumentResult",
    "SummaryOutput",
    "UsageSummary",
]

CHUNK_LIMIT: Final = 16000


class SummarizeDocumentInput(BaseModel):
    """``model`` is always a non-empty string, resolved by the API."""

    project_id: UUID
    document_id: UUID
    requested_by: UUID
    model: str = Field(min_length=1)
    trace_sensitive_data: bool


class UsageSummary(BaseModel):
    requests: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class SummarizeDocumentResult(BaseModel):
    document_id: UUID
    summary_chars: int = Field(ge=0)
    title: str
    model: str
    usage: UsageSummary


class DocumentRunContext(BaseModel):
    """The agent run context; the only source of ids for tools."""

    project_id: UUID
    run_id: str
    document_id: UUID


class ReadChunkInput(BaseModel):
    project_id: UUID
    document_id: UUID
    offset: int = Field(ge=0)
    limit: int = Field(default=CHUNK_LIMIT, ge=1, le=CHUNK_LIMIT)


class DocumentChunk(BaseModel):
    text: str
    offset: int = Field(ge=0)
    next_offset: int | None = None
    total_chars: int = Field(ge=0)


class SaveSummarizationInput(BaseModel):
    project_id: UUID
    document_id: UUID
    summary: str
    title: str
    model: str
    workflow_id: str
    requested_by: UUID


class SummaryOutput(BaseModel):
    """The ``submit_summary`` tool result, validated by the workflow."""

    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=8000)
