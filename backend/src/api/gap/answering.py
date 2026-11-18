"""What the web and the agent answer routes share (docs/graph/gap/api.md "Web routes", docs/mcp-server/apps.md
"Terminal dialogs"): the API errors of the gap service's refusals, and the record of an answer with its scheduled
apply. Logs carry ids and codes only (NFR-GSEC-02); the routes log their own events."""

from __future__ import annotations

from api.errors import ApiError
from api.gap.state import GapRunnerReads
from graph.gap import AnswerInput, AnswerInvalidError, AnswerResult, GapStateError


def gap_not_found() -> ApiError:
    return ApiError(404, "gap_not_found", "The gap does not exist.")


def gap_wrong_state(error: GapStateError) -> ApiError:
    return ApiError(
        409,
        "gap_wrong_state",
        "The gap is not in a state this action applies to.",
        details={"status": error.status, "allowed": list(error.allowed)},
    )


def gap_stale() -> ApiError:
    return ApiError(409, "gap_stale", "The gap changed since it was read. Reload it.")


def answer_invalid(error: AnswerInvalidError) -> ApiError:
    return ApiError(
        400, "answer_invalid", "The answer is not valid.", details={"field": error.field, "reason": error.reason}
    )


async def record_answer(runner: GapRunnerReads, project_id: str, answer: AnswerInput) -> AnswerResult:
    """``GapService.answer_gap``; unless the answer applies at once, the apply waits for the project's quiet period.
    The service's refusals pass through for the route to map."""

    service = await runner.service()
    result = await service.answer_gap(project_id, answer)
    if not result.immediate:
        runner.schedule_apply(project_id, result.apply_at)
    return result
