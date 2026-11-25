"""Reading the answers back from the sub-tasks (docs/jira-sync/gaps.md "Answers").

No webhook (``manage:jira-webhook`` is not granted and a new scope means consent again) and no public route: one
in-process loop wakes every ``POLL_SECONDS`` and, for each enabled target, makes ONE search (the children of the
target's Story changed since the cursor). The poll stops at the Jira door when the bound member's connection is expired
or gone (nothing is sent) and picks up again once the binding works.

A comment answers when its author's Atlassian account id is the connection of a project member; the acting account's
own comments (the confirmations and the hint) are never read. Every effect is idempotent, so the cursor (the newest
``updated`` read, less ``OVERLAP``) only bounds the search: an answered question is skipped (its row), the "how to
link" hint is posted once per person and sub-task (the hint mentions them), and a refusal is not repeated once a
comment of the acting account follows the refused comment. A comment's text is read only to parse the answer and is
never logged or kept.
"""

from __future__ import annotations

import asyncio
import math
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from api.contained import Contained
from api.gap.replies import parse_reply, record_and_apply
from api.jira_gaps import text
from api.jira_gaps.acting import Acting, ActingError, acting_gateway
from db import jira_gaps as gaps_db
from db.jira_gaps import GapIssueView, GapTargetView
from graph.gap import AnswerInput, AnswerInvalidError, GapNotFoundError, GapStateError
from jira.gateway import JiraError
from jira.http import TokenSource
from jira.issues import JiraIssueReader
from jira.reader import AtlassianReader, jira_url
from jira.reasons import Reason
from logger.logger import INFO, WARNING, get_logger, log_event

logger = get_logger("api.jira_gaps.poller")

POLL_SECONDS: Final = 60  # how often answers are read; the form copy says "about every 60 seconds"
OVERLAP: Final = timedelta(minutes=2)
ASSIGN_FAILED: Final = "api.jira_gaps.assign_failed"
CONTRADICTION: Final = "contradiction"
OPEN: Final = "open"

Sleep = Callable[[float], Awaitable[None]]
ReaderFactory = Callable[[str, TokenSource], AbstractAsyncContextManager[JiraIssueReader]]
Comment = dict[str, Any]


@asynccontextmanager
async def issue_reader(cloud_id: str, tokens: TokenSource) -> AsyncIterator[JiraIssueReader]:
    """The reads of one user's Jira (the one place that builds the HTTP client)."""

    reader = AtlassianReader(jira_url(cloud_id), tokens)
    try:
        yield JiraIssueReader(reader)
    finally:
        await reader.aclose()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def jira_time(value: Any) -> datetime | None:
    """A Jira timestamp (``2026-11-25T10:00:00.000+0000``), or ``None`` when it is not one."""

    try:
        stamp = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return stamp if stamp.tzinfo is not None else None


@dataclass(frozen=True)
class _Poll:
    """What one poll of one project works with."""

    project_id: str
    target: GapTargetView
    acting: Acting
    accounts: Mapping[str, uuid.UUID]


@dataclass
class _Issue:
    """A sub-task read: its key, the questions in it still open (by number) and all its comments."""

    key: str
    open_rows: dict[int, GapIssueView]
    comments: Sequence[Comment]


class RefusedError(Exception):
    """A question the answer cannot be recorded for; ``line`` is what the confirmation says, ``closes`` whether the
    question is closed for good (it is not open any more)."""

    def __init__(self, line: str, *, closes: bool = False) -> None:
        super().__init__("refused")
        self.line = line
        self.closes = closes


class JiraGapPoller:
    """Polls the sub-tasks of every enabled target; held by ``JiraGaps``."""

    def __init__(
        self,
        runtime: Any,
        *,
        now: Callable[[], datetime] = _utc_now,
        sleep: Sleep = asyncio.sleep,
        reader: ReaderFactory = issue_reader,
    ) -> None:
        self._runtime = runtime
        self._now = now
        self._sleep = sleep
        self._reader = reader
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="jira-gap-poller")

    async def aclose(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self) -> None:
        while True:
            await self.tick()
            await self._sleep(POLL_SECONDS)

    async def tick(self) -> int:
        """One round: every enabled target polled once, a failure of one never stops the others; answers recorded."""

        answered = 0
        with Contained() as listed:
            async with self._runtime.transaction() as session:
                targets = await gaps_db.enabled_targets(session)
        if listed.failed:
            log_event(logger, WARNING, "api.jira_gaps.poll_failed", project_id=None, error_type=listed.error_type)
            return 0
        for target in targets:
            with Contained() as polled:
                answered += await self.poll_once(str(target.project_id))
            if polled.failed:
                log_event(
                    logger,
                    WARNING,
                    "api.jira_gaps.poll_failed",
                    project_id=str(target.project_id),
                    error_type=polled.error_type,
                )
        return answered

    async def poll_once(self, project_id: str) -> int:
        """Poll one project's Story; how many questions were answered. A refused read keeps its closed code on the
        target and answers nothing."""

        pid = uuid.UUID(project_id)
        async with self._runtime.transaction() as session:
            target = await gaps_db.get_target(session, pid)
            rows = await gaps_db.list_issues(session, pid) if target is not None else []
        open_rows = [row for row in rows if row.answered_at is None]
        if target is None or not target.enabled or target.story_key is None or not open_rows:
            return 0
        try:
            acting = await acting_gateway(self._runtime, pid)
            issues, newest = await self._read(target, acting, open_rows)
            async with self._runtime.transaction() as session:
                accounts = await gaps_db.member_accounts(session, pid)
            poll = _Poll(project_id, target, acting, accounts)
            answered = sum([await self._issue(poll, issue) for issue in issues])
        except (ActingError, JiraError) as error:
            code = error.reason.value if isinstance(error, JiraError) else error.code
            async with self._runtime.transaction() as session:
                await gaps_db.record_error(session, pid, code)
            log_event(logger, WARNING, "api.jira_gaps.poll_refused", project_id=project_id, error_code=code)
            return 0
        async with self._runtime.transaction() as session:
            await gaps_db.record_poll(session, pid, cursor=_cursor(target, newest), now=self._now(), error_code=None)
        if answered:
            log_event(logger, INFO, "api.jira_gaps.answered", project_id=project_id, gaps=answered)
        return answered

    async def _read(
        self, target: GapTargetView, acting: Acting, rows: Sequence[GapIssueView]
    ) -> tuple[list[_Issue], datetime | None]:
        """The changed sub-tasks that still ask something, with all their comments (one search, then the pages of the
        comments an issue did not bring whole), and the newest ``updated`` among everything the search returned."""

        connections = self._runtime.jira.connections
        view = await connections.get(acting.user_id)
        if view is None or view.status != "active":
            raise JiraError(Reason.NOT_CONNECTED if view is None else Reason.RECONNECT_REQUIRED)
        within = None
        if target.poll_cursor is not None:
            within = math.ceil((self._now() - target.poll_cursor).total_seconds() / 60) + 1
        found: list[_Issue] = []
        stamps: list[datetime] = []
        async with self._reader(view.cloud_id, connections.token_source(acting.user_id)) as reader:
            for issue in await reader.updated_children(str(target.story_key), within):
                if (stamp := jira_time((issue.get("fields") or {}).get("updated"))) is not None:
                    stamps.append(stamp)
                key = str(issue.get("key", ""))
                open_rows = {row.number: row for row in rows if row.issue_key == key}
                if open_rows:
                    found.append(_Issue(key, open_rows, await reader.comments(issue)))
        return found, max(stamps, default=None)

    async def _issue(self, poll: _Poll, issue: _Issue) -> int:
        """Every comment of one sub-task, oldest first; how many questions it answered."""

        answered = 0
        for comment in sorted(issue.comments, key=lambda c: str(c.get("created", ""))):
            answered += await self._comment(poll, issue, comment)
        return answered

    async def _comment(self, poll: _Poll, issue: _Issue, comment: Comment) -> int:
        acting = poll.acting
        author = str((comment.get("author") or {}).get("accountId") or "")
        if not author or author == acting.account_id:
            return 0  # the acting account's own comments are Graphico's: never read as answers
        body = text.comment_text(comment.get("body"))
        entries = text.numbered_answers(body) if poll.target.grouping == "area" else ({1: body} if body else {})
        entries = {number: said for number, said in entries.items() if number in issue.open_rows}
        if not entries:
            return 0
        user_id = poll.accounts.get(author)
        if user_id is None:
            if not any(text.is_hint_for(c.get("body"), author) for c in issue.comments):
                await acting.gateway.add_comment(issue.key, text.hint_doc(author))
            return 0
        lines = [
            line for number, said in entries.items() if (line := await self._one(poll, issue, user_id, number, said))
        ]
        done = sum(1 for ok, _ in lines if ok)
        if done or (lines and not _followed(issue.comments, comment, acting.account_id)):
            await acting.gateway.add_comment(issue.key, text.recorded_doc(author, [line for _, line in lines]))
        return done

    async def _one(
        self, poll: _Poll, issue: _Issue, user_id: uuid.UUID, number: int, said: str
    ) -> tuple[bool, str] | None:
        """Record one question's answer: ``(True, line)`` when recorded, ``(False, line)`` when refused, ``None`` when
        the words are no answer. A question the service no longer holds open is closed here for good."""

        label = f"Question {number}: " if poll.target.grouping == "area" else ""
        try:
            gap = await self._open(poll, issue.open_rows[number].gap_id, label)
            rank, own = _reading(gap, said, label)
            if rank is None and own is None:
                return None
            applied = await self._record(poll, issue.open_rows[number].gap_id, user_id, (rank, own), label)
        except RefusedError as refused:
            if refused.closes:
                await self._mark(poll, issue, number)
            return False, refused.line
        await self._mark(poll, issue, number)
        index = next((i for i, option in enumerate(gap.options) if option.rank == rank), None)
        chosen = (
            "your own answer"
            if index is None
            else f"{chr(ord('A') + index)}) {text.cut(gap.options[index].label, 150)}"
        )
        return True, f"{label}recorded {chosen}: {'applying in one update build' if applied else 'saved'}."

    async def _open(self, poll: _Poll, gap_id: str, label: str) -> Any:
        """The gap when it is still open, else ``RefusedError`` (which closes the question)."""

        service = await self._runtime.gaps.service()
        try:
            gap = await service.get_gap(poll.project_id, gap_id)
        except GapNotFoundError:
            raise RefusedError(f"{label}not recorded, this question is no longer open.", closes=True) from None
        if gap.status != OPEN:
            raise RefusedError(f"{label}not recorded, this question is {gap.status} now.", closes=True)
        return gap

    async def _record(
        self, poll: _Poll, gap_id: str, user_id: uuid.UUID, said: tuple[int | None, str | None], label: str
    ) -> bool:
        """Record, assign and apply the answer; ``RefusedError`` when the service refuses it."""

        answer = AnswerInput(
            gap_id=gap_id, option_rank=said[0], own_text=said[1], answered_by=str(user_id), updated_at=None, via="jira"
        )
        try:
            return await record_and_apply(
                self._runtime.gaps, poll.project_id, answer, user_id, assign_event=ASSIGN_FAILED
            )
        except GapStateError as error:
            raise RefusedError(f"{label}not recorded, this question is {error.status} now.", closes=True) from None
        except GapNotFoundError:
            raise RefusedError(f"{label}not recorded, this question is no longer open.", closes=True) from None
        except AnswerInvalidError as error:
            log_event(
                logger, WARNING, "api.jira_gaps.answer_refused", project_id=poll.project_id, error_code=error.code
            )
            raise RefusedError(f"{label}not recorded, the answer was refused.") from None

    async def _mark(self, poll: _Poll, issue: _Issue, number: int) -> None:
        row = issue.open_rows.pop(number)
        async with self._runtime.transaction() as session:
            await gaps_db.mark_answered(session, uuid.UUID(poll.project_id), row.gap_id, now=self._now())


def _reading(gap: Any, said: str, label: str) -> tuple[int | None, str | None]:
    """The words as ``(option rank, own text)``; a contradiction takes one option letter only."""

    rank, own = parse_reply(said, {chr(ord("A") + i): o.rank for i, o in enumerate(gap.options)}, text.OWN_MAX)
    if own is not None and gap.kind == CONTRADICTION:
        raise RefusedError(f"{label}not recorded, this question takes one option letter.")
    return rank, own


def _cursor(target: GapTargetView, newest: datetime | None) -> datetime | None:
    """The new cursor: the newest ``updated`` read less ``OVERLAP``; ``None`` (keep the stored one) when it would not
    move forward."""

    if newest is None:
        return None
    cursor = newest - OVERLAP
    return None if target.poll_cursor is not None and cursor <= target.poll_cursor else cursor


def _followed(comments: Sequence[Mapping[str, Any]], comment: Mapping[str, Any], account_id: str) -> bool:
    """Whether the acting account commented after ``comment`` (it was answered, refused or hinted at already)."""

    created = jira_time(comment.get("created"))
    if created is None:
        return False
    for other in comments:
        stamp = jira_time(other.get("created"))
        if (other.get("author") or {}).get("accountId") == account_id and stamp is not None and stamp > created:
            return True
    return False
