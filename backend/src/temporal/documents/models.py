"""Pydantic inputs and results of the parse queue (SPECS section 11).

Workflow-sandbox safe: only pydantic, uuid and typing are imported.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field


class ParseDocumentInput(BaseModel):
    project_id: UUID
    document_id: UUID


class ParseDocumentResult(BaseModel):
    document_id: UUID
    status: str
    markdown_chars: int = Field(ge=0)


class MarkFailedInput(BaseModel):
    """Input of ``mark_document_failed`` and ``mark_summarization_failed``.

    ``error_code`` is a fixed, sanitized code (it is stored in history).
    """

    project_id: UUID
    document_id: UUID
    error_code: str = Field(min_length=1, max_length=100)
