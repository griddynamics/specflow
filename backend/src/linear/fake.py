"""``FakeLinear``: an in-memory ``LinearGateway`` that behaves like Linear where the streams depend on it (client UUID
ids and "already exists" on a reused one, team-scoped identifiers, parent and child issues, relations, comments with
authors, workflow state types, estimate validation, the project and milestone tree, paging) and lets a test inject
failures and rate limits. For tests of the Linear streams and parity tests of ``linear.http``; never used in production.

Seeding helpers (``add_team``, ``add_user``, ``add_project`` ...) are plain methods; ``calls`` records every gateway
call and ``writes`` every mutation, so a test can assert "a re-run makes no write"."""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from linear.errors import (
    LinearAlreadyExistsError,
    LinearError,
    LinearInvalidError,
    LinearNotFoundError,
    LinearRateLimitedError,
)
from linear.models import (
    RELATION_TYPES,
    Attachment,
    Comment,
    Document,
    Issue,
    Label,
    Milestone,
    NewIssue,
    Organization,
    Page,
    Project,
    Relation,
    RelationType,
    StateType,
    Team,
    Viewer,
    WorkflowState,
)

PAGE_SIZE: Final = 250
MAX_PRIORITY: Final = 4
UPDATABLE: Final = frozenset(
    {"title", "description", "state_id", "estimate", "priority", "label_ids", "project_id", "milestone_id", "parent_id"}
)
DEFAULT_STATES: Final[tuple[tuple[str, StateType], ...]] = (
    ("Backlog", "backlog"),
    ("Todo", "unstarted"),
    ("In Progress", "started"),
    ("In Review", "started"),
    ("Done", "completed"),
    ("Canceled", "canceled"),
)
_IDENTIFIER: Final = re.compile(r"[A-Za-z][A-Za-z0-9]*-\d+")
_ISSUE_URL: Final = re.compile(r"https?://linear\.app/([^/]+)/issue/([A-Za-z][A-Za-z0-9]*-\d+)(?:/.*)?")
_PROJECT_URL: Final = re.compile(r"https?://linear\.app/([^/]+)/project/([^/?#]+)(?:/.*)?")
_WRITES: Final = frozenset(
    {
        "create_project",
        "create_milestone",
        "create_label",
        "create_issue",
        "update_issue",
        "create_comment",
        "create_relation",
        "attach_link",
    }
)


@dataclass
class _Injection:
    method: str | None
    error: LinearError
    times: int


def _uuid(value: str, field: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise LinearInvalidError(field) from None


class FakeLinear:
    """One member's view of one Linear workspace."""

    def __init__(
        self,
        *,
        organization: Organization | None = None,
        user_id: str | None = None,
        user_name: str = "Fake Member",
        estimate_allowed: Callable[[int], bool] | None = None,
        page_size: int = PAGE_SIZE,
        start: datetime | None = None,
    ) -> None:
        self.organization = organization or Organization(str(uuid.uuid4()), "Graphico", "graphico-app")
        self.user_id = user_id or str(uuid.uuid4())
        self.user_name = user_name
        self.estimate_allowed = estimate_allowed  # the team's estimate scale; None = any integer
        self.page_size = page_size
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.writes: list[tuple[str, tuple[Any, ...]]] = []
        self._clock = start or datetime(2026, 1, 1, tzinfo=UTC)
        self._users: dict[str, str] = {self.user_id: user_name}
        self._teams: dict[str, Team] = {}
        self._states: dict[str, list[WorkflowState]] = {}
        self._labels: dict[str, Label] = {}
        self._projects: dict[str, Project] = {}
        self._milestones: dict[str, Milestone] = {}
        self._issues: dict[str, Issue] = {}
        self._numbers: dict[str, int] = {}
        self._comments: dict[str, Comment] = {}
        self._relations: dict[str, Relation] = {}
        self._attachments: dict[str, Attachment] = {}
        self._documents: dict[str, Document] = {}
        self._injections: list[_Injection] = []

    # --- seeding and test controls ------------------------------------------------------------------------------

    def now(self) -> datetime:
        """The fake clock: every call is one second later than the last, so ordering is deterministic."""

        self._clock += timedelta(seconds=1)
        return self._clock

    def add_user(self, name: str, user_id: str | None = None) -> str:
        user = user_id or str(uuid.uuid4())
        self._users[user] = name
        return user

    def add_team(
        self,
        key: str,
        name: str | None = None,
        *,
        team_id: str | None = None,
        states: Sequence[tuple[str, StateType]] = DEFAULT_STATES,
    ) -> Team:
        team = Team(team_id or str(uuid.uuid4()), key, name or key)
        self._teams[team.id] = team
        self._states[team.id] = [
            WorkflowState(str(uuid.uuid4()), state, kind, float(position), team.id)
            for position, (state, kind) in enumerate(states)
        ]
        self._numbers[team.id] = 0
        return team

    def add_project(self, team_id: str, name: str, *, project_id: str | None = None) -> Project:
        self._team(team_id)
        project = Project(project_id or str(uuid.uuid4()), name, "", (team_id,))
        project = replace(project, url=self._project_url(project))
        self._projects[project.id] = project
        return project

    def add_document(self, project_id: str, title: str, content: str) -> Document:
        self._project(project_id)
        document_id = str(uuid.uuid4())
        document = Document(
            document_id, project_id, title, content, f"{self._base()}/document/{document_id}", self.now()
        )
        self._documents[document_id] = document
        return document

    def add_comment_as(self, issue_id: str, user_id: str, body: str, *, created_at: datetime | None = None) -> Comment:
        """A comment of another member (a human answering): the fake's way to simulate what the webhook delivers."""

        issue = self._issue(issue_id)
        comment = Comment(
            str(uuid.uuid4()), issue.id, body, user_id, created_at or self.now(), self._users.get(user_id)
        )
        self._comments[comment.id] = comment
        return comment

    def assign_issue(self, issue_id: str, user_name: str | None) -> Issue:
        """A person assigns an issue in the Linear UI (no write is recorded)."""

        return self._store(replace(self._issue(issue_id), assignee_name=user_name, updated_at=self.now()))

    def move_issue(self, issue_id: str, state_name: str) -> Issue:
        """A person moves an issue in the Linear UI (no write is recorded)."""

        issue = self._issue(issue_id)
        state = self._state_named(issue.team_id, state_name)
        return self._store(replace(issue, state=state, updated_at=self.now()))

    def fail_next(self, error: LinearError, *, method: str | None = None, times: int = 1) -> None:
        """Make the next ``times`` calls (of ``method``, or of any) raise ``error``."""

        self._injections.append(_Injection(method, error, times))

    def rate_limit_next(self, retry_after: float | None = 1.0, *, method: str | None = None, times: int = 1) -> None:
        self.fail_next(LinearRateLimitedError(retry_after), method=method, times=times)

    def count(self, method: str) -> int:
        return sum(1 for name, _ in self.calls if name == method)

    # --- reads ---------------------------------------------------------------------------------------------------

    async def viewer(self) -> Viewer:
        self._enter("viewer")
        return Viewer(self.user_id, self.user_name, self.organization)

    async def teams(self) -> list[Team]:
        self._enter("teams")
        return list(self._teams.values())

    async def projects(self, team_id: str) -> list[Project]:
        self._enter("projects", team_id)
        self._team(team_id)
        return [p for p in self._projects.values() if team_id in p.team_ids]

    async def project(self, project_id: str) -> Project:
        self._enter("project", project_id)
        return self._project(project_id)

    async def workflow_states(self, team_id: str) -> list[WorkflowState]:
        self._enter("workflow_states", team_id)
        self._team(team_id)
        return list(self._states[team_id])

    async def labels(self, team_id: str) -> list[Label]:
        self._enter("labels", team_id)
        self._team(team_id)
        return [label for label in self._labels.values() if label.team_id in (team_id, None)]

    async def issue(self, issue_id: str) -> Issue:
        self._enter("issue", issue_id)
        return self._issue(issue_id)

    async def issues_of_project(self, project_id: str, cursor: str | None = None) -> Page[Issue]:
        self._enter("issues_of_project", project_id, cursor)
        self._project(project_id)
        issues = [i for i in self._issues.values() if i.project_id == project_id]
        start = int(cursor) if cursor else 0
        end = start + self.page_size
        return Page(tuple(issues[start:end]), str(end) if end < len(issues) else None)

    async def children(self, issue_id: str) -> list[Issue]:
        self._enter("children", issue_id)
        parent = self._issue(issue_id)
        return [i for i in self._issues.values() if i.parent_id == parent.id]

    async def comments(self, issue_id: str, since: datetime | None = None) -> list[Comment]:
        self._enter("comments", issue_id, since)
        issue = self._issue(issue_id)
        found = [c for c in self._comments.values() if c.issue_id == issue.id]
        return sorted((c for c in found if since is None or c.created_at > since), key=lambda c: c.created_at)

    async def relations(self, issue_id: str) -> list[Relation]:
        self._enter("relations", issue_id)
        issue = self._issue(issue_id)
        return [r for r in self._relations.values() if issue.id in (r.issue_id, r.related_issue_id)]

    async def project_documents(self, project_id: str) -> list[Document]:
        self._enter("project_documents", project_id)
        self._project(project_id)
        return [d for d in self._documents.values() if d.project_id == project_id]

    async def resolve_link(self, url_or_identifier: str) -> Issue | Project:
        self._enter("resolve_link", url_or_identifier)
        text = url_or_identifier.strip()
        issue_url = _ISSUE_URL.fullmatch(text)
        if issue_url:
            self._workspace(issue_url.group(1))
            return self._issue(issue_url.group(2))
        project_url = _PROJECT_URL.fullmatch(text)
        if project_url:
            self._workspace(project_url.group(1))
            return self._project_by_slug(project_url.group(2))
        if _IDENTIFIER.fullmatch(text):
            return self._issue(text)
        raise LinearInvalidError("link")

    # --- writes --------------------------------------------------------------------------------------------------

    async def create_project(
        self, project_id: str, team_id: str, name: str, *, description: str | None = None
    ) -> Project:
        self._enter("create_project", project_id, team_id, name)
        key = _uuid(project_id, "project_id")
        self._team(team_id)
        self._free(key, self._projects)
        if not name.strip():
            raise LinearInvalidError("name")
        project = Project(key, name, "", (team_id,), description)
        project = replace(project, url=self._project_url(project))
        self._projects[key] = project
        return project

    async def create_milestone(
        self,
        project_id: str,
        milestone_id: str,
        name: str,
        *,
        description: str | None = None,
        sort_order: float | None = None,
    ) -> Milestone:
        self._enter("create_milestone", project_id, milestone_id, name)
        key = _uuid(milestone_id, "milestone_id")
        self._project(project_id)
        self._free(key, self._milestones)
        if not name.strip():
            raise LinearInvalidError("name")
        order = float(len(self._milestones)) if sort_order is None else sort_order
        milestone = Milestone(key, project_id, name, description, order)
        self._milestones[key] = milestone
        return milestone

    async def create_label(
        self,
        team_id: str,
        label_id: str,
        name: str,
        *,
        color: str | None = None,
        parent_id: str | None = None,
        is_group: bool = False,
    ) -> Label:
        self._enter("create_label", team_id, label_id, name)
        key = _uuid(label_id, "label_id")
        self._team(team_id)
        self._free(key, self._labels)
        if not name.strip():
            raise LinearInvalidError("name")
        if any(label.name == name and label.parent_id == parent_id for label in self._labels.values()):
            raise LinearAlreadyExistsError
        if parent_id is not None:
            self._check_group(parent_id, is_group=is_group)
        label = Label(key, name, color, parent_id, is_group, team_id)
        self._labels[key] = label
        return label

    async def create_issue(self, issue: NewIssue) -> Issue:
        self._enter("create_issue", issue.issue_id, issue.team_id, issue.title)
        key = _uuid(issue.issue_id, "issue_id")
        team_id = issue.team_id
        self._team(team_id)
        self._free(key, self._issues)
        if not issue.title.strip():
            raise LinearInvalidError("title")
        state = self._states[team_id][0] if issue.state_id is None else self._state(team_id, issue.state_id)
        parent = None if issue.parent_id is None else self._issue(issue.parent_id)
        if parent is not None and parent.team_id != team_id:
            raise LinearInvalidError("parent_id")
        self._check_placement(issue.project_id, issue.milestone_id)
        self._check_estimate(issue.estimate)
        self._check_priority(issue.priority)
        labels = self._pick_labels(team_id, issue.label_ids)
        self._numbers[team_id] += 1
        identifier = f"{self._teams[team_id].key}-{self._numbers[team_id]}"
        created = self.now()
        made = Issue(
            id=key,
            identifier=identifier,
            url=f"{self._base()}/issue/{identifier}/{_slug(issue.title)}",
            title=issue.title,
            state=state,
            team_id=team_id,
            description=issue.description,
            parent_id=None if parent is None else parent.id,
            project_id=issue.project_id,
            milestone_id=issue.milestone_id,
            labels=labels,
            estimate=issue.estimate,
            priority=issue.priority or 0,
            created_at=created,
            updated_at=created,
        )
        self._issues[key] = made
        return made

    async def update_issue(self, issue_id: str, **fields: Any) -> Issue:
        self._enter("update_issue", issue_id, tuple(sorted(fields)))
        unknown = set(fields) - UPDATABLE
        if unknown:
            raise LinearInvalidError(min(unknown))
        issue = self._issue(issue_id)
        changes = self._simple_changes(issue, fields)
        if "project_id" in fields or "milestone_id" in fields:
            project = fields.get("project_id", issue.project_id)
            milestone = fields.get("milestone_id", issue.milestone_id)
            self._check_placement(project, milestone)
            changes["project_id"], changes["milestone_id"] = project, milestone
        if "parent_id" in fields:
            changes["parent_id"] = self._new_parent(issue, fields["parent_id"])
        return self._store(replace(issue, **changes, updated_at=self.now()))

    def _simple_changes(self, issue: Issue, fields: dict[str, Any]) -> dict[str, Any]:
        changes: dict[str, Any] = {}
        if "title" in fields:
            if not str(fields["title"]).strip():
                raise LinearInvalidError("title")
            changes["title"] = fields["title"]
        if "description" in fields:
            changes["description"] = fields["description"]
        if "state_id" in fields:
            changes["state"] = self._state(issue.team_id, fields["state_id"])
        if "estimate" in fields:
            self._check_estimate(fields["estimate"])
            changes["estimate"] = fields["estimate"]
        if "priority" in fields:
            self._check_priority(fields["priority"])
            changes["priority"] = fields["priority"] or 0
        if "label_ids" in fields:
            changes["labels"] = self._pick_labels(issue.team_id, fields["label_ids"])
        return changes

    def _new_parent(self, issue: Issue, parent_id: str | None) -> str | None:
        if parent_id is None:
            return None
        parent = self._issue(parent_id)
        if parent.team_id != issue.team_id or self._is_descendant(parent.id, issue.id):
            raise LinearInvalidError("parent_id")
        return parent.id

    def _check_group(self, parent_id: str, *, is_group: bool) -> None:
        parent = self._labels.get(parent_id)
        if parent is None:
            raise LinearNotFoundError
        if not parent.is_group or is_group:  # a parent must be a group, and groups do not nest
            raise LinearInvalidError("parent_id")

    async def create_comment(self, issue_id: str, body: str, *, comment_id: str | None = None) -> Comment:
        self._enter("create_comment", issue_id, body)
        issue = self._issue(issue_id)
        key = str(uuid.uuid4()) if comment_id is None else _uuid(comment_id, "comment_id")
        self._free(key, self._comments)
        if not body.strip():
            raise LinearInvalidError("body")
        comment = Comment(key, issue.id, body, self.user_id, self.now(), self.user_name)
        self._comments[key] = comment
        return comment

    async def create_relation(
        self, relation_id: str, issue_id: str, related_issue_id: str, relation_type: RelationType
    ) -> Relation:
        self._enter("create_relation", relation_id, issue_id, related_issue_id, relation_type)
        key = _uuid(relation_id, "relation_id")
        if relation_type not in RELATION_TYPES:
            raise LinearInvalidError("relation_type")
        first, second = self._issue(issue_id), self._issue(related_issue_id)
        self._free(key, self._relations)
        if first.id == second.id:
            raise LinearInvalidError("related_issue_id")
        if any(
            r.type == relation_type and {r.issue_id, r.related_issue_id} == {first.id, second.id}
            for r in self._relations.values()
        ):
            raise LinearAlreadyExistsError
        relation = Relation(key, relation_type, first.id, second.id)
        self._relations[key] = relation
        return relation

    async def attach_link(self, issue_id: str, url: str, title: str) -> Attachment:
        self._enter("attach_link", issue_id, url)
        issue = self._issue(issue_id)
        if not url.startswith(("http://", "https://")):
            raise LinearInvalidError("url")
        for existing in self._attachments.values():
            if existing.issue_id == issue.id and existing.url == url:
                updated = replace(existing, title=title)
                self._attachments[existing.id] = updated
                return updated
        attachment = Attachment(str(uuid.uuid4()), issue.id, url, title)
        self._attachments[attachment.id] = attachment
        return attachment

    # --- inspection for tests -------------------------------------------------------------------------------------

    def attachments(self, issue_id: str) -> list[Attachment]:
        return [a for a in self._attachments.values() if a.issue_id == self._issue(issue_id).id]

    def all_issues(self) -> list[Issue]:
        return list(self._issues.values())

    # --- internals ------------------------------------------------------------------------------------------------

    def _enter(self, method: str, *args: Any) -> None:
        """Record the call and raise an injected failure; a write that fails is not recorded as a write."""

        self.calls.append((method, args))
        for injection in self._injections:
            if injection.times > 0 and injection.method in (None, method):
                injection.times -= 1
                raise injection.error
        if method in _WRITES:
            self.writes.append((method, args))

    def _base(self) -> str:
        return f"https://linear.app/{self.organization.url_key}"

    def _project_url(self, project: Project) -> str:
        return f"{self._base()}/project/{_slug(project.name)}-{project.id[:12].replace('-', '')}"

    def _workspace(self, url_key: str) -> None:
        if url_key != self.organization.url_key:
            raise LinearNotFoundError

    @staticmethod
    def _free(key: str, table: dict[str, Any]) -> None:
        if key in table:
            raise LinearAlreadyExistsError

    def _team(self, team_id: str) -> Team:
        team = self._teams.get(team_id)
        if team is None:
            raise LinearNotFoundError
        return team

    def _project(self, project_id: str) -> Project:
        project = self._projects.get(project_id)
        if project is None:
            raise LinearNotFoundError
        return project

    def _project_by_slug(self, slug: str) -> Project:
        suffix = slug.rsplit("-", 1)[-1]
        for project in self._projects.values():
            if project.url.rsplit("/", 1)[-1] == slug or project.id.replace("-", "").startswith(suffix):
                return project
        raise LinearNotFoundError

    def _issue(self, issue_id: str) -> Issue:
        found = self._issues.get(issue_id)
        if found is None and _IDENTIFIER.fullmatch(issue_id):
            found = next((i for i in self._issues.values() if i.identifier.lower() == issue_id.lower()), None)
        if found is None:
            raise LinearNotFoundError
        return found

    def _store(self, issue: Issue) -> Issue:
        self._issues[issue.id] = issue
        return issue

    def _state(self, team_id: str, state_id: str) -> WorkflowState:
        for state in self._states[team_id]:
            if state.id == state_id:
                return state
        raise LinearInvalidError("state_id")

    def _state_named(self, team_id: str, name: str) -> WorkflowState:
        for state in self._states[team_id]:
            if state.name == name:
                return state
        raise LinearNotFoundError

    def _check_placement(self, project_id: str | None, milestone_id: str | None) -> None:
        if project_id is not None:
            self._project(project_id)
        if milestone_id is not None:
            milestone = self._milestones.get(milestone_id)
            if milestone is None:
                raise LinearNotFoundError
            if milestone.project_id != project_id:
                raise LinearInvalidError("milestone_id")

    def _check_estimate(self, estimate: int | None) -> None:
        if estimate is None:
            return
        if estimate < 0 or (self.estimate_allowed is not None and not self.estimate_allowed(estimate)):
            raise LinearInvalidError("estimate")

    @staticmethod
    def _check_priority(priority: int | None) -> None:
        if priority is not None and not 0 <= priority <= MAX_PRIORITY:
            raise LinearInvalidError("priority")

    def _pick_labels(self, team_id: str, label_ids: Sequence[str]) -> tuple[Label, ...]:
        picked: list[Label] = []
        for label_id in dict.fromkeys(label_ids):
            label = self._labels.get(label_id)
            if label is None or label.team_id not in (team_id, None):
                raise LinearNotFoundError
            if label.is_group:
                raise LinearInvalidError("label_ids")  # a group is not applied itself
            picked.append(label)
        groups = [label.parent_id for label in picked if label.parent_id is not None]
        if len(groups) != len(set(groups)):
            raise LinearInvalidError("label_ids")  # one child per group (single select)
        return tuple(picked)

    def _is_descendant(self, candidate_id: str, ancestor_id: str) -> bool:
        current: str | None = candidate_id
        while current is not None:
            if current == ancestor_id:
                return True
            current = self._issues[current].parent_id
        return False


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48] or "item"
