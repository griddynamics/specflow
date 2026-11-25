"""The Jira epic importer (docs/sources/importers.md): the epic and every issue under it, each with its comments, as
documents.

Runs in the API process as the member the project's Atlassian is connected by. The issues are found by JQL
(``parent = <epic>``, paged); an issue that member cannot see is never returned by Jira, and one that vanishes or is
refused while it is read is skipped and counted as an item in error. At most ``MAX_EPIC_ISSUES`` issues are taken (the
cap is logged as a count). Text lives in documents only: events carry ids, the kind and counts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from api.imports.atlassian import NO_ACCESS, Connections, access_of, import_failure, jira_reader
from api.imports.issues import import_issue
from api.imports.model import ImportFailedError, ImportJob
from api.imports.writer import ImportWriter
from jira.gateway import JiraError
from jira.issue_document import issue_title
from jira.issues import JiraIssueReader
from logger.logger import WARNING, get_logger, log_event

logger = get_logger("api.imports.jira_epic")

MAX_EPIC_ISSUES: Final = 200  # issues under the epic taken by one import (the epic itself is one more)
EPIC_LEVEL: Final = 1  # ``issuetype.hierarchyLevel`` of an epic


@dataclass(frozen=True)
class JiraEpicImport:
    """What to import: the epic with this key, and every issue under it whose key is not in ``exclude``."""

    key: str
    exclude: frozenset[str] = frozenset()


@dataclass(frozen=True)
class EpicChild:
    """One issue under the epic, as the check lists it."""

    key: str
    summary: str
    type: str
    status: str


@dataclass(frozen=True)
class EpicInspection:
    """What a key would import, for the check: the epic's summary and how many issues it has for the member."""

    key: str
    summary: str
    issues: int
    is_epic: bool
    children: tuple[EpicChild, ...] = ()
    capped: bool = False


def is_epic(issue: dict[str, Any]) -> bool:
    kind = (issue.get("fields") or {}).get("issuetype") or {}
    return kind.get("hierarchyLevel") == EPIC_LEVEL or kind.get("name") == "Epic"


async def inspect_epic(jira: JiraIssueReader, key: str) -> EpicInspection:
    """Read the epic and count its issues (``JiraError`` when a read is refused); a key that is not an epic is reported
    as such without counting."""

    epic = await jira.issue(key)
    summary = str((epic.get("fields") or {}).get("summary") or "")
    if not is_epic(epic):
        return EpicInspection(key, summary, 0, is_epic=False)
    found, capped = await jira.epic_children(key, MAX_EPIC_ISSUES)
    children = tuple(_child(issue) for issue in found)
    return EpicInspection(
        key, summary, await jira.count_epic_issues(key), is_epic=True, children=children, capped=capped
    )


def _child(issue: dict[str, Any]) -> EpicChild:
    fields = issue.get("fields") or {}
    return EpicChild(
        str(issue.get("key", "")),
        str(fields.get("summary") or ""),
        str((fields.get("issuetype") or {}).get("name") or ""),
        str((fields.get("status") or {}).get("name") or ""),
    )


class JiraEpicImporter:
    """An ``Importer`` (``api.imports.runner``) of one Jira epic."""

    def __init__(self, connections: Connections, source: JiraEpicImport) -> None:
        self._connections = connections
        self._source = source

    async def __call__(self, job: ImportJob, writer: ImportWriter) -> None:
        access = await access_of(self._connections, job.user_id)
        if access is None:
            raise ImportFailedError(NO_ACCESS)
        try:
            async with jira_reader(access) as jira:
                epic = await jira.issue(self._source.key)
                issues, capped = await jira.epic_issues(self._source.key, MAX_EPIC_ISSUES)
                if capped:
                    log_event(
                        logger,
                        WARNING,
                        "api.imports.epic_cap_reached",
                        project_id=str(job.project_id),
                        cap=MAX_EPIC_ISSUES,
                    )
                issues = [issue for issue in issues if issue["key"] not in self._source.exclude]
                for issue in (epic, *issues):
                    await writer.declare(f"issue:{issue['key']}", issue_title(issue))
                for issue in (epic, *issues):
                    await import_issue(writer, jira, str(issue["key"]), issue)
        except JiraError as error:
            raise import_failure(error) from None
