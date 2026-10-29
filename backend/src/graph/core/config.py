"""Immutable typed settings the host builds (docs/graph/architecture.md "Settings").

The library never reads environment variables; the host maps its own settings onto these objects.
Invalid values raise ``ConfigurationError`` naming the fields, never their values.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from graph.core.errors import ConfigurationError

CallClass = Literal["extraction", "small", "judge", "embedding"]
CALL_CLASSES: tuple[CallClass, ...] = ("extraction", "small", "judge", "embedding")


class FrozenModel(BaseModel):
    """Base of every settings object: frozen, no unknown fields, errors as ``ConfigurationError``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    def __init__(self, **data: Any) -> None:
        try:
            super().__init__(**data)
        except ValidationError as error:
            fields = sorted({".".join(str(part) for part in item["loc"]) for item in error.errors()})
            raise ConfigurationError("invalid_settings", model=type(self).__name__, fields=",".join(fields)) from None


class Neo4jConfig(FrozenModel):
    """Neo4j connection; ``database`` None means the server's home database."""

    uri: str = Field(min_length=1)
    user: str = Field(min_length=1)
    password: SecretStr
    database: str | None = None


class CallBudget(FrozenModel):
    """Timeout per HTTP attempt and attempts per call for one call class."""

    timeout_s: float = Field(gt=0)
    attempts: int = Field(ge=1)


DEFAULT_BUDGETS: dict[CallClass, CallBudget] = {
    "extraction": CallBudget(timeout_s=180.0, attempts=4),
    "small": CallBudget(timeout_s=60.0, attempts=4),
    "judge": CallBudget(timeout_s=120.0, attempts=3),
    "embedding": CallBudget(timeout_s=60.0, attempts=4),
}


class LLMConfig(FrozenModel):
    """One OpenAI-compatible endpoint for chat and embeddings, shared by every call class."""

    endpoint: str = Field(min_length=1)
    api_key: SecretStr
    main_model: str = Field(min_length=1)
    small_model: str = Field(min_length=1)
    judge_model: str | None = None
    embedding_model: str = Field(min_length=1)
    embedding_dim: int = Field(gt=0, le=4096)
    concurrency: int = Field(default=8, ge=1)
    budgets: dict[CallClass, CallBudget] = Field(default_factory=lambda: dict(DEFAULT_BUDGETS))

    def model_for(self, call_class: CallClass) -> str:
        """The model a call class uses when a call names none; the judge defaults to the main model."""

        if call_class == "small":
            return self.small_model
        if call_class == "judge":
            return self.judge_model or self.main_model
        if call_class == "embedding":
            return self.embedding_model
        return self.main_model

    def budget(self, call_class: CallClass) -> CallBudget:
        return self.budgets.get(call_class, DEFAULT_BUDGETS[call_class])
