"""Tests for the git-provider abstraction: resolution, sanitization, strategies."""

import pytest

from app.core.config import Settings
from app.services.git_provider import (
    GitProvider,
    GitProviderResolutionError,
    all_strategies,
    resolve_active_git_provider,
    resolve_active_git_provider_from_flags,
    strategy_for,
)


class TestResolveActiveGitProviderFromSettings:
    def test_github_only_settings_resolves_github(self):
        settings = Settings(GITHUB_TOKEN="gh-token", BITBUCKET_TOKEN=None, GIT_PROVIDER=None)
        assert resolve_active_git_provider(settings) == GitProvider.GITHUB

    def test_bitbucket_only_settings_resolves_bitbucket(self):
        settings = Settings(GITHUB_TOKEN=None, BITBUCKET_TOKEN="bb-token", GIT_PROVIDER=None)
        assert resolve_active_git_provider(settings) == GitProvider.BITBUCKET_CLOUD

    def test_github_only_deployment_is_byte_identical_default(self):
        """A GitHub-only deployment must resolve exactly as it did before BitBucket existed."""
        settings = Settings(GITHUB_TOKEN="gh-token", BITBUCKET_TOKEN=None, GIT_PROVIDER=None)
        assert resolve_active_git_provider(settings) == GitProvider.GITHUB


class TestResolveActiveGitProvider:
    def test_only_github_token_resolves_github(self):
        provider = resolve_active_git_provider_from_flags(
            override=None, has_github_token=True, has_bitbucket_token=False
        )
        assert provider == GitProvider.GITHUB

    def test_only_bitbucket_token_resolves_bitbucket(self):
        provider = resolve_active_git_provider_from_flags(
            override=None, has_github_token=False, has_bitbucket_token=True
        )
        assert provider == GitProvider.BITBUCKET_CLOUD

    def test_both_tokens_without_override_raises(self):
        with pytest.raises(GitProviderResolutionError, match="Both"):
            resolve_active_git_provider_from_flags(
                override=None, has_github_token=True, has_bitbucket_token=True
            )

    def test_neither_token_raises(self):
        with pytest.raises(GitProviderResolutionError, match="No git provider"):
            resolve_active_git_provider_from_flags(
                override=None, has_github_token=False, has_bitbucket_token=False
            )

    def test_explicit_override_wins_even_with_both_tokens(self):
        provider = resolve_active_git_provider_from_flags(
            override="bitbucket_cloud", has_github_token=True, has_bitbucket_token=True
        )
        assert provider == GitProvider.BITBUCKET_CLOUD

    def test_explicit_override_is_case_insensitive(self):
        provider = resolve_active_git_provider_from_flags(
            override="GITHUB", has_github_token=False, has_bitbucket_token=True
        )
        assert provider == GitProvider.GITHUB

    def test_invalid_override_raises(self):
        with pytest.raises(GitProviderResolutionError, match="not valid"):
            resolve_active_git_provider_from_flags(
                override="gitlab", has_github_token=True, has_bitbucket_token=False
            )


class TestStrategies:
    def test_github_default_git_user(self):
        assert strategy_for(GitProvider.GITHUB).default_git_user == "x-access-token"

    def test_bitbucket_default_git_user(self):
        assert strategy_for(GitProvider.BITBUCKET_CLOUD).default_git_user == "x-token-auth"

    def test_all_strategies_covers_both_providers(self):
        providers = {s.provider for s in all_strategies()}
        assert providers == {GitProvider.GITHUB, GitProvider.BITBUCKET_CLOUD}


class TestSanitizationPatterns:
    def _sanitize(self, provider: GitProvider, message: str) -> str:
        sanitized = message
        for pattern, replacement in strategy_for(provider).sanitization_patterns:
            sanitized = pattern.sub(replacement, sanitized)
        return sanitized

    def test_github_url_scrubbed(self):
        msg = "clone https://x-access-token:ghp_abc123@github.com/org/repo.git failed"
        out = self._sanitize(GitProvider.GITHUB, msg)
        assert "ghp_abc123" not in out
        assert "[REDACTED]" in out

    def test_github_pat_token_scrubbed_standalone(self):
        assert "[REDACTED]" in self._sanitize(GitProvider.GITHUB, "token=github_pat_XYZ123")

    def test_bitbucket_url_scrubbed(self):
        msg = "clone https://x-token-auth:ATCTT3xFfGN0abc@bitbucket.org/ws/repo.git failed"
        out = self._sanitize(GitProvider.BITBUCKET_CLOUD, msg)
        assert "ATCTT3xFfGN0abc" not in out
        assert "[REDACTED]" in out

    def test_bitbucket_token_scrubbed_standalone(self):
        assert "[REDACTED]" in self._sanitize(GitProvider.BITBUCKET_CLOUD, "token=ATCTT3xFfGN0abc_def-2")

    def test_github_patterns_do_not_touch_bitbucket_url(self):
        msg = "https://x-token-auth:ATCTT3xFfGN0abc@bitbucket.org/ws/repo.git"
        assert self._sanitize(GitProvider.GITHUB, msg) == msg
