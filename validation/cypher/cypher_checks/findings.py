"""One reported defect and the exit-code contract every check shares.

A finding renders as `path:line:col: rule: message (fix: ...; doc: ...)`; a run exits 0 when
clean, 1 with findings and 2 on a tool or configuration error.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TextIO

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2


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
        parts = (f"fix: {self.fix}" if self.fix else "", f"doc: {self.doc}" if self.doc else "")
        tail = [t for t in parts if t]
        suffix = f" ({'; '.join(tail)})" if tail else ""
        return f"{self.path}:{self.line}:{self.col}: {self.rule}: {self.message}{suffix}"


def sort_key(finding: Finding) -> tuple[str, int, int]:
    return finding.path, finding.line, finding.col


def report(findings: Iterable[Finding], stream: TextIO | None = None) -> int:
    """Write every finding, one per line, and return the run's exit code."""
    out = sys.stdout if stream is None else stream
    ordered = sorted(findings, key=sort_key)
    for finding in ordered:
        out.write(finding.render() + "\n")
    return EXIT_FINDINGS if ordered else EXIT_OK
