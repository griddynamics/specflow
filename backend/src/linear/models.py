"""What the Linear gateway returns: frozen, plain values with no GraphQL shape. Ids are Linear UUIDs as strings."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Literal

# The workflow state types of Linear; the status mirror moves forward along this order only.
STATE_TYPES: Final = ("triage", "backlog", "unstarted", "started", "completed", "canceled")
StateType = Literal["triage", "backlog", "unstarted", "started", "completed", "canceled"]
RELATION_TYPES: Final = ("blocks", "related", "duplicate")
RelationType = Literal["blocks", "related", "duplicate"]


@dataclass(frozen=True)
class Organization:
    id: str
    name: str
    url_key: str


@dataclass(frozen=True)
class Viewer:
    """The member the token belongs to and the workspace it was granted in."""

    id: str
    name: str
    organization: Organization


@dataclass(frozen=True)
class Team:
    id: str
    key: str
    name: str


@dataclass(frozen=True)
class WorkflowState:
    id: str
    name: str
    type: StateType
    position: float
    team_id: str


@dataclass(frozen=True)
class Label:
    id: str
    name: str
    color: str | None = None
    parent_id: str | None = None  # the group label this one belongs to
    is_group: bool = False
    team_id: str | None = None  # None = a workspace label


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    url: str
    team_ids: tuple[str, ...] = ()
    description: str | None = None


@dataclass(frozen=True)
class Milestone:
    id: str
    project_id: str
    name: str
    description: str | None = None
    sort_order: float = 0.0


@dataclass(frozen=True)
class Issue:
    id: str
    identifier: str  # ``ENG-12``
    url: str
    title: str
    state: WorkflowState
    team_id: str
    description: str | None = None
    parent_id: str | None = None
    project_id: str | None = None
    milestone_id: str | None = None
    labels: tuple[Label, ...] = ()
    estimate: int | None = None
    priority: int = 0  # 0 none, 1 urgent, 2 high, 3 medium, 4 low (Linear's scale)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    assignee_name: str | None = None  # the display name only, never an e-mail address

    @property
    def label_ids(self) -> tuple[str, ...]:
        return tuple(label.id for label in self.labels)


@dataclass(frozen=True)
class NewIssue:
    """What ``create_issue`` takes. ``issue_id`` is the client UUID (see ``linear.ids``); ``label_ids`` may not hold two
    children of one group label."""

    issue_id: str
    team_id: str
    title: str
    description: str | None = None
    parent_id: str | None = None
    project_id: str | None = None
    milestone_id: str | None = None
    label_ids: tuple[str, ...] = ()
    estimate: int | None = None
    priority: int | None = None
    state_id: str | None = None


@dataclass(frozen=True)
class Comment:
    id: str
    issue_id: str
    body: str
    user_id: str | None  # None = a comment made by an integration or a deleted user
    created_at: datetime
    user_name: str | None = None


@dataclass(frozen=True)
class Document:
    """A project document (a page of the project's Documents tab)."""

    id: str
    project_id: str
    title: str
    content: str
    url: str
    updated_at: datetime | None = None


@dataclass(frozen=True)
class Relation:
    """``issue_id`` ``type`` ``related_issue_id``: ``blocks`` reads "issue_id blocks related_issue_id"."""

    id: str
    type: RelationType
    issue_id: str
    related_issue_id: str


@dataclass(frozen=True)
class Attachment:
    id: str
    issue_id: str
    url: str
    title: str


@dataclass(frozen=True)
class Page[T]:
    """One page of a paged read; ``next_cursor`` is ``None`` on the last."""

    items: tuple[T, ...] = field(default_factory=tuple)
    next_cursor: str | None = None
