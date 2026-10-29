"""Smoke test for a Temporal deployment: one hello workflow and one activity.

Run from the repository root with the local env file:

    uv --directory backend run --env-file ../.env.local python examples/temporal_hello.py

Worker and client share one process. Reads TEMPORAL_ADDRESS and
TEMPORAL_NAMESPACE, and, when set, TEMPORAL_API_KEY and TEMPORAL_TLS_CERT /
TEMPORAL_TLS_KEY (a file path or the PEM text). The workflow id and the task
queue are unique per run (``hello-<uuid>``), so the run never collides with
application queues. Exits non-zero when anything fails.
"""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.service import TLSConfig
from temporalio.worker import Worker

# The workflow sandbox re-imports this module; the logger and example helpers are
# not workflow code, so they pass through instead of being re-imported.
with workflow.unsafe.imports_passed_through():
    from example_lib import run_example
    from logger.logger import INFO, get_logger, log_event

logger = get_logger("examples.temporal_hello")

RUN_TIMEOUT = timedelta(seconds=30)


@activity.defn
async def greet(name: str) -> str:
    return f"Hello, {name}!"


@workflow.defn
class HelloWorkflow:
    @workflow.run
    async def run(self, name: str) -> str:
        return await workflow.execute_activity(greet, name, start_to_close_timeout=timedelta(seconds=10))


def _pem(value: str) -> bytes:
    path = Path(value)
    return path.read_bytes() if path.is_file() else value.encode()


def _settings() -> tuple[str, str, str | None, TLSConfig | None]:
    missing = [name for name in ("TEMPORAL_ADDRESS", "TEMPORAL_NAMESPACE") if not os.environ.get(name, "").strip()]
    if missing:
        raise RuntimeError(f"Missing {', '.join(missing)}; run with `--env-file ../.env.local`.")
    cert = os.environ.get("TEMPORAL_TLS_CERT", "").strip()
    key = os.environ.get("TEMPORAL_TLS_KEY", "").strip()
    if bool(cert) != bool(key):
        raise RuntimeError("Set both TEMPORAL_TLS_CERT and TEMPORAL_TLS_KEY, or neither.")
    tls = TLSConfig(client_cert=_pem(cert), client_private_key=_pem(key)) if cert else None
    api_key = os.environ.get("TEMPORAL_API_KEY", "").strip() or None
    return os.environ["TEMPORAL_ADDRESS"], os.environ["TEMPORAL_NAMESPACE"], api_key, tls


async def main() -> int:
    address, namespace, api_key, tls = _settings()
    client = await Client.connect(address, namespace=namespace, api_key=api_key, tls=tls or bool(api_key))
    run_id = uuid4().hex[:12]
    task_queue = f"hello-{run_id}"
    workflow_id = f"hello-{run_id}"
    log_event(
        logger,
        INFO,
        "example.temporal_hello.start",
        address=address,
        namespace=namespace,
        task_queue=task_queue,
        workflow_id=workflow_id,
        auth="api-key" if api_key else "none",
        tls=tls is not None,
    )
    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[HelloWorkflow],
        activities=[greet],
    ):
        result = await client.execute_workflow(
            HelloWorkflow.run,
            "Temporal",
            id=workflow_id,
            task_queue=task_queue,
            run_timeout=RUN_TIMEOUT,
        )
    log_event(
        logger,
        INFO,
        "example.temporal_hello.done",
        workflow_id=workflow_id,
        task_queue=task_queue,
        result=result,
    )
    print(f"workflow_id={workflow_id} result={result}")
    return 0 if result == "Hello, Temporal!" else 1


if __name__ == "__main__":
    raise SystemExit(run_example(main))
