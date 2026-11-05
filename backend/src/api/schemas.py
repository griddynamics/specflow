"""Request and response models (JSON snake_case; SPECS section 9, AN 3.3)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator

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


class GraphBuildRequest(BaseModel):
    """Optional body of ``POST /graph_build``; the level is only validated and logged."""

    model_config = ConfigDict(extra="forbid")

    reasoning_level: int | None = Field(default=None, ge=1, le=5)


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


# Plan (graph.plan) ---------------------------------------------------------


class PlanStartRequest(BaseModel):
    """Optional body of ``POST /plan``: ``replan`` re-plans every area from the current graph."""

    model_config = ConfigDict(extra="forbid")

    replan: StrictBool = False


class PlanStatus(BaseModel):
    """``/plan``: the latest plan run and whether the project has a plan; ids, step names and codes only.

    ``step`` is the step that runs (or the last one that ran); ``steps_done`` of ``steps_total`` are done.
    While a long step runs, ``step_items_done`` of ``step_items_total`` of its items are done (the Tasks step:
    planning parts); null otherwise.
    """

    status: Literal["idle", "running", "succeeded", "partially_succeeded", "failed"]
    has_plan: bool = False
    run_id: str | None = None
    mode: str | None = None
    replan: bool | None = None
    step: str | None = None
    steps_done: int = 0
    steps_total: int = 0
    step_items_done: int | None = None
    step_items_total: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    unchanged: bool | None = None
    error_code: str | None = None


class PlanNotBuildableOut(BaseModel):
    """A member of an area the planner returned as no code work: its id, name (none when the node is gone),
    reason code and note."""

    spec_id: str
    name: str | None = None
    reason: str
    note: str | None = None


class PlanAreaOut(BaseModel):
    area_id: str
    kind: str
    title: str | None = None
    description: str | None = None
    status: str
    planned_by: str | None = None
    not_buildable: list[PlanNotBuildableOut] = []


class PlanStoryOut(BaseModel):
    story_id: str
    area_id: str
    kind: str
    title: str
    summary: str | None = None
    progress: str
    status: str


class PlanDoneWhenOut(BaseModel):
    text: str
    source: str


class PlanSpecRef(BaseModel):
    """A spec node a task implements: its id, label and name (none when the node is gone)."""

    spec_id: str
    label: str | None = None
    name: str | None = None


class PlanTaskOut(BaseModel):
    task_id: str
    area_id: str
    story_id: str
    phase: str
    title: str
    description: str | None = None
    done_when: list[PlanDoneWhenOut] = []
    complexity: str
    status: str
    implements: list[PlanSpecRef] = []
    waits_for: list[str] = []
    ready: bool = False
    revision: int
    commit: str | None = None
    block_reason: str | None = None
    has_open_questions: bool = False
    planned_by: str = "model"


class PlanReviewIssueOut(BaseModel):
    """One finding of the run's review: advisory, it changed nothing."""

    area_id: str
    task_id: str | None = None
    problem: str
    severity: str


class PlanQualityOut(BaseModel):
    """How the run that wrote the plan went.

    ``fallbacks`` counts planner answers that stayed invalid (or failed) and were replaced by rules, per
    planner (``describe``, ``project``, ``tasks``); ``review`` counts review findings by severity;
    ``unplaced`` counts spec nodes no area anchor reached; ``oversized`` counts tasks over the task size.
    """

    run_id: str
    status: str
    finished_at: datetime | None = None
    fallbacks: dict[str, int] = {}
    tasks_planned: int = 0
    review: dict[str, int] = {}
    review_failed: bool = False
    unplaced: int = 0
    oversized: int = 0


class PlanContent(BaseModel):
    """``/plan/tasks``: the persisted plan; tasks in phase order, then by area, story and id. ``quality`` and
    ``review`` come from the run that last wrote the plan (none when its result is gone)."""

    plan_id: str
    last_build_id: str | None = None
    updated_at: datetime
    areas: list[PlanAreaOut]
    stories: list[PlanStoryOut]
    tasks: list[PlanTaskOut]
    quality: PlanQualityOut | None = None
    review: list[PlanReviewIssueOut] = []


class PlanFeatureOut(BaseModel):
    spec_id: str
    name: str


class PlanPreview(BaseModel):
    """``/plan/preview``: what a plan run would start from, before it runs.

    ``features`` are the spec nodes that anchor feature stories; ``calls`` and ``input_tokens`` are the
    estimate of the run (none without a finished graph build or when the estimate failed).
    """

    build_id: str | None = None
    mode: str | None = None
    unchanged: bool | None = None
    calls: int | None = None
    input_tokens: int | None = None
    features: list[PlanFeatureOut] = []


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
