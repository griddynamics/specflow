"""GitHub repository provisioning for workspace-pool slots.

The one place that creates workspace repositories. Extracted from
``scripts/create_generation_session_repos.py`` so both the bootstrap script and the
pool-expansion endpoint drive the same client — a second implementation would drift on the
details that matter here (owner-type detection, ``auto_init``, team grants).

Scope is deliberately narrow: create and inspect repositories. Credentials for *git*
operations live in :mod:`app.services.github_auth`; this module takes a token as an argument
and never resolves one itself.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

GITHUB_API_BASE_URL = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"

# Spacing between repo creations, to stay clear of GitHub's secondary rate limits.
DEFAULT_CREATE_DELAY_SECONDS = 0.1


class GitHubProvisioningError(Exception):
    """Raised when a repository could not be created or granted."""


@dataclass(frozen=True)
class ProvisionedRepo:
    """One repository that is ready to be used as a workspace."""

    name: str
    full_name: str
    html_url: str
    already_existed: bool


class GitHubAPIClient:
    """Client for GitHub repository operations (organization or personal account).

    ``owner`` may be either an organization login or a personal account login; the correct
    creation endpoint is resolved once from the account type and cached, because
    ``POST /orgs/{org}/repos`` fails for personal accounts and vice versa.
    """

    def __init__(self, token: str, owner: str, *, client: Optional[httpx.AsyncClient] = None):
        self.token = token
        self.owner = owner
        self.base_url = GITHUB_API_BASE_URL
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        }
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(timeout=30.0)
        self._is_user_account: Optional[bool] = None

    # ``org`` kept as an alias so the existing script keeps reading naturally.
    @property
    def org(self) -> str:
        return self.owner

    async def get_authenticated_user(self) -> Dict[str, Any]:
        """The token's own account — used to resolve a git author name when unset."""
        response = await self.client.get(f"{self.base_url}/user", headers=self.headers)
        response.raise_for_status()
        return response.json()

    async def _resolve_owner_type(self) -> bool:
        """True when ``owner`` is a personal account rather than an organization."""
        if self._is_user_account is None:
            response = await self.client.get(
                f"{self.base_url}/users/{self.owner}", headers=self.headers
            )
            response.raise_for_status()
            self._is_user_account = response.json().get("type") == "User"
        return bool(self._is_user_account)

    async def create_repository(self, repo_name: str) -> Dict[str, Any]:
        """Create a private, initialized repository under ``owner``.

        ``auto_init=True`` is required, not cosmetic: the pool clones every ``repo_url``, and
        an empty repository has no default branch to clone.
        """
        is_user = await self._resolve_owner_type()
        url = (
            f"{self.base_url}/user/repos"
            if is_user
            else f"{self.base_url}/orgs/{self.owner}/repos"
        )
        data = {
            "name": repo_name,
            "private": True,
            "auto_init": True,
            "description": f"Generation workspace repository: {repo_name}",
        }
        response = await self.client.post(url, json=data, headers=self.headers)
        response.raise_for_status()
        return response.json()

    async def repository_exists(self, repo_name: str) -> bool:
        """Whether ``owner/repo_name`` already exists. Makes creation idempotent."""
        response = await self.client.get(
            f"{self.base_url}/repos/{self.owner}/{repo_name}", headers=self.headers
        )
        return response.status_code == 200

    async def add_team_repository_write(self, team_slug: str, repo_name: str) -> None:
        """Grant a team push access. Idempotent — GitHub treats a repeat PUT as a no-op."""
        url = (
            f"{self.base_url}/orgs/{self.owner}/teams/{team_slug}/repos/"
            f"{self.owner}/{repo_name}"
        )
        response = await self.client.put(url, json={"permission": "push"}, headers=self.headers)
        response.raise_for_status()

    async def close(self) -> None:
        """Close the HTTP client, unless one was injected by the caller."""
        if self._owns_client:
            await self.client.aclose()


async def provision_repositories(
    github_client: GitHubAPIClient,
    repo_names: List[str],
    team_slug: Optional[str] = None,
    delay: float = DEFAULT_CREATE_DELAY_SECONDS,
    on_progress: Optional[Callable[[str], Any]] = None,
) -> List[ProvisionedRepo]:
    """Ensure every name in ``repo_names`` exists, creating what is missing.

    Idempotent by design: an existing repository is reported with
    ``already_existed=True`` rather than being an error, so a re-run after a partial failure
    completes the remainder instead of aborting. Team grants are re-applied either way.

    Raises:
        GitHubProvisioningError: creation or the team grant failed for some repository. Names
            processed before the failure are already created; re-running finishes the job.
    """
    provisioned: List[ProvisionedRepo] = []
    owner = github_client.owner

    def report(message: str) -> None:
        logger.info(message)
        if on_progress is not None:
            on_progress(message)

    for index, repo_name in enumerate(repo_names):
        try:
            if await github_client.repository_exists(repo_name):
                report(f"Repository {owner}/{repo_name} already exists — reusing it")
                provisioned.append(
                    ProvisionedRepo(
                        name=repo_name,
                        full_name=f"{owner}/{repo_name}",
                        html_url=f"https://github.com/{owner}/{repo_name}",
                        already_existed=True,
                    )
                )
            else:
                report(f"Creating repository {owner}/{repo_name}")
                data = await github_client.create_repository(repo_name)
                provisioned.append(
                    ProvisionedRepo(
                        name=data.get("name") or repo_name,
                        full_name=data.get("full_name") or f"{owner}/{repo_name}",
                        html_url=data.get("html_url")
                        or f"https://github.com/{owner}/{repo_name}",
                        already_existed=False,
                    )
                )

            if team_slug:
                await github_client.add_team_repository_write(team_slug, repo_name)
                report(f"Granted team {team_slug} write access on {repo_name}")
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text if exc.response is not None else str(exc)
            raise GitHubProvisioningError(
                f"GitHub rejected provisioning of {owner}/{repo_name}: {detail}"
            ) from exc

        if delay and index < len(repo_names) - 1:
            await asyncio.sleep(delay)

    return provisioned
