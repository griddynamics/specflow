"""The plan's findings port in the host, ``api.gap.findings.OpenGaps``: the open gaps of the gap runner's
``GapService`` (docs/graph/gap/lifecycle.md "Plan link", FR-GPLN-02)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from api.gap.findings import OpenGaps
from graph.core import ConfigurationError, InfrastructureUnavailableError

AT = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class Item:
    finding_id: str
    blocking: bool
    minute: int
    about_spec_ids: tuple[str, ...] = ("REQ::A",)

    @property
    def created_at(self) -> datetime:
        return AT.replace(minute=self.minute)


@dataclass(frozen=True)
class Page:
    findings: tuple[Item, ...]


@dataclass(frozen=True)
class View:
    gap_id: str
    severity: str


class Source:
    """A gap service with fixed findings and counts; records the calls."""

    def __init__(self, items: Sequence[Item], counts: dict[str, int] | None = None) -> None:
        self.items = tuple(items)
        self.counts = counts or {}
        self.calls: list[tuple[str, Any]] = []
        self.asked: list[tuple[str, Any]] = []

    async def open_counts(self, project_id: str, spec_ids: Sequence[str]) -> dict[str, int]:
        self.calls.append(("open_counts", (project_id, *spec_ids)))
        return {spec_id: self.counts.get(spec_id, 0) for spec_id in spec_ids}

    async def findings(self, project_id: str, *, spec_ids: Sequence[str] | None = None, limit: int = 50) -> Page:
        self.calls.append(("findings", limit))
        self.asked.append((project_id, spec_ids))
        return Page(findings=self.items[:limit])

    async def list_gaps(self, project_id: str, *, about: Sequence[str], limit: int) -> Any:
        self.calls.append(("list_gaps", tuple(about)))
        self.asked.append((project_id, limit))
        return type("P", (), {"gaps": tuple(View(item.finding_id, "blocker") for item in self.items)})()


class Runner:
    def __init__(self, service: Any = None, error: Exception | None = None) -> None:
        self._service = service
        self._error = error

    async def service(self) -> Any:
        if self._error is not None:
            raise self._error
        return self._service


async def test_open_counts_are_the_gap_services() -> None:
    gaps = Source([], {"REQ::A": 2, "REQ::B": 1})
    port = OpenGaps(lambda: Runner(gaps))

    assert await port.open_counts("p", ["REQ::A", "REQ::B", "REQ::A"]) == {"REQ::A": 2, "REQ::B": 1}
    assert await port.open_counts("p", []) == {}


async def test_findings_are_ordered_blocking_first_then_newest_first_within_the_limit() -> None:
    gaps = Source(
        [
            Item("GAP::1", blocking=False, minute=5),
            Item("GAP::2", blocking=True, minute=1),
            Item("GAP::3", blocking=False, minute=1),
        ]
    )
    port = OpenGaps(lambda: Runner(gaps))

    page = await port.findings("p", spec_ids=["REQ::A"], limit=2)

    assert [item.finding_id for item in page.findings] == ["GAP::2", "GAP::1"]
    assert ("findings", 200) in gaps.calls  # the service is asked for a full page


async def test_without_a_gap_runner_no_finding_is_read() -> None:
    port = OpenGaps(lambda: None)

    assert (await port.findings("p")).findings == ()
    assert await port.open_counts("p", ["REQ::A"]) == {"REQ::A": 0}
    assert await port.severities("p", [Item("GAP::1", blocking=False, minute=1)]) == {}
    assert await port.finding("p", "GAP::1") is None
    assert await port.blocking_ids("p", "u1") == ()


async def test_a_gap_service_that_cannot_be_built_is_left_out_and_logged(caplog) -> None:
    port = OpenGaps(lambda: Runner(error=ConfigurationError("gap_rules_invalid")))

    page = await port.findings("p")

    assert page.findings == ()
    assert "api.gap.findings_unavailable" in caplog.text


async def test_a_store_outage_of_the_gap_service_passes_through() -> None:
    port = OpenGaps(lambda: Runner(error=InfrastructureUnavailableError("neo4j_unreachable")))

    with pytest.raises(InfrastructureUnavailableError):
        await port.findings("p")


async def test_severities_name_the_gaps_among_the_shown_findings_only() -> None:
    gaps = Source([Item("GAP::1", blocking=True, minute=1), Item("GAP::OTHER", blocking=False, minute=1)])
    port = OpenGaps(lambda: Runner(gaps))

    shown = [
        Item("GAP::1", blocking=True, minute=1, about_spec_ids=("REQ::B", "REQ::A")),
        Item("GAP::2", blocking=False, minute=1),
    ]

    assert await port.severities("p", shown) == {"GAP::1": "blocker"}
    assert ("list_gaps", ("REQ::A", "REQ::B")) in gaps.calls
    assert await port.severities("p", []) == {}
