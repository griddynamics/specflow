"""Development gaps over the agent routes, offline (docs/graph/gap/lifecycle.md "Development gaps", "Plan link"):
the helpers of ``test_api_gap_development`` and the acceptance tests FR-GDEV-01 to 06 and FR-GPLN-03/04.

``dev_rig`` is ``gap_agent_support.serve_gaps`` with ``run_sites``: the shop gap corpus after a gap run, the claim
tests' mini plan whose ``TASK::A::API_LOGIC`` (story ``STORY::A::API``) implements ``REQ::FR::PLACE_ORDER``, the host's
``PlanHolds`` over that plan, and a runner whose ``start_site`` runs the site run at once with ``OneReport``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from agent_contracts import AGENT_ROUTES
from gap_acceptance_support import Planted
from gap_agent_support import ALPHA, API_LOGIC, API_STORY, GapRig, serve_gaps
from gap_port_fakes import MemoryBuildRequests
from graph.gap.models import Edit, EditTarget, GapReport, InvestigationResult, ReportOption, WitnessRef
from specflow_api_agent_support import agent_env

__all__ = ["ALPHA", "API_LOGIC", "API_STORY", "GapRig"]

ADD = AGENT_ROUTES["add_finding"]
CLAIM = AGENT_ROUTES["claim_story"]
GAPS = AGENT_ROUTES["list_gaps"]
API_CONTRACT = "TASK::A::API_CONTRACT"
REFUND_TO_CARD = "REQ::FR::REFUND_TO_ORIGINAL_CARD"
REFUNDS = "upload:payments-spec#payments-service-specification/refunds#56c00bc8"
QUESTION = "How long does a refund to the original card take for a placed order?"
MEMBER = "user-member-2"


class OneReport:
    """An ``Investigator`` answering every site with ``report`` (a site run investigates one site), no model call."""

    prompt_version = "development.1"

    def __init__(self, report: GapReport) -> None:
        self.report = report
        self.calls = 0

    async def investigate(self, pack: Any, reads: Any, budget: Any) -> InvestigationResult:
        del pack, reads, budget
        self.calls += 1
        return InvestigationResult(report=self.report, model_calls=1)


def report(**changes: Any) -> GapReport:
    """A minor ``missing`` report about Place order, every gate's evidence present: two options, A recommended."""

    document = Edit(
        placement="document",
        target=EditTarget(heading="Place order"),
        operation="append_section",
        text="A refund to the original card takes at most 5 business days.",
    )
    values: dict[str, Any] = {
        "verdict": "gap",
        "kind": "missing",
        "severity": "minor",
        "title": "Refund time to the card",
        "question": "How long does a refund take?",
        "why": "No line states the refund time.",
        "about": (Planted.PLACE_ORDER,),
        "witness": (WitnessRef(fact=0),),
        "options": (
            ReportOption(
                label="5 business days",
                recommended=True,
                edits=(document,),
            ),
            ReportOption(
                label="10 business days",
                recommended=False,
                edits=(document.model_copy(update={"text": "A refund takes at most 10 business days."}),),
            ),
        ),
    }
    return GapReport.model_validate(values | changes)


async def claim(rig: GapRig) -> str:
    response = await rig.rig.post(CLAIM, {"story_id": API_STORY}, project_id=ALPHA)
    assert response.status_code == 200, response.text
    token: str = response.json()["claim_token"]
    return token


async def report_blocking(
    rig: GapRig,
    token: str,
    kind: str = "question",
    text: str = QUESTION,
    about: tuple[str, ...] = (Planted.PLACE_ORDER,),
) -> dict[str, Any]:
    body = {
        "kind": kind,
        "text": text,
        "about": list(about),
        "story_id": API_STORY,
        "claim_token": token,
        "blocking": True,
        "refers_to": [{"type": "commit", "value": "ABCDEF1"}],
    }
    response = await rig.rig.post(ADD, body, project_id=ALPHA)
    assert response.status_code == 200, response.text
    found: dict[str, Any] = response.json()
    return found


def task(rig: GapRig, task_id: str) -> dict[str, Any]:
    found: dict[str, Any] = rig.store.tasks[task_id]
    return found


@asynccontextmanager
async def dev_rig(monkeypatch: pytest.MonkeyPatch, valid_env: dict[str, str]) -> AsyncIterator[GapRig]:
    async with serve_gaps(monkeypatch, agent_env(valid_env), run_sites=True, builds=MemoryBuildRequests()) as rig:
        rig.world.investigator = OneReport(report())
        yield rig
