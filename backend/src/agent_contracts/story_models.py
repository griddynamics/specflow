"""The argument and result models of the people tools and the first write tools: ``list_areas``, ``list_stories``,
``preview_story``, ``claim_story`` and ``release_story``, and ``ErrorDetails`` (tools.md "Story and spec tools",
"Error details"). The API maps ``graph.plan`` objects to these; cross-field and format rules (``agent_label``,
``branch``) are checked by the API (``invalid_arguments``), not here. No model carries a user id, an email or a claim
token other than ``StoryClaim.claim_token``, the caller's own."""

from __future__ import annotations

from typing import Final

from pydantic import AwareDatetime, BaseModel, Field

from agent_contracts.models import DEFAULT_LIMIT, Limit, ProjectArguments
from agent_contracts.spec_models import MAX_ID_CHARS, FindingItem, Holder
from graph.plan.names import AreaKind, StoryKind, StoryState, StoryView, TaskProgress

MAX_NOTE_CHARS: Final = 500
MAX_TOKEN_CHARS: Final = 200
QUESTION_CHARS: Final = 80  # the question of a story row and the finding text of a preview row are cut to this
MAX_ALTERNATIVES: Final = 3
MAX_BLOCKERS: Final = 10

STORY_ID_DESCRIPTION: Final = "The story_id of a story from list_stories."
AGENT_LABEL_DESCRIPTION: Final = (
    "1 to 80 printable characters naming your agent, such as Claude Code and the machine; shown to people next to the "
    "holder."
)
BRANCH_DESCRIPTION: Final = "1 to 200 printable characters: the branch the work is on; shown to people."


# --- list_areas -----------------------------------------------------------------------------------------------


class ListAreasArgs(ProjectArguments):
    pass


class AreaInProgress(BaseModel):
    count: int
    holders: list[Holder]


class AreaItem(BaseModel):
    area_id: str
    title: str | None
    kind: AreaKind
    ready: int
    in_progress: AreaInProgress
    waiting: int
    blocked: int
    done: int
    open_tasks: int


class AreaList(BaseModel):
    project_id: str
    items: list[AreaItem]


# --- list_stories ---------------------------------------------------------------------------------------------


class ListStoriesQuery(BaseModel):
    """The arguments of ``list_stories`` besides the project: the query parameters of its route."""

    area_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_ID_CHARS,
        description="An area_id from list_areas; leave it out to list every area.",
    )
    view: StoryView = Field(
        default="open",
        description="open: stories to take, ready first; mine: yours or blocked by you; done: finished.",
    )
    limit: Limit = DEFAULT_LIMIT
    cursor: str | None = None


class ListStoriesArgs(ListStoriesQuery, ProjectArguments):
    pass


class StoryItem(BaseModel):
    rank: int
    story_id: str
    title: str
    kind: StoryKind
    state: StoryState
    holder: Holder | None
    waits_on: list[str]
    tasks: int
    unblocks: int
    findings: int
    question: str | None
    # view ``done`` only
    pr_url: str | None = None
    merge_commit: str | None = None
    completed_by: Holder | None = None
    completed_at: AwareDatetime | None = None


class StoryList(BaseModel):
    project_id: str
    area_id: str | None
    view: StoryView
    total_count: int
    items: list[StoryItem]
    next_cursor: str | None


# --- preview_story --------------------------------------------------------------------------------------------


class PreviewStoryArgs(ProjectArguments):
    story_id: str = Field(min_length=1, max_length=MAX_ID_CHARS, description=STORY_ID_DESCRIPTION)


class StoryHeader(BaseModel):
    story_id: str
    title: str
    kind: StoryKind
    area_id: str
    summary: str | None
    release: str | None
    state: StoryState
    holder: Holder | None
    since: AwareDatetime | None
    branch: str | None
    note: str | None


class StoryTaskItem(BaseModel):
    """An open task of a story or unit, as the preview and the claim show it."""

    task_id: str
    title: str
    status: str
    ready: bool
    complexity: str
    progress: TaskProgress | None


class StoryRefItem(BaseModel):
    """A story named in an answer or an error: a blocker of a preview, an alternative of ``story_claimed``."""

    story_id: str
    title: str | None
    state: StoryState
    holder: Holder | None


class StoryPreview(BaseModel):
    project_id: str
    story: StoryHeader
    story_ids: list[str]
    items: list[StoryTaskItem]
    blockers: list[StoryRefItem]
    findings: list[FindingItem]
    unblocks: list[str]


# --- claim_story, release_story -------------------------------------------------------------------------------


class ClaimStoryBody(BaseModel):
    """The JSON body of ``POST /story-claim``: the arguments besides the project."""

    story_id: str = Field(min_length=1, max_length=MAX_ID_CHARS, description=STORY_ID_DESCRIPTION)
    agent_label: str | None = Field(default=None, description=AGENT_LABEL_DESCRIPTION)
    branch: str | None = Field(default=None, description=BRANCH_DESCRIPTION)


class ClaimStoryArgs(ClaimStoryBody, ProjectArguments):
    pass


class StoryClaim(BaseModel):
    project_id: str
    story_id: str
    claim_token: str
    story_ids: list[str]
    claimed_at: AwareDatetime
    items: list[StoryTaskItem]


class ReleaseStoryBody(BaseModel):
    """The JSON body of ``POST /story-release``."""

    story_id: str = Field(min_length=1, max_length=MAX_ID_CHARS, description="The claimed story.")
    claim_token: str = Field(min_length=1, max_length=MAX_TOKEN_CHARS, description="The claim_token from claim_story.")
    note: str | None = Field(
        default=None, max_length=MAX_NOTE_CHARS, description="Why you stop and where the work stands."
    )
    branch: str | None = Field(default=None, description="The branch that holds the work so far. " + BRANCH_DESCRIPTION)


class ReleaseStoryArgs(ReleaseStoryBody, ProjectArguments):
    pass


class StoryRelease(BaseModel):
    project_id: str
    story_id: str
    released: list[str]


# --- error details --------------------------------------------------------------------------------------------


class ErrorDetails(BaseModel):
    """What an error answer may carry beside its fixed message (tools.md "Error details"); a field is set only by the
    codes that name it. No claim token, user id or email, and no text from documents except titles."""

    holder: Holder | None = None
    since: AwareDatetime | None = None
    alternatives: list[StoryRefItem] | None = Field(default=None, max_length=MAX_ALTERNATIVES)
    blockers: list[StoryRefItem] | None = Field(default=None, max_length=MAX_BLOCKERS)
    state: StoryState | None = None
    task_id: str | None = None
    argument: str | None = None
    spec_ids: list[str] | None = None
