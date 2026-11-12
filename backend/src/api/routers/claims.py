"""The claims board of a project for people (web audience): who holds which story or task, since when, and the
forced release of a forgotten story claim.

Access is the project membership every web route applies; any member may force a release, there is no role
(access-control.md AC12). The release is ``PlanService.release_story(force=True)``: the claimed tasks go back to
``pending`` and the claim's holder, label and time are cleared; the holder's own token then answers ``claim_mismatch``.
A task claimed on its own is listed but has no forced release (a library host operation for story claims only).
The ``api.claims.released`` event carries ids, the count and the outcome, never a branch or a name.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Path

from api.agent_reads import agent_reads
from api.agent_views import holders_of
from api.deps import RuntimeDep, UserDep
from api.errors import ApiError
from api.routers.agent import AgentRoute
from api.routers.documents import require_project
from api.work_models import ClaimsOut, ReleasedClaimOut, claim_out
from api.write_support import plan_read, write_refusal
from graph.plan.errors import TaskStateError
from logger.logger import INFO, get_logger, log_event

logger = get_logger("api.routers.claims")
router = APIRouter(prefix="/projects/{pid}/plan/claims", tags=["plan"], route_class=AgentRoute)

_MAX_ID = 200


@router.get("", response_model=ClaimsOut)
async def list_claims(pid: uuid.UUID, runtime: RuntimeDep, user: UserDep) -> ClaimsOut:
    """The claims of the project, oldest first; ``404 no_plan`` without a plan."""

    await require_project(runtime, user.id, pid)
    reads = await agent_reads(runtime)
    rows = await plan_read(lambda: reads.plan.claims(str(pid)))
    holders = await holders_of(runtime, pid, user.id, [row.claimed_by for row in rows])
    return ClaimsOut(claims=[claim_out(row, holders) for row in rows])


@router.post("/{story_id}/release", response_model=ReleasedClaimOut)
async def release_claim(
    pid: uuid.UUID, story_id: Annotated[str, Path(max_length=_MAX_ID)], runtime: RuntimeDep, user: UserDep
) -> ReleasedClaimOut:
    """Free the story claim that holds ``story_id`` (its whole unit); a story with no claim answers ``released: 0``."""

    await require_project(runtime, user.id, pid)
    reads = await agent_reads(runtime)
    outcome, tasks = "error", 0
    try:
        await plan_read(lambda: reads.plan.story_preview(str(pid), story_id))  # an unknown story is story_not_found
        tasks = len((await reads.plan.release_story(str(pid), story_id, force=True)).released)
        outcome = "ok"
    except TaskStateError as error:
        outcome = error.code
        raise await write_refusal(reads, str(pid), story_id, error) from None
    except ApiError as error:
        outcome = error.code
        raise
    finally:
        log_event(
            logger,
            INFO,
            "api.claims.released",
            project_id=str(pid),
            user_id=str(user.id),
            story_id=story_id,
            tasks=tasks,
            outcome=outcome,
        )
    return ReleasedClaimOut(released=tasks)
