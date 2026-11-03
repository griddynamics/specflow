"""Building status of a project."""

from __future__ import annotations

from graph.build.models import STEPS, ProjectBuildStatus, StepName
from graph.build.ports import BuildRecord, BuildStore


async def project_status(builds: BuildStore, project_id: str) -> ProjectBuildStatus:
    """The building status of ``project_id`` from ``builds`` (read-only)."""

    finished = await builds.latest_build(project_id, finished_only=True)
    result = await builds.get_result(project_id, finished.build_id) if finished is not None else None
    settlement = result.settlement if result is not None else None
    latest = await builds.latest_build(project_id)
    if latest is None or latest.status != "running" or latest.finished_at is not None:
        return ProjectBuildStatus(project_id=project_id, settlement=settlement)
    return ProjectBuildStatus(
        project_id=project_id,
        building=True,
        build_id=latest.build_id,
        mode=latest.mode,
        step=current_step(latest),
        started_at=latest.started_at,
        follow_up_attempt=latest.follow_up_attempt,
        settlement=settlement,
    )


def current_step(record: BuildRecord) -> StepName | None:
    """The step a running build is at: the last step marked ``running``, else the first step not
    ``done`` (between two steps); None when every step is done."""

    running = [step for step in STEPS if record.progress.get(step) == "running"]
    if running:
        return running[-1]
    return next((step for step in STEPS if record.progress.get(step) != "done"), None)
