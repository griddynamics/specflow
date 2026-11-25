"""What the Jira sync runner exposes: the trigger names, the plan runner's hook and the pure status builders.

The read rules are docs/jira-sync/sync.md#triggers-and-runs: a live pass reads ``running``; a stored ``running`` with no
live pass reads ``failed`` / ``interrupted`` (``skipped`` / ``jira_not_configured`` when the target is disabled).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Literal, Protocol, cast

from agent_contracts.jira_models import JiraItemKind, JiraSyncCounts, JiraSyncItem, JiraSyncStatus
from api.exports.status import run_state
from api.jira.sync import NOT_CONFIGURED
from db.jira import JiraTargetView
from db.models import JIRA_ISSUE_KINDS, JiraIssue

Trigger = Literal["plan_run", "mcp", "settings", "recovery"]


class JiraHooks(Protocol):
    """The call the plan runner makes after a returned plan run."""

    async def after_plan(self, project_id: str) -> None: ...


def status_of(target: JiraTargetView, *, live: bool, queued: bool, created_epic: str | None = None) -> JiraSyncStatus:
    """The sync status of a target. ``live``: a pass of the project is queued or runs in this process;
    ``queued``: another pass follows it; ``created_epic``: the key of the Epic Specflow created, if any."""

    base: dict[str, Any] = {
        "project_id": str(target.project_id),
        "enabled": target.enabled,
        "queued": live and queued,
        "jira_project": target.jira_project,
        "epic_key": target.epic_key or created_epic,
        "trigger": target.last_trigger,
        "started_at": target.last_started_at,
        "finished_at": None,
        "error_code": None,
        "counts": None,
    }
    run = run_state(target, live=live, not_configured=NOT_CONFIGURED)
    counts = JiraSyncCounts(**run.counts) if run.counts else None
    return JiraSyncStatus(
        **{**base, "status": run.status, "finished_at": run.finished_at, "error_code": run.error_code, "counts": counts}
    )


def items_of(rows: Iterable[JiraIssue], site_url: str) -> list[JiraSyncItem]:
    """The map rows as items, ordered by kind (epic, story, subtask) then node id."""

    order = {kind: index for index, kind in enumerate(JIRA_ISSUE_KINDS)}
    items = [
        JiraSyncItem(
            node_id=row.node_id,
            kind=cast(JiraItemKind, row.kind),
            jira_key=row.jira_key,
            url=f"{site_url}/browse/{row.jira_key}" if row.jira_key else None,
            removed=row.removed_at is not None,
            error_code=row.error_code,
        )
        for row in rows
    ]
    return sorted(items, key=lambda item: (order[item.kind], item.node_id))
