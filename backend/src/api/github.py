"""Shared parts of the GitHub routes (docs/github/api.md): the service of the runtime and the API errors of the
GitHub codes. Messages are fixed text; no GitHub text, token, code, ``state`` or login reaches an error."""

from __future__ import annotations

from typing import Final

from api.errors import ApiError
from api.runtime import Runtime
from github.codes import GitHubCode, GitHubError
from github.connections import GitHubConnections

_ERRORS: Final[dict[GitHubCode, tuple[int, str]]] = {
    GitHubCode.NOT_CONNECTED: (409, "Connect your GitHub account first."),
    GitHubCode.RECONNECT_REQUIRED: (409, "Your GitHub connection expired. Connect GitHub again."),
    GitHubCode.STATE_INVALID: (400, "The sign-in to GitHub expired or belongs to another session. Try again."),
    GitHubCode.CODE_INVALID: (400, "GitHub did not accept the sign-in. Try again."),
    GitHubCode.UNREACHABLE: (502, "GitHub could not be reached. Try again later."),
    GitHubCode.AUTH_FAILED: (502, "GitHub refused the request of this server."),
    GitHubCode.REPO_NOT_ACCESSIBLE: (404, "Your GitHub account cannot read this repository through the app."),
    GitHubCode.REPO_TOO_LARGE: (422, "The repository is larger than 1 GiB."),
    GitHubCode.PERMISSIONS_MISSING: (
        409,
        "The GitHub App has not been approved for issues and organization projects in this organization yet.",
    ),
    GitHubCode.PROJECT_NOT_FOUND: (404, "The organization project does not exist or your account cannot see it."),
    GitHubCode.NOT_AN_ORGANIZATION: (422, "The repository belongs to a user, not to an organization."),
}


def github_unavailable() -> ApiError:
    return ApiError(503, "github_unavailable", "The GitHub integration is not configured on this server.")


def repository_not_found() -> ApiError:
    return ApiError(404, "repository_not_found", "The repository is not in this project.")


def repository_exists() -> ApiError:
    return ApiError(409, "repository_exists", "The project already has this repository.")


def github_error(error: GitHubError) -> ApiError:
    """The API error of a GitHub failure; a code without its own answer is ``github_auth_failed``."""

    code = error.code if error.code in _ERRORS else GitHubCode.AUTH_FAILED
    status, message = _ERRORS[code]
    return ApiError(status, code.value, message)


def repository_read_only() -> ApiError:
    return ApiError(422, "github_repo_read_only", "Your GitHub account cannot create issues in this repository.")


def github_connections(runtime: Runtime) -> GitHubConnections:
    """The runtime's GitHub connections; ``503 github_unavailable`` when the integration is off."""

    connections: GitHubConnections | None = runtime.github
    if connections is None:
        raise github_unavailable()
    return connections
