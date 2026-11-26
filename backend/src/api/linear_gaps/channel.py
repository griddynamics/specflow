"""``LinearGaps``: the Linear gap channel of the runtime (docs/linear/gaps.md). It is a gap-run notice (``add_notice``:
the poster), reads answers from the webhook's comments and from the catch-up read, and serves the routes (the
target, the access check). Held on ``Runtime.linear_gaps``; built only with the Linear OAuth app and gap runs."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from api.contained import Contained
from api.linear_gaps.acting import Acting, ActingError, acting_gateway, code_of
from api.linear_gaps.answers import Incoming, LinearAnswers
from api.linear_gaps.models import LinearGapsOut
from api.linear_gaps.poster import LinearGapPoster
from api.linear_gaps.webhook import Deliveries, Event
from db import linear_gaps as gaps_db
from db.linear_gaps import GapTargetSaved
from linear.errors import LinearError
from linear.models import Project, Team
from logger.logger import INFO, WARNING, get_logger, log_event

logger = get_logger("api.linear_gaps.channel")

MARGIN: Final = timedelta(minutes=1)  # the catch-up reads from a minute before the first question was posted
TEAM_FIELD: Final = "team_key"
PROJECT_FIELD: Final = "linear_project"
TEAM_NOT_FOUND: Final = "linear_team_not_found"
PROJECT_NOT_FOUND: Final = "linear_project_not_found"


class UnresolvedError(Exception):
    """The typed team or Linear project names nothing the member can see; ``reason`` is a closed API code and ``field``
    the input at fault."""

    def __init__(self, reason: str, field: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.field = field


@dataclass(frozen=True)
class Resolved:
    """The team and optional Linear project a target names, as Linear knows them."""

    team: Team
    project: Project | None


def _project_of(projects: list[Project], reference: str) -> Project | None:
    wanted = reference.strip().rstrip("/")
    for project in projects:
        if wanted.lower() == project.name.lower() or wanted == project.id:
            return project
        if project.url and wanted.startswith(project.url.rstrip("/")):
            return project
    return None


def _utc_now() -> datetime:
    return datetime.now(UTC)


class LinearGaps:
    """The poster, the answers and what the routes and the webhook need."""

    def __init__(self, runtime: Any, poster: LinearGapPoster, answers: LinearAnswers) -> None:
        self._runtime = runtime
        self.poster = poster
        self.answers = answers
        self.deliveries = Deliveries()
        self._tasks: set[asyncio.Task[None]] = set()

    async def send(self, project_id: str, result: Any) -> None:
        """The gap runner's notice (``add_notice``): post the new questions; never raises."""

        del result
        await self._post(project_id, then_read=False)

    def start(self) -> None:
        """Read what came while the process was down (AC4), in the background."""

        self._spawn(self._catch_up_all(), "linear-gaps-catch-up")

    async def aclose(self) -> None:
        tasks = [task for task in self._tasks if not task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _spawn(self, work: Coroutine[Any, Any, None], name: str) -> None:
        task = asyncio.create_task(work, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def after_save(self, project_id: str, saved: GapTargetSaved) -> None:
        """The target was saved: when it was turned on (or its destination changed) post the open questions now, then
        read the comments already in them, in the background; never raises and never waits for Linear."""

        if saved.turned_on or saved.destination_changed:
            self._spawn(self._post(project_id, then_read=True), f"linear-gaps-post-{project_id}")

    async def _post(self, project_id: str, *, then_read: bool) -> None:
        with Contained() as posted:
            await self.poster.post_open(project_id)
            if then_read:
                await self.catch_up(project_id)
        if posted.failed:
            log_event(
                logger, WARNING, "api.linear_gaps.post_failed", project_id=project_id, error_type=posted.error_type
            )

    def receive(self, event: Event, delivery: str | None) -> None:
        """A verified webhook delivery: a new comment is read in the background (the route answers at once, Linear
        waits five seconds); a delivery already handled, or any other event, does nothing."""

        comment = event.comment()
        if comment is not None and self.deliveries.first(delivery or event.webhook_id):
            self._spawn(self._read_comment(comment), "linear-gaps-comment")

    async def _read_comment(self, comment: Incoming) -> None:
        with Contained() as read:
            await self.answers.process(comment)
        if read.failed:
            log_event(logger, WARNING, "api.linear_gaps.comment_failed", error_type=read.error_type)

    # Catch-up -----------------------------------------------------------------------------------------------------

    async def _catch_up_all(self) -> None:
        with Contained() as listed:
            async with self._runtime.transaction() as session:
                targets = await gaps_db.enabled_targets(session)
        if listed.failed:
            log_event(logger, WARNING, "api.linear_gaps.catch_up_failed", error_type=listed.error_type)
            return
        for target in targets:
            with Contained() as caught:
                await self.catch_up(str(target.project_id))
            if caught.failed:
                log_event(
                    logger,
                    WARNING,
                    "api.linear_gaps.catch_up_failed",
                    project_id=str(target.project_id),
                    error_type=caught.error_type,
                )

    async def catch_up(self, project_id: str) -> int:
        """Read the comments of the project's issues that still ask something and process each like a delivery (AC4:
        a delivery Linear gave up on is read here); how many questions were answered. A refused read keeps its closed
        code on the target."""

        pid = uuid.UUID(project_id)
        async with self._runtime.transaction() as session:
            target = await gaps_db.get_target(session, pid)
            rows = await gaps_db.list_issues(session, pid) if target is not None else []
        open_rows = [row for row in rows if row.answered_at is None]
        if target is None or not target.enabled or not open_rows:
            return 0
        try:
            async with acting_gateway(self._runtime, pid) as acting:
                answered = await self._read_issues(acting, {row.issue_id: row.posted_at for row in reversed(open_rows)})
        except (ActingError, LinearError) as error:
            code = error.code if isinstance(error, ActingError) else code_of(error)
            async with self._runtime.transaction() as session:
                await gaps_db.record_error(session, pid, code)
            log_event(logger, WARNING, "api.linear_gaps.read_refused", project_id=project_id, error_code=code)
            return 0
        log_event(logger, INFO, "api.linear_gaps.caught_up", project_id=project_id, gaps=answered)
        return answered

    async def _read_issues(self, acting: Acting, posted: dict[uuid.UUID, datetime]) -> int:
        answered = 0
        for issue_id, first in posted.items():
            for comment in await acting.gateway.comments(str(issue_id), first - MARGIN):
                incoming = Incoming(comment.id, comment.issue_id, acting.organization_id, comment.user_id, comment.body)
                answered += await self.answers.process(incoming, acting)
        return answered

    # The routes ---------------------------------------------------------------------------------------------------

    async def view(self, project_id: uuid.UUID, *, available: bool) -> LinearGapsOut:
        async with self._runtime.transaction() as session:
            target = await gaps_db.get_target(session, project_id)
            rows = await gaps_db.list_issues(session, project_id) if target is not None else []
            key = await gaps_db.url_key(session, project_id) if target is not None else None
        identifier = None if target is None else target.parent_issue_identifier
        return LinearGapsOut(
            available=available,
            enabled=target is not None and target.enabled,
            team_id=None if target is None else target.team_id,
            team_key=None if target is None else target.team_key,
            linear_project_id=None if target is None else target.linear_project_id,
            grouping=None if target is None else ("area" if target.grouping == "area" else "one"),
            parent_identifier=identifier,
            parent_url=f"https://linear.app/{key}/issue/{identifier}" if key and identifier else None,
            posted=len(rows),
            answered=sum(1 for row in rows if row.answered_at is not None),
            error_code=None if target is None else target.last_error_code,
        )

    async def resolve(self, project_id: uuid.UUID, team_key: str, project: str | None) -> Resolved:
        """The team and Linear project the member (the one the project's Linear is connected by) names, as Linear knows
        them: ``UnresolvedError`` when they name nothing visible, ``ActingError`` or ``LinearError`` when Linear cannot
        be asked. No transaction is open across a Linear call and no Linear text is kept."""

        async with acting_gateway(self._runtime, project_id) as acting:
            gateway = acting.gateway
            team = next((t for t in await gateway.teams() if t.key.upper() == team_key.upper()), None)
            found = None if team is None or project is None else _project_of(await gateway.projects(team.id), project)
        if team is None:
            raise UnresolvedError(TEAM_NOT_FOUND, TEAM_FIELD)
        if project is None:
            return Resolved(team, None)
        if found is None:
            raise UnresolvedError(PROJECT_NOT_FOUND, PROJECT_FIELD)
        return Resolved(team, found)


async def revoke_organization(runtime: Any, event: Event) -> None:
    """The workspace withdrew the app: its connections are ``revoked`` (a person must connect again). Never raises."""

    with Contained() as revoked:
        async with runtime.transaction() as session:
            count = await gaps_db.revoke_connections(
                session, organization_id=event.organization_id, linear_user_id=event.user_id
            )
        log_event(logger, INFO, "api.linear_gaps.app_revoked", connections=count)
    if revoked.failed:
        log_event(logger, WARNING, "api.linear_gaps.revoke_failed", error_type=revoked.error_type)
