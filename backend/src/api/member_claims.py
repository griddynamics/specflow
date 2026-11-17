"""Free the story claims a person held when they leave a project or are removed (``docs/projects/api.md``,
"Remove, leave and claims").

Runs after the membership delete has committed: access has already ended, so nothing here can undo it. Best effort:
any graph error (the listing, the plan reads or one release) sets ``failed`` and the removal stands; the other claims
are still tried. A project without a plan has nothing to release. A task claimed on its own has no forced release
(``PlanService`` offers one for story claims only) and is only counted in the event. The ``api.member.claims_released``
event carries ids and counts, never a label or a branch.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final

from api.agent_reads import AgentReads, agent_reads
from api.contained import Contained
from api.runtime import Runtime
from graph.plan import ClaimRow
from graph.plan.errors import PlanReadError
from logger.logger import INFO, get_logger, log_event

logger = get_logger("api.member_claims")

NO_PLAN: Final = "no_plan"


@dataclass(frozen=True)
class ClaimsReleased:
    released: int
    failed: bool


async def release_member_claims(runtime: Runtime, project_id: uuid.UUID, member_id: uuid.UUID) -> ClaimsReleased:
    """Force-release every story claim ``member_id`` holds in ``project_id``; never raises (``Exception``)."""

    released, task_claims_left, failed = 0, 0, False
    graph_id, holder = str(project_id), str(member_id)
    reads: AgentReads | None = None
    with Contained() as load:
        reads = await agent_reads(runtime)
    if load.failed or reads is None:
        failed = True
    else:
        rows: tuple[ClaimRow, ...] = ()
        with Contained() as listing:
            try:
                rows = await reads.plan.claims(graph_id)
            except PlanReadError as error:
                if error.code != NO_PLAN:
                    raise
        failed = listing.failed
        held = [row for row in rows if row.claimed_by == holder]
        task_claims_left = sum(1 for row in held if row.kind == "task")
        for row in held:
            if row.kind != "story":
                continue
            with Contained() as release:
                await reads.plan.release_story(graph_id, row.story_id, force=True)
            if release.failed:
                failed = True
            else:
                released += 1
    log_event(
        logger,
        INFO,
        "api.member.claims_released",
        project_id=graph_id,
        member_user_id=holder,
        released=released,
        failed=failed,
        task_claims_left=task_claims_left,
    )
    return ClaimsReleased(released, failed)
