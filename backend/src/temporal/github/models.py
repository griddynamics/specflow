"""Input and names of the clone check (docs/github/clone.md).

Workflow-sandbox safe (pydantic, uuid, typing only). Ids only: never a token, a repository name or a GitHub text
(history rule).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final
from uuid import UUID

from pydantic import BaseModel, Field

WORKFLOW_NAME: Final = "GithubSnapshotWorkflow"
#: The clone (10 minutes at most), the upload and the retries fit well inside.
EXECUTION_TIMEOUT: Final = timedelta(minutes=45)


class GitHubSnapshotInput(BaseModel):
    project_id: UUID
    repository_id: UUID
    #: Workflow-id prefix of tier-2 tests (empty in production).
    id_prefix: str = Field(default="", max_length=64)


class GitHubSnapshotResult(BaseModel):
    """How a clone check ended: ``code`` is ``None`` on success, else a closed failure code
    (``github.codes.CLONE_FAILURE_CODES``); ``ended`` is false when the repository was gone."""

    code: str | None = None
    ended: bool = True


class GitHubSnapshotFailInput(BaseModel):
    """Mark the repository ``failed`` with a closed ``code`` (the workflow's last resort)."""

    project_id: UUID
    repository_id: UUID
    code: str = Field(max_length=64)
