"""First-run onboarding wizard content + validation — pure, no Textual import.

Mirrors the ``render.py`` / ``constants.py`` pattern: every structure and
function here is pure and unit-testable without a terminal. ``app.py``'s
``OnboardingScreen`` is rendering + key handling only and consumes this module
for step content and validation, so the "what to collect / what's required"
rules live in exactly one place.

The secret keys and masking referenced here are *derived* from ``tui.config``
(``ENV_SECRET_KEYS`` / ``MASKED_KEYS``) — never restated — and a module-load
assertion fails loudly if a step ever references a key that isn't a known
secret, so this content cannot silently drift from the writer in ``config.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from tui.config import ENV_SECRET_KEYS, MASKED_KEYS

# Provider option ids for the LLM-provider choice step.
PROVIDER_OPENROUTER = "openrouter"
PROVIDER_ANTHROPIC = "anthropic"

# Secret keys for the two LLM providers (exactly one is collected per run).
PROVIDER_KEYS: dict[str, str] = {
    PROVIDER_OPENROUTER: "OPENROUTER_API_KEY",
    PROVIDER_ANTHROPIC: "ANTHROPIC_API_KEY",
}


class StepKind(Enum):
    """What a wizard step renders/collects."""

    INFO = "info"  # explanatory only — no fields
    CHOICE = "choice"  # provider pick — reveals one field for the choice
    FIELDS = "fields"  # one or more plain input rows
    REVIEW = "review"  # read-only recap + Save & Initialize


@dataclass(frozen=True)
class Field:
    """A single collected ``.env`` value."""

    key: str
    label: str
    required: bool = False
    hint: str = ""  # placeholder/help shown for optional fields

    @property
    def masked(self) -> bool:
        """Render as a password input — derived from ``config.MASKED_KEYS``."""
        return self.key in MASKED_KEYS


@dataclass(frozen=True)
class Choice:
    """One option in a ``CHOICE`` step, revealing a single field when picked."""

    option_id: str
    label: str
    why: str
    how_to: tuple[str, ...]
    url: str
    field: Field


@dataclass(frozen=True)
class Step:
    """One wizard step."""

    step_id: str
    title: str
    kind: StepKind
    why: str = ""
    how_to: tuple[str, ...] = ()
    url: str = ""
    fields: tuple[Field, ...] = ()
    choices: tuple[Choice, ...] = ()
    default_choice: str = ""


# ---------------------------------------------------------------------------
# Step content — the single source of truth for onboarding instructions.
# Text mirrors .env.quickstart.example, QUICKSTART.md and docs/quickstart-compass.md.
# ---------------------------------------------------------------------------

_WELCOME = Step(
    step_id="welcome",
    title="Welcome & prerequisites",
    kind=StepKind.INFO,
    why=(
        "SpecFlow needs a few credentials to drive disposable workspace repos, "
        "score variants, and call an LLM. This wizard collects them one at a "
        "time, then runs `specflow init` for you."
    ),
    how_to=(
        "Before you start, make sure these are installed:",
        "1. Docker + Docker Compose",
        "2. uv  (https://github.com/astral-sh/uv)",
        "3. curl",
    ),
)

_PROVIDER = Step(
    step_id="provider",
    title="Choose your LLM provider",
    kind=StepKind.CHOICE,
    why=(
        "SpecFlow needs exactly one LLM provider. OpenRouter is an aggregator "
        "and the default; Anthropic is direct access. We collect only the key "
        "for the provider you choose."
    ),
    default_choice=PROVIDER_OPENROUTER,
    choices=(
        Choice(
            option_id=PROVIDER_OPENROUTER,
            label="OpenRouter  (default — aggregator)",
            why="Routes requests to Claude and other models. The default provider.",
            how_to=(
                "1. Open https://openrouter.ai/keys",
                "2. Create a key and paste it below.",
            ),
            url="https://openrouter.ai/keys",
            field=Field("OPENROUTER_API_KEY", "OpenRouter API key", required=True),
        ),
        Choice(
            option_id=PROVIDER_ANTHROPIC,
            label="Anthropic  (direct access)",
            why="Direct Anthropic Claude API. init sets DEFAULT_PROVIDER=anthropic automatically.",
            how_to=(
                "1. Open https://console.anthropic.com/settings/keys",
                "2. Create a key and paste it below.",
            ),
            url="https://console.anthropic.com/settings/keys",
            field=Field("ANTHROPIC_API_KEY", "Anthropic API key", required=True),
        ),
    ),
)

_GITHUB = Step(
    step_id="github",
    title="GitHub access",
    kind=StepKind.FIELDS,
    why=(
        "A GitHub Personal Access Token lets SpecFlow create and manage the "
        "disposable workspace repos where agents commit generated code. Create "
        "ONE token here and reuse it for the Compass integration in the next "
        "step — no second GitHub key needed."
    ),
    how_to=(
        "1. Open https://github.com/settings/tokens/new?scopes=repo,read:user,workflow,admin:repo_hook,user",
        "2. Create a classic PAT. SpecFlow always uses `repo` + `read:user` (to create",
        "   workspace repos and resolve your GitHub login), plus `workflow` for deploy/E2E",
        "   runs; `admin:repo_hook` + full `user` are added so the SAME token also works",
        "   for the Compass GitHub integration in the next step.",
        "3. Paste it below.",
    ),
    url="https://github.com/settings/tokens/new?scopes=repo,read:user,workflow,admin:repo_hook,user",
    fields=(
        Field("GITHUB_TOKEN", "GitHub token", required=True),
        Field(
            "GITHUB_ORG",
            "GitHub org (optional)",
            hint="GH org for workspace repos; blank = your GitHub login",
        ),
        Field(
            "GIT_USER_NAME",
            "GitHub username (optional)",
            hint="auto-resolved from your GitHub login (GET /user) when blank",
        ),
    ),
)

_COMPASS = Step(
    step_id="compass",
    title="Compass by P10Y",
    kind=StepKind.FIELDS,
    why=(
        "Compass scores generated-code complexity and compares implementation "
        "variants across parallel workspaces. An enterprise account is required "
        "for API access."
    ),
    how_to=(
        "1. Create a Compass account at https://compass.p10y.com (enterprise required).",
        "2. Connect your GitHub org: Settings > Integrations > New Integration > GitHub;",
        "   add the GH username/org owning the workspace repos; paste the SAME GitHub",
        "   PAT you created in the previous step (it already has the scopes Compass",
        "   needs: admin:repo_hook, repo:*, user:*, workflow:*); enable auto-discovery; save.",
        "3. Generate an API token: Settings > API Tokens > Generate.",
        "Docs: docs/quickstart-compass.md. init auto-resolves P10Y_ORGANISATION_ID.",
    ),
    url="https://compass.p10y.com",
    fields=(Field("P10Y_API_KEY", "P10Y / Compass API key", required=True),),
)

_REVIEW = Step(
    step_id="review",
    title="Review & initialize",
    kind=StepKind.REVIEW,
    why=(
        "Review the values below, then Save & Initialize. This writes `.env` and "
        "runs the bootstrap — only non-empty values are stored."
    ),
)

STEPS: tuple[Step, ...] = (_WELCOME, _PROVIDER, _GITHUB, _COMPASS, _REVIEW)

# The provider-choice step, exposed so the screen need not hardcode an index.
PROVIDER_STEP: Step = _PROVIDER


# ---------------------------------------------------------------------------
# Consistency guard: every key a step collects must be a known secret in
# config.py. A drift here is a load-time failure, never a silent bug.
# ---------------------------------------------------------------------------


def _all_field_keys() -> set[str]:
    keys: set[str] = set()
    for step in STEPS:
        keys.update(f.key for f in step.fields)
        keys.update(c.field.key for c in step.choices)
    return keys


_UNKNOWN_KEYS = _all_field_keys() - set(ENV_SECRET_KEYS)
assert not _UNKNOWN_KEYS, (
    f"onboarding STEPS reference keys absent from config.ENV_SECRET_KEYS: {_UNKNOWN_KEYS}"
)


# ---------------------------------------------------------------------------
# Pure validation + collection
# ---------------------------------------------------------------------------


def provider_field(chosen_provider: str) -> Field:
    """The single LLM key field for the chosen provider."""
    choice = next(c for c in _PROVIDER.choices if c.option_id == chosen_provider)
    return choice.field


def required_keys(chosen_provider: str) -> list[str]:
    """All keys that must be non-empty for a complete setup, in step order."""
    keys: list[str] = [provider_field(chosen_provider).key]
    for step in STEPS:
        keys.extend(f.key for f in step.fields if f.required)
    return keys


def validate_step(step: Step, values: dict[str, str], chosen_provider: str) -> str | None:
    """Return an error string if the step's required inputs are unmet, else None."""
    if step.kind is StepKind.CHOICE:
        f = provider_field(chosen_provider)
        if not values.get(f.key, "").strip():
            return f"{f.label} is required — paste it to continue."
        return None
    if step.kind is StepKind.FIELDS:
        missing = [f.label for f in step.fields if f.required and not values.get(f.key, "").strip()]
        if missing:
            return "Missing required field(s): " + ", ".join(missing)
    return None


def validate_all(values: dict[str, str], chosen_provider: str) -> str | None:
    """Final gate before save — every required key must be present.

    Reproduces the original ``_validation_error`` rule (GitHub token + P10Y key
    + exactly the chosen LLM provider key) so the contract lives in one place.
    """
    field_by_key = {f.key: f for step in STEPS for f in step.fields}
    field_by_key.update({c.field.key: c.field for c in _PROVIDER.choices})
    missing = [
        field_by_key[key].label
        for key in required_keys(chosen_provider)
        if not values.get(key, "").strip()
    ]
    if missing:
        return "Missing required field(s): " + ", ".join(missing)
    return None


def collected_secrets(values: dict[str, str], chosen_provider: str) -> dict[str, str]:
    """Non-empty values to write to ``.env``, omitting the non-chosen provider key."""
    drop = {k for k in PROVIDER_KEYS.values() if k != provider_field(chosen_provider).key}
    return {
        key: value.strip()
        for key, value in values.items()
        if key not in drop and value.strip()
    }
