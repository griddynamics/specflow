"""A stand-in workflow that completes at once (no tests here).

It lives in a module of its own so the workflow sandbox re-imports nothing but
``temporalio`` (the support module pulls in pydantic and the API runtime, whose
subclasses of proxied objects the sandbox rejects).
"""

from __future__ import annotations

from temporalio import workflow


@workflow.defn(name="PytestQuickWorkflow")
class QuickWorkflow:
    @workflow.run
    async def run(self) -> str:
        return "done"
