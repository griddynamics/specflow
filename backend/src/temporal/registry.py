"""Per-queue worker registration, imported lazily.

Contract for the step that provides a queue (parse: ``documents``, agents:
``agents``). The module ``temporal.<documents|agents>.registration``
defines::

    def register(settings: RoleSettings) -> Registration

``Registration`` lists the queue's ``workflows`` (classes) and its own
``activities`` (the model activities of the OpenAI Agents plugin are added by
the plugin, not listed). ``close`` is an optional async callable that releases
what ``register`` opened (database engines, drivers); the worker runner awaits
it after the queue's worker stopped. ``register`` must be cheap and must not
connect to anything the worker can still refuse to start without.

Executors: a ``def`` (sync) activity runs on a thread pool. The runner gives
every queue that has at least one sync activity its OWN one-thread executor,
never shared with another queue, and queues with only ``async def`` activities
get none. Prefer ``async def`` activities: the ``agents`` queue is expected to
hold only those, and a long sync activity (``parse_document``, up to 45 min)
occupies its queue's single thread, so other sync activities of that queue
wait behind it.

The module is imported only when its queue is polled, so a process that polls
one queue never imports the other's dependencies (sqlalchemy, neo4j, ...).
"""

from __future__ import annotations

import importlib
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Final

from settings import RoleSettings

REGISTRATION_MODULES: Final[dict[str, str]] = {
    "parse": "temporal.documents.registration",
    "agents": "temporal.agents.registration",
}
REGISTER_FUNCTION: Final = "register"


class RegistrationUnavailableError(RuntimeError):
    """The registration module of a queue is missing or has the wrong shape."""


@dataclass(frozen=True)
class Registration:
    """What one queue's ``Worker`` runs.

    ``close`` (optional) releases resources the registration opened (database
    engines, drivers); the worker runner awaits it after the worker stopped.
    """

    workflows: Sequence[type]
    activities: Sequence[Callable[..., Any]]
    close: Callable[[], Awaitable[None]] | None = None


def load_registration(
    queue: str,
    settings: RoleSettings,
    *,
    import_module: Callable[[str], ModuleType] = importlib.import_module,
) -> Registration:
    """Import the queue's registration module and call ``register(settings)``."""

    module_name = REGISTRATION_MODULES.get(queue)
    if module_name is None:
        raise RegistrationUnavailableError(f"Unknown queue {queue!r}")
    try:
        module = import_module(module_name)
    except ModuleNotFoundError as error:
        parts = module_name.split(".")
        if error.name not in {".".join(parts[: i + 1]) for i in range(len(parts))}:
            raise
        raise RegistrationUnavailableError(
            f"Queue {queue!r} cannot be polled: {module_name} is not available in this build."
        ) from error
    register = getattr(module, REGISTER_FUNCTION, None)
    if not callable(register):
        raise RegistrationUnavailableError(f"{module_name} must define {REGISTER_FUNCTION}(settings) -> Registration.")
    registration = register(settings)
    if not isinstance(registration, Registration):
        raise RegistrationUnavailableError(f"{module_name}.{REGISTER_FUNCTION} must return a Registration.")
    return registration
