"""The Review step: one advisory call on the ``judge`` call class with ``PlanOptions.review_model``.

Issues are recorded and counted; they change nothing. A failed call (or an answer naming unknown areas
or tasks only) never fails the run: it is recorded as ``failed``.
"""

from __future__ import annotations

from typing import Final, cast, get_args

from graph.core.errors import LLMError, LLMFatalError
from graph.plan.answers import ReviewAnswer
from graph.plan.inputs import review_messages
from graph.plan.models import (
    DescribeArtifact,
    PlannerOutcome,
    ProjectArtifact,
    ReviewArtifact,
    ReviewIssue,
    ReviewSeverity,
    StitchArtifact,
    TasksArtifact,
)
from graph.plan.options import PlanContext
from graph.plan.prompts import gateway_prompt
from graph.plan.view import PlanView
from logger.logger import WARNING, get_logger, log_event

logger = get_logger("graph.plan")

_SEVERITIES: Final = get_args(ReviewSeverity)


async def review(
    context: PlanContext,
    view: PlanView,
    *,
    describe: DescribeArtifact,
    project: ProjectArtifact,
    tasks: TasksArtifact,
    stitch: StitchArtifact,
) -> ReviewArtifact:
    """Review the stitched plan, or skip when ``review`` is off."""

    if not context.options.review:
        return ReviewArtifact(skipped=True)
    planned = (*project.setup, *project.closing, *tasks.tasks)
    waits: dict[str, list[str]] = {}
    for wait in stitch.waits:
        waits.setdefault(wait.source, []).append(wait.target)
    messages = review_messages(
        view, describe=describe, project=project, tasks=planned, waits=waits, tasks_artifact=tasks
    )
    try:
        answer = await context.gateway.structured(
            "judge", context.options.review_model, messages, ReviewAnswer, prompt=gateway_prompt("review")
        )
    except LLMFatalError:
        raise
    except LLMError as error:
        log_event(logger, WARNING, "graph.plan.review_failed", code=error.code)
        return ReviewArtifact(failed=True, outcome=PlannerOutcome(calls=1, problems=(error.code,)))
    areas = {area.area_id for area in view.active_areas}
    keys = {task.task_key for task in planned}
    issues = tuple(
        ReviewIssue(
            area_id=item.area_id,
            task_id=item.task_id or None,
            problem=item.problem,
            severity=cast(ReviewSeverity, item.severity),
        )
        for item in answer.issues
        if item.area_id in areas
        and (not item.task_id or item.task_id in keys)
        and item.severity in _SEVERITIES
        and item.problem.strip()
    )
    dropped = len(answer.issues) - len(issues)
    return ReviewArtifact(issues=issues, outcome=PlannerOutcome(calls=1, invalid=1 if dropped else 0))
