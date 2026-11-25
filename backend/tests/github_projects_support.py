"""A fake GitHub behind ``ProjectsGateway``, an in-memory ``ProjectsStore`` and a harness for the GitHub Projects pass
(no tests in here). The plans are the Jira sync tests' plans."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from api.github_projects.store import TargetChangedError
from api.github_projects.sync import PushDeps, PushResult, push_once
from db.github_projects import ProjectTargetView
from db.models import GitHubProjectItem
from github.codes import GitHubCode, GitHubError
from github.projects import IssueContent, IssueRef, IssueState, ProjectInfo, RepositoryInfo
from graph.plan.reader import LoadedPlan
from jira_sync_support import T0, basic_plan

PROJECT_NODE = "PVT_project1"
REPOSITORY = "acme/shop"


@dataclass
class FakeIssue:
    number: int
    title: str
    body: str
    labels: list[str]
    parent: int | None = None
    comments: list[str] = field(default_factory=list)
    open: bool = True
    status: str | None = None
    deleted: bool = False


class FakeGateway:
    """A scripted GitHub: issues by number, the project's items in order, every call recorded. ``fail`` makes a method
    raise the given error (every time, until removed)."""

    def __init__(self) -> None:
        self.issues: dict[int, FakeIssue] = {}
        self.items: list[str] = []  # item ids in project order
        self.item_issue: dict[str, int] = {}
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fail: dict[str, GitHubError] = {}
        self.refuse_title: str | None = None  # an issue with this title is refused (422)
        self._next = 100

    def _call(self, name: str, *args: Any) -> None:
        self.calls.append((name, args))
        if name in self.fail:
            raise self.fail[name]

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def writes(self) -> list[str]:
        reads = {"issue_states", "repository", "organization_projects", "project"}
        return [name for name in self.names() if name not in reads]

    def number_of(self, title: str) -> int:
        return next(number for number, issue in self.issues.items() if issue.title == title)

    # ProjectsGateway ------------------------------------------------------------------------------------------

    async def repository(self, full_name: str) -> RepositoryInfo:
        self._call("repository", full_name)
        return RepositoryInfo(full_name, "acme", can_write=True)

    async def organization_projects(self, organization: str) -> list[ProjectInfo]:
        self._call("organization_projects", organization)
        return [ProjectInfo(PROJECT_NODE, 1, "Plan", "https://github.com/orgs/acme/projects/1")]

    async def project(self, node_id: str) -> tuple[ProjectInfo, str | None]:
        self._call("project", node_id)
        return ProjectInfo(node_id, 1, "Plan", "https://github.com/orgs/acme/projects/1"), "acme"

    async def create_project(self, organization: str, title: str) -> ProjectInfo:
        self._call("create_project", organization, title)
        return ProjectInfo("PVT_new", 2, title, "https://github.com/orgs/acme/projects/2")

    async def issue_states(self, project_id: str, issue_node_ids: Sequence[str]) -> dict[str, IssueState]:
        self._call("issue_states", project_id, tuple(issue_node_ids))
        found = {}
        for node in issue_node_ids:
            issue = self.issues.get(int(node.removeprefix("I_")))
            if issue is not None and not issue.deleted:
                found[node] = IssueState(issue.open, issue.status)
        return found

    async def create_issue(self, repository: str, content: IssueContent) -> IssueRef:
        self._call("create_issue", repository, content.title)
        if content.title == self.refuse_title:
            raise GitHubError(GitHubCode.ITEM_INVALID)
        self._next += 1
        self.issues[self._next] = FakeIssue(self._next, content.title, content.body, list(content.labels))
        return IssueRef(self._next, self._next * 10, f"I_{self._next}")

    async def edit_issue(self, repository: str, number: int, content: IssueContent) -> None:
        self._call("edit_issue", repository, number)
        issue = self.issues[number]
        issue.title, issue.body, issue.labels = content.title, content.body, list(content.labels)

    async def comment(self, repository: str, number: int, body: str) -> None:
        self._call("comment", repository, number)
        self.issues[number].comments.append(body)

    async def add_labels(self, repository: str, number: int, labels: Sequence[str]) -> None:
        self._call("add_labels", repository, number, tuple(labels))
        self.issues[number].labels += [label for label in labels if label not in self.issues[number].labels]

    async def remove_label(self, repository: str, number: int, label: str) -> None:
        self._call("remove_label", repository, number, label)
        self.issues[number].labels = [x for x in self.issues[number].labels if x != label]

    async def add_sub_issue(self, repository: str, parent: int, sub_issue_id: int) -> None:
        self._call("add_sub_issue", repository, parent, sub_issue_id)
        self.issues[sub_issue_id // 10].parent = parent

    async def add_item(self, project_id: str, issue_node_id: str) -> str:
        self._call("add_item", project_id, issue_node_id)
        item = f"PVTI_{issue_node_id}"
        self.items.append(item)
        self.item_issue[item] = int(issue_node_id.removeprefix("I_"))
        return item

    async def move_item(self, project_id: str, item_id: str, after: str | None) -> None:
        self._call("move_item", project_id, item_id, after)
        self.items.remove(item_id)
        self.items.insert(0 if after is None else self.items.index(after) + 1, item_id)


class FakeStore:
    """``ProjectsStore`` over a dict; ``changed`` makes the next write raise ``TargetChangedError``."""

    def __init__(self, target: ProjectTargetView | None) -> None:
        self.target = target
        self.items: dict[str, GitHubProjectItem] = {}
        self.changed = False

    async def get_target(self, project_id: uuid.UUID) -> ProjectTargetView | None:
        return self.target if self.target is not None and self.target.project_id == project_id else None

    async def list_items(self, project_id: uuid.UUID) -> list[GitHubProjectItem]:
        found = [item for item in self.items.values() if item.project_id == project_id]
        return sorted(found, key=lambda item: item.node_id)

    async def add_pending(self, target: ProjectTargetView, *, node_id: str, kind: str, requested_at: datetime) -> None:
        if self.changed:
            raise TargetChangedError
        if node_id not in self.items:
            self.items[node_id] = GitHubProjectItem(
                project_id=target.project_id, node_id=node_id, kind=kind, requested_at=requested_at
            )

    async def update_item(self, target: ProjectTargetView, node_id: str, **columns: Any) -> None:
        if self.changed or self.target is None or target.project_id != self.target.project_id:
            raise TargetChangedError
        for name, value in columns.items():
            setattr(self.items[node_id], name, value)


def target_view(project_id: uuid.UUID, *, enabled: bool = True) -> ProjectTargetView:
    return ProjectTargetView(
        project_id=project_id,
        repository_id=uuid.uuid4(),
        repository_name=REPOSITORY,
        project_node_id=PROJECT_NODE,
        project_title="Plan",
        project_url="https://github.com/orgs/acme/projects/1",
        enabled=enabled,
        updated_at=T0,
        last_status=None,
        last_trigger=None,
        last_started_at=None,
        last_finished_at=None,
        last_error_code=None,
        last_counts=None,
        connected_user_id=uuid.uuid4(),
    )


class Harness:
    """A project with an enabled target, the fake GitHub and the fake rows; ``push`` runs one pass."""

    def __init__(self, plan: LoadedPlan | None = None, *, enabled: bool = True) -> None:
        self.project_id = uuid.uuid4()
        self.plan = plan or basic_plan()
        self.gateway = FakeGateway()
        self.store = FakeStore(target_view(self.project_id, enabled=enabled))
        self.gateway_error: GitHubError | None = None
        self.clock = T0

    async def _gateway_for(self, _user_id: uuid.UUID | None) -> FakeGateway:
        if self.gateway_error is not None:
            raise self.gateway_error
        return self.gateway

    async def _load_plan(self, _project_id: str) -> LoadedPlan:
        return self.plan

    async def _spec_names(self, _project_id: str, _ids: Sequence[str]) -> dict[str, Any]:
        return {}

    def deps(self) -> PushDeps:
        return PushDeps(
            store=self.store,
            gateway_for=self._gateway_for,
            load_plan=self._load_plan,
            spec_names=self._spec_names,
            now=lambda: datetime.now(UTC),
        )

    async def push(self) -> PushResult:
        return await push_once(str(self.project_id), self.deps())
