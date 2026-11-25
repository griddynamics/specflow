"""Runtime hook of stream S4: state shared by the Linear project importer. FOUNDATION STUB: a no-op until S4 fills it
(the importer itself is started by its route, as the Jira epic importer is)."""

from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Any


def add_linear_importer(runtime: Any, stack: AsyncExitStack) -> None:
    """S4: called once ``runtime.imports`` exists and ``runtime.linear`` is set; keep any shared importer state here."""

    del runtime, stack
