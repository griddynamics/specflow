"""Task-queue builders: logical queue name (``parse``/``agents``) to Temporal name."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final, Literal, Protocol


class TemporalSettings(Protocol):
    """The queue names of ``settings.TemporalSettings``.

    Declared here, not imported: workflow modules import this one inside the sandbox, which
    must never load ``settings`` (pydantic, environment) as a side effect.
    """

    @property
    def queue_parse(self) -> str: ...

    @property
    def queue_agents(self) -> str: ...

    @property
    def worker_queues(self) -> Sequence[str]: ...


Queue = Literal["parse", "agents"]
PARSE: Final = "parse"
AGENTS: Final = "agents"


def queue_name(temporal: TemporalSettings, queue: str, *, prefix: str = "") -> str:
    """The configured Temporal task-queue name, with an optional test ``prefix``."""

    if queue == PARSE:
        return f"{prefix}{temporal.queue_parse}"
    if queue == AGENTS:
        return f"{prefix}{temporal.queue_agents}"
    raise ValueError(f"Unknown queue: {queue!r}")


def polled_queues(temporal: TemporalSettings, *, prefix: str = "") -> tuple[tuple[str, str], ...]:
    """``(logical, task_queue)`` for every queue in ``WORKER_QUEUES``, in order."""

    return tuple((queue, queue_name(temporal, queue, prefix=prefix)) for queue in temporal.worker_queues)
