"""Support of the tier-2 API Temporal tests (no tests here).

``QuickWorkflow`` is a stand-in that completes at once. It lives in its own module so the
workflow sandbox imports nothing but ``temporalio``. ``api_runtime`` builds the API
``Runtime`` on a real client (pydantic data converter, like the API's own) with the
test's UNIQUE id prefix, so every workflow id and every task queue it uses is prefixed
``pytest-<run>-``: the owner's ``parse`` queue is never touched.
"""

from __future__ import annotations

import os
from contextlib import suppress
from typing import Any

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.service import RPCError

from api.runtime import Runtime
from settings import load_settings
from specflow_live import LOCAL_NAMESPACE, TemporalLive


async def connect_with_converter() -> Client:
    return await Client.connect(
        os.environ.get("TEMPORAL_ADDRESS", "localhost:7233").strip(),
        namespace=LOCAL_NAMESPACE,
        data_converter=pydantic_data_converter,
    )


def api_runtime(client: Client, live: TemporalLive, valid_env: dict[str, str]) -> Runtime:
    settings = load_settings(
        "api",
        {
            **valid_env,
            "TEMPORAL_ADDRESS": os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"),
        },
    )
    return Runtime(
        settings=settings,
        session_factory=None,
        store=None,
        temporal=client,
        driver=None,
        readiness=None,
        id_prefix=live.id_prefix,
    )


async def terminate_prefixed(client: Client, prefix: str) -> int:
    """Terminate every still-running workflow whose id starts with ``prefix`` (teardown)."""

    tried = 0
    query = f'WorkflowId STARTS_WITH "{prefix}" AND ExecutionStatus = "Running"'
    with suppress(RPCError):  # a server without the visibility query: the tracked handles remain
        async for found in client.list_workflows(query):
            tried += 1
            with suppress(RPCError):
                await client.get_workflow_handle(found.id).terminate("pytest teardown")
    return tried


def handle_of(client: Client, live: TemporalLive, workflow_id: str) -> Any:
    """A tracked handle: ``live.cleanup`` terminates it at teardown."""

    return live.track(client.get_workflow_handle(workflow_id))
