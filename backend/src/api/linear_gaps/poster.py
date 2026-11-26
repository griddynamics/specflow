"""Posting the open questions to Linear (docs/linear/gaps.md "Posting").

After every gap run (``add_notice``) the project's open questions routed to people (``route=ask``) that the channel has
not posted are posted as the acting member (the member the project's Linear is connected by): one parent issue "Open
questions · <project>" per project, created once and reused, and under it one sub-issue per question (grouping ``one``)
or one per work area with numbered questions (grouping ``area``; a question that comes later in a known area is added as
a comment with the next number). Every issue carries the label ``graphico-gap`` and the priority of its severity.

A gap is posted once: its row in ``linear_gap_issues`` outlives its answer, and every create carries a client id made of
the project and the gap (``linear.ids``), so a crash between a create and its row posts nothing twice: the next pass
finds the issue by its id. No transaction is open across a Linear call. Logs carry ids, codes and counts.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from api.linear_gaps import text
from api.linear_gaps.acting import Acting, ActingError, acting_gateway, code_of
from db import linear_gaps as gaps_db
from db.linear_gaps import DestinationChangedError, GapIssueView, GapTargetView
from graph.plan.reader import LoadedPlan
from linear import ids
from linear.errors import LinearAlreadyExistsError, LinearError
from linear.models import Issue, NewIssue
from logger.logger import INFO, WARNING, get_logger, log_event

logger = get_logger("api.linear_gaps.poster")

ASK: Final = "ask"
OPEN_MAX: Final = 50  # questions posted at once; the rest follow after the next gap run
GENERAL: Final = "general"  # the group of a question no plan task is behind
PARENT_NODE: Final = "gaps"
# Linear's priority scale is 1 urgent, 2 high, 3 medium, 4 low (0 = none). A blocker stops the plan (urgent), a major
# question shapes it (high), a minor one can wait (low); medium is left to the people who triage in Linear.
PRIORITY: Final = {"blocker": 1, "major": 2, "minor": 4}
DEFAULT_PRIORITY: Final = 4


@dataclass(frozen=True)
class _Where:
    """Where a pass posts: the project's target, the acting member, the parent issue and the label."""

    project_id: str
    target: GapTargetView
    acting: Acting
    parent_id: str
    label_id: str


@dataclass
class _Group:
    """The questions of one issue: ``area`` is its group id (the gap id for grouping ``one``)."""

    area: str
    title: str
    views: list[Any]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def priority_of(views: list[Any]) -> int:
    """The Linear priority of the most severe question in an issue."""

    return min((PRIORITY.get(str(getattr(view, "severity", "")), DEFAULT_PRIORITY) for view in views), default=0)


def question_of(number: int, view: Any) -> text.Question:
    options = [(chr(ord("A") + index), o.label, o.detail, o.recommended) for index, o in enumerate(view.options)]
    return text.Question(number, view.title, view.question, view.why, options)


async def ensure_issue(acting: Acting, issue: NewIssue) -> Issue:
    """Create the issue under its client id; the same id again is the issue a crashed pass made."""

    try:
        return await acting.gateway.create_issue(issue)
    except LinearAlreadyExistsError:
        return await acting.gateway.issue(issue.issue_id)


class LinearGapPoster:
    """Posts the open questions of the projects with an enabled target; held by ``LinearGaps``."""

    def __init__(
        self,
        runtime: Any,
        *,
        name_of: Callable[[str], Awaitable[str | None]],
        plan_of: Callable[[str], Awaitable[LoadedPlan]],
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._runtime = runtime
        self._name_of = name_of
        self._plan_of = plan_of
        self._clock = clock
        self._locks: dict[str, asyncio.Lock] = {}

    async def post_open(self, project_id: str) -> int:
        """Post the questions the channel has not posted; how many. A refused Linear call ends the pass: its closed code
        is kept on the target and the questions stay for the next run."""

        async with self._locks.setdefault(project_id, asyncio.Lock()):
            return await self._pass(uuid.UUID(project_id))

    async def _pass(self, pid: uuid.UUID) -> int:
        target = await self._enabled(pid)
        if target is None:
            return 0
        try:
            async with acting_gateway(self._runtime, pid) as acting:
                count = await self._post(target, acting)
        except DestinationChangedError:  # the destination changed during the pass: the change posts afresh
            log_event(logger, INFO, "api.linear_gaps.post_superseded", project_id=str(pid))
            return 0
        except (ActingError, LinearError) as error:
            await self._keep(pid, error.code if isinstance(error, ActingError) else code_of(error))
            return 0
        await self._keep(pid, None)
        return count

    async def _enabled(self, pid: uuid.UUID) -> GapTargetView | None:
        async with self._runtime.transaction() as session:
            target = await gaps_db.get_target(session, pid)
        usable = target is not None and target.enabled and self._runtime.gaps is not None
        return target if usable else None

    async def _keep(self, pid: uuid.UUID, code: str | None) -> None:
        """Remember the closed code of the pass on the target (``None`` after a good one)."""

        async with self._runtime.transaction() as session:
            await gaps_db.record_error(session, pid, code)
        if code is not None:
            log_event(logger, WARNING, "api.linear_gaps.post_refused", project_id=str(pid), error_code=code)

    async def _post(self, target: GapTargetView, acting: Acting) -> int:
        project_id = str(target.project_id)
        service = await self._runtime.gaps.service()
        async with self._runtime.transaction() as session:
            rows = await gaps_db.list_issues(session, target.project_id)
        fresh = await _fresh_gap_ids(service, project_id, {row.gap_id for row in rows})
        if not fresh:
            return 0
        views = [await service.get_gap(project_id, gap_id) for gap_id in fresh]
        parent = await self._parent(target, acting)
        where = _Where(project_id, target, acting, parent, await self._label(target, acting))
        posted = 0
        for group in await self._groups(where, views):
            posted += await self._post_group(where, group, rows)
        log_event(logger, INFO, "api.linear_gaps.posted", project_id=project_id, gaps=posted)
        return posted

    async def _parent(self, target: GapTargetView, acting: Acting) -> str:
        """The id of the "Open questions" issue (made once, then kept on the target)."""

        if target.parent_issue_id is not None:
            return str(target.parent_issue_id)
        project_id = str(target.project_id)
        name = await self._name_of(project_id) or "project"
        made = await ensure_issue(
            acting,
            NewIssue(
                issue_id=str(ids.node_uuid(project_id, ids.KIND_PARENT, PARENT_NODE)),
                team_id=target.team_id,
                title=text.parent_title(name),
                description=text.parent_description(name),
                project_id=target.linear_project_id,
            ),
        )
        async with self._runtime.transaction() as session:
            await _guard(session, target)
            await gaps_db.set_parent(session, target.project_id, uuid.UUID(made.id), made.identifier)
        log_event(logger, INFO, "api.linear_gaps.parent_created", project_id=project_id)
        return made.id

    async def _label(self, target: GapTargetView, acting: Acting) -> str:
        """The id of the team's ``graphico-gap`` label (made when the team has none)."""

        for label in await acting.gateway.labels(target.team_id):
            if label.name == text.LABEL_NAME and not label.is_group:
                return label.id
        label_id = str(ids.node_uuid(target.project_id, ids.KIND_LABEL, "gap"))
        try:
            return (await acting.gateway.create_label(target.team_id, label_id, text.LABEL_NAME)).id
        except LinearAlreadyExistsError:  # a pass that crashed after the create, or a label made by hand meanwhile
            found = [x for x in await acting.gateway.labels(target.team_id) if x.name == text.LABEL_NAME]
            return found[0].id if found else label_id

    async def _groups(self, where: _Where, views: list[Any]) -> list[_Group]:
        """The new questions in groups, in the order of the gaps: one group per gap (grouping ``one``), or one per work
        area of the plan that the tasks behind the gap sit in (``area``)."""

        if where.target.grouping != "area":
            return [_Group(view.gap_id, view.title, [view]) for view in views]
        plan = await self._plan_of(where.project_id)
        by_area: dict[str, list[Any]] = {}
        for view in views:
            by_area.setdefault(_area_of(plan, view), []).append(view)
        names = {area.area_id: area.title or area.anchor or area.area_id for area in plan.current.areas}
        return [_Group(area, names.get(area, "General"), items) for area, items in by_area.items()]

    async def _post_group(self, where: _Where, group: _Group, rows: list[GapIssueView]) -> int:
        """Post one group as an issue (or as a comment of its area's issue) and remember where each question went."""

        numbered = where.target.grouping == "area"
        known = [row for row in rows if numbered and row.area == group.area]
        start = max((row.number for row in known), default=0) + 1
        questions = [question_of(start + offset, view) for offset, view in enumerate(group.views)]
        if known:  # a known area: the new questions go in a comment of its issue, with the next numbers
            home = (str(known[0].issue_id), known[0].identifier)
            await self._more(where, home[0], group, questions)
        else:
            made = await ensure_issue(where.acting, _new_issue(where, group, questions, numbered=numbered))
            home = (made.id, made.identifier)
        await self._remember(where, group, questions, home)
        return len(group.views)

    async def _remember(
        self, where: _Where, group: _Group, questions: list[text.Question], home: tuple[str, str]
    ) -> None:
        """The rows of the posted questions: the issue they are in and their numbers."""

        posted_at = self._clock()
        async with self._runtime.transaction() as session:
            await _guard(session, where.target)
            for question, view in zip(questions, group.views, strict=True):
                await gaps_db.add_issue(
                    session,
                    where.target.project_id,
                    gap_id=view.gap_id,
                    issue_id=uuid.UUID(home[0]),
                    identifier=home[1],
                    area=group.area,
                    number=question.number,
                    now=posted_at,
                )

    async def _more(self, where: _Where, issue_id: str, group: _Group, questions: list[text.Question]) -> None:
        """The follow-up questions of a known area as one comment; its client id is made of the first gap, so a retried
        pass does not comment twice."""

        comment_id = str(ids.node_uuid(where.project_id, ids.KIND_COMMENT, f"more:{group.views[0].gap_id}"))
        try:
            await where.acting.gateway.create_comment(
                issue_id, text.more_questions_comment(questions), comment_id=comment_id
            )
        except LinearAlreadyExistsError:
            return


def _new_issue(where: _Where, group: _Group, questions: list[text.Question], *, numbered: bool) -> NewIssue:
    node = f"area:{group.area}" if numbered else group.area
    return NewIssue(
        issue_id=str(ids.node_uuid(where.project_id, ids.KIND_GAP, node)),
        team_id=where.target.team_id,
        title=text.issue_title(group.title, numbered=numbered),
        description=text.issue_description(questions, numbered=numbered),
        parent_id=where.parent_id,
        project_id=where.target.linear_project_id,
        label_ids=(where.label_id,),
        priority=priority_of(group.views),
    )


async def _fresh_gap_ids(service: Any, project_id: str, known: set[str]) -> list[str]:
    """The ids of up to ``OPEN_MAX`` open questions for people that were not posted yet; it pages past the ones already
    posted, which must not hide the newer ones."""

    found: list[str] = []
    cursor: str | None = None
    while True:
        page = await service.list_gaps(project_id, status=["open"], route=[ASK], cursor=cursor, limit=OPEN_MAX)
        found.extend(gap.gap_id for gap in page.gaps if gap.gap_id not in known)
        cursor = page.next
        if cursor is None or len(found) >= OPEN_MAX:
            return found[:OPEN_MAX]


async def _guard(session: Any, target: GapTargetView) -> None:
    await gaps_db.require_destination(
        session, target.project_id, team_id=target.team_id, linear_project_id=target.linear_project_id
    )


def _area_of(plan: LoadedPlan, view: Any) -> str:
    reach = view.reach.tasks if view.reach is not None else ()
    areas = sorted({plan.tasks[t].area_id for t in reach if t in plan.tasks and plan.tasks[t].area_id})
    return areas[0] if areas else GENERAL
