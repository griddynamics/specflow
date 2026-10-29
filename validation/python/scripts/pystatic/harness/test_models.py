from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pydantic
import pytest

from pystatic.harness import models
from pystatic.harness.models import SUMMARY_PATH


class SummaryModelTests(unittest.TestCase):
    def test_valid_entry_round_trips(self) -> None:
        entry = {
            "unit": "api",
            "step": "ruff",
            "exit": 1,
            "status": "findings",
            "duration_ms": 12,
            "findings": 3,
        }
        validated = models.SummaryModel.model_validate([entry])
        assert validated.root[0].step == "ruff"

    def test_unknown_status_is_rejected(self) -> None:
        entry = {
            "unit": "api",
            "step": "ruff",
            "exit": 1,
            "status": "bogus",
            "duration_ms": 1,
            "findings": 0,
        }
        with pytest.raises(pydantic.ValidationError):
            models.SummaryModel.model_validate([entry])

    def test_extra_field_is_rejected(self) -> None:
        entry = {
            "unit": "api",
            "step": "ruff",
            "exit": 0,
            "status": "ok",
            "duration_ms": 1,
            "findings": 0,
            "surprise": True,
        }
        with pytest.raises(pydantic.ValidationError):
            models.SummaryModel.model_validate([entry])

    def test_web_entry_shape_without_status_validates(self) -> None:
        """A producer that tags a `workspace` and writes no `status` field at
        all: the schema must accept a foreign producer's entries."""
        entry = {
            "unit": "frontend",
            "workspace": "web",
            "step": "prettier",
            "exit": 0,
            "duration_ms": 12,
            "findings": 0,
        }
        validated = models.SummaryModel.model_validate([entry])
        assert validated.root[0].status is None
        assert validated.root[0].workspace == "web"

    def test_api_entry_has_no_workspace(self) -> None:
        entry = {
            "unit": "api",
            "step": "ruff",
            "exit": 0,
            "status": "ok",
            "duration_ms": 1,
            "findings": 0,
        }
        validated = models.SummaryModel.model_validate([entry])
        assert validated.root[0].workspace is None


class StepReportModelTests(unittest.TestCase):
    def test_findings_default_empty(self) -> None:
        report = models.StepReportModel(unit="api", step="ruff", exit=0, status="ok", duration_ms=1)
        assert report.findings == []

    def test_finding_shape(self) -> None:
        # `model_validate` (not the constructor) on purpose: this proves a
        # plain dict -- the shape a real `.reports/*.json` round-trip reads
        # back -- coerces into a `Finding`, which `model_validate`'s `Any`
        # parameter lets mypy see without a cast or type suppression.
        report = models.StepReportModel.model_validate(
            {
                "unit": "api",
                "step": "ruff",
                "exit": 1,
                "status": "findings",
                "duration_ms": 1,
                "findings": [{"path": "a.py", "line": 1, "col": 1, "rule": "r", "message": "m"}],
            }
        )
        assert report.findings[0].path == "a.py"
        assert report.findings[0].fix == ""


class WriteSummarySchemaTests(unittest.TestCase):
    def test_writes_a_json_schema_array_of_the_summary_entry_shape(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            orig = models.SUMMARY_SCHEMA_PATH
            models.SUMMARY_SCHEMA_PATH = Path(td) / "build" / "summary.schema.json"
            try:
                models.write_summary_schema()
                schema = json.loads(models.SUMMARY_SCHEMA_PATH.read_text())
            finally:
                models.SUMMARY_SCHEMA_PATH = orig
        assert schema["type"] == "array"
        item_props = schema["$defs"]["SummaryEntryModel"]["properties"]
        assert set(item_props) == {
            "unit",
            "workspace",
            "step",
            "exit",
            "status",
            "duration_ms",
            "findings",
            "detail",
        }


class SummaryEntryDetailTests(unittest.TestCase):
    """A producer may attach a free-text `detail` to a summary entry."""

    def test_an_entry_carrying_a_detail_validates(self) -> None:
        entries = [
            {
                "unit": "python",
                "step": "ruff",
                "exit": 0,
                "status": "ok",
                "duration_ms": 12,
                "findings": 0,
                "detail": "ran on 3 targets",
            },
            {
                "unit": "python",
                "step": "mypy",
                "exit": 0,
                "status": "skipped",
                "duration_ms": 0,
                "findings": 0,
                "detail": "skipped: no targets",
            },
        ]

        validated = models.SummaryModel.model_validate(entries).root

        assert [entry.detail for entry in validated] == [
            "ran on 3 targets",
            "skipped: no targets",
        ]

    def test_an_entry_without_a_detail_has_none(self) -> None:
        entry = models.SummaryEntryModel.model_validate(
            {"unit": "api", "step": "ruff", "exit": 0, "duration_ms": 1, "findings": 0}
        )

        assert entry.detail is None

    def test_an_unknown_field_is_still_refused(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            models.SummaryEntryModel.model_validate(
                {
                    "unit": "api",
                    "step": "ruff",
                    "exit": 0,
                    "duration_ms": 1,
                    "findings": 0,
                    "note": "x",
                }
            )


FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "summary_sample.json"


class RealSummaryJsonTests(unittest.TestCase):
    """The published schema validates a real entry from every producer, unconditionally.

    Covers the kit's `python` unit and a foreign `frontend` producer that omits `status`,
    without depending on an existing `.reports/summary.json`."""

    def test_fixture_summary_matches_schema(self) -> None:
        data = json.loads(FIXTURE_PATH.read_text())
        assert {e["unit"] for e in data} == {"python", "frontend"}
        models.SummaryModel.model_validate(data)  # raises on any shape drift

    def test_python_entries_always_set_status(self) -> None:
        # `status` is `Status | None` only because a foreign producer may
        # omit it -- the unit this kit's own runner writes (python) must
        # never omit it, or the skip-is-machine-readable contract regresses.
        validated = models.SummaryModel.model_validate(json.loads(FIXTURE_PATH.read_text())).root
        for entry in validated:
            if entry.unit == "python":
                assert entry.status is not None, f"{entry.unit}/{entry.step} has no status"

    def test_whole_summary_file_matches_schema_when_present(self) -> None:
        # an additional, stronger assertion when this checkout already has
        # a real `.reports/summary.json` -- never the only proof (see the
        # fixture test above), and never a skip when it is absent.
        if not SUMMARY_PATH.exists():
            return
        data = json.loads(SUMMARY_PATH.read_text())
        models.SummaryModel.model_validate(data)  # raises on any shape drift


if __name__ == "__main__":
    unittest.main()
