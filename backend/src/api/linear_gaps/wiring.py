"""Runtime hook of stream S5: register the Linear gap channel (docs/linear/gaps.md)."""

from __future__ import annotations

import uuid
from contextlib import AsyncExitStack
from typing import Any

from api.linear_gaps.answers import LinearAnswers
from api.linear_gaps.channel import LinearGaps
from api.linear_gaps.poster import LinearGapPoster
from db import linear_gaps as gaps_db
from graph.core.store import as_executor
from graph.plan.reader import LoadedPlan, load_plan


def add_linear_gaps(runtime: Any, stack: AsyncExitStack) -> None:
    """With the member connections (``runtime.linear``) and gap runs (``runtime.gaps``): build the channel, assign it to
    ``runtime.linear_gaps``, register it as a gap-run notice, start the catch-up read and stop it with ``stack``. The
    channel acts as the member the project's Linear is connected by, so it needs the connections."""

    if runtime.linear is None or runtime.gaps is None:
        return
    store = runtime.graph_store

    async def name_of(project_id: str) -> str | None:
        async with runtime.transaction() as session:
            return await gaps_db.project_name(session, uuid.UUID(project_id))

    async def plan_of(project_id: str) -> LoadedPlan:
        return await load_plan(as_executor(store), project_id)

    poster = LinearGapPoster(runtime, name_of=name_of, plan_of=plan_of)
    runtime.linear_gaps = LinearGaps(runtime, poster, LinearAnswers(runtime))
    runtime.gaps.add_notice(runtime.linear_gaps)
    runtime.linear_gaps.start()
    stack.push_async_callback(runtime.linear_gaps.aclose)
