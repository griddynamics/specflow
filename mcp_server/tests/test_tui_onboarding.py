"""Tests for the pure onboarding wizard content + validation module.

No Textual dependency — these exercise the step data model and the validation /
collection rules directly, the way render.py is tested independently of the app.
"""

from tui import config, onboarding
from tui.onboarding import (
    PROVIDER_ANTHROPIC,
    PROVIDER_OPENROUTER,
    StepKind,
)


def _all_fields():
    fields = []
    for step in onboarding.STEPS:
        fields.extend(step.fields)
        fields.extend(c.field for c in step.choices)
    return fields


class TestStepContent:
    def test_step_order(self):
        assert [s.step_id for s in onboarding.STEPS] == [
            "welcome",
            "provider",
            "github",
            "compass",
            "review",
        ]

    def test_every_field_key_is_a_known_secret(self):
        for f in _all_fields():
            assert f.key in config.ENV_SECRET_KEYS

    def test_masking_is_derived_from_config(self):
        for f in _all_fields():
            assert f.masked == (f.key in config.MASKED_KEYS)

    def test_provider_step_defaults_to_openrouter(self):
        assert onboarding.PROVIDER_STEP.default_choice == PROVIDER_OPENROUTER
        assert onboarding.PROVIDER_STEP.kind is StepKind.CHOICE

    def test_optional_identity_fields_not_required(self):
        github = next(s for s in onboarding.STEPS if s.step_id == "github")
        by_key = {f.key: f for f in github.fields}
        assert by_key["GITHUB_TOKEN"].required is True
        assert by_key["GITHUB_ORG"].required is False
        assert by_key["GIT_USER_NAME"].required is False


class TestRequiredKeys:
    def test_openrouter_required_set(self):
        assert set(onboarding.required_keys(PROVIDER_OPENROUTER)) == {
            "OPENROUTER_API_KEY",
            "GITHUB_TOKEN",
            "P10Y_API_KEY",
        }

    def test_anthropic_required_set(self):
        assert set(onboarding.required_keys(PROVIDER_ANTHROPIC)) == {
            "ANTHROPIC_API_KEY",
            "GITHUB_TOKEN",
            "P10Y_API_KEY",
        }


class TestValidateStep:
    def _step(self, step_id):
        return next(s for s in onboarding.STEPS if s.step_id == step_id)

    def test_info_and_review_always_pass(self):
        assert onboarding.validate_step(self._step("welcome"), {}, PROVIDER_OPENROUTER) is None
        assert onboarding.validate_step(self._step("review"), {}, PROVIDER_OPENROUTER) is None

    def test_provider_requires_chosen_key(self):
        step = self._step("provider")
        assert onboarding.validate_step(step, {}, PROVIDER_OPENROUTER) is not None
        ok = {"OPENROUTER_API_KEY": "k"}
        assert onboarding.validate_step(step, ok, PROVIDER_OPENROUTER) is None
        # The unchosen key does not satisfy the chosen provider.
        assert onboarding.validate_step(step, ok, PROVIDER_ANTHROPIC) is not None

    def test_github_requires_token_but_not_optionals(self):
        step = self._step("github")
        assert onboarding.validate_step(step, {}, PROVIDER_OPENROUTER) is not None
        assert (
            onboarding.validate_step(step, {"GITHUB_TOKEN": "t"}, PROVIDER_OPENROUTER) is None
        )


class TestValidateAll:
    def _complete(self):
        return {
            "OPENROUTER_API_KEY": "or",
            "GITHUB_TOKEN": "gh",
            "P10Y_API_KEY": "p1",
        }

    def test_complete_passes(self):
        assert onboarding.validate_all(self._complete(), PROVIDER_OPENROUTER) is None

    def test_missing_p10y_fails(self):
        vals = self._complete()
        del vals["P10Y_API_KEY"]
        assert onboarding.validate_all(vals, PROVIDER_OPENROUTER) is not None

    def test_wrong_provider_key_fails(self):
        # OpenRouter set but Anthropic chosen → the chosen key is missing.
        assert onboarding.validate_all(self._complete(), PROVIDER_ANTHROPIC) is not None


class TestCollectedSecrets:
    def test_drops_empty_and_unchosen_provider_key(self):
        values = {
            "OPENROUTER_API_KEY": "or",
            "ANTHROPIC_API_KEY": "ant",  # set but not chosen → dropped
            "GITHUB_TOKEN": "gh",
            "GITHUB_ORG": "",  # empty → dropped
            "P10Y_API_KEY": "p1",
        }
        out = onboarding.collected_secrets(values, PROVIDER_OPENROUTER)
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
        out = onboarding.collected_secrets(values, PROVIDER_ANTHROPIC)
        assert out["GITHUB_ORG"] == "my-org"
        assert "OPENROUTER_API_KEY" not in out
