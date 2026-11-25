"""Posting the open questions to Jira (docs/jira-sync/gaps.md "Posting").

After every gap run (``add_notice``) the project's open questions routed to people (``route=ask``) that the channel has
not posted are posted as the acting account (the member the project's Atlassian is connected by): one Story per project,
created once and reused, and under it one sub-task per question (grouping ``one``) or one per work area with numbered
questions (grouping ``area``; a question that comes later in a known area is added as a comment with the next number).
A gap is posted once: its row in ``jira_gap_issues`` outlives its answer. No transaction is open across a Jira call; a
crash between a create and its row can post that question twice after the restart. Logs carry ids, codes and counts.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from api.contained import Contained
from api.jira_gaps import text
from api.jira_gaps.acting import Acting, ActingError, acting_gateway
from db import jira_gaps as gaps_db
from db.jira_gaps import GapIssueView, GapTargetView
from graph.plan.reader import LoadedPlan
from jira.gateway import JiraError
from jira.reasons import Reason
from logger.logger import INFO, WARNING, get_logger, log_event

logger = get_logger("api.jira_gaps.poster")

ASK: Final = "ask"
OPEN_MAX: Final = 50  # questions posted at once; the rest follow after the next gap run
STORY_NODE: Final = "gaps:story"
GENERAL: Final = "general"  # the group of a question no plan task is behind


@dataclass(frozen=True)
class _Where:
    """Where a pass posts: the project's target, the acting account, the Story and the Sub-task issue type."""

    project_id: str
    target: GapTargetView
    acting: Acting
    story: str
    subtask_type: str


@dataclass
class _Group:
    """The questions of one sub-task: ``area`` is its group id (a gap id for grouping ``one``)."""

    area: str
    title: str
    views: list[Any]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class JiraGapPoster:
    """Posts the open questions of the projects with an enabled target; held by ``JiraGaps``."""

    def __init__(
        self,
        runtime: Any,
        *,
        project_name: Callable[[str], Awaitable[str | None]],
        load_plan: Callable[[str], Awaitable[LoadedPlan]],
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._runtime = runtime
        self._project_name = project_name
        self._load_plan = load_plan
        self._now = now
        self._locks: dict[str, asyncio.Lock] = {}

    async def send(self, project_id: str, result: Any) -> None:
        """The gap runner's notice after a run: post the new questions; never raises."""

        del result
        with Contained() as posted:
            await self.post_open(project_id)
        if posted.failed:
            log_event(logger, WARNING, "api.jira_gaps.post_failed", project_id=project_id, error_type=posted.error_type)

    async def post_open(self, project_id: str) -> int:
        """Post the questions the channel has not posted; how many. A refused Jira call ends the pass: its closed code
        is kept on the target and the questions stay for the next run."""

        lock = self._locks.setdefault(project_id, asyncio.Lock())
        async with lock:
            pid = uuid.UUID(project_id)
            async with self._runtime.transaction() as session:
                target = await gaps_db.get_target(session, pid)
            if target is None or not target.enabled or self._runtime.gaps is None:
                return 0
            try:
                acting = await acting_gateway(self._runtime, pid)
                posted = await self._post(project_id, target, acting)
            except (ActingError, JiraError) as error:
                code = error.reason.value if isinstance(error, JiraError) else error.code
                async with self._runtime.transaction() as session:
                    await gaps_db.record_error(session, pid, code)
                log_event(logger, WARNING, "api.jira_gaps.post_refused", project_id=project_id, error_code=code)
                return 0
            async with self._runtime.transaction() as session:
                await gaps_db.record_error(session, pid, None)
            return posted

    async def _post(self, project_id: str, target: GapTargetView, acting: Acting) -> int:
        pid = uuid.UUID(project_id)
        service = await self._runtime.gaps.service()
        page = await service.list_gaps(project_id, status=["open"], route=[ASK], limit=OPEN_MAX)
        async with self._runtime.transaction() as session:
            rows = await gaps_db.list_issues(session, pid)
        known = {row.gap_id for row in rows}
        new = [gap.gap_id for gap in page.gaps if gap.gap_id not in known]
        if not new:
            return 0
        views = [await service.get_gap(project_id, gap_id) for gap_id in new]
        types = await acting.gateway.project(target.jira_project)
        if types.story_type_id is None or types.subtask_type_id is None:
            raise JiraError(Reason.ISSUE_TYPES_MISSING)
        story = target.story_key or await self._story(project_id, target, acting, types.story_type_id)
        where = _Where(project_id, target, acting, story, types.subtask_type_id)
        posted = 0
        for group in await self._groups(where, views):
            posted += await self._post_group(where, group, rows)
        log_event(logger, INFO, "api.jira_gaps.posted", project_id=project_id, gaps=posted)
        return posted

    async def _story(self, project_id: str, target: GapTargetView, acting: Acting, type_id: str) -> str:
        name = await self._project_name(project_id) or "project"
        fields = {
            "project": {"key": target.jira_project},
            "issuetype": {"id": type_id},
            "summary": text.story_summary(name),
            "description": text.story_doc(name),
        }
        key = await acting.gateway.create_issue(fields, project_id=project_id, node_id=STORY_NODE)
        async with self._runtime.transaction() as session:
            await gaps_db.set_story(session, uuid.UUID(project_id), key)
        log_event(logger, INFO, "api.jira_gaps.story_created", project_id=project_id)
        return key

    async def _groups(self, where: _Where, views: list[Any]) -> list[_Group]:
        """The new questions in groups, in the order of the gaps: one group per gap (grouping ``one``), or one per work
        area of the plan that the tasks behind the gap sit in (``area``)."""

        if where.target.grouping != "area":
            return [_Group(view.gap_id, view.title, [view]) for view in views]
        plan = await self._load_plan(where.project_id)
        titles = {area.area_id: area.title or area.anchor or area.area_id for area in plan.current.areas}
        groups: dict[str, _Group] = {}
        for view in views:
            reach = view.reach.tasks if view.reach is not None else ()
            areas = sorted({plan.tasks[t].area_id for t in reach if t in plan.tasks and plan.tasks[t].area_id})
            area = areas[0] if areas else GENERAL
            groups.setdefault(area, _Group(area, titles.get(area, "General"), [])).views.append(view)
        return list(groups.values())

    async def _post_group(self, where: _Where, group: _Group, rows: list[GapIssueView]) -> int:
        numbered = where.target.grouping == "area"
        same = [row for row in rows if row.area == group.area] if numbered else []
        first = max((row.number for row in same), default=0) + 1
        questions = [_question(first + index, view) for index, view in enumerate(group.views)]
        if same:  # a known area: the new questions go in a comment of its sub-task, with the next numbers
            key = same[0].issue_key
            await where.acting.gateway.add_comment(key, text.more_questions_doc(questions))
        else:
            fields = {
                "project": {"key": where.target.jira_project},
                "issuetype": {"id": where.subtask_type},
                "parent": {"key": where.story},
                "summary": text.area_summary(group.title) if numbered else text.cut(group.title, text.SUMMARY_MAX),
                "description": text.subtask_doc(questions, numbered=numbered),
            }
            key = await where.acting.gateway.create_issue(
                fields, project_id=where.project_id, node_id=f"gaps:{group.area}"
            )
        now = self._now()
        async with self._runtime.transaction() as session:
            for question, view in zip(questions, group.views, strict=True):
                await gaps_db.add_issue(
                    session,
                    uuid.UUID(where.project_id),
                    gap_id=view.gap_id,
                    issue_key=key,
                    area=group.area,
                    number=question.number,
                    now=now,
                )
        return len(group.views)


def _question(number: int, view: Any) -> text.Question:
    options = [(chr(ord("A") + index), o.label, o.detail, o.recommended) for index, o in enumerate(view.options)]
    return text.Question(number, view.title, view.question, view.why, options)
