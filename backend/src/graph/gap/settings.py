"""Settings of ``graph.gap``: ``GapSettings``, the run limits and their environment names.

docs/graph/gap/architecture.md "Settings" names the environment variables; the host maps them to ``GapSettings``
(``gap_settings_from_env`` does that mapping for it). The limits come from the rules file (``limits``); a project may
override some of them through ``GapSettings.limits``.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Final, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from graph.core.errors import ConfigurationError
from graph.gap.errors import INVALID_GAP_SETTINGS
from graph.gap.names import InvestigatorName

OPTIONS_FLOOR: Final = 2
OPTIONS_CEILING: Final = 4

ENV_ENABLED: Final = "GAP_ENABLED"
ENV_AUTO_RUN_AFTER_BUILD: Final = "GAP_AUTO_RUN_AFTER_BUILD"
ENV_INVESTIGATOR: Final = "GAP_INVESTIGATOR"
ENV_AI_ANSWERS: Final = "GAP_AI_ANSWERS"
ENV_RULES_PATH: Final = "GAP_RULES_PATH"
ENV_NAMES: Final = (
    ENV_ENABLED,
    ENV_AUTO_RUN_AFTER_BUILD,
    ENV_INVESTIGATOR,
    ENV_AI_ANSWERS,
    ENV_RULES_PATH,
)
_TRUE: Final = frozenset({"1", "true", "yes", "on"})
_FALSE: Final = frozenset({"0", "false", "no", "off"})


class GapLimits(BaseModel):
    """The limits of a gap run (docs/graph/gap/detection.md "Limits"); every value positive."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    sites_update_run: int = Field(gt=0)
    sites_full_run: int = Field(gt=0)
    rounds_per_site: int = Field(gt=0)
    reads_per_round: int = Field(gt=0)
    context_tokens: int = Field(gt=0)
    pack_tokens: int = Field(gt=0)
    new_questions_per_run: int = Field(gt=0)
    ai_answers_per_run: int = Field(gt=0)
    ai_answers_max_node_share: float = Field(gt=0, le=1)
    options_min: int = Field(ge=OPTIONS_FLOOR)
    options_max: int = Field(le=OPTIONS_CEILING, gt=0)

    @model_validator(mode="after")
    def _options_order(self) -> Self:
        if self.options_min > self.options_max:
            raise ValueError("options_min must not exceed options_max")
        return self


LIMIT_NAMES: Final = frozenset(GapLimits.model_fields)


class ReconcileSettings(BaseModel):
    """The thresholds of reconcile (docs/graph/gap/reconcile.md "The four questions"), tuned on the shop corpus's
    re-uploads: ``anchor_similarity`` for a reworded anchor line under the same heading, ``section_similarity`` for a
    section whose heading changed, ``judge_min_confidence`` below which a same-meaning verdict counts as different."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    anchor_similarity: float = Field(default=0.7, gt=0, le=1)
    section_similarity: float = Field(default=0.6, gt=0, le=1)
    judge_min_confidence: float = Field(default=0.7, ge=0, le=1)


class GapSettings(BaseModel):
    """What the host decides for gap runs (docs/graph/gap/api.md "Models").

    ``limits`` overrides single limits of the rules file by name; ``ignore_cache`` makes a ``full`` run investigate
    every site again (investigation.md "Verdict cache"); ``reconcile`` holds the thresholds of reconcile
    (reconcile.md).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = True
    rules_path: Path | None = None
    investigator: InvestigatorName = "loop"
    auto_run_after_build: bool = True
    ai_answers: bool = True
    limits: Mapping[str, int | float] = Field(default_factory=lambda: MappingProxyType({}))
    ignore_cache: bool = False
    reconcile: ReconcileSettings = Field(default_factory=ReconcileSettings)

    @field_validator("limits")
    @classmethod
    def _known_limits(cls, value: Mapping[str, int | float]) -> Mapping[str, int | float]:
        unknown = sorted(set(value) - LIMIT_NAMES)
        if unknown:
            raise ValueError(f"unknown limits: {', '.join(unknown)}")
        return MappingProxyType(dict(value))

    def effective_limits(self, base: GapLimits) -> GapLimits:
        """``base`` (the rules' limits) with this project's overrides; ``ConfigurationError`` when they break a
        limit's range."""

        overrides = dict(self.limits)
        if not overrides:
            return base
        try:
            return GapLimits.model_validate({**base.model_dump(), **overrides})
        except ValidationError as error:
            raise ConfigurationError(INVALID_GAP_SETTINGS, problems=error.error_count()) from error


def gap_settings_from_env(variables: Mapping[str, str]) -> GapSettings:
    """``GapSettings`` from the ``GAP_*`` variables of ``variables`` (architecture.md "Settings"); an unset or empty
    variable keeps the default. Raises ``ConfigurationError("invalid_gap_settings")`` naming the variable."""

    values: dict[str, object] = {}
    for name, key in (
        (ENV_ENABLED, "enabled"),
        (ENV_AUTO_RUN_AFTER_BUILD, "auto_run_after_build"),
        (ENV_AI_ANSWERS, "ai_answers"),
    ):
        raw = _raw(variables, name)
        if raw is not None:
            values[key] = _boolean(name, raw)
    investigator = _raw(variables, ENV_INVESTIGATOR)
    if investigator is not None:
        values["investigator"] = investigator
    rules_path = _raw(variables, ENV_RULES_PATH)
    if rules_path is not None:
        values["rules_path"] = Path(rules_path)
    try:
        return GapSettings.model_validate(values)
    except ValidationError as error:
        field = str(error.errors()[0]["loc"][0])
        variable = f"GAP_{field.upper()}"
        raise ConfigurationError(INVALID_GAP_SETTINGS, variable=variable) from error


def _raw(variables: Mapping[str, str], name: str) -> str | None:
    value = variables.get(name, "").strip()
    return value or None


def _boolean(name: str, raw: str) -> bool:
    lowered = raw.lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ConfigurationError(INVALID_GAP_SETTINGS, variable=name)
