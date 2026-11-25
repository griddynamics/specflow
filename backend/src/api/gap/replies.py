"""What the chat channels share when a person answers a gap in a message (Slack threads, Jira sub-tasks): the reply
read as an answer, and the sequence after it - record the answer, assign the gap to the answerer, apply it now.

The channel keeps what is its own: who the replier is, its refusals and its confirmation. Logs carry ids and codes only;
a reply's text never reaches one.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from typing import Any, Final

from api.contained import Contained
from api.gap.answering import record_answer
from graph.gap import AnswerInput, GapStateError
from logger.logger import WARNING, get_logger, log_event

logger = get_logger(__name__)

REPLY_OPTION: Final = re.compile(r"^\s*([A-Da-d])[\s.!)]*$")


def parse_reply(text: str, codes: Mapping[str, int], own_max: int) -> tuple[int | None, str | None]:
    """A reply as an answer: one option letter alone (``B``, ``b.``) is that option when the gap offers it; any other
    text is the person's own answer, cut at ``own_max``. ``(None, None)`` for an empty reply."""

    found = REPLY_OPTION.match(text)
    if found is not None and found.group(1).upper() in codes:
        return codes[found.group(1).upper()], None
    own = " ".join(text.split())
    return None, (own[:own_max] or None)


async def assign_answerer(runner: Any, project_id: str, gap_id: str, user_id: uuid.UUID, *, event: str) -> None:
    """After a chat answer: a gap nobody is assigned to is assigned to the person who answered, so the web shows it as
    theirs (an assigned gap keeps its assignee). Never raises: the answer is recorded either way; a failure is logged
    as ``event``."""

    with Contained() as assigned:
        service = await runner.service()
        gap = await service.get_gap(project_id, gap_id)
        if gap.assigned_to is None:
            await service.assign_gap(project_id, gap_id, str(user_id), by=str(user_id))
    if assigned.error is not None:
        log_event(
            logger,
            WARNING,
            event,
            project_id=project_id,
            gap_id=gap_id,
            error_type=type(assigned.error).__name__,
        )


async def apply_staged(runner: Any, project_id: str, user_id: uuid.UUID) -> bool:
    """``apply_now`` for the answer just recorded (and everything else staged); ``False`` when nothing waited (an
    answer applied at once, or someone applied first)."""

    service = await runner.service()
    try:
        await service.apply_now(project_id, by=str(user_id))
    except GapStateError:
        return False
    return True


async def record_and_apply(
    runner: Any, project_id: str, answer: AnswerInput, user_id: uuid.UUID, *, assign_event: str
) -> bool:
    """Record ``answer``, assign the gap to ``user_id`` when nobody holds it and apply at once; whether an update build
    was requested. The service's refusals of the record (``GapStateError``, ``GapNotFoundError``,
    ``AnswerInvalidError``) pass through for the channel to say."""

    await record_answer(runner, project_id, answer)
    await assign_answerer(runner, project_id, answer.gap_id, user_id, event=assign_event)
    return await apply_staged(runner, project_id, user_id)
