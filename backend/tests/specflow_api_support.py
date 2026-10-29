"""Shared offline fixtures for the specflow API tests (no tests in here).

An in-memory repository stands in for ``db.repositories`` (the real
queries are covered by the tier-2 tests), the object store is the S3 memory
store, Temporal and Neo4j are small fakes, and tokens are real dev-mode JWTs
checked by the real ``DevTokenVerifier``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import httpx
import jwt
import pytest_asyncio
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from api.app import create_app
from api.runtime import Runtime
from health import Readiness
from settings import RoleSettings, load_settings
from specflow_batches_support import FakeBatches
from storage.memory import InMemoryObjectStore
from test_specflow_api_fakes import FakeRepo

DEV_KEY = "k" * 40
PDF = b"%PDF-1.4\n" + b"synthetic body\n" * 20


def token(email: str = "alice@example.com", sub: str | None = None, **claims: Any) -> str:
    now = int(time.time())
    payload = {
        "iss": "specflow-dev-login",
        "aud": "specflow-api",
        "azp": "specflow-web",
        "typ": "Bearer",
        "sub": sub or f"dev:{email}",
        "email": email,
        "email_verified": True,
        "name": email.split("@", maxsplit=1)[0].title(),
        "iat": now,
        "exp": now + 300,
    }
    payload.update(claims)
    return jwt.encode({k: v for k, v in payload.items() if v is not None}, DEV_KEY, "HS256")


def bearer(email: str = "alice@example.com", **claims: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {token(email, **claims)}"}


@dataclass
class FakeTemporal:
    """Records started executions; ``fail`` makes the next starts raise.

    Like the server, a start whose workflow id is already running returns the
    existing handle (``USE_EXISTING``) and starts nothing; ``calls`` therefore
    holds one entry per execution and ``attempts`` counts every start call.
    """

    calls: list[dict[str, Any]] = field(default_factory=list)
    attempts: int = 0
    fail: Exception | None = None
    terminated: list[str] = field(default_factory=list)  # workflow ids, in order
    terminate_error: Exception | None = None  # raised by every terminate
    describe_error: Exception | None = None
    healthy: bool = True
    service_client: Any = None

    def __post_init__(self) -> None:
        async def check_health(**_: Any) -> bool:
            return self.healthy

        self.service_client = SimpleNamespace(check_health=check_health)

    async def start_workflow(self, workflow: str, arg: Any, **options: Any):
        if self.fail is not None:
            raise self.fail
        self.attempts += 1
        if any(call["id"] == options["id"] for call in self.calls):
            if options.get("id_conflict_policy") != WorkflowIDConflictPolicy.USE_EXISTING:
                raise WorkflowAlreadyStartedError(options["id"], workflow, run_id="existing-run")
            return SimpleNamespace(id=options["id"])
        self.calls.append({"workflow": workflow, "arg": arg, **options})
        return SimpleNamespace(id=options["id"])

    def get_workflow_handle(self, workflow_id: str, **_: Any):
        """A handle with ``describe`` and ``terminate``; a workflow is known once started."""

        def known() -> bool:
            return any(call["id"] == workflow_id for call in self.calls)

        async def describe():
            if self.describe_error is not None:
                raise self.describe_error
            if not known():
                raise RPCError("workflow not found", RPCStatusCode.NOT_FOUND, b"")
            return SimpleNamespace(status=SimpleNamespace(name="RUNNING"))

        async def terminate(*_: Any, reason: str | None = None, **__: Any):
            if self.terminate_error is not None:
                raise self.terminate_error
            if not known():
                raise RPCError("workflow not found", RPCStatusCode.NOT_FOUND, b"")
            self.terminated.append(workflow_id)

        return SimpleNamespace(id=workflow_id, describe=describe, terminate=terminate)


class FakeDriver:
    """The Neo4j driver the runtime holds; no request handler may use it (only ``/readyz``)."""

    def __init__(self) -> None:
        self.queries = 0

    async def execute_query(self, *_args: Any, **_kwargs: Any):
        self.queries += 1
        raise ConnectionError("graph down")


@dataclass
class Harness:
    app: Any
    client: httpx.AsyncClient
    repo: FakeRepo
    store: InMemoryObjectStore
    temporal: FakeTemporal
    driver: FakeDriver
    runtime: Runtime
    probes: dict[str, Any]
    batches: FakeBatches | None = None  # the live tier has no in-memory model


def api_settings(valid_env: dict[str, str]) -> RoleSettings:
    return load_settings("api", valid_env)


@pytest_asyncio.fixture
async def harness(monkeypatch, valid_env):
    fake = FakeRepo()
    fake.install(monkeypatch)
    batches = FakeBatches(fake)
    batches.install(monkeypatch)
    store, temporal, driver = (
        InMemoryObjectStore(app_env="local"),
        FakeTemporal(),
        FakeDriver(),
    )
    settings = api_settings(valid_env)
    probes: dict[str, Any] = {}

    async def ok() -> None:
        return None

    for name in ("database", "temporal", "storage", "neo4j"):
        probes[name] = ok

    async def factory(settings: RoleSettings) -> Runtime:
        return Runtime(
            settings=settings,
            session_factory=fake,
            store=store,
            temporal=temporal,
            driver=driver,
            readiness=Readiness(probes, ttl=0),
        )

    app = create_app(settings, runtime_factory=factory)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield Harness(
                app,
                client,
                fake,
                store,
                temporal,
                driver,
                app.state.runtime,
                probes,
                batches,
            )
