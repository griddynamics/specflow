"""The LLM gateway model provider: OpenAI-compatible Chat Completions on the worker.

The provider runs inside ``invoke_model_activity``. It owns one ``AsyncOpenAI``
client (created on the first call), disables the SDK retries (Temporal retries
the activity) and sets a client timeout below the activity's start-to-close.
An empty key or model id is a non-retryable ``ConfigurationError`` that names
the variable, never a value.
"""

from __future__ import annotations

from typing import Final, override

from agents import Model, ModelProvider, OpenAIChatCompletionsModel
from openai import AsyncOpenAI
from temporalio.exceptions import ApplicationError

from settings import AgentSettings

CONFIGURATION_ERROR_TYPE: Final = "ConfigurationError"
CLIENT_TIMEOUT_SECONDS: Final = 110.0
CLIENT_MAX_RETRIES: Final = 0


def configuration_error(variable: str) -> ApplicationError:
    """A non-retryable error that names the empty ``variable``."""

    return ApplicationError(
        f"{variable} is empty on this worker",
        type=CONFIGURATION_ERROR_TYPE,
        non_retryable=True,
    )


class LlmGatewayModelProvider(ModelProvider):
    """``get_model`` for every model call; one client per provider instance."""

    def __init__(
        self,
        agent: AgentSettings,
        *,
        default_model: str | None = None,
        timeout: float = CLIENT_TIMEOUT_SECONDS,
    ) -> None:
        self._agent = agent
        self._default_model = default_model
        self._timeout = timeout
        self._client: AsyncOpenAI | None = None

    @override
    def get_model(self, model_name: str | None) -> Model:
        name = (model_name or self._default_model or "").strip()
        if not name:
            raise configuration_error("AGENT_MODEL")
        return OpenAIChatCompletionsModel(model=name, openai_client=self._client_or_create())

    @override
    async def aclose(self) -> None:
        """Close the HTTP client when one was created (idempotent; no-op before the first call)."""

        client, self._client = self._client, None
        if client is not None:
            await client.close()

    def _client_or_create(self) -> AsyncOpenAI:
        if self._client is None:
            key = self._agent.llm_gateway_api_key.get_secret_value().strip()
            if not key:
                raise configuration_error("LLM_GATEWAY_API_KEY")
            base_url = self._agent.llm_gateway_url.strip()
            if not base_url:
                raise configuration_error("LLM_GATEWAY_URL")
            self._client = AsyncOpenAI(
                base_url=base_url, api_key=key, max_retries=CLIENT_MAX_RETRIES, timeout=self._timeout
            )
        return self._client


class UnconfiguredModelProvider(ModelProvider):
    """For roles without LLM gateway settings (the API, a parse-only worker).

    The plugin's default provider would demand ``OPENAI_API_KEY`` and could
    reach OpenAI; this one never calls out and always refuses.
    """

    @override
    def get_model(self, model_name: str | None) -> Model:
        del model_name
        raise ApplicationError(
            "LLM_GATEWAY_URL, LLM_GATEWAY_API_KEY and AGENT_MODEL are not configured on this worker",
            type=CONFIGURATION_ERROR_TYPE,
            non_retryable=True,
        )
