"""Runtime hook of stream S4: the Linear project importer needs no state shared between imports. Its route
(``api.routers.source_linear``) builds a ``LinearProjectImporter`` per source over ``runtime.linear`` and starts it on
``runtime.imports``, as the Jira epic route does; a source the restart interrupted is failed by the runner's recovery
(``linear_project`` is in ``api.imports.model.IMPORT_KINDS``). This hook only says so: with the integration off
(``runtime.linear`` is ``None``) the routes answer ``503 linear_unavailable`` and nothing is registered."""

from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Any

from logger.logger import INFO, get_logger, log_event

logger = get_logger("api.imports.linear_wiring")


def add_linear_importer(runtime: Any, stack: AsyncExitStack) -> None:
    """Called once ``runtime.imports`` exists and ``runtime.linear`` is set (or ``None``); logs whether it is on."""

    del stack
    log_event(logger, INFO, "api.imports.linear", mode="on" if runtime.linear is not None else "off")
