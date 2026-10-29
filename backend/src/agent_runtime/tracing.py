"""Tracing wiring: the single entry point for the API and the worker.

Order matters (SP-A report): ``configure`` runs first, then ``build_plugin``,
then ``Client.connect``. With Langfuse on, the global tracer provider must be
a replay-safe one before the plugin is built. With Langfuse off, the plugin's
own trace provider ignores ``set_tracing_disabled``, so the process env flag
``OPENAI_AGENTS_DISABLE_TRACING`` is set before the plugin exists. The Langfuse
SDK client getter is never used: it installs a second span processor.
"""

from __future__ import annotations

import base64
import os
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Final

import opentelemetry.exporter.otlp.proto.http.trace_exporter as otlp_trace_exporter
import opentelemetry.sdk.trace.export as otel_export
from agents import set_trace_processors, set_tracing_disabled
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.resources import Resource
from temporalio.contrib import opentelemetry as temporal_otel

from logger.logger import INFO, get_logger, log_event
from settings import RoleSettings

logger = get_logger("agent_runtime.tracing")

DISABLE_ENV: Final = "OPENAI_AGENTS_DISABLE_TRACING"
OTLP_PATH: Final = "/api/public/otel/v1/traces"


@dataclass
class TracingHandle:
    """``enabled`` says whether spans are exported; ``shutdown`` flushes then stops."""

    enabled: bool
    _shutdown: Callable[[], None] | None = field(default=None, repr=False)

    def shutdown(self) -> None:
        """Force-flush and shut the exporter down; idempotent, never raises."""

        action, self._shutdown = self._shutdown, None
        if action is not None:
            with suppress(Exception):
                action()


def configure(settings: RoleSettings) -> TracingHandle:
    """Configure tracing for this process from ``settings.langfuse``."""

    langfuse = settings.langfuse
    if langfuse is not None and langfuse.enabled:
        handle = _enable(settings)
    else:
        _disable()
        handle = TracingHandle(enabled=False)
    log_event(logger, INFO, "tracing.configured", enabled=handle.enabled, role=settings.role)
    return handle


def _disable() -> None:
    os.environ.update({DISABLE_ENV: "1"})
    set_trace_processors([])
    set_tracing_disabled(True)


def _enable(settings: RoleSettings) -> TracingHandle:
    langfuse = settings.langfuse
    if langfuse is None or langfuse.public_key is None or langfuse.secret_key is None:
        raise ValueError("Langfuse is not configured")
    base_url = (langfuse.base_url or "").rstrip("/")
    credentials = f"{langfuse.public_key.get_secret_value()}:{langfuse.secret_key.get_secret_value()}"
    exporter = otlp_trace_exporter.OTLPSpanExporter(
        endpoint=f"{base_url}{OTLP_PATH}",
        headers={
            "Authorization": "Basic " + base64.b64encode(credentials.encode()).decode(),
            "x-langfuse-ingestion-version": "4",
        },
    )
    set_tracing_disabled(False)
    set_trace_processors([])  # drop the default OpenAI exporter
    provider = temporal_otel.create_tracer_provider(
        resource=Resource.create({"service.name": f"specflow-{settings.role}"})
    )
    provider.add_span_processor(otel_export.BatchSpanProcessor(exporter))
    otel_trace.set_tracer_provider(provider)

    def shutdown() -> None:
        provider.force_flush()
        provider.shutdown()

    return TracingHandle(enabled=True, _shutdown=shutdown)
