"""Request and response models (JSON snake_case; SPECS section 9, AN 3.3)."""

from __future__ import annotations

import unicodedata
import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    ValidationInfo,
    field_validator,
    model_validator,
)

from agent_contracts import JiraSyncStatus
from integrations.return_to import RETURN_TO_MAX, is_same_site_path

NAME_MAX = 200
COLLABORATORS_MAX = 49
SUGGESTION_Q_MIN, SUGGESTION_Q_MAX = 3, 254


class InvalidNameError(ValueError):
    """A project or source name is blank, too long or has a control character."""


class InvalidSuggestionTextError(ValueError):
    """The suggestion text is shorter than 3 or longer than 254 characters."""


class JiraEpicPrefixError(ValueError):
    """The Epic key does not start with the Jira project key."""


class ProjectChoiceError(ValueError):
    """A GitHub Projects target names both or neither of an existing project and a new project's title."""


class ReturnToError(ValueError):
    """``return_to`` is not a path of this site."""


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


def clean_name(value: str) -> str:
    """The project name rule (create and rename): trimmed, 1-200 characters."""

    value = value.strip()
    if not value or len(value) > NAME_MAX:
        raise InvalidNameError(f"name must be 1-{NAME_MAX} characters")
    return value


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    # Emails are checked as a list by ``auth.emails.check_collaborators`` (api.md "Create with collaborators").
    collaborators: list[str] | None = Field(default=None, max_length=COLLABORATORS_MAX)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return clean_name(value)


class ProjectRename(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return clean_name(value)


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
ISSUE_KEY_MAX = 64
DRIVE_FILES_LIST_MAX = (
    1000  # = temporal.sources.models.DRIVE_EXCLUDE_MAX (asserted in the tests); the import has no cap
)
EPIC_EXCLUDE_MAX = 200  # = api.imports.jira_epic.MAX_EPIC_ISSUES (asserted in the tests)


class DriveIdentityOut(BaseModel):
    email: str


class DriveLinkRequest(BaseModel):
    """The pasted link; checked by the server (an unusable one is ``invalid_link``, not 422)."""

    model_config = ConfigDict(extra="forbid")

    link: str = Field(max_length=LINK_MAX)


DriveFileId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,128}$")]


class DriveSaveRequest(DriveLinkRequest):
    """The link to add and the files of the folder not to import (the test ignores nothing: it has no such body)."""

    exclude: list[DriveFileId] = Field(default_factory=list, max_length=DRIVE_FILES_LIST_MAX)


class DriveFolderOut(BaseModel):
    id: str
    name: str


class DriveCategoryOut(BaseModel):
    category: str
    processing_class: str
    count: int


class DriveFileOut(BaseModel):
    """One listed file: ``path`` is the folder names below the shared root, joined with ``/`` (``""`` in the root)."""

    id: str
    name: str
    path: str
    category: str


class DriveTestOk(BaseModel):
    result: Literal["ok"] = "ok"
    folder: DriveFolderOut
    folder_count: int
    file_count: int
    categories: list[DriveCategoryOut]
    files: list[DriveFileOut]  # by path, then name (case-insensitive), at most DRIVE_FILES_LIST_MAX
    files_capped: bool  # more files than listed


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
SourceKindName = Literal["gdrive", "upload", "confluence", "jira_epic", "linear_project", "box"]
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


SOURCE_NAME_MAX = 80


def clean_source_name(value: str) -> str:
    """A batch name as stored: stripped, 1..80 characters, no control characters."""

    name = value.strip()
    if not 1 <= len(name) <= SOURCE_NAME_MAX:
        raise InvalidNameError(f"the name must be 1..{SOURCE_NAME_MAX} characters")
    if any(unicodedata.category(char) in ("Cc", "Zl", "Zp") for char in name):
        raise InvalidNameError("the name must not contain control characters")
    return name


class BatchStartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_files: StrictInt = Field(ge=1, le=MAX_EXPECTED_FILES)
    name: str | None = None  # absent, null or blank: the batch keeps its default name

    @field_validator("name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return clean_source_name(value)


class SourceRenameIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return clean_source_name(value)


class BatchSourceOut(BaseModel):
    """Answer of batch start (201) and end (200)."""

    source: SourceSummaryOut


class BatchFileOut(BaseModel):
    outcome: BatchOutcomeName
    file: FileOut | None
    holder: HolderOut | None


Role = Literal["owner", "collaborator"]


class MemberOut(BaseModel):
    user_id: uuid.UUID
    name: str | None
    email: str
    role: Role
    joined_at: datetime
    is_you: bool


class InviteOut(BaseModel):
    id: uuid.UUID
    email: str
    invited_at: datetime


class MembersOut(BaseModel):
    role: Role
    limit: int
    members: list[MemberOut]
    invites: list[InviteOut]


class MemberAddedMember(BaseModel):
    kind: Literal["member"] = "member"
    member: MemberOut


class MemberAddedInvite(BaseModel):
    kind: Literal["invite"] = "invite"
    invite: InviteOut


MemberAdded = Annotated[MemberAddedMember | MemberAddedInvite, Field(discriminator="kind")]


class ClaimsOutcome(BaseModel):
    claims_released: int
    claims_release_failed: bool


class MemberAdd(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str


class SuggestionsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    q: str
    project_id: str | None = None  # caller text: a malformed id is project_not_found, as a path id is

    @field_validator("q")
    @classmethod
    def _q(cls, value: str) -> str:
        value = value.strip().lower()
        if not SUGGESTION_Q_MIN <= len(value) <= SUGGESTION_Q_MAX:
            raise InvalidSuggestionTextError(f"q must be {SUGGESTION_Q_MIN}-{SUGGESTION_Q_MAX} characters")
        return value


class SuggestionOut(BaseModel):
    name: str | None
    email: str


class SuggestionsOut(BaseModel):
    items: list[SuggestionOut]


# --- Jira settings and access check (docs/jira-sync/api.md "Web routes", "Access check", "Shapes") ---------------

JIRA_PROJECT_PATTERN = r"^[A-Z][A-Z0-9_]{1,9}$"
JIRA_EPIC_PATTERN = r"^[A-Z][A-Z0-9_]{1,9}-[1-9][0-9]*$"


class JiraAccessCheckIn(BaseModel):
    """The typed Jira target before it is saved: trimmed and upper-cased, a blank Epic is null; the Epic key carries
    the prefix of ``jira_project``."""

    model_config = ConfigDict(extra="forbid")

    jira_project: str = Field(pattern=JIRA_PROJECT_PATTERN)
    epic_key: str | None = Field(default=None, pattern=JIRA_EPIC_PATTERN)

    @field_validator("jira_project", mode="before")
    @classmethod
    def _normalise_project(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("epic_key", mode="before")
    @classmethod
    def _normalise_epic(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        text = value.strip().upper()
        return text or None

    @field_validator("epic_key")
    @classmethod
    def _epic_in_project(cls, value: str | None, info: ValidationInfo) -> str | None:
        project = info.data.get("jira_project")
        if value is not None and project is not None and not value.startswith(f"{project}-"):
            raise JiraEpicPrefixError("the Epic key must start with the Jira project key")
        return value


class JiraSettingsIn(JiraAccessCheckIn):
    enabled: StrictBool


class JiraConnectionOut(BaseModel):
    """The acting account of the target (``oauth`` mode, docs/jira-sync/oauth.md): whose Atlassian connection the
    sync uses and its state; ``missing`` when the row is gone (or no owner saved the target since ``oauth``)."""

    connected_by_name: str | None
    connected_by_me: bool
    status: Literal["active", "expired", "missing"]


class JiraSettingsOut(BaseModel):
    available: bool
    editable: bool
    site_url: str | None
    jira_project: str | None
    epic_key: str | None
    created_epic_key: str | None
    enabled: bool
    sync: JiraSyncStatus | None
    connection: JiraConnectionOut | None = Field(
        default=None, description="Null outside the oauth mode and when the project has no target."
    )


JiraAccessReason = Literal[
    "jira_auth_failed",
    "jira_not_found",
    "jira_issue_types_missing",
    "jira_forbidden",
    "jira_epic_invalid",
    "jira_unreachable",
    "jira_unexpected",
    "jira_not_connected",
    "jira_reconnect_required",
]
JiraAccessField = Literal["jira_project", "epic_key"]


class JiraAccessOk(BaseModel):
    result: Literal["ok"] = "ok"
    project_name: str
    epic_summary: str | None


class JiraAccessFailed(BaseModel):
    result: Literal["failed"] = "failed"
    reason: JiraAccessReason
    field: JiraAccessField | None


JiraAccessResult = Annotated[JiraAccessOk | JiraAccessFailed, Field(discriminator="result")]


# --- Jira gaps (docs/jira-sync/gaps.md "Routes") -------------------------------------------------------------------


class JiraGapAccessCheckIn(BaseModel):
    """The typed Jira project of the gap channel before it is saved: trimmed and upper-cased."""

    model_config = ConfigDict(extra="forbid")

    jira_project: str = Field(pattern=JIRA_PROJECT_PATTERN)

    @field_validator("jira_project", mode="before")
    @classmethod
    def _normalise_project(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value


class JiraGapsIn(JiraGapAccessCheckIn):
    grouping: Literal["one", "area"]
    enabled: StrictBool


class JiraGapsOut(BaseModel):
    """The Jira gap channel of a project: the target (null fields while there is none), the counts of its sub-tasks and
    its last poll. ``available`` is false when the server has no Atlassian OAuth or gap runs are off."""

    available: bool
    jira_project: str | None
    grouping: Literal["one", "area"] | None
    enabled: bool
    story_key: str | None
    story_url: str | None
    posted: int
    answered: int
    last_polled_at: datetime | None
    error_code: str | None = Field(description="The closed code of the last refused post or poll, or null.")
    poll_seconds: int = Field(description="Answers are read about every this many seconds.")


# --- GitHub Projects (docs/github/projects.md "Routes") ------------------------------------------------------------

GitHubProjectsState = Literal["idle", "running", "succeeded", "partial", "failed", "skipped"]
PROJECT_NODE_ID_MAX = 100
PROJECT_TITLE_MAX = 100


class GitHubProjectsCounts(BaseModel):
    created: int = 0
    updated: int = 0
    commented: int = 0
    removed: int = 0
    ranked: int = 0
    unchanged: int = 0
    failed: int = 0


class GitHubProjectsStatus(BaseModel):
    """The last export pass of the destination (``error_code`` is one closed code, never GitHub text)."""

    project_id: str
    enabled: bool
    status: GitHubProjectsState
    queued: bool
    trigger: str | None
    started_at: datetime | None
    finished_at: datetime | None
    error_code: str | None
    counts: GitHubProjectsCounts | None
    repository: str
    project_title: str


class GitHubProjectsAccessIn(BaseModel):
    """The typed destination before it is saved: the repository of the project and an organization project, or the
    title of a new one (the check then only proves the account may list and create projects)."""

    model_config = ConfigDict(extra="forbid")

    repository_id: uuid.UUID
    project_node_id: str | None = Field(default=None, min_length=1, max_length=PROJECT_NODE_ID_MAX)
    new_project_title: str | None = Field(default=None, max_length=PROJECT_TITLE_MAX)

    @field_validator("new_project_title", mode="before")
    @classmethod
    def _trim_title(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        return value.strip() or None

    @model_validator(mode="after")
    def _one_project(self) -> GitHubProjectsAccessIn:
        if (self.project_node_id is None) == (self.new_project_title is None):
            raise ProjectChoiceError("name an existing project or the title of a new one, not both and not neither")
        return self


class GitHubProjectsIn(GitHubProjectsAccessIn):
    enabled: StrictBool


class GitHubProjectChoice(BaseModel):
    node_id: str
    number: int
    title: str
    url: str


class GitHubProjectsListOut(BaseModel):
    """The open organization projects a project's repository can use (``organization`` null when a user owns it)."""

    organization: str | None
    projects: list[GitHubProjectChoice]


class GitHubProjectsOut(BaseModel):
    """The GitHub Projects destination of a project: null fields while there is none. ``available`` is false when the
    GitHub integration is off on this server."""

    available: bool
    repository_id: uuid.UUID | None
    repository: str | None
    project_node_id: str | None
    project_title: str | None
    project_url: str | None
    enabled: bool
    sync: GitHubProjectsStatus | None
    connected_by_name: str | None = Field(description="The login of the account the export acts as, or null.")
    connected_by_me: bool


GitHubProjectsAccessReason = Literal[
    "github_not_connected",
    "github_reconnect_required",
    "github_unreachable",
    "github_auth_failed",
    "github_repo_not_accessible",
    "github_not_an_organization",
    "github_permissions_missing",
    "github_project_not_found",
    "github_repo_read_only",
]
GitHubProjectsAccessField = Literal["repository", "project"]


class GitHubProjectsAccessOk(BaseModel):
    result: Literal["ok"] = "ok"
    organization: str
    project_title: str | None


class GitHubProjectsAccessFailed(BaseModel):
    result: Literal["failed"] = "failed"
    reason: GitHubProjectsAccessReason
    field: GitHubProjectsAccessField | None


GitHubProjectsAccessResult = Annotated[
    GitHubProjectsAccessOk | GitHubProjectsAccessFailed, Field(discriminator="result")
]


# --- Atlassian connections (docs/jira-sync/oauth.md "Routes") ------------------------------------------------------


class AtlassianConnectionOut(BaseModel):
    """The caller's Atlassian connection: ``mode`` is the server's Jira mode (null = Jira off)."""

    mode: Literal["oauth", "token"] | None
    connected: bool
    status: Literal["active", "expired"] | None
    account_name: str | None
    connected_at: datetime | None
    last_used_at: datetime | None


def same_site_path(value: str) -> str:
    """``value`` when it is a path of this site (``integrations.return_to``), else ``ReturnToError``."""

    if not is_same_site_path(value):
        raise ReturnToError("return_to must be a path of this site")
    return value


class AtlassianAuthorizeIn(BaseModel):
    """Where the browser returns after the consent: a same-origin path (``/``, not ``//``, so no scheme or host; no
    backslash, which a browser reads as ``/``; no dot segment or encoded separator, which a URL parser can turn into
    ``//``)."""

    model_config = ConfigDict(extra="forbid")

    return_to: str = Field(min_length=1, max_length=RETURN_TO_MAX)

    @field_validator("return_to")
    @classmethod
    def _same_origin_path(cls, value: str) -> str:
        return same_site_path(value)


class AtlassianAuthorizeOut(BaseModel):
    url: str


class AtlassianCallbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=4096)
    state: str = Field(min_length=1, max_length=4096)


class AtlassianCallbackOut(BaseModel):
    connection: AtlassianConnectionOut
    return_to: str


# --- GitHub integration (docs/github/api.md) --------------------------------------------------------------------------


class GitHubConnectionOut(BaseModel):
    """The caller's GitHub connection; ``enabled`` is whether the server has the GitHub App configured."""

    enabled: bool
    connected: bool
    status: Literal["active", "expired"] | None
    login: str | None
    connected_at: datetime | None
    last_used_at: datetime | None
    manage_url: str | None


class GitHubReturnToIn(BaseModel):
    """Where the browser returns after GitHub: a path of this site (the Atlassian rule)."""

    model_config = ConfigDict(extra="forbid")

    return_to: str = Field(min_length=1, max_length=RETURN_TO_MAX)

    @field_validator("return_to")
    @classmethod
    def _same_origin_path(cls, value: str) -> str:
        return same_site_path(value)


class GitHubUrlOut(BaseModel):
    url: str


class GitHubCallbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=4096)
    state: str = Field(min_length=1, max_length=4096)


class GitHubCallbackOut(BaseModel):
    connection: GitHubConnectionOut
    return_to: str


class GitHubSetupIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: str = Field(min_length=1, max_length=4096)


class GitHubSetupOut(BaseModel):
    return_to: str


class GitHubRepositoryOut(BaseModel):
    """A repository the caller can read through the GitHub App."""

    github_repo_id: int
    full_name: str
    html_url: str
    private: bool
    default_branch: str | None
    size_kb: int
    too_large: bool


class GitHubRepositoriesOut(BaseModel):
    repositories: list[GitHubRepositoryOut]
    truncated: bool


class ProjectRepositoryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    github_repo_id: StrictInt = Field(ge=1)


class ProjectRepositoryOut(BaseModel):
    """A GitHub repository of the project and its last clone check (docs/github/api.md)."""

    id: uuid.UUID
    github_repo_id: int
    full_name: str
    html_url: str
    private: bool
    added_by_name: str | None
    added_by_me: bool
    clone_state: Literal["pending", "cloning", "cloned", "failed"]
    failure_code: str | None
    default_branch: str | None
    head_sha: str | None
    file_count: int | None
    size_bytes: int | None
    snapshot_bytes: int | None
    checked_at: datetime | None
    cloned_at: datetime | None
    created_at: datetime


class ProjectRepositoriesOut(BaseModel):
    enabled: bool
    can_manage: bool
    repositories: list[ProjectRepositoryOut]


class ConfluenceLinkIn(BaseModel):
    """A Confluence page or space link of the configured site; ``follow`` also reads the pages and Jira issues the
    imported pages link to (one hop)."""

    model_config = ConfigDict(extra="forbid")

    link: str = Field(max_length=LINK_MAX)
    follow: bool = False


class ConfluenceCheckOut(BaseModel):
    """What the link would import: ``pages`` (at most the page cap, ``capped`` when there are more) and
    ``linked_issues``, the Jira issues the first page links to (0 without ``follow`` or for a space)."""

    kind: Literal["page", "space"]
    title: str
    pages: int
    capped: bool
    linked_issues: int


# --- Box integration (docs/box/oauth.md, docs/box/source.md) --------------------------------------------------------


class BoxConnectionOut(BaseModel):
    """The caller's Box connection; ``enabled`` is whether the server has the Box app configured. ``name`` is the
    Box display name; ``enterprise_name`` is null for a personal account."""

    enabled: bool
    connected: bool
    status: Literal["active", "expired"] | None
    name: str | None
    enterprise_name: str | None
    connected_at: datetime | None
    last_used_at: datetime | None


class BoxReturnToIn(BaseModel):
    """Where the browser returns after Box: a path of this site (the GitHub rule)."""

    model_config = ConfigDict(extra="forbid")

    return_to: str = Field(min_length=1, max_length=RETURN_TO_MAX)

    @field_validator("return_to")
    @classmethod
    def _same_origin_path(cls, value: str) -> str:
        return same_site_path(value)


class BoxUrlOut(BaseModel):
    url: str


class BoxCallbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=4096)
    state: str = Field(min_length=1, max_length=4096)


class BoxCallbackOut(BaseModel):
    connection: BoxConnectionOut
    return_to: str


class BoxLinkIn(BaseModel):
    """One Box link: a folder, a file or a shared link (docs/box/source.md "Links")."""

    model_config = ConfigDict(extra="forbid")

    link: str = Field(max_length=LINK_MAX)


class BoxCheckOut(BaseModel):
    """What the link would import: ``files`` the import takes (at most the file cap), ``skipped`` the items of
    another type that it declares and skips, ``folders`` the folders it walks, ``capped`` when a cap stopped the walk.
    ``name`` is the name of the folder or file in Box (shown, not stored by the check)."""

    kind: Literal["folder", "file"]
    name: str
    files: int
    skipped: int
    folders: int
    capped: bool


IssueKey = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]*-\d+$", max_length=ISSUE_KEY_MAX)]


class JiraEpicIn(BaseModel):
    """A Jira epic by its key (``ABC-12``) or by a link of the configured site."""

    model_config = ConfigDict(extra="forbid")

    key_or_link: str = Field(max_length=LINK_MAX)
    # issues under the epic not to import (the check ignores it)
    exclude: list[IssueKey] = Field(default_factory=list, max_length=EPIC_EXCLUDE_MAX)


class JiraEpicChildOut(BaseModel):
    """One issue under the epic: its key, summary (shown, not stored), issue type name and status name."""

    key: str
    summary: str
    type: str
    status: str


class JiraEpicCheckOut(BaseModel):
    """The epic an import would take: its key, its summary (shown, not stored) and how many issues it has for the
    member (at most the issue cap is imported)."""

    key: str
    summary: str
    issues: int
    children: list[JiraEpicChildOut]  # in key order, at most the issue cap
    capped: bool  # more issues than the cap
