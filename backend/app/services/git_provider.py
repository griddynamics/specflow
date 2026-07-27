"""
Minimal git-host provider abstraction.

A deployment is either all-GitHub or all-BitBucket, never mixed — the active
provider is resolved once, globally, from configured tokens (or an explicit
override). See docs/plans/workspace-repos/bitbucket-pr1-commit-and-read.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from app.core.config import Settings


class GitProvider(str, Enum):
    GITHUB = "github"
    BITBUCKET_CLOUD = "bitbucket_cloud"


class GitProviderResolutionError(Exception):
    """Raised when the active git provider cannot be determined unambiguously."""


@dataclass(frozen=True)
class GitHostStrategy:
    provider: GitProvider
    default_git_user: str
    sanitization_patterns: tuple[tuple[re.Pattern[str], str], ...]


_GITHUB = GitHostStrategy(
    provider=GitProvider.GITHUB,
    default_git_user="x-access-token",
    sanitization_patterns=(
        (re.compile(r"(https?://[^:]+:)[^@]+(@github\.com)"), r"\1[REDACTED]\2"),
        (re.compile(r"gh[ps]_[A-Za-z0-9_]+"), "[REDACTED]"),
        (re.compile(r"github_pat_[A-Za-z0-9_]+"), "[REDACTED]"),
    ),
)

_BITBUCKET = GitHostStrategy(
    provider=GitProvider.BITBUCKET_CLOUD,
    default_git_user="x-token-auth",
    sanitization_patterns=(
        (re.compile(r"(https?://[^:]+:)[^@]+(@bitbucket\.org)"), r"\1[REDACTED]\2"),
        (re.compile(r"ATCTT[A-Za-z0-9_=\-]+"), "[REDACTED]"),
    ),
)

_STRATEGIES: dict[GitProvider, GitHostStrategy] = {
    GitProvider.GITHUB: _GITHUB,
    GitProvider.BITBUCKET_CLOUD: _BITBUCKET,
}


def strategy_for(provider: GitProvider) -> GitHostStrategy:
    return _STRATEGIES[provider]


def repository_url(provider: GitProvider, owner: str, repo_name: str) -> str:
    """Build the canonical HTTPS URL for a repository on the selected provider."""
    host = "bitbucket.org" if provider is GitProvider.BITBUCKET_CLOUD else "github.com"
    return f"https://{host}/{owner}/{repo_name}"


def all_strategies() -> list[GitHostStrategy]:
    return list(_STRATEGIES.values())


def resolve_active_git_provider_from_flags(
    *,
    override: Optional[str],
    has_github_token: bool,
    has_bitbucket_token: bool,
) -> GitProvider:
    """Pure resolution logic shared by settings-based and Kubernetes-secret-map loading."""
    explicit = (override or "").strip().lower()
    if explicit:
        try:
            return GitProvider(explicit)
        except ValueError as e:
            valid = [p.value for p in GitProvider]
            raise GitProviderResolutionError(
                f"GIT_PROVIDER={explicit!r} is not valid; expected one of {valid}"
            ) from e

    if has_github_token and has_bitbucket_token:
        raise GitProviderResolutionError(
            "Both a GitHub and a BitBucket token are configured; set GIT_PROVIDER "
            "explicitly to choose the active git host (mixing providers is not supported)"
        )
    if has_github_token:
        return GitProvider.GITHUB
    if has_bitbucket_token:
        return GitProvider.BITBUCKET_CLOUD

    raise GitProviderResolutionError(
        "No git provider configured; set GITHUB_TOKEN or BITBUCKET_TOKEN "
        "(and GIT_PROVIDER if both might be set)"
    )


def resolve_active_git_provider(settings: "Settings") -> GitProvider:
    return resolve_active_git_provider_from_flags(
        override=settings.GIT_PROVIDER,
        has_github_token=bool(settings.GITHUB_TOKEN_DEFAULT),
        has_bitbucket_token=bool(settings.BITBUCKET_TOKEN_DEFAULT),
    )
