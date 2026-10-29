"""The ``OpenAIAgentsPlugin`` of this process (API and every worker)."""

from __future__ import annotations

from datetime import timedelta

from agents import ModelProvider
from temporalio.common import RetryPolicy
from temporalio.openai_agents import ModelActivityParameters, OpenAIAgentsPlugin

from agent_runtime.provider import (
    CONFIGURATION_ERROR_TYPE,
    LlmGatewayModelProvider,
    UnconfiguredModelProvider,
)
from settings import RoleSettings


def model_params() -> ModelActivityParameters:
    """The model activity's timeouts and retries (SPECS section 11).

    A new instance each call: the plugin may fill defaults into it. A custom
    provider requires a start-to-close or schedule-to-close timeout.
    """

    return ModelActivityParameters(
        start_to_close_timeout=timedelta(seconds=120),
        schedule_to_close_timeout=timedelta(minutes=5),
        heartbeat_timeout=timedelta(seconds=30),
        retry_policy=RetryPolicy(
            initial_interval=timedelta(seconds=2),
            backoff_coefficient=2.0,
            maximum_interval=timedelta(seconds=30),
            maximum_attempts=3,
            non_retryable_error_types=[CONFIGURATION_ERROR_TYPE],
        ),
    )


def build_plugin(
    settings: RoleSettings,
    *,
    otel: bool | None = None,
    model_provider: ModelProvider | None = None,
) -> OpenAIAgentsPlugin:
    """Build the plugin; call ``tracing.configure(settings)`` first.

    ``otel`` defaults to ``settings.langfuse.enabled``. With it on, the plugin
    raises ``ValueError`` unless ``configure`` already installed the
    replay-safe tracer provider. The LLM gateway provider is used when the role
    has agent settings; ``model_provider`` overrides it (tests). A role without
    them (the API, a parse-only worker) gets a provider that always refuses.
    """

    if otel is None:
        otel = settings.langfuse is not None and settings.langfuse.enabled
    if model_provider is None:
        model_provider = _default_provider(settings)
    return OpenAIAgentsPlugin(
        model_params=model_params(),
        model_provider=model_provider,
        use_otel_instrumentation=otel,
    )


def _default_provider(settings: RoleSettings) -> ModelProvider:
    if settings.agent is None:
        return UnconfiguredModelProvider()
    default_model = settings.agent_run.model if settings.agent_run is not None else None
    return LlmGatewayModelProvider(settings.agent, default_model=default_model)
