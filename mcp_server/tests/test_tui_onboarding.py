"""Tests for the pure onboarding wizard content + validation module.

No Textual dependency — these exercise the step data model and the validation /
collection rules directly, the way render.py is tested independently of the app.
"""

from tui import config, onboarding
from tui.onboarding import (
    GIT_PROVIDER_BITBUCKET,
    GIT_PROVIDER_GITHUB,
    PROVIDER_ANTHROPIC,
    PROVIDER_OPENROUTER,
    StepKind,
)


def _all_fields():
    fields = []
    for step in onboarding.STEPS:
        fields.extend(step.fields)
        for choice in step.choices:
            fields.extend(choice.fields)
    return fields


def _chosen(provider=PROVIDER_OPENROUTER, git=GIT_PROVIDER_GITHUB):
    return {"provider": provider, "git": git}


class TestStepContent:
    def test_step_order(self):
        assert [s.step_id for s in onboarding.STEPS] == [
            "welcome",
            "provider",
            "git",
            "compass",
            "advanced",
            "review",
        ]

    def test_every_field_key_is_a_known_secret_or_langfuse(self):
        allowed = set(config.ENV_SECRET_KEYS) | set(config.LANGFUSE_KEYS)
        for f in _all_fields():
            assert f.key in allowed

    def test_advanced_step_langfuse_fields_optional(self):
        advanced = next(s for s in onboarding.STEPS if s.step_id == "advanced")
        by_key = {f.key: f for f in advanced.fields}
        assert set(by_key) == set(config.LANGFUSE_KEYS)
        assert all(not f.required for f in advanced.fields)

    def test_masking_is_derived_from_config(self):
        for f in _all_fields():
            assert f.masked == (f.key in config.MASKED_KEYS)

    def test_provider_step_defaults_to_openrouter(self):
        assert onboarding.PROVIDER_STEP.default_choice == PROVIDER_OPENROUTER
        assert onboarding.PROVIDER_STEP.kind is StepKind.CHOICE

    def test_git_step_defaults_to_github(self):
        assert onboarding.GIT_STEP.default_choice == GIT_PROVIDER_GITHUB
        assert onboarding.GIT_STEP.kind is StepKind.CHOICE

    def test_choice_steps_are_provider_and_git(self):
        assert [s.step_id for s in onboarding.CHOICE_STEPS] == ["provider", "git"]

    def test_default_choices(self):
        assert onboarding.default_choices() == {
            "provider": PROVIDER_OPENROUTER,
            "git": GIT_PROVIDER_GITHUB,
        }

    def test_github_optional_identity_fields_not_required(self):
        fields = onboarding.choice_fields(onboarding.GIT_STEP, GIT_PROVIDER_GITHUB)
        by_key = {f.key: f for f in fields}
        assert by_key["GITHUB_TOKEN"].required is True
        assert by_key["GITHUB_ORG"].required is False
        assert by_key["GIT_USER_NAME"].required is False

    def test_bitbucket_fields_both_required(self):
        fields = onboarding.choice_fields(onboarding.GIT_STEP, GIT_PROVIDER_BITBUCKET)
        by_key = {f.key: f for f in fields}
        assert by_key["BITBUCKET_TOKEN"].required is True
        assert by_key["BITBUCKET_WORKSPACE"].required is True


class TestRequiredKeys:
    def test_openrouter_github_required_set(self):
        assert set(onboarding.required_keys(_chosen(PROVIDER_OPENROUTER, GIT_PROVIDER_GITHUB))) == {
            "OPENROUTER_API_KEY",
            "GITHUB_TOKEN",
            "P10Y_API_KEY",
        }

    def test_anthropic_required_set(self):
        assert set(onboarding.required_keys(_chosen(PROVIDER_ANTHROPIC, GIT_PROVIDER_GITHUB))) == {
            "ANTHROPIC_API_KEY",
            "GITHUB_TOKEN",
            "P10Y_API_KEY",
        }

    def test_bitbucket_required_set(self):
        assert set(onboarding.required_keys(_chosen(PROVIDER_OPENROUTER, GIT_PROVIDER_BITBUCKET))) == {
            "OPENROUTER_API_KEY",
            "BITBUCKET_TOKEN",
            "BITBUCKET_WORKSPACE",
            "P10Y_API_KEY",
        }


class TestValidateStep:
    def _step(self, step_id):
        return next(s for s in onboarding.STEPS if s.step_id == step_id)

    def test_info_and_review_always_pass(self):
        assert onboarding.validate_step(self._step("welcome"), {}, _chosen()) is None
        assert onboarding.validate_step(self._step("review"), {}, _chosen()) is None

    def test_provider_requires_chosen_key(self):
        step = self._step("provider")
        assert onboarding.validate_step(step, {}, _chosen(PROVIDER_OPENROUTER)) is not None
        ok = {"OPENROUTER_API_KEY": "k"}
        assert onboarding.validate_step(step, ok, _chosen(PROVIDER_OPENROUTER)) is None
        # The unchosen key does not satisfy the chosen provider.
        assert onboarding.validate_step(step, ok, _chosen(PROVIDER_ANTHROPIC)) is not None

    def test_git_github_requires_token_but_not_optionals(self):
        step = self._step("git")
        assert onboarding.validate_step(step, {}, _chosen(git=GIT_PROVIDER_GITHUB)) is not None
        assert (
            onboarding.validate_step(step, {"GITHUB_TOKEN": "t"}, _chosen(git=GIT_PROVIDER_GITHUB))
            is None
        )

    def test_advanced_step_blank_langfuse_passes(self):
        # All LangFuse fields blank → optional step advances cleanly (skip).
        assert onboarding.validate_step(self._step("advanced"), {}, _chosen()) is None

    def test_advanced_step_all_three_langfuse_passes(self):
        vals = {
            "LANGFUSE_PUBLIC_KEY": "pk",
            "LANGFUSE_SECRET_KEY": "sk",
            "LANGFUSE_BASE_URL": "https://lf",
        }
        assert onboarding.validate_step(self._step("advanced"), vals, _chosen()) is None

    def test_advanced_step_partial_langfuse_blocks(self):
        vals = {"LANGFUSE_PUBLIC_KEY": "pk"}  # secret + host missing
        error = onboarding.validate_step(self._step("advanced"), vals, _chosen())
        assert error is not None
        assert "LangFuse" in error

    def test_git_bitbucket_requires_both_fields(self):
        step = self._step("git")
        chosen = _chosen(git=GIT_PROVIDER_BITBUCKET)
        assert onboarding.validate_step(step, {}, chosen) is not None
        assert (
            onboarding.validate_step(step, {"BITBUCKET_TOKEN": "t"}, chosen) is not None
        )
        assert (
            onboarding.validate_step(
                step, {"BITBUCKET_TOKEN": "t", "BITBUCKET_WORKSPACE": "ws"}, chosen
            )
            is None
        )
        # GitHub fields left over from a prior choice don't satisfy BitBucket.
        assert onboarding.validate_step(step, {"GITHUB_TOKEN": "t"}, chosen) is not None


class TestValidateAll:
    def _complete_github(self):
        return {
            "OPENROUTER_API_KEY": "or",
            "GITHUB_TOKEN": "gh",
            "P10Y_API_KEY": "p1",
        }

    def _complete_bitbucket(self):
        return {
            "OPENROUTER_API_KEY": "or",
            "BITBUCKET_TOKEN": "bb",
            "BITBUCKET_WORKSPACE": "ws",
            "P10Y_API_KEY": "p1",
        }

    def test_complete_github_passes(self):
        assert onboarding.validate_all(self._complete_github(), _chosen()) is None

    def test_complete_bitbucket_passes(self):
        assert onboarding.validate_all(self._complete_bitbucket(), _chosen(git=GIT_PROVIDER_BITBUCKET)) is None

    def test_missing_p10y_fails(self):
        vals = self._complete_github()
        del vals["P10Y_API_KEY"]
        assert onboarding.validate_all(vals, _chosen()) is not None

    def test_wrong_llm_provider_key_fails(self):
        # OpenRouter set but Anthropic chosen → the chosen key is missing.
        assert onboarding.validate_all(self._complete_github(), _chosen(PROVIDER_ANTHROPIC)) is not None

    def test_wrong_git_provider_fields_fail(self):
        # GitHub fields set but BitBucket chosen → the chosen fields are missing.
        assert (
            onboarding.validate_all(self._complete_github(), _chosen(git=GIT_PROVIDER_BITBUCKET))
            is not None
        )


class TestEnvSatisfiesRequirements:
    def test_complete_openrouter_github_env_passes(self):
        assert onboarding.env_satisfies_requirements(
            {"OPENROUTER_API_KEY": "or", "GITHUB_TOKEN": "gh", "P10Y_API_KEY": "p1"}
        )

    def test_complete_anthropic_github_env_passes(self):
        assert onboarding.env_satisfies_requirements(
            {"ANTHROPIC_API_KEY": "an", "GITHUB_TOKEN": "gh", "P10Y_API_KEY": "p1"}
        )

    def test_complete_bitbucket_env_passes(self):
        assert onboarding.env_satisfies_requirements(
            {
                "OPENROUTER_API_KEY": "or",
                "BITBUCKET_TOKEN": "bb",
                "BITBUCKET_WORKSPACE": "ws",
                "P10Y_API_KEY": "p1",
            }
        )

    def test_missing_llm_key_fails(self):
        assert not onboarding.env_satisfies_requirements(
            {"GITHUB_TOKEN": "gh", "P10Y_API_KEY": "p1"}
        )

    def test_missing_shared_key_fails(self):
        assert not onboarding.env_satisfies_requirements(
            {"OPENROUTER_API_KEY": "or", "GITHUB_TOKEN": "gh"}
        )

    def test_bitbucket_missing_workspace_fails(self):
        assert not onboarding.env_satisfies_requirements(
            {"OPENROUTER_API_KEY": "or", "BITBUCKET_TOKEN": "bb", "P10Y_API_KEY": "p1"}
        )

    def test_empty_env_fails(self):
        assert not onboarding.env_satisfies_requirements({})


class TestCollectedSecrets:
    def test_drops_empty_and_unchosen_provider_key(self):
        values = {
            "OPENROUTER_API_KEY": "or",
            "ANTHROPIC_API_KEY": "ant",  # set but not chosen → dropped
            "GITHUB_TOKEN": "gh",
            "GITHUB_ORG": "",  # empty → dropped
            "P10Y_API_KEY": "p1",
        }
        out = onboarding.collected_secrets(values, _chosen(PROVIDER_OPENROUTER))
        assert out == {
            "OPENROUTER_API_KEY": "or",
            "GITHUB_TOKEN": "gh",
            "P10Y_API_KEY": "p1",
        }

    def test_keeps_present_optionals(self):
        values = {
            "ANTHROPIC_API_KEY": "ant",
            "GITHUB_TOKEN": "gh",
            "GITHUB_ORG": "my-org",
            "P10Y_API_KEY": "p1",
        }
        out = onboarding.collected_secrets(values, _chosen(PROVIDER_ANTHROPIC))
        assert out["GITHUB_ORG"] == "my-org"
        assert "OPENROUTER_API_KEY" not in out

    def test_bitbucket_choice_drops_github_fields(self):
        values = {
            "OPENROUTER_API_KEY": "or",
            "GITHUB_TOKEN": "gh",  # left over from a prior choice → dropped
            "BITBUCKET_TOKEN": "bb",
            "BITBUCKET_WORKSPACE": "ws",
            "P10Y_API_KEY": "p1",
        }
        out = onboarding.collected_secrets(values, _chosen(git=GIT_PROVIDER_BITBUCKET))
        assert out == {
            "OPENROUTER_API_KEY": "or",
            "BITBUCKET_TOKEN": "bb",
            "BITBUCKET_WORKSPACE": "ws",
            "P10Y_API_KEY": "p1",
        }
