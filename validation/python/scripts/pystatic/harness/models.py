"""Shared paths plus the runner-contract types, declared
once: `Finding`/`StepResult` are Pydantic dataclasses (validated, and still
positional-constructible the way every step builds them), so `StepReportModel`
holds them directly -- no second `FindingModel` twin, no `__dict__` marshalling."""

from __future__ import annotations

import json
import os
from dataclasses import field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, RootModel
from pydantic.dataclasses import dataclass as pydantic_dataclass

# The tree a run reports on: the working directory, or `PYSTATIC_REPO_ROOT`.
REPO_ROOT = Path(os.environ.get("PYSTATIC_REPO_ROOT", Path.cwd()))
REPORTS_DIR = Path(os.environ.get("PYSTATIC_REPORTS_ROOT", REPO_ROOT / ".reports"))
SUMMARY_PATH = REPORTS_DIR / "summary.json"
BUILD_DIR = REPO_ROOT / "build"
SUMMARY_SCHEMA_PATH = BUILD_DIR / "summary.schema.json"

Status = Literal["ok", "findings", "skipped", "tool-error"]
EXIT_OK, EXIT_FINDINGS, EXIT_TOOL_ERROR = 0, 1, 2


@pydantic_dataclass
class Finding:
    """One reported line: `path:line:col: rule: message (fix: ...; doc: ...)`."""

    path: str
    line: int
    col: int
    rule: str
    message: str
    fix: str = ""
    doc: str = ""

    def render(self) -> str:
        parts = (f"fix: {self.fix}" if self.fix else "", f"doc: {self.doc}" if self.doc else "")
        tail = [t for t in parts if t]
        suffix = f" ({'; '.join(tail)})" if tail else ""
        return f"{self.path}:{self.line}:{self.col}: {self.rule}: {self.message}{suffix}"


@pydantic_dataclass
class StepResult:
    """One step's outcome: exit 0 clean, 1 findings, 2 tool/config error."""

    step: str
    exit_code: int
    findings: list[Finding] = field(default_factory=list)
    duration_ms: int = 0
    detail: str = ""  # "skipped: ..." -- a skip is never silent
    raw_output: str = ""
    sarif: str = ""  # a tool's own SARIF payload, persisted as `<step>.sarif`

    @classmethod
    def ok(cls, step: str, detail: str = "") -> StepResult:
        return cls(step=step, exit_code=EXIT_OK, detail=detail)

    @classmethod
    def step_missing(cls, step: str, path: str) -> StepResult:
        return cls(step=step, exit_code=EXIT_TOOL_ERROR, detail=f"step-missing: {path}")

    @classmethod
    def tool_missing(cls, step: str, tool: str) -> StepResult:
        return cls(step=step, exit_code=EXIT_TOOL_ERROR, detail=f"tool-missing: {tool}")

    @classmethod
    def from_findings(cls, step: str, findings: list[Finding]) -> StepResult:
        return cls(step=step, exit_code=EXIT_FINDINGS if findings else EXIT_OK, findings=findings)


class StepReportModel(BaseModel):
    """The shape of `.reports/<unit>/<step>.json`."""

    model_config = ConfigDict(extra="forbid")
    unit: str
    step: str
    exit: int
    status: Status
    duration_ms: int
    findings: list[Finding] = []
    detail: str = ""


class SummaryEntryModel(BaseModel):
    """One entry of `.reports/summary.json` (a count, not the findings). `workspace`/
    `status` are optional: a producer in another language may omit them."""

    model_config = ConfigDict(extra="forbid")
    unit: str
    workspace: str | None = None
    step: str
    exit: int
    status: Status | None = None
    duration_ms: int
    findings: int
    # Optional free-text note a producer may attach.
    detail: str | None = None


class SummaryModel(RootModel[list[SummaryEntryModel]]):
    """`.reports/summary.json` is a plain JSON array, not `{"entries": [...]}`."""


def write_summary_schema() -> None:
    schema = SummaryModel.model_json_schema()
    SUMMARY_SCHEMA_PATH.parent.mkdir(parents=True, exist_ok=True)
    SUMMARY_SCHEMA_PATH.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")
