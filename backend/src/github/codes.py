"""The closed codes of the GitHub integration (docs/github/oauth.md "Codes"). ``str(error)`` is the code: no GitHub
text, token, code, ``state`` or login is ever inside an error."""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class GitHubCode(StrEnum):
    NOT_CONNECTED = "github_not_connected"
    RECONNECT_REQUIRED = "github_reconnect_required"
    STATE_INVALID = "github_state_invalid"
    CODE_INVALID = "github_code_invalid"
    UNREACHABLE = "github_unreachable"
    AUTH_FAILED = "github_auth_failed"  # GitHub refused the app's own credentials or a fresh token (a setup problem)
    REPO_NOT_ACCESSIBLE = "github_repo_not_accessible"
    REPO_TOO_LARGE = "github_repo_too_large"
    CLONE_FAILED = "github_clone_failed"
    CLONE_TIMEOUT = "github_clone_timeout"
    STORAGE_FAILED = "storage_failed"
    # Inside only: a dead refresh token (``bad_refresh_token``, a rotated key) becomes RECONNECT_REQUIRED; any other
    # refusal of the token endpoint becomes CODE_INVALID (sign-in) or AUTH_FAILED (refresh).
    GRANT_INVALID = "github_grant_invalid"
    REFUSED = "github_refused"


# Why a clone check failed (``project_repositories.failure_code``); a test keeps ``db.models`` equal to it.
CLONE_FAILURE_CODES: Final = (
    GitHubCode.NOT_CONNECTED.value,
    GitHubCode.RECONNECT_REQUIRED.value,
    GitHubCode.REPO_NOT_ACCESSIBLE.value,
    GitHubCode.REPO_TOO_LARGE.value,
    GitHubCode.UNREACHABLE.value,
    GitHubCode.AUTH_FAILED.value,
    GitHubCode.CLONE_FAILED.value,
    GitHubCode.CLONE_TIMEOUT.value,
    GitHubCode.STORAGE_FAILED.value,
)


class GitHubError(Exception):
    """A GitHub failure as one closed code; ``str(e)`` is the code."""

    def __init__(self, code: GitHubCode) -> None:
        super().__init__(code.value)
        self.code = code
