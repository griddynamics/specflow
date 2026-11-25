"""``JiraGaps``: the Jira gap channel of the runtime (docs/jira-sync/gaps.md). It is a gap-run notice (``add_notice``:
the poster) and owns the poller; the routes read and save the target through it. Held on ``Runtime.jira_gaps``; built
only with Atlassian OAuth and gap runs."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Final

from api.contained import Contained
from api.jira_gaps.acting import ActingError, acting_gateway
from api.jira_gaps.poller import POLL_SECONDS, JiraGapPoller
from api.jira_gaps.poster import JiraGapPoster
from api.schemas import JiraAccessFailed, JiraAccessOk, JiraGapsOut
from db import jira_gaps as gaps_db
from db.jira_gaps import GapTargetSaved
from jira.gateway import JiraError
from jira.reasons import Reason
from logger.logger import WARNING, get_logger, log_event

logger = get_logger("api.jira_gaps.channel")

NEEDED: Final = ("BROWSE_PROJECTS", "CREATE_ISSUES", "ADD_COMMENTS")  # a sub-task is created with CREATE_ISSUES
_OF_INPUT: Final = frozenset({Reason.NOT_FOUND, Reason.FORBIDDEN})


def _failed(reason: str, *, field: bool = False) -> JiraAccessFailed:
    return JiraAccessFailed.model_validate({"reason": reason, "field": "jira_project" if field else None})


class JiraGaps:
    """The poster, the poller and what the routes need."""

    def __init__(self, runtime: Any, poster: JiraGapPoster, poller: JiraGapPoller) -> None:
        self._runtime = runtime
        self.poster = poster
        self.poller = poller
        self._tasks: set[asyncio.Task[None]] = set()

    async def send(self, project_id: str, result: Any) -> None:
        """The gap runner's notice (``add_notice``)."""

        await self.poster.send(project_id, result)

    def start(self) -> None:
        self.poller.start()

    async def aclose(self) -> None:
        await self.poller.aclose()
        tasks = [task for task in self._tasks if not task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def after_save(self, project_id: str, saved: GapTargetSaved) -> None:
        """The target was saved: when it was turned on (or its Jira project changed) post the open questions now, in the
        background, so the form shows the Story; never raises and never waits for Jira."""

        if saved.turned_on or saved.project_changed:
            task = asyncio.create_task(self._post(project_id), name=f"jira-gaps-post-{project_id}")
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _post(self, project_id: str) -> None:
        with Contained() as posted:
            await self.poster.post_open(project_id)
        if posted.failed:
            log_event(logger, WARNING, "api.jira_gaps.post_failed", project_id=project_id, error_type=posted.error_type)

    async def view(self, project_id: uuid.UUID, *, available: bool) -> JiraGapsOut:
        async with self._runtime.transaction() as session:
            target = await gaps_db.get_target(session, project_id)
            rows = await gaps_db.list_issues(session, project_id) if target is not None else []
        site = self._runtime.settings.jira.site_url if self._runtime.settings.jira is not None else None
        story = None if target is None else target.story_key
        return JiraGapsOut(
            available=available,
            jira_project=None if target is None else target.jira_project,
            grouping=None if target is None else ("area" if target.grouping == "area" else "one"),
            enabled=target is not None and target.enabled,
            story_key=story,
            story_url=f"{site.rstrip('/')}/browse/{story}" if story and site else None,
            posted=len(rows),
            answered=sum(1 for row in rows if row.answered_at is not None),
            last_polled_at=None if target is None else target.last_polled_at,
            error_code=None if target is None else target.last_error_code,
            poll_seconds=POLL_SECONDS,
        )

    async def check_access(self, project_id: uuid.UUID, jira_project: str) -> JiraAccessOk | JiraAccessFailed:
        """What the channel needs of the bound member in ``jira_project``: they authenticate, the project has Story and
        Sub-task types, and they may browse it, create issues (a sub-task is one) and add comments. No transaction is
        open across a Jira call; no Jira text is kept."""

        try:
            acting = await acting_gateway(self._runtime, project_id)
        except ActingError as error:
            return _failed(error.code)
        except JiraError as error:
            return _failed(error.reason.value)
        gateway = acting.gateway
        try:
            await gateway.myself()
            project = await gateway.project(jira_project)
            if project.story_type_id is None or project.subtask_type_id is None:
                return _failed(Reason.ISSUE_TYPES_MISSING.value, field=True)
            missing = await gateway.missing_permissions(jira_project)
        except JiraError as error:
            known = error.reason in _OF_INPUT
            reason = (
                error.reason
                if error.reason in {*_OF_INPUT, Reason.AUTH_FAILED, Reason.UNREACHABLE}
                else Reason.UNEXPECTED
            )
            return _failed(reason.value, field=known)
        if any(name in missing for name in NEEDED):
            return _failed(Reason.FORBIDDEN.value, field=True)
        return JiraAccessOk(project_name=project.name, epic_summary=None)
