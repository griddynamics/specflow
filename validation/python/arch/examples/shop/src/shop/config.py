"""Runtime settings, read from the environment once per process."""

from __future__ import annotations

from functools import lru_cache
from typing import Self

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: The placeholder the code ships; a deployment must replace it, so startup refuses it.
_PLACEHOLDER_SECRET = "change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    secret_key: SecretStr = SecretStr(_PLACEHOLDER_SECRET)

    @model_validator(mode="after")
    def _refuse_placeholder_secrets(self) -> Self:
        if self.secret_key.get_secret_value() == _PLACEHOLDER_SECRET:
            msg = "placeholder secrets must be replaced: secret_key"
            raise ValueError(msg)
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
