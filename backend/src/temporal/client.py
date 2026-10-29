"""The Temporal client of this process: tracing, then plugin, then connect."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from temporalio.client import Client
from temporalio.service import TLSConfig

from agent_runtime import tracing
from agent_runtime.plugin import build_plugin
from agent_runtime.tracing import TracingHandle
from logger.logger import INFO, get_logger, log_event
from settings import RoleSettings, TemporalSettings

logger = get_logger("temporal.client")


@dataclass(frozen=True)
class TemporalConnection:
    """The plugin client plus the tracing handle the process must shut down."""

    client: Client
    tracing: TracingHandle


def _pem(value: str) -> bytes:
    path = Path(value)
    return path.read_bytes() if path.is_file() else value.encode()


def _tls(temporal: TemporalSettings) -> TLSConfig | bool:
    api_key = temporal.api_key is not None
    if temporal.tls_cert is not None and temporal.tls_key is not None:
        return TLSConfig(
            client_cert=_pem(temporal.tls_cert.get_secret_value()),
            client_private_key=_pem(temporal.tls_key.get_secret_value()),
        )
    return api_key


async def open_connection(settings: RoleSettings) -> TemporalConnection:
    """``tracing.configure`` -> ``build_plugin`` -> ``Client.connect(plugins=[plugin])``.

    Call once per process and share the client: workers and the API reuse it.
    """

    temporal = settings.temporal
    if temporal is None:
        raise ValueError(f"Role {settings.role!r} has no Temporal settings")
    handle = tracing.configure(settings)
    plugin = build_plugin(settings)
    client = await Client.connect(
        temporal.address,
        namespace=temporal.namespace,
        api_key=temporal.api_key.get_secret_value() if temporal.api_key is not None else None,
        tls=_tls(temporal),
        plugins=[plugin],
    )
    log_event(
        logger,
        INFO,
        "temporal.client.connected",
        address=temporal.address,
        namespace=temporal.namespace,
        tracing_enabled=handle.enabled,
    )
    return TemporalConnection(client=client, tracing=handle)


async def connect(settings: RoleSettings) -> Client:
    """The plugin client; use ``open_connection`` when the tracing handle is needed."""

    return (await open_connection(settings)).client
