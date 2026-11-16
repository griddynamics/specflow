"""The relational port contract (``gap_records_contract``) on the in-memory fakes: FR-GOWN-02, FR-GOWN-04.

The same cases run on the PostgreSQL adapters in ``test_api_gap_records_live.py``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import get_args

import pytest

from db import models as db_models
from gap_port_fakes import MemoryAmendments, MemoryGapRecords
from gap_records_contract import CASES, Backend, Case
from graph.gap import names
from graph.gap.models import EventRow, NoiseRuleRow, NoteRow, RunRow, SeedRow, SiteRow
from graph.gap.settings import OPTIONS_CEILING


class MemoryProbe:
    def __init__(self, records: MemoryGapRecords) -> None:
        self.records = records

    async def runs(self, project_id: str) -> Sequence[RunRow]:
        return tuple(self.records.runs[project_id].values())

    async def seeds(self, project_id: str) -> Sequence[SeedRow]:
        return tuple(self.records.seeds[project_id])

    async def sites(self, project_id: str) -> Sequence[SiteRow]:
        return tuple(self.records.sites[project_id])

    async def notes(self, project_id: str) -> Sequence[NoteRow]:
        return tuple(self.records.notes[project_id])

    async def events(self, project_id: str) -> Sequence[EventRow]:
        return tuple(self.records.events[project_id])

    async def add_noise_rule(self, project_id: str, row: NoiseRuleRow) -> None:
        self.records.noise[project_id].append(row)


@pytest.mark.parametrize("case", CASES, ids=[case.__name__ for case in CASES])
async def test_fr_gown_02_the_fakes_keep_the_relational_port_contract(case: Case) -> None:
    records = MemoryGapRecords()
    await case(Backend(records, MemoryAmendments(), MemoryProbe(records), "project-a", "project-b"))


def test_the_gap_table_checks_spell_the_gap_vocabularies() -> None:
    """``db.models`` writes the vocabularies out (db does not import graph); they stay equal to graph.gap.names."""

    pairs = {
        db_models.GAP_RUN_MODES: names.RunMode,
        db_models.GAP_RUN_STATUSES: names.RunStatus,
        db_models.GAP_SEED_STRENGTHS: names.SeedStrength,
        db_models.GAP_RELEVANCES: names.Relevance,
        db_models.GAP_SITE_OUTCOMES: names.SiteOutcome,
        db_models.GAP_DECIDED_BY: names.DecidedBy,
        db_models.GAP_NOISE_SCOPES: names.NoiseScope,
        db_models.AMENDMENT_OPERATIONS: names.AmendmentOperation,
        db_models.AMENDMENT_STATUSES: names.AmendmentStatus,
    }
    for spelled, literal in pairs.items():
        assert spelled == get_args(literal)
    assert db_models.GAP_OPTIONS_MAX == OPTIONS_CEILING
