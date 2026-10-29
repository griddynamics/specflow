"""Helpers of the settings/live-helper tests (no tests in here)."""

from __future__ import annotations

import re
from pathlib import Path

ENV_EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"
NAME = re.compile(r"^#?\s?([A-Z][A-Z0-9_]*)=(.*)$")


def example_entries() -> dict[str, str]:
    entries = {}
    for line in ENV_EXAMPLE.read_text().splitlines():
        match = NAME.match(line)
        if match:
            entries[match.group(1)] = match.group(2)
    return entries


class FakeHandle:
    def __init__(self, workflow_id: str, error: Exception | None = None) -> None:
        self.id = workflow_id
        self.error = error
        self.reasons: list[str] = []

    async def terminate(self, reason: str) -> None:
        self.reasons.append(reason)
        if self.error:
            raise self.error


class FakeClient:
    def __init__(self) -> None:
        self.started: list[tuple] = []

    async def start_workflow(self, workflow, *args, **options):
        workflow_id = options.pop("id")
        self.started.append((workflow, args, workflow_id, options))
        return FakeHandle(workflow_id)
