"""One Jira issue as a document (shared by the Jira epic importer and the Confluence "follow links"): the issue with all
its comments as Markdown, its original as JSON. An issue the member cannot read is skipped and counted, not failed."""

from __future__ import annotations

from typing import Any

from api.imports.atlassian import NO_ACCESS, READ_STEP, SKIPPABLE
from api.imports.writer import ImportWriter
from jira.gateway import JiraError
from jira.issue_document import issue_markdown, issue_original, issue_title
from jira.issues import JiraIssueReader


async def import_issue(
    writer: ImportWriter, jira: JiraIssueReader, key: str, issue: dict[str, Any] | None = None
) -> None:
    """Store issue ``key`` (read now, or the ``issue`` the caller already has) with its comments as ``issue:<KEY>``; an
    issue Jira says is missing or forbidden is one item in error (``no_access``, step ``read``). Any other refusal
    raises ``JiraError`` for the importer to turn into the failure of the source."""

    external_id = f"issue:{key}"
    try:
        found = issue if issue is not None else await jira.issue(key)
        comments = await jira.comments(found)
    except JiraError as error:
        if error.reason not in SKIPPABLE:
            raise
        await writer.fail(external_id, key, reason=NO_ACCESS, step=READ_STEP)
        return
    await writer.add(
        external_id,
        name=issue_title(found),
        original=issue_original(found, comments),
        markdown=issue_markdown(found, comments),
    )
