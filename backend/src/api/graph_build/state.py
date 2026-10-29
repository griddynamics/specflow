"""What the graph build routes need from a build runner, without loading the graph library.

``api.graph_build.runner`` imports Neo4j and Graphiti; the HTTP layer only needs the status
shape and the three runner calls, so the routes type against this module and the app loads
the runner lazily (``api.runtime.build_runtime``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

Status = Literal["idle", "running", "succeeded", "partially_succeeded", "failed"]


@dataclass(frozen=True)
class BuildState:
    """What ``GET .../graph_build`` answers: ids, counts and codes only."""

    status: Status
    build_id: str | None = None
    mode: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    settled: bool | None = None
    spec_changes: dict[str, int] | None = None
    verification_ok: bool | None = None
    error_code: str | None = None


class BuildRunner(Protocol):
    """The calls of ``api.graph_build.runner.GraphBuildRunner`` the routes make."""

    def running(self, project_id: str) -> bool: ...

    async def start(self, project_id: str) -> BuildState: ...

    async def status(self, project_id: str) -> BuildState: ...
