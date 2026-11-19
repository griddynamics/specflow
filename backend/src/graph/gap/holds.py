"""Blocker holds and their release (docs/graph/gap/lifecycle.md "Plan link", "Development gaps"; decision GD2).

``graph.gap`` never imports ``graph.plan``: the host's ``StoryHolds`` (the plan service) holds and releases tasks.

- An open ``blocker`` gap holds the tasks implementing its ``ABOUT`` nodes (``StoryHolds.hold_tasks``): their claim is
  refused naming the gap while it is open (FR-GPLN-03). A blocking development gap holds the reporting agent's claimed
  story through the plan's block call instead (``GapService.raise_gap``).
- When a gap resolves, is dismissed, closed as not spec or becomes obsolete, ``release`` returns the tasks it holds to
  ``pending`` (FR-GPLN-04): for the gap id and every alias it keeps (a migrated first-slice finding's ``FND::`` id,
  which the plan's blocked tasks may still name).

A hold or release that fails (the plan refuses with ``plan_applying`` during a persist, a store error) is logged and
left, never a failed run or answer: ``hold_blockers`` holds every open blocker gap again at each gap run and before
each story claim (a plan run that reset a held task, a claim that ended, a follow-up that inherited the severity), and
a release is repeated by the next end of the gap or a person's "unblock" in the plan.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final

from graph.core.errors import GraphError
from graph.gap.gaps import StoredGap
from graph.gap.models import HoldResult
from graph.gap.names import OPEN_STATUSES
from graph.gap.ports import StoryHolds
from logger.logger import INFO, WARNING, get_logger, log_event

logger = get_logger("graph.gap.holds")

BLOCKER: Final = "blocker"
HOLD_REASONS: Final[Mapping[str, str]] = {
    "missing": "spec_missing",
    "contradiction": "spec_contradiction",
    "inconsistency": "spec_contradiction",
}
"""The plan's block reason of a gap kind; any other kind is ``spec_ambiguous``."""
DEFAULT_REASON: Final = "spec_ambiguous"


def hold_reason(kind: str | None) -> str:
    """The plan's block reason for a gap of ``kind``."""

    return HOLD_REASONS.get(str(kind or ""), DEFAULT_REASON)


def aliases_of(props: Mapping[str, Any]) -> tuple[str, ...]:
    """The other ids a gap answers to (``aliases``: a migrated finding's ``FND::`` id)."""

    found = props.get("aliases") or ()
    return tuple(str(item) for item in found if item) if isinstance(found, list | tuple) else ()


async def hold(
    holds: StoryHolds | None, project_id: str, gap_id: str, spec_ids: Sequence[str], kind: str | None
) -> HoldResult:
    """Hold the tasks implementing ``spec_ids`` for an open blocker gap; nothing without a port or ids."""

    if holds is None or not spec_ids:
        return HoldResult()
    try:
        result = await holds.hold_tasks(project_id, gap_id, list(spec_ids), reason=hold_reason(kind))
    except GraphError as error:
        log_event(logger, WARNING, "graph.gap.hold_failed", project_id=project_id, gap_id=gap_id, error_code=error.code)
        return HoldResult()
    log_event(
        logger,
        INFO,
        "graph.gap.tasks_held",
        project_id=project_id,
        gap_id=gap_id,
        held=len(result.held),
        in_progress=len(result.in_progress),
    )
    return result


async def release(
    holds: StoryHolds | None, project_id: str, gap_id: str, aliases: Iterable[str] = (), *, strict: bool = False
) -> tuple[str, ...]:
    """Release the tasks the gap (or one of its aliases) holds; their ids. A store failure is logged, not raised,
    unless ``strict`` (a person's dismissal releases before it deletes the gap, so a refusal can be retried)."""

    if holds is None:
        return ()
    released: list[str] = []
    for held_by in dict.fromkeys((gap_id, *aliases)):
        try:
            released.extend(await holds.release_gap(project_id, held_by))
        except GraphError as error:
            if strict:
                raise
            log_event(
                logger,
                WARNING,
                "graph.gap.release_failed",
                project_id=project_id,
                gap_id=gap_id,
                error_code=error.code,
            )
    if released:
        log_event(logger, INFO, "graph.gap.tasks_released", project_id=project_id, gap_id=gap_id, tasks=len(released))
    return tuple(dict.fromkeys(released))


async def hold_blockers(
    holds: StoryHolds | None, project_id: str, gaps: Iterable[StoredGap], spec_ids: Iterable[str] | None = None
) -> HoldResult:
    """Hold the tasks of every open ``blocker`` gap among ``gaps`` (GD2), or only of those ``ABOUT`` one of ``spec_ids``
    (current ids) when given; idempotent: tasks already held stay as they are. The tasks held now and the claimed ones
    left alone."""

    wanted = None if spec_ids is None else set(spec_ids)
    held: set[str] = set()
    busy: set[str] = set()
    if holds is None:
        return HoldResult()
    for gap in gaps:
        if gap.status not in OPEN_STATUSES or gap.props.get("severity") != BLOCKER or not gap.about_ids:
            continue
        if wanted is not None and not wanted & set(gap.about_ids):
            continue
        result = await hold(holds, project_id, gap.gap_id, gap.about_ids, str(gap.props.get("kind") or ""))
        held |= set(result.held)
        busy |= set(result.in_progress)
    return HoldResult(held=tuple(sorted(held)), in_progress=tuple(sorted(busy - held)))
