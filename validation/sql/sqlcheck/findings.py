"""The two result types every step returns: one `Finding` per reported location and one
`StepResult` per step. Exit codes: 0 clean, 1 findings, 2 a tool or configuration error."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

EXIT_OK, EXIT_FINDINGS, EXIT_TOOL_ERROR = 0, 1, 2


@dataclass(frozen=True)
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
        parts = [
            part
            for part in (
                f"fix: {self.fix}" if self.fix else "",
                f"doc: {self.doc}" if self.doc else "",
            )
            if part
        ]
        suffix = f" ({'; '.join(parts)})" if parts else ""
        return f"{self.path}:{self.line}:{self.col}: {self.rule}: {self.message}{suffix}"


@dataclass
class StepResult:
    """One step's outcome. A skip always carries a `detail` that says why."""

    step: str
    exit_code: int
    findings: list[Finding] = field(default_factory=list)
    detail: str = ""

    @classmethod
    def ok(cls, step: str, detail: str = "") -> StepResult:
        return cls(step, EXIT_OK, [], detail)

    @classmethod
    def tool_error(cls, step: str, detail: str) -> StepResult:
        return cls(step, EXIT_TOOL_ERROR, [], detail)

    @classmethod
    def from_findings(cls, step: str, findings: list[Finding], detail: str = "") -> StepResult:
        ordered = sorted(findings, key=lambda f: (f.path, f.line, f.col, f.rule))
        return cls(step, EXIT_FINDINGS if ordered else EXIT_OK, ordered, detail)

    @property
    def status(self) -> str:
        if self.exit_code == EXIT_TOOL_ERROR:
            return "tool-error"
        if self.exit_code == EXIT_FINDINGS:
            return "findings"
        if self.detail.startswith("skipped:"):
            return "skipped"
        return "degraded" if self.detail.startswith("degraded:") else "ok"

    def render(self) -> str:
        head = f"==> {self.step}: {self.status}" + (f" ({self.detail})" if self.detail else "")
        return "\n".join([head, *(f.render() for f in self.findings)])

    def to_json(self) -> dict[str, object]:
        return {
            "step": self.step,
            "status": self.status,
            "exit_code": self.exit_code,
            "detail": self.detail,
            "findings": [asdict(f) for f in self.findings],
        }


def overall_exit(results: list[StepResult]) -> int:
    """The worst exit code across steps: a tool error outranks findings."""
    return max((r.exit_code for r in results), default=EXIT_OK)


def dump_json(results: list[StepResult]) -> str:
    return json.dumps([r.to_json() for r in results], indent=2)
