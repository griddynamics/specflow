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

A wizard can have more than one ``CHOICE`` step (LLM provider, git host, ...).
Each is independent: the "chosen option per step" state is a
``dict[step_id, option_id]`` (see ``default_choices``), and every function
below that used to take a single "chosen provider" string now takes that dict.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from enum import Enum

from tui.config import ENV_SECRET_KEYS, LANGFUSE_KEYS, MASKED_KEYS, langfuse_partial_error

# Provider option ids for the LLM-provider choice step.
PROVIDER_OPENROUTER = "openrouter"
PROVIDER_ANTHROPIC = "anthropic"

# Provider option ids for the git-host choice step. A deployment is either
# all-GitHub or all-BitBucket, never mixed — mirrors the backend's GitProvider.
GIT_PROVIDER_GITHUB = "github"
GIT_PROVIDER_BITBUCKET = "bitbucket_cloud"


class StepKind(Enum):
    """What a wizard step renders/collects."""

    INFO = "info"  # explanatory only — no fields
    CHOICE = "choice"  # pick one option — reveals that option's field(s)
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
    """One option in a ``CHOICE`` step, revealing its field(s) when picked."""

    option_id: str
    label: str
    why: str
    how_to: tuple[str, ...]
    url: str
    fields: tuple[Field, ...]


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
            fields=(Field("OPENROUTER_API_KEY", "OpenRouter API key", required=True),),
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
            fields=(Field("ANTHROPIC_API_KEY", "Anthropic API key", required=True),),
        ),
    ),
)

_GIT = Step(
    step_id="git",
    title="Choose your git host",
    kind=StepKind.CHOICE,
    why=(
        "SpecFlow needs exactly one git host to create and manage the disposable "
        "workspace repos where agents commit generated code. A deployment is "
        "either all-GitHub or all-BitBucket, never mixed."
    ),
    default_choice=GIT_PROVIDER_GITHUB,
    choices=(
        Choice(
            option_id=GIT_PROVIDER_GITHUB,
            label="GitHub  (default)",
            why=(
                "A GitHub Personal Access Token lets SpecFlow create and manage the "
                "disposable workspace repos. Create ONE token here and reuse it for "
                "the Compass integration in the next step — no second GitHub key needed."
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
        ),
        Choice(
            option_id=GIT_PROVIDER_BITBUCKET,
            label="BitBucket Cloud",
            why=(
                "A BitBucket Cloud Workspace access token lets SpecFlow create and "
                "manage the disposable workspace repos. No username needed — access "
                "tokens always authenticate as the fixed actor `x-token-auth`."
            ),
            how_to=(
                "1. Open your BitBucket workspace > Settings > Access tokens > Create access token.",
                "2. Grant it repository read, write, and admin scopes.",
                "3. Paste the token and your workspace slug below.",
            ),
            url="https://bitbucket.org",
            fields=(
                Field("BITBUCKET_TOKEN", "BitBucket access token", required=True),
                Field(
                    "BITBUCKET_WORKSPACE",
                    "BitBucket workspace",
                    required=True,
                    hint="workspace slug that owns the workspace repos",
                ),
            ),
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
        "2. Connect your git host: Settings > Integrations > New Integration;",
        "   add the username/org/workspace owning the workspace repos; paste the SAME",
        "   token you created in the previous step; enable auto-discovery; save.",
        "3. Generate an API token: Settings > API Tokens > Generate.",
        "Docs: docs/quickstart-compass.md. init auto-resolves P10Y_ORGANISATION_ID.",
    ),
    url="https://compass.p10y.com",
    fields=(Field("P10Y_API_KEY", "P10Y / Compass API key", required=True),),
)

_ADVANCED = Step(
    step_id="advanced",
    title="Advanced settings (optional)",
    kind=StepKind.FIELDS,
    why=(
        "Optional — leave blank to skip (you can add these later from Settings). "
        "LangFuse captures LLM traces for debugging and cost analysis. All three "
        "values are required together to enable tracing."
    ),
    how_to=(
        "1. In your LangFuse project: Settings > API Keys > Create.",
        "2. Paste the public key, secret key, and host URL below — or skip.",
    ),
    url="https://cloud.langfuse.com",
    fields=(
        Field("LANGFUSE_PUBLIC_KEY", "LangFuse public key", hint="pk-lf-…  (blank = skip)"),
        Field("LANGFUSE_SECRET_KEY", "LangFuse secret key", hint="sk-lf-…  (blank = skip)"),
        Field("LANGFUSE_BASE_URL", "LangFuse host URL", hint="https://cloud.langfuse.com"),
    ),
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

STEPS: tuple[Step, ...] = (_WELCOME, _PROVIDER, _GIT, _COMPASS, _ADVANCED, _REVIEW)

# Choice steps, exposed so the screen need not hardcode which steps are CHOICE.
PROVIDER_STEP: Step = _PROVIDER
GIT_STEP: Step = _GIT
CHOICE_STEPS: tuple[Step, ...] = tuple(s for s in STEPS if s.kind is StepKind.CHOICE)


# ---------------------------------------------------------------------------
# Consistency guard: every key a step collects must be a known secret in
# config.py. A drift here is a load-time failure, never a silent bug.
# ---------------------------------------------------------------------------


def _all_field_keys() -> set[str]:
    keys: set[str] = set()
    for step in STEPS:
        keys.update(f.key for f in step.fields)
        for choice in step.choices:
            keys.update(f.key for f in choice.fields)
    return keys


# Fields may collect either a core secret or an advanced/optional one (LangFuse);
# both are keys the .env writer knows about.
_WRITABLE_KEYS = set(ENV_SECRET_KEYS) | set(LANGFUSE_KEYS)
_UNKNOWN_KEYS = _all_field_keys() - _WRITABLE_KEYS
assert not _UNKNOWN_KEYS, (
    f"onboarding STEPS reference keys absent from config secret/LangFuse keys: {_UNKNOWN_KEYS}"
)


# ---------------------------------------------------------------------------
# Pure validation + collection
#
# ``chosen`` is a dict[step_id, option_id] — one entry per CHOICE step. Use
# ``default_choices()`` to seed it.
# ---------------------------------------------------------------------------


def default_choices() -> dict[str, str]:
    """The default option id for every CHOICE step, keyed by step_id."""
    return {step.step_id: step.default_choice for step in CHOICE_STEPS}


def choice_fields(step: Step, chosen_option: str) -> tuple[Field, ...]:
    """The field(s) revealed by the chosen option of a CHOICE step."""
    choice = next(c for c in step.choices if c.option_id == chosen_option)
    return choice.fields


def _field_by_key() -> dict[str, Field]:
    field_by_key: dict[str, Field] = {f.key: f for step in STEPS for f in step.fields}
    for step in CHOICE_STEPS:
        for choice in step.choices:
            field_by_key.update({f.key: f for f in choice.fields})
    return field_by_key


def required_keys(chosen: dict[str, str]) -> list[str]:
    """All keys that must be non-empty for a complete setup, in step order."""
    keys: list[str] = []
    for step in STEPS:
        if step.kind is StepKind.CHOICE:
            keys.extend(f.key for f in choice_fields(step, chosen[step.step_id]) if f.required)
        else:
            keys.extend(f.key for f in step.fields if f.required)
    return keys


def validate_step(step: Step, values: dict[str, str], chosen: dict[str, str]) -> str | None:
    """Return an error string if the step's required inputs are unmet, else None."""
    if step.kind is StepKind.CHOICE:
        missing = [
            f.label
            for f in choice_fields(step, chosen[step.step_id])
            if f.required and not values.get(f.key, "").strip()
        ]
        if missing:
            return "Missing required field(s): " + ", ".join(missing)
        return None
    if step.kind is StepKind.FIELDS:
        missing = [f.label for f in step.fields if f.required and not values.get(f.key, "").strip()]
        if missing:
            return "Missing required field(s): " + ", ".join(missing)
        # LangFuse fields are optional but all-or-nothing — enforce only on the
        # step that actually carries them (the shared config rule, one place).
        if any(f.key in LANGFUSE_KEYS for f in step.fields):
            return langfuse_partial_error(values)
    return None


def validate_all(values: dict[str, str], chosen: dict[str, str]) -> str | None:
    """Final gate before save — every required key (for the chosen options) must be present."""
    field_by_key = _field_by_key()
    missing = [
        field_by_key[key].label
        for key in required_keys(chosen)
        if not values.get(key, "").strip()
    ]
    if missing:
        return "Missing required field(s): " + ", ".join(missing)
    return None


def env_satisfies_requirements(secrets: dict[str, str]) -> bool:
    """True if an existing ``.env`` already has every required key for some combination
    of choices (e.g. some LLM provider AND some git host).

    Reuses ``validate_all`` against every combination so the "what's required"
    contract stays defined in exactly one place (no second validator).
    """
    option_lists = [[c.option_id for c in step.choices] for step in CHOICE_STEPS]
    for combo in itertools.product(*option_lists):
        chosen = {step.step_id: option for step, option in zip(CHOICE_STEPS, combo)}
        if validate_all(secrets, chosen) is None:
            return True
    return False


def collected_secrets(values: dict[str, str], chosen: dict[str, str]) -> dict[str, str]:
    """Non-empty values to write to ``.env``, omitting fields for unchosen options."""
    drop: set[str] = set()
    for step in CHOICE_STEPS:
        chosen_option = chosen[step.step_id]
        for choice in step.choices:
            if choice.option_id != chosen_option:
                drop.update(f.key for f in choice.fields)
    return {
        key: value.strip()
        for key, value in values.items()
        if key not in drop and value.strip()
    }
