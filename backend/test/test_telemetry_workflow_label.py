"""TelemetryWorkflowLabel validation."""

import pytest
from pydantic import ValidationError

from app.schemas.telemetry_workflow import PhaseKind, TelemetryWorkflowLabel


def test_plain_and_phase_stored_strings() -> None:
    assert TelemetryWorkflowLabel.plain("planning").to_stored_string() == "planning"
    assert (
        TelemetryWorkflowLabel.phase(PhaseKind.GENERATION, 1, "coding").to_stored_string()
        == "generation_phase_1_coding"
    )


def test_rejects_incomplete_phase_fields() -> None:
    with pytest.raises(ValidationError):
        TelemetryWorkflowLabel(phase_kind=PhaseKind.GENERATION, phase_num=1, agent_role=None)


def test_rejects_label_plus_phase_mix() -> None:
    with pytest.raises(ValidationError):
        TelemetryWorkflowLabel(label="x", phase_kind=PhaseKind.GENERATION)
