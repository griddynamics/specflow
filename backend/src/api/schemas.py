"""Request and response models (JSON snake_case; SPECS section 9, AN 3.3)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

NAME_MAX = 200


class InvalidNameError(ValueError):
    """A project name is blank or too long."""


class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None = None


class CheckOut(BaseModel):
    """One dependency check of ``GET /readyz``; ``detail`` is a fixed code, never exception text."""

    ok: bool
    detail: str
    duration_ms: int


class ReadyzOut(BaseModel):
    """``GET /readyz``: 200 ``ready`` or 503 ``not_ready`` with every check named."""

    status: Literal["ready", "not_ready"]
    checks: dict[str, CheckOut]


class MeOut(BaseModel):
    id: uuid.UUID
    actor_id: str
    email: str
    display_name: str


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > NAME_MAX:
            raise InvalidNameError(f"name must be 1-{NAME_MAX} characters")
        return value


class ProjectOut(BaseModel):
    id: uuid.UUID
    name: str
    created_at: datetime


class DocumentOut(BaseModel):
    id: uuid.UUID
    original_filename: str
    size_bytes: int
    status: str
    error: str | None
    summarization_status: str
    created_at: datetime


class DocumentDetailOut(DocumentOut):
    markdown: str | None
    markdown_truncated: bool
    summarization: str | None
    summarization_model: str | None
    summarization_error: str | None
    summarized_at: datetime | None


class UploadOut(BaseModel):
    outcome: str | None = None
    document: DocumentOut


MODEL_MAX = 200


class SummarizeRequest(BaseModel):
    """``model`` is optional and only accepted when it equals the server's ``AGENT_MODEL``."""

    model_config = ConfigDict(extra="forbid")

    # No control characters (Cc) at all, cheap to reject before any comparison.
    model: str | None = Field(default=None, max_length=MODEL_MAX, pattern=r"^[^\x00-\x1f\x7f-\x9f]*$")


class ParseStarted(BaseModel):
    workflow_id: str
    status: str


class SummarizeStarted(BaseModel):
    workflow_id: str
    summarization_status: str


class GraphBuildStatus(BaseModel):
    """``/graph_build``: ids, counts and codes only, never document text.

    ``settled``, ``spec_changes`` (count per kind) and ``verification_ok`` come
    with a finished build; ``error_code`` with a failed one.
    """

    status: Literal["idle", "running", "succeeded", "partially_succeeded", "failed"]
    build_id: str | None = None
    mode: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    settled: bool | None = None
    spec_changes: dict[str, int] | None = None
    verification_ok: bool | None = None
    error_code: str | None = None


# Sources: Google Drive (INT-GDUI-0001..0004) -------------------------------

LINK_MAX = 2048


class DriveIdentityOut(BaseModel):
    email: str


class DriveLinkRequest(BaseModel):
    """The pasted link; checked by the server (an unusable one is ``invalid_link``, not 422)."""

    model_config = ConfigDict(extra="forbid")

    link: str = Field(max_length=LINK_MAX)


class DriveFolderOut(BaseModel):
    id: str
    name: str


class DriveCategoryOut(BaseModel):
    category: str
    processing_class: str
    count: int


class DriveTestOk(BaseModel):
    result: Literal["ok"] = "ok"
    folder: DriveFolderOut
    folder_count: int
    file_count: int
    categories: list[DriveCategoryOut]


class DriveTestFailed(BaseModel):
    result: Literal["failed"] = "failed"
    reason: str


class ProgressOut(BaseModel):
    listing_finished: bool
    total: int
    finished: int


ConnectionStateName = Literal["listing", "importing", "done", "failed"]


class SourceOut(BaseModel):
    id: uuid.UUID
    kind: Literal["gdrive"]
    folder_id: str
    link: str
    name: str
    state: ConnectionStateName
    failure_reason: str | None
    connected_by: uuid.UUID
    created_at: datetime
    last_synced_at: datetime | None
    progress: ProgressOut


class SourceCreated(BaseModel):
    source: SourceOut


class UploadsStatusOut(BaseModel):
    progress: ProgressOut
    documents: int
    failed: int


class SourcesStatusOut(BaseModel):
    progress: ProgressOut
    sources: list[SourceOut]
    uploads: UploadsStatusOut | None
    processed_documents: int = 0  # parsed documents of any origin; >= 1 = Ready


# Sources v2: unified source cards (INT-SRC-0001..0004) ---------------------------
# Field names are pinned to ``api/fixtures/sources-contract.json`` by a test.

FileStateName = Literal[
    "waiting",
    "processing",
    "done",
    "not_read",
    "skipped",
    "failed",
    "already_in_project",
]
SourceKindName = Literal["gdrive", "upload"]
BatchOutcomeName = Literal["added", "already_in_project", "read_again", "duplicate"]
MAX_EXPECTED_FILES = 10_000


class AddedByOut(BaseModel):
    id: uuid.UUID
    display_name: str


class CountsOut(BaseModel):
    waiting: int
    processing: int
    done: int
    not_read: int
    skipped: int
    failed: int
    already_in_project: int


class SourceSummaryOut(BaseModel):
    id: uuid.UUID
    kind: SourceKindName
    name: str
    name_suffix: int
    added_by: AddedByOut
    created_at: datetime
    listing_finished: bool
    open: bool
    total: int
    counts: CountsOut
    failure_reason: str | None
    folder_id: str | None
    link: str | None


class SourcesStatusV2Out(BaseModel):
    progress: ProgressOut
    processed_documents: int
    removed_files: int
    sources: list[SourceSummaryOut]


class HolderOut(BaseModel):
    id: uuid.UUID
    kind: SourceKindName
    name: str
    name_suffix: int
    created_at: datetime


class FileOut(BaseModel):
    id: uuid.UUID
    name: str
    category: str
    state: FileStateName
    reason: str | None
    holder: HolderOut | None


class FilePageOut(BaseModel):
    source: SourceSummaryOut
    items: list[FileOut]
    next_cursor: str | None


class BatchStartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_files: StrictInt = Field(ge=1, le=MAX_EXPECTED_FILES)


class BatchSourceOut(BaseModel):
    """Answer of batch start (201) and end (200)."""

    source: SourceSummaryOut


class BatchFileOut(BaseModel):
    outcome: BatchOutcomeName
    file: FileOut | None
    holder: HolderOut | None
