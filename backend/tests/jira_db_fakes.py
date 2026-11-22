"""In-memory stand-in for ``db.jira`` (no tests in here); ``test_jira_db_fakes`` keeps its signatures equal."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from db import jira as jira_module
from db.jira import ISSUE_COLUMNS, JiraTargetView, TargetSaved
from db.models import JiraIssue


class FakeJiraDb:
    """The ``db.jira`` functions over dicts. ``hook`` is called with the function name before each call, so a test can
    change the stored target between two calls (FR-JIRA-RUN-08); ``in_transaction`` tells whether ``transaction()`` is
    open (NFR-JIRA-04)."""

    def __init__(self) -> None:
        self.targets: dict[uuid.UUID, JiraTargetView] = {}
        self.issues: dict[tuple[uuid.UUID, str], JiraIssue] = {}
        self.project_names: dict[uuid.UUID, str] = {}
        self.hook: Callable[[str], None] | None = None
        self.in_transaction = False
        self.calls: list[tuple[str, AsyncSession]] = []  # every repository call and the session it got

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in PUBLIC_FUNCTIONS:
            monkeypatch.setattr(jira_module, name, getattr(self, name))

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        self.in_transaction = True
        try:
            yield None
        finally:
            self.in_transaction = False

    def set_target(self, project_id: uuid.UUID, **changes: Any) -> None:
        """Change the stored target behind the repository's back."""

        self.targets[project_id] = replace(self.targets[project_id], **changes)

    def _called(self, name: str, session: AsyncSession) -> None:
        self.calls.append((name, session))
        if self.hook is not None:
            self.hook(name)

    async def get_target(self, session: AsyncSession, project_id: uuid.UUID) -> JiraTargetView | None:
        self._called("get_target", session)
        return self.targets.get(project_id)

    async def save_target(
        self,
        session: AsyncSession,
        project_id: uuid.UUID,
        *,
        jira_project: str,
        epic_key: str | None,
        enabled: bool,
        now: datetime,
    ) -> TargetSaved:
        self._called("save_target", session)
        before = self.targets.get(project_id)
        project_changed = before is not None and before.jira_project != jira_project
        keys_changed = before is None or project_changed or before.epic_key != epic_key
        turned_on = enabled and (before is None or not before.enabled)
        if project_changed:
            self.issues = {key: row for key, row in self.issues.items() if key[0] != project_id}
        changed = before is None or keys_changed or before.enabled != enabled
        self.targets[project_id] = JiraTargetView(
            project_id=project_id,
            project_name=self.project_names.get(project_id, "Project"),
            jira_project=jira_project,
            epic_key=epic_key,
            enabled=enabled,
            updated_at=now if changed else before.updated_at,
            last_status=before.last_status if before else None,
            last_trigger=before.last_trigger if before else None,
            last_started_at=before.last_started_at if before else None,
            last_finished_at=before.last_finished_at if before else None,
            last_error_code=before.last_error_code if before else None,
            last_counts=before.last_counts if before else None,
        )
        return TargetSaved(project_changed=project_changed, turned_on=turned_on, keys_changed=keys_changed)

    async def target_unchanged(
        self, session: AsyncSession, project_id: uuid.UUID, *, jira_project: str, epic_key: str | None
    ) -> bool:
        self._called("target_unchanged", session)
        target = self.targets.get(project_id)
        return target is not None and target.jira_project == jira_project and target.epic_key == epic_key

    async def record_start(
        self, session: AsyncSession, project_id: uuid.UUID, *, trigger: str, queued_at: datetime
    ) -> None:
        self._called("record_start", session)
        self.set_target(
            project_id,
            last_status="running",
            last_trigger=trigger,
            last_started_at=queued_at,
            last_finished_at=None,
            last_error_code=None,
            last_counts=None,
        )

    async def record_end(
        self,
        session: AsyncSession,
        project_id: uuid.UUID,
        *,
        status: str,
        error_code: str | None,
        counts: Mapping[str, int],
        finished_at: datetime,
    ) -> None:
        self._called("record_end", session)
        self.set_target(
            project_id,
            last_status=status,
            last_error_code=error_code,
            last_counts=dict(counts),
            last_finished_at=finished_at,
        )

    async def running_targets(self, session: AsyncSession) -> list[uuid.UUID]:
        self._called("running_targets", session)
        return sorted(pid for pid, target in self.targets.items() if target.enabled and target.last_status == "running")

    async def list_issues(self, session: AsyncSession, project_id: uuid.UUID) -> list[JiraIssue]:
        self._called("list_issues", session)
        return sorted((row for (pid, _), row in self.issues.items() if pid == project_id), key=lambda r: r.node_id)

    async def add_pending(
        self, session: AsyncSession, project_id: uuid.UUID, *, node_id: str, kind: str, requested_at: datetime
    ) -> None:
        self._called("add_pending", session)
        row = self.issues.get((project_id, node_id))
        if row is None:
            self.issues[project_id, node_id] = JiraIssue(
                project_id=project_id,
                node_id=node_id,
                kind=kind,
                requested_at=requested_at,
                **dict.fromkeys(ISSUE_COLUMNS),
            )
        elif row.jira_key is None:
            row.requested_at, row.error_code = requested_at, None

    async def update_issue(self, session: AsyncSession, project_id: uuid.UUID, node_id: str, **columns: Any) -> None:
        self._called("update_issue", session)
        if unknown := set(columns) - set(ISSUE_COLUMNS):
            raise TypeError(f"not a column of jira_issues to update: {sorted(unknown)}")
        if (row := self.issues.get((project_id, node_id))) is not None:
            for column, value in columns.items():
                setattr(row, column, value)

    async def set_positions(self, session: AsyncSession, project_id: uuid.UUID, positions: Mapping[str, int]) -> None:
        self._called("set_positions", session)
        for node_id, position in positions.items():
            row = self.issues.get((project_id, node_id))
            if row is not None:
                row.position = position


PUBLIC_FUNCTIONS = (
    "get_target",
    "save_target",
    "target_unchanged",
    "record_start",
    "record_end",
    "running_targets",
    "list_issues",
    "add_pending",
    "update_issue",
    "set_positions",
)
