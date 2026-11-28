"""The caller's GitHub connection (docs/github/api.md "Connection routes").

The web app's route handlers forward GitHub's ``code`` and ``state`` (callback) or ``state`` (setup) here; ``state``
binds each to the user who asked for the URL and to its purpose. The ``installation_id`` of the setup redirect is never
read. Events carry the user id and a code only.
"""

from __future__ import annotations

from fastapi import APIRouter

from api.deps import RuntimeDep, UserDep
from api.github import github_connections, github_error
from api.runtime import Runtime
from api.schemas import (
    GitHubCallbackIn,
    GitHubCallbackOut,
    GitHubConnectionOut,
    GitHubRepositoriesOut,
    GitHubRepositoryOut,
    GitHubReturnToIn,
    GitHubSetupIn,
    GitHubSetupOut,
    GitHubUrlOut,
)
from github.client import MANAGE_URL
from github.codes import GitHubCode, GitHubError
from github.connections import ConnectionView
from logger.logger import INFO, get_logger, log_event

logger = get_logger("api.routers.github")
router = APIRouter(prefix="/integrations/github", tags=["github"])


def _out(runtime: Runtime, view: ConnectionView | None) -> GitHubConnectionOut:
    enabled = runtime.github is not None
    return GitHubConnectionOut(
        enabled=enabled,
        connected=view is not None,
        status=view.status if view is not None else None,
        login=view.login if view is not None else None,
        connected_at=view.connected_at if view is not None else None,
        last_used_at=view.last_used_at if view is not None else None,
        manage_url=MANAGE_URL if enabled else None,
    )


def _refused(user_id: object, error: GitHubError, step: str) -> GitHubError:
    log_event(logger, INFO, "api.github.refused", user_id=str(user_id), step=step, code=error.code.value)
    return error


@router.get("", response_model=GitHubConnectionOut)
async def get_github_connection(runtime: RuntimeDep, user: UserDep) -> GitHubConnectionOut:
    view = await runtime.github.get(user.id) if runtime.github is not None else None
    return _out(runtime, view)


@router.post("/authorize", response_model=GitHubUrlOut)
async def authorize_github(body: GitHubReturnToIn, runtime: RuntimeDep, user: UserDep) -> GitHubUrlOut:
    """GitHub's sign-in page of the app; its ``state`` holds the user, ``return_to`` and a 10-minute expiry."""

    return GitHubUrlOut(url=github_connections(runtime).authorize_url(user.id, body.return_to))


@router.post("/callback", response_model=GitHubCallbackOut)
async def github_callback(body: GitHubCallbackIn, runtime: RuntimeDep, user: UserDep) -> GitHubCallbackOut:
    """Check ``state``, exchange ``code``, read the account and keep the connection."""

    connections = github_connections(runtime)
    try:
        view, return_to = await connections.connect(user.id, code=body.code, state=body.state)
    except GitHubError as error:
        raise github_error(_refused(user.id, error, "callback")) from None
    return GitHubCallbackOut(connection=_out(runtime, view), return_to=return_to)


@router.post("/install", response_model=GitHubUrlOut)
async def install_github(body: GitHubReturnToIn, runtime: RuntimeDep, user: UserDep) -> GitHubUrlOut:
    """The app's install page, where the user picks the repositories; GitHub hands ``state`` to the setup URL."""

    return GitHubUrlOut(url=github_connections(runtime).install_url(user.id, body.return_to))


@router.post("/setup", response_model=GitHubSetupOut)
async def github_setup(body: GitHubSetupIn, runtime: RuntimeDep, user: UserDep) -> GitHubSetupOut:
    """The ``return_to`` of an install ``state`` of the caller (the setup redirect after an install or update)."""

    try:
        return GitHubSetupOut(return_to=github_connections(runtime).check_setup(user.id, body.state))
    except GitHubError as error:
        raise github_error(_refused(user.id, error, "setup")) from None


@router.get("/repositories", response_model=GitHubRepositoriesOut)
async def list_github_repositories(runtime: RuntimeDep, user: UserDep) -> GitHubRepositoriesOut:
    """The repositories the caller can read through the app (at most 1000)."""

    try:
        repositories, truncated = await github_connections(runtime).repositories(user.id)
    except GitHubError as error:
        if error.code is GitHubCode.REPO_NOT_ACCESSIBLE:  # a list has no single repository to miss
            error = GitHubError(GitHubCode.AUTH_FAILED)
        raise github_error(_refused(user.id, error, "repositories")) from None
    return GitHubRepositoriesOut(
        repositories=[
            GitHubRepositoryOut(
                github_repo_id=r.id,
                full_name=r.full_name,
                html_url=r.html_url,
                private=r.private,
                default_branch=r.default_branch,
                size_kb=r.size_kb,
                too_large=r.too_large,
            )
            for r in repositories
        ],
        truncated=truncated,
    )
