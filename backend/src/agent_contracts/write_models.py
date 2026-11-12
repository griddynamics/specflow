"""The argument and result models of the agent write tools ``add_finding``, ``report_task`` and ``complete_story``
(tools.md). Cross-field rules (blocking needs a story, a claim token and a non-``note`` kind; ``skipped`` needs a note)
and the formats of ``refers_to``, ``merge_commit`` and ``pr_url`` are checked by the API, not here. No model carries a
user id, an email or a claim token."""

from __future__ import annotations

from typing import Annotated, Final, Literal

from pydantic import AwareDatetime, BaseModel, Field

from agent_contracts.models import ProjectArguments
from agent_contracts.spec_models import MAX_ID_CHARS, Holder
from agent_contracts.story_models import MAX_NOTE_CHARS, MAX_TOKEN_CHARS
from graph.plan.models import TaskItem
from graph.plan.names import TaskProgress

MAX_TEXT_CHARS: Final = 2000
MAX_ABOUT: Final = 20
MAX_REFERS_TO: Final = 10
MAX_DONE: Final = 200

FROM_CLAIM_STORY: Final = "The claim_token from claim_story."

Id = Annotated[str, Field(max_length=MAX_ID_CHARS)]

FindingKind = Literal["gap", "conflict", "question", "note"]
FindingStatus = Literal["open", "resolved", "dismissed"]
RefType = Literal["pr", "commit", "file", "url"]


# --- add_finding ----------------------------------------------------------------------------------------------


class RefersTo(BaseModel):
    """A thing outside the spec a finding is about; stored, never fetched. The API checks ``value`` by ``type``."""

    type: RefType = Field(description="pr, commit, file or url.")
    value: str = Field(description="Checked by type.")


class AddFindingBody(BaseModel):
    """The JSON body of ``POST /findings``: the arguments besides the project."""

    kind: FindingKind = Field(
        description="gap: the spec does not say; conflict: two parts disagree; question: for the owner; note: learned."
    )
    text: str = Field(
        min_length=1, max_length=MAX_TEXT_CHARS, description="What you found, in 1 to 2000 characters; no secrets."
    )
    about: list[Id] = Field(
        min_length=1, max_length=MAX_ABOUT, description="1 to 20 spec ids the finding is about; aliases resolve."
    )
    refers_to: list[RefersTo] = Field(
        default_factory=list, max_length=MAX_REFERS_TO, description="Up to 10 pull requests, commits, files or URLs."
    )
    story_id: str | None = Field(
        default=None, min_length=1, max_length=MAX_ID_CHARS, description="The story the finding came from."
    )
    claim_token: str | None = Field(
        default=None, min_length=1, max_length=MAX_TOKEN_CHARS, description="Required when blocking."
    )
    blocking: bool = Field(default=False, description="The story cannot go on until a person answers.")


class AddFindingArgs(AddFindingBody, ProjectArguments):
    pass


class FindingDetail(BaseModel):
    """A finding as ``add_finding`` answers it."""

    finding_id: str
    kind: FindingKind
    text: str
    status: FindingStatus
    blocking: bool
    about: list[str]
    refers_to: list[RefersTo]
    story_id: str | None
    author: Holder
    created_at: AwareDatetime
    orphaned: bool


class FindingResult(BaseModel):
    project_id: str
    finding: FindingDetail
    created: bool
    blocked: list[str]


# --- report_task ----------------------------------------------------------------------------------------------


class ReportTaskBody(BaseModel):
    """The JSON body of ``POST /task-progress``."""

    task_id: str = Field(min_length=1, max_length=MAX_ID_CHARS, description="A task of the story you claimed.")
    claim_token: str = Field(min_length=1, max_length=MAX_TOKEN_CHARS, description=FROM_CLAIM_STORY)
    progress: TaskProgress = Field(
        description="done: built and tested on the branch; skipped: not needed, note says why; todo: not done (again)."
    )
    note: str | None = Field(default=None, max_length=MAX_NOTE_CHARS, description="A short note; required for skipped.")


class ReportTaskArgs(ReportTaskBody, ProjectArguments):
    pass


class ReportedTask(TaskItem):
    """A ``list_tasks`` item with the progress just reported (its status stays ``in_progress``)."""

    progress: TaskProgress | None = None


class TaskReport(BaseModel):
    project_id: str
    task: ReportedTask


# --- complete_story -------------------------------------------------------------------------------------------


class CompleteStoryBody(BaseModel):
    """The JSON body of ``POST /story-completion``."""

    story_id: str = Field(min_length=1, max_length=MAX_ID_CHARS, description="The claimed story.")
    claim_token: str = Field(min_length=1, max_length=MAX_TOKEN_CHARS, description=FROM_CLAIM_STORY)
    merge_commit: str = Field(description="The merge commit: the full 40 or 64 character hexadecimal hash.")
    pr_url: str | None = Field(default=None, description="The merged pull request, an http or https URL.")
    done: list[Id] | None = Field(
        default=None,
        max_length=MAX_DONE,
        description="The task ids the merge finished; leave it out for every task of the claim.",
    )


class CompleteStoryArgs(CompleteStoryBody, ProjectArguments):
    pass


class StoryCompletion(BaseModel):
    project_id: str
    story_id: str
    completed: list[str]
    released: list[str]
    now_available: list[str]
    spec_changed: list[str]
