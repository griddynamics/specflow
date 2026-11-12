"""What the story and agent write routes share: the mapping of plan refusals to API errors, the check of text
arguments and the ``api.agent.written`` audit event (ids, the outcome and ``repeat``; never a label, branch, note, text
or claim token)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Final

from api.agent_reads import AgentReads
from api.errors import (
    ApiError,
    area_not_found,
    claim_mismatch,
    invalid_arguments,
    no_plan,
    plan_applying,
    story_not_found,
    task_not_found,
)
from api.runtime import agent_error
from db.models import User
from graph.core.errors import ConfigurationError
from graph.plan.errors import PlanReadError, TaskStateError
from logger.logger import INFO, get_logger, log_event

logger = get_logger("api.write_support")

_PLAN_ERRORS: Final[dict[str, Callable[[], ApiError]]] = {
    "no_plan": no_plan,
    "story_not_found": story_not_found,
    "area_not_found": area_not_found,
}
_STORY_GONE: Final = frozenset({"unknown_story", "wrong_state"})


async def plan_read[T](call: Callable[[], Awaitable[T]]) -> T:
    """``call``'s answer; a plan read that finds nothing becomes the error of the same code."""

    try:
        return await call()
    except PlanReadError as error:
        mapped = _PLAN_ERRORS.get(error.code)
        if mapped is None:
            raise
        raise mapped() from None


def printable(**checked: tuple[str | None, Callable[[str | None], None]]) -> None:
    """Each text argument passes its library check, else ``invalid_arguments`` naming the argument (never its value)."""

    for name, (value, check) in checked.items():
        try:
            check(value)
        except ConfigurationError:
            raise invalid_arguments(name) from None


async def written[T](
    route: str, graph_id: str, user: User, ids: Mapping[str, Any], call: Callable[[], Awaitable[tuple[T, bool]]]
) -> T:
    """Run the write and log its ``api.agent.written`` event: the ``ids`` (read after the call, so it may add some),
    the outcome (``ok`` or the code) and ``repeat``."""

    outcome, repeat = "ok", False
    try:
        result, repeat = await call()
    except ApiError as error:
        outcome = error.code
        raise
    except Exception as error:
        mapped = agent_error(error)
        outcome = mapped.code if mapped is not None else "internal_error"
        raise
    else:
        return result
    finally:
        log_event(
            logger,
            INFO,
            "api.agent.written",
            route=route,
            project_id=graph_id,
            user_id=str(user.id),
            **ids,
            outcome=outcome,
            repeat=repeat,
        )


async def write_refusal(reads: AgentReads, graph_id: str, story_id: str, error: TaskStateError) -> ApiError:
    """The API error of a refused story or task write; an unknown story is ``no_plan`` for a project without a plan."""

    if error.code in _STORY_GONE:
        try:
            await reads.plan.story_preview(graph_id, story_id)
        except PlanReadError as read:
            return _PLAN_ERRORS.get(read.code, story_not_found)()
        return story_not_found()
    if error.code == "unknown_task":
        return task_not_found()
    if error.code == "claim_mismatch":
        return claim_mismatch()
    if error.code == "plan_applying":
        return plan_applying()
    raise error
