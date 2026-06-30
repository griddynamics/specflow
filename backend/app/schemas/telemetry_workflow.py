"""Telemetry workflow label — structured input for ``TelemetryContext.set_workflow``."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, model_validator


class PhaseKind(str, Enum):
    """Codegen vs deploy/QA phase loops (telemetry dimension)."""

    GENERATION = "generation"
    DEPLOY = "deploy"


class TelemetryWorkflowLabel(BaseModel):
    """Describes the workflow dimension stored in context: a plain name or a phased agent role."""

    model_config = ConfigDict(frozen=True)

    label: str | None = None
    phase_kind: PhaseKind | None = None
    phase_num: int | None = None
    agent_role: str | None = None

    @model_validator(mode="after")
    def _exactly_one_shape(self) -> TelemetryWorkflowLabel:
        has_plain = self.label is not None
        has_any_phase = any(
            x is not None for x in (self.phase_kind, self.phase_num, self.agent_role)
        )
        if has_plain and has_any_phase:
            raise ValueError("Use either label or phase fields, not both")
        if has_plain:
            if not self.label:
                raise ValueError("label cannot be empty")
            return self
        if has_any_phase:
            if self.phase_kind is None or self.phase_num is None or self.agent_role is None:
                raise ValueError("phase_kind, phase_num, and agent_role must be set together")
            return self
        raise ValueError("Either label or all phase fields must be set")

    @classmethod
    def plain(cls, label: str) -> TelemetryWorkflowLabel:
        """A single workflow name (e.g. ``WorkflowName.PLANNING`` value)."""
        return cls(label=label)

    @classmethod
    def phase(cls, phase_kind: PhaseKind, phase_num: int, agent_role: str) -> TelemetryWorkflowLabel:
        """``{phase_kind}_phase_{phase_num}_{agent_role}`` for PostHog ``workflow`` string."""
        return cls(phase_kind=phase_kind, phase_num=phase_num, agent_role=agent_role)

    def to_stored_string(self) -> str:
        if self.label is not None:
            return self.label
        if self.phase_kind is None or self.phase_num is None or self.agent_role is None:
            raise ValueError(
                f"TelemetryWorkflowLabel has no label and incomplete phase fields "
                f"(phase_kind={self.phase_kind!r}, phase_num={self.phase_num!r}, agent_role={self.agent_role!r})"
            )
        return f"{self.phase_kind.value}_phase_{self.phase_num}_{self.agent_role}"

    def __str__(self) -> str:
        # Pydantic's default __str__ is the verbose model repr ("label=... phase_kind=None ...").
        # The canonical string form of a workflow label is its stored-string representation.
        return self.to_stored_string()
