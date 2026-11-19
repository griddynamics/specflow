"""Development gaps as the first-slice finding shapes (docs/graph/gap/lifecycle.md "Development gaps", api.md "MCP
tools", "Web routes").

A coding agent's ``add_finding`` is a development gap; its answer and the ``/projects/{pid}/findings`` alias routes
still speak the finding shapes of the first slice. The mapping both ways:

| Finding kind | Gap kind |
|---|---|
| ``question`` | ``open_question`` |
| ``conflict`` | ``contradiction`` |
| ``gap`` | ``missing`` |

Any other gap kind reads as the finding kind ``gap``; ``note`` is no gap (a ``gap_note`` row). A gap that is
``open``, ``answered`` or ``applying`` is an ``open`` finding, a ``resolved`` one a ``resolved`` finding (a dismissed
gap is gone).
"""

from __future__ import annotations

from typing import Final, Literal

from api.errors import ApiError
from api.runtime import Runtime
from graph.gap import GapService

FindingKind = Literal["gap", "conflict", "question", "note"]
FindingStatus = Literal["open", "resolved", "dismissed"]

GAP_KINDS: Final[dict[str, str]] = {"question": "open_question", "conflict": "contradiction", "gap": "missing"}
FINDING_KINDS: Final[dict[str, FindingKind]] = {"open_question": "question", "contradiction": "conflict"}
OPEN_GAP_STATUSES: Final = ("open", "answered", "applying")


def gap_kind(kind: str) -> str:
    """The gap kind of a finding kind (``note`` has none: it is not a gap)."""

    return GAP_KINDS[kind]


def finding_kind(kind: str) -> FindingKind:
    """The finding kind a gap of ``kind`` reads as."""

    return FINDING_KINDS.get(kind, "gap")


def finding_status(status: str) -> FindingStatus:
    return "resolved" if status == "resolved" else "open"


def gap_unavailable() -> ApiError:
    return ApiError(503, "gap_unavailable", "Gap runs are not configured.")


async def development_service(runtime: Runtime) -> GapService:
    """The gap service of the runtime's gap runner; ``503 gap_unavailable`` while gap runs are not configured."""

    if runtime.gaps is None:
        raise gap_unavailable()
    service: GapService = await runtime.gaps.service()
    return service
