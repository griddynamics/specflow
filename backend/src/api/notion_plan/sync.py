"""One Notion plan pass of a project (docs/notion/plan.md "Pass"). Stream E1 fills ``push_once``.

The plan's stories and tasks become rows of one database "Graphico plan - <project>" created under the target's parent
page on the first pass (N11); the desired set is ``api.jira.desired.build`` with a placeholder epic (N7). A row's
owned fields are overwritten only while its Status is in the ``to_do`` group, otherwise one comment lists the changes;
a row that leaves the plan gets ``Removed`` checked and a comment, never a delete (N8). A pass writes at most
``MAX_ROWS_PER_PASS`` rows; when rows are left it calls ``start(project_id, "plan_run")`` on its runner before it ends
so one more pass follows (N9, no change to ``api.exports.runner``). Notion calls never run inside a transaction; every
row write checks that the destination is unchanged. A crash between a create and its row is repaired by a query on
``Graphico ID``.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from api.jira.desired import SpecRef
from api.notion_plan.store import PlanStore
from graph.plan.reader import LoadedPlan
from notion.gateway import NotionGateway

NOT_CONFIGURED: Final = "notion_plan_not_configured"
NO_PLAN, PLAN_APPLYING = "no_plan", "plan_applying"
TARGET_CHANGED, UNEXPECTED_ERROR = "target_changed", "unexpected_error"
MAX_ROWS_PER_PASS: Final = 120  # rows written (created, updated, commented) by one pass; the rest follow in a new pass


@dataclass
class PushCounts:
    created: int = 0
    updated: int = 0
    commented: int = 0
    removed: int = 0
    unchanged: int = 0
    failed: int = 0
    deferred: int = 0  # rows left for the next pass


@dataclass(frozen=True)
class PushResult:
    status: str
    error_code: str | None
    counts: PushCounts


@dataclass(frozen=True)
class PushDeps:
    """What a pass needs from its host; ``gateway_for`` is the gateway of the member the project's Notion is
    connected by NOW, read for every pass (a refusal is a ``NotionError`` and ends the pass ``failed`` with no Notion
    call); ``restart`` asks the runner for one more pass (``NotionPlanRunner.start`` with ``plan_run``)."""

    store: PlanStore
    gateway_for: Callable[[uuid.UUID], Awaitable[NotionGateway]]
    load_plan: Callable[[str], Awaitable[LoadedPlan]]
    spec_names: Callable[[str, Sequence[str]], Awaitable[Mapping[str, SpecRef]]]
    restart: Callable[[str], Awaitable[None]]
    now: Callable[[], datetime]


async def push_once(project_id: str, deps: PushDeps) -> PushResult:
    """One pass of the project."""

    raise NotImplementedError("E1 fills push_once")
