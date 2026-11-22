"""The access check of ``docs/jira-sync/api.md#access-check``: what the typed Jira target needs, asked of Jira in order.

The first failure ends the check. A Jira answer is never kept: the reason is one of the closed codes and ``field``
names the input to fix; the project and Epic names go only to the ``JiraAccessOk`` the owner sees.
"""

from __future__ import annotations

from typing import Final

from api.schemas import JiraAccessFailed, JiraAccessField, JiraAccessOk
from jira.gateway import JiraError, JiraGateway, JiraProject
from jira.reasons import Reason

EPIC_TYPE: Final = "Epic"
_OF_INPUT: Final = frozenset({Reason.NOT_FOUND, Reason.FORBIDDEN})  # failures that name the input to fix
_KNOWN: Final = _OF_INPUT | {Reason.AUTH_FAILED, Reason.UNREACHABLE}


def _failed(reason: Reason, field: JiraAccessField | None) -> JiraAccessFailed:
    return JiraAccessFailed.model_validate({"reason": reason.value, "field": field})


def _from_error(error: JiraError, field: JiraAccessField) -> JiraAccessFailed:
    """A Jira failure of one step: a code of the table (else ``jira_unexpected``); only a rejected input has a field."""

    reason = error.reason if error.reason in _KNOWN else Reason.UNEXPECTED
    return _failed(reason, field if reason in _OF_INPUT else None)


async def _project_problem(gateway: JiraGateway, key: str) -> JiraAccessFailed | JiraProject:
    """The project, or why it cannot be used: authentication, existence, issue types, permissions."""

    try:
        await gateway.myself()
        project = await gateway.project(key)
        if None in (project.epic_type_id, project.story_type_id, project.subtask_type_id):
            return _failed(Reason.ISSUE_TYPES_MISSING, "jira_project")
        if await gateway.missing_permissions(key):
            return _failed(Reason.FORBIDDEN, "jira_project")
    except JiraError as error:
        return _from_error(error, "jira_project")
    return project


async def _epic_summary(gateway: JiraGateway, project_key: str, epic_key: str) -> JiraAccessFailed | str:
    try:
        epic = await gateway.issue_summary(epic_key)
    except JiraError as error:
        return _from_error(error, "epic_key")
    if epic.project_key != project_key or epic.type_name != EPIC_TYPE:
        return _failed(Reason.EPIC_INVALID, "epic_key")
    return epic.summary


async def check_access(
    gateway: JiraGateway, jira_project: str, epic_key: str | None
) -> JiraAccessOk | JiraAccessFailed:
    """Authenticate, read the project, its issue types and the permissions, then the Epic when one is given."""

    project = await _project_problem(gateway, jira_project)
    if isinstance(project, JiraAccessFailed):
        return project
    if epic_key is None:
        return JiraAccessOk(project_name=project.name, epic_summary=None)
    summary = await _epic_summary(gateway, jira_project, epic_key)
    if isinstance(summary, JiraAccessFailed):
        return summary
    return JiraAccessOk(project_name=project.name, epic_summary=summary)
