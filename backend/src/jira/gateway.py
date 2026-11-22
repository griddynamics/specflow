"""The Jira gateway contract. Every method is async.

No error carries Jira's text, a URL, a header or the token: ``str(e)`` is the reason value.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from jira.reasons import Reason


class JiraError(Exception):
    """A Jira failure as one closed reason; ``str(e)`` is the reason value."""

    def __init__(self, reason: Reason, *, http_status: int | None = None) -> None:
        super().__init__(reason.value)
        self.reason = reason
        self.http_status = http_status


@dataclass(frozen=True)
class JiraProject:
    """The project name and the ids of its Epic, Story and sub-task types (``None`` when it lacks one)."""

    name: str
    epic_type_id: str | None
    story_type_id: str | None
    subtask_type_id: str | None


@dataclass(frozen=True)
class IssueSummary:
    project_key: str
    type_name: str
    summary: str


@dataclass(frozen=True)
class IssueState:
    """The status category key (``new`` = To Do), the status name and ``blocked_by``: prerequisite key -> link id."""

    category: str
    status: str
    blocked_by: Mapping[str, str] = field(default_factory=dict)


class JiraGateway(Protocol):
    async def myself(self) -> str:
        """The account id the credentials belong to; JiraError(jira_auth_failed) when refused."""

    async def project(self, key: str) -> JiraProject:
        """jira_not_found | jira_forbidden."""

    async def missing_permissions(self, key: str) -> frozenset[str]:
        """The checked permissions the account lacks in project ``key``."""

    async def issue_summary(self, key: str) -> IssueSummary:
        """Project key, issue type name and summary of one issue."""

    async def story_points_field(self) -> str | None:
        """The id of the custom field named ``Story Points`` (cached per instance); ``None`` when absent."""

    async def create_issue(self, fields: Mapping[str, Any], *, project_id: str, node_id: str) -> str:
        """Create an issue holding the entity property ``specflow``; return its key."""

    async def edit_issue(
        self,
        key: str,
        *,
        fields: Mapping[str, Any],
        add_labels: Sequence[str] = (),
        remove_labels: Sequence[str] = (),
    ) -> None:
        """Overwrite ``fields`` and apply label operations."""

    async def issue_states(self, keys: Sequence[str]) -> dict[str, IssueState | None]:
        """Status and Blocks links per key; ``None`` = the issue no longer exists in Jira."""

    async def find_created(self, project_key: str, *, project_id: str, minutes: int) -> dict[str, str]:
        """node id -> key of the issues created in the last ``minutes`` whose property names ``project_id``."""

    async def add_comment(self, key: str, doc: Mapping[str, Any]) -> None:
        """Post one ADF comment."""

    async def add_blocks_link(self, *, prerequisite: str, waiter: str) -> None:
        """``prerequisite`` blocks ``waiter``."""

    async def delete_link(self, link_id: str) -> None:
        """Delete one issue link."""

    async def rank(self, keys: Sequence[str], *, before: str | None = None, after: str | None = None) -> None:
        """Move up to 50 issues before or after another issue (agile)."""

    async def upsert_remote_link(self, key: str, *, global_id: str, url: str, title: str) -> None:
        """Create or replace the remote link with ``global_id``."""

    async def aclose(self) -> None:
        """Release the HTTP client."""
