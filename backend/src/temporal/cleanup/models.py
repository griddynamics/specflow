"""Inputs and results of the source cleanup (FR-SRC-0017, NFR-REL-0003).

Workflow-sandbox safe (pydantic, uuid only). Ids and counts only: never a file or
source name (history rule).
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field


class SourceCleanupInput(BaseModel):
    project_id: UUID
    source_id: UUID
    #: At most ``temporal.ids.CLEANUP_CHUNK_SIZE`` ids; the API starts one workflow per chunk.
    document_ids: list[UUID] = Field(default_factory=list)
    #: Workflow-id prefix of tier-2 tests (empty in production).
    id_prefix: str = Field(default="", max_length=64)
    #: Sleep before sweep 1, and between sweep 1 and sweep 2 (60 s, 44 min; R21). Only
    #: tests set them. A start argument, so the workflow stays deterministic on replay.
    first_delay_s: int = Field(default=60, ge=0)
    second_delay_s: int = Field(default=44 * 60, ge=0)


class SweepInput(SourceCleanupInput):
    #: 1 = about a minute after the removal, 2 = about 45 minutes after it.
    sweep: int = Field(ge=1, le=2)


class SweepResult(BaseModel):
    #: The source row still exists: the removal rolled back or is not committed; nothing deleted.
    source_present: bool
    #: Ids whose objects were deleted (no ``documents`` row has the id).
    deleted: int = 0
    #: Ids left alone (a row exists, or the source is present).
    kept: int = 0
