"""Shared tier-2 fixture: the API on a real, migrated scratch database (no tests in here).

Object storage is the in-memory store and Temporal is a fake. ``live`` is registered for the
whole suite through ``conftest.pytest_plugins``.
"""

from __future__ import annotations

from dataclasses import replace

import httpx
import pytest_asyncio

from api.app import create_app
from api.runtime import Runtime
from db.session import build_engine, build_session_factory
from health import Readiness
from settings import load_settings
from specflow_api_support import FakeDriver, FakeTemporal, Harness
from storage.memory import InMemoryObjectStore


async def open_harness(migrated_database, valid_env, temporal, id_prefix=""):
    engine = build_engine(migrated_database)
    factory = build_session_factory(engine)
    settings = replace(load_settings("api", valid_env), database=migrated_database)
    store, driver = InMemoryObjectStore(app_env="local"), FakeDriver()

    async def ok() -> None:
        return None

    async def build(settings):
        return Runtime(
            settings=settings,
            session_factory=factory,
            store=store,
            temporal=temporal,
            driver=driver,
            readiness=Readiness({"database": ok}, ttl=0),
            id_prefix=id_prefix,
        )

    app = create_app(settings, runtime_factory=build)
    return engine, factory, store, app


@pytest_asyncio.fixture
async def live(migrated_database, valid_env):
    temporal = FakeTemporal()
    engine, factory, store, app = await open_harness(migrated_database, valid_env, temporal)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield Harness(
                app,
                client,
                factory,
                store,
                temporal,
                FakeDriver(),
                app.state.runtime,
                {},
            )
    await engine.dispose()
