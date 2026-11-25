"""Reading an answer from a Linear comment (docs/linear/gaps.md "Answers"): the one path both the webhook and the
catch-up read take, so they cannot disagree.

A comment answers when its author's Linear user id (in the comment's workspace) is the connection of a project
member and it sits in an issue the channel posted. Every effect is idempotent: an answered question is skipped (its
row), and the confirmation and the "how to link" hint carry client comment ids made of the triggering comment (or of
the person and the issue), so a second delivery or a replayed read posts neither twice. The channel's own comments
(``text.is_own``) and the comments of an integration are never read. A comment's text is read only to parse the
answer and is never logged or kept.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from api.gap.replies import parse_reply, record_and_apply
from api.linear_gaps import text
from api.linear_gaps.acting import Acting, ActingError, acting_gateway
from db import linear_gaps as gaps_db
from db.linear_gaps import GapIssueView, GapTargetView
from graph.gap import AnswerInput, AnswerInvalidError, GapNotFoundError, GapStateError
from linear import ids
from linear.errors import LinearAlreadyExistsError, LinearError
from logger.logger import INFO, WARNING, get_logger, log_event

logger = get_logger("api.linear_gaps.answers")

ASSIGN_FAILED: Final = "api.linear_gaps.assign_failed"
CONTRADICTION: Final = "contradiction"
OPEN: Final = "open"
COMPLETED: Final = "completed"


@dataclass(frozen=True)
class Incoming:
    """A comment as the channel reads it, from the webhook or from the gateway: ``user_id`` is ``None`` for a comment of
    an integration."""

    comment_id: str
    issue_id: str
    organization_id: str
    user_id: str | None
    body: str


@dataclass(frozen=True)
class _Read:
    """What one comment works with: the posted gaps of its issue (by number) and the target."""

    project_id: str
    target: GapTargetView
    rows: Mapping[int, GapIssueView]
    incoming: Incoming


def _utc_now() -> datetime:
    return datetime.now(UTC)


class LinearAnswers:
    """Turns comments into answers; held by ``LinearGaps``."""

    def __init__(self, runtime: Any, *, now: Callable[[], datetime] = _utc_now) -> None:
        self._runtime = runtime
        self._now = now
        self._locks: dict[uuid.UUID, asyncio.Lock] = {}

    async def process(self, incoming: Incoming, acting: Acting | None = None) -> int:
        """One comment: record what answers a question open in its issue; how many questions it answered. ``acting``
        is the account the confirmation is made as (the project's bound member when ``None``)."""

        if incoming.user_id is None or text.is_own(incoming.body):
            return 0
        try:
            issue = uuid.UUID(incoming.issue_id)
        except ValueError:
            return 0
        async with self._locks.setdefault(issue, asyncio.Lock()):  # one reader of an issue at a time
            read = await self._read(issue, incoming)
            if read is None:
                return 0
            return await self._answer(read, acting)

    async def _read(self, issue: uuid.UUID, incoming: Incoming) -> _Read | None:
        async with self._runtime.transaction() as session:
            posted = await gaps_db.issue_rows(session, issue)
            target = await gaps_db.get_target(session, posted[0].project_id) if posted else None
        if target is None or not target.enabled:
            return None  # an issue the channel did not post (or a channel that is off): nothing to do
        rows = {row.number: row for row in posted if row.answered_at is None}
        entries = _entries(target, incoming.body)
        if not rows or not any(number in rows for number in entries):
            return None
        return _Read(str(target.project_id), target, rows, incoming)

    async def _answer(self, read: _Read, acting: Acting | None) -> int:
        incoming, pid = read.incoming, uuid.UUID(read.project_id)
        async with self._runtime.transaction() as session:
            member = await gaps_db.member_of(
                session, pid, organization_id=incoming.organization_id, linear_user_id=str(incoming.user_id)
            )
        if member is None:
            await self._hint(read, acting)
            return 0
        lines = []
        done: list[GapIssueView] = []
        entries = _entries(read.target, incoming.body)
        for number, said in entries.items():
            if number not in read.rows:
                continue
            outcome = await self._one(read, member, number, said)
            if outcome is None:
                continue
            lines.append(outcome[1])
            if outcome[0]:
                done.append(read.rows[number])
        if lines:
            await self._confirm(read, acting, lines, done)
        return len(done)

    async def _one(self, read: _Read, member: uuid.UUID, number: int, said: str) -> tuple[bool, str] | None:
        """Record one question's answer: ``(True, line)`` when recorded, ``(False, line)`` when refused, ``None`` when
        the words are no answer. A question the service no longer holds open is closed here for good."""

        row, pid = read.rows[number], read.project_id
        label = f"Question {number}: " if read.target.grouping == "area" else ""
        service = await self._runtime.gaps.service()
        try:
            gap = await service.get_gap(pid, row.gap_id)
        except GapNotFoundError:
            gap = None
        if gap is None or gap.status != OPEN:
            await self._mark(pid, row)
            state = "no longer open" if gap is None else f"{gap.status} now"
            return False, f"{label}not recorded, this question is {state}."
        return await self._record(read, member, (row, gap), (said, label))

    async def _record(
        self, read: _Read, member: uuid.UUID, posted: tuple[GapIssueView, Any], words: tuple[str, str]
    ) -> tuple[bool, str] | None:
        """Parse the words as the answer to an open gap and record it (``_one``'s second half)."""

        (row, gap), (said, label), pid = posted, words, read.project_id
        rank, own = parse_reply(said, {chr(ord("A") + i): o.rank for i, o in enumerate(gap.options)}, text.OWN_MAX)
        if rank is None and own is None:
            return None
        if own is not None and gap.kind == CONTRADICTION:
            return False, f"{label}not recorded, this question takes one option letter."
        answer = AnswerInput(
            gap_id=row.gap_id, option_rank=rank, own_text=own, answered_by=str(member), updated_at=None, via="linear"
        )
        try:
            applied = await record_and_apply(self._runtime.gaps, pid, answer, member, assign_event=ASSIGN_FAILED)
        except (GapStateError, GapNotFoundError) as error:
            await self._mark(pid, row)
            now = error.status if isinstance(error, GapStateError) else "closed"
            return False, f"{label}not recorded, this question is {now} now."
        except AnswerInvalidError as error:
            log_event(logger, WARNING, "api.linear_gaps.answer_refused", project_id=pid, error_code=error.code)
            return False, f"{label}not recorded, the answer was refused."
        await self._mark(pid, row)
        state = "applying in one update build" if applied else "saved"
        return True, f"{label}recorded {_chosen(gap, rank)}: {state}."

    async def _mark(self, project_id: str, row: GapIssueView) -> None:
        async with self._runtime.transaction() as session:
            await gaps_db.mark_answered(session, uuid.UUID(project_id), row.gap_id, now=self._now())

    async def _confirm(
        self, read: _Read, acting: Acting | None, lines: Sequence[str], done: list[GapIssueView]
    ) -> None:
        """The confirmation comment, and the sub-issue of an answered question moved to the team's first ``completed``
        state (grouping ``one``). Best effort: the answer is recorded whatever Linear says."""

        issue, pid = read.incoming.issue_id, read.project_id
        comment_id = str(ids.node_uuid(pid, ids.KIND_COMMENT, f"recorded:{read.incoming.comment_id}"))
        try:
            async with AsyncExitStack() as stack:
                gateway = await self._gateway(stack, pid, acting)
                if gateway is None:
                    return
                await _comment_once(gateway, issue, text.recorded_comment(lines), comment_id)
                if done and read.target.grouping == "one":
                    await _complete(gateway, read.target.team_id, issue)
        except LinearError as error:
            log_event(logger, WARNING, "api.linear_gaps.confirm_failed", project_id=pid, error_code=error.code.value)
            return
        log_event(logger, INFO, "api.linear_gaps.answered", project_id=pid, gaps=len(done))

    async def _hint(self, read: _Read, acting: Acting | None) -> None:
        """Tell a person who is no member how to link their account: once per person and issue (the client id of the
        comment is made of both, so a replayed comment or a restart posts nothing more)."""

        who = f"hint:{read.incoming.issue_id}:{read.incoming.user_id}"
        comment_id = str(ids.node_uuid(read.project_id, ids.KIND_COMMENT, who))
        try:
            async with AsyncExitStack() as stack:
                gateway = await self._gateway(stack, read.project_id, acting)
                if gateway is not None:
                    await _comment_once(gateway, read.incoming.issue_id, text.hint_comment(), comment_id)
        except LinearError as error:
            log_event(
                logger, WARNING, "api.linear_gaps.hint_failed", project_id=read.project_id, error_code=error.code.value
            )

    async def _gateway(self, stack: AsyncExitStack, project_id: str, acting: Acting | None) -> Any:
        """The gateway to confirm with: the caller's, or the bound member's opened on ``stack``; ``None`` when the
        project has no member to act as."""

        if acting is not None:
            return acting.gateway
        try:
            return (await stack.enter_async_context(acting_gateway(self._runtime, uuid.UUID(project_id)))).gateway
        except (ActingError, LinearError):
            log_event(logger, WARNING, "api.linear_gaps.no_acting", project_id=project_id)
            return None


def _entries(target: GapTargetView, body: str) -> dict[int, str]:
    """``{number: words}`` a comment answers: the numbered lines (grouping ``area``) or the whole comment as question
    1."""

    body = body.strip()
    if target.grouping == "area":
        return text.numbered_answers(body)
    return {1: body} if body else {}


def _chosen(gap: Any, rank: int | None) -> str:
    index = next((i for i, option in enumerate(gap.options) if option.rank == rank), None)
    if index is None:
        return "your own answer"
    return f"{chr(ord('A') + index)}) {text.cut(gap.options[index].label, 150)}"


async def _comment_once(gateway: Any, issue_id: str, body: str, comment_id: str) -> None:
    try:
        await gateway.create_comment(issue_id, body, comment_id=comment_id)
    except LinearAlreadyExistsError:
        return


async def _complete(gateway: Any, team_id: str, issue_id: str) -> None:
    states = [state for state in await gateway.workflow_states(team_id) if state.type == COMPLETED]
    if states:
        await gateway.update_issue(issue_id, state_id=min(states, key=lambda state: state.position).id)
