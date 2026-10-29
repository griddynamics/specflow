"""The pytest plugin behind `archkit run`: loads the configuration and serves the fixtures.

Loaded with `-p archkit.plugin` only; the kit registers no `pytest11` entry point, so an
adopter's own test run never picks these fixtures up by accident. Every fixture name
starts with `archkit_` for the same reason."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

from archkit.config import (
    Config,
    ConfigError,
    install_python_path,
    load_config,
    require_checks,
    resolve,
)
from archkit.support import http_table

CONFIG_KEY = pytest.StashKey[Config]()


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("archkit")
    group.addoption(
        "--archkit-config",
        default=os.environ.get("ARCHKIT_CONFIG", "archkit.toml"),
        help="the adopter's archkit TOML configuration (default: $ARCHKIT_CONFIG or archkit.toml)",
    )


def pytest_configure(config: pytest.Config) -> None:
    try:
        loaded = load_config(Path(config.getoption("--archkit-config")))
        require_checks(loaded)
    except ConfigError as error:
        raise pytest.UsageError(str(error)) from error
    install_python_path(loaded)
    config.stash[CONFIG_KEY] = loaded


@pytest.fixture(scope="session")
def archkit_config(request: pytest.FixtureRequest) -> Config:
    """The parsed adopter configuration."""
    return request.config.stash[CONFIG_KEY]


@pytest.fixture(autouse=True)
def _archkit_hermetic(archkit_config: Config, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No check reads a developer's `.env` or reaches the network through their settings.

    `[archkit.hermetic]`: `settings_class` gets `env_file = None`, `env` is exported, and every
    `cache_clear` getter (an `lru_cache`d settings factory) is cleared around each check."""
    hermetic = archkit_config.settings.get("hermetic", {})
    settings_ref = hermetic.get("settings_class")
    if settings_ref:
        monkeypatch.setitem(resolve(settings_ref).model_config, "env_file", None)
    for key, value in hermetic.get("env", {}).items():
        monkeypatch.setenv(key, str(value))
    getters = [resolve(reference) for reference in hermetic.get("cache_clear", [])]
    for getter in getters:
        getter.cache_clear()
    yield
    for getter in getters:
        getter.cache_clear()


@pytest.fixture
def archkit_app_factory(archkit_config: Config) -> Callable[[], Any]:
    """The zero-argument `[archkit] app_factory` returning a fresh ASGI (FastAPI) app."""
    reference = str(archkit_config.settings["app_factory"])
    factory: Callable[[], Any] = resolve(reference)
    if not callable(factory):
        msg = f"[archkit] app_factory {reference!r} is not callable"
        raise ConfigError(msg)
    return factory


@pytest.fixture
def archkit_app(archkit_app_factory: Callable[[], Any]) -> Any:
    """A fresh app per check, built inside the hermetic environment."""
    return archkit_app_factory()


@pytest.fixture
def archkit_openapi(archkit_app: Any) -> dict[str, Any]:
    """The app's own OpenAPI document: the contract surface the route checks read."""
    schema: dict[str, Any] = archkit_app.openapi()
    return schema


def _client(app: Any) -> httpx.AsyncClient:
    # `raise_app_exceptions=False`: a server error middleware sends the error response and
    # then re-raises; the checks inspect the response, not the raise.
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    )


@pytest_asyncio.fixture
async def archkit_client(archkit_app: Any) -> AsyncIterator[httpx.AsyncClient]:
    """An ASGI client wired straight to the app: no server, no lifespan."""
    async with _client(archkit_app) as client:
        yield client


@pytest.fixture
def archkit_boom(
    archkit_config: Config, archkit_app_factory: Callable[[], Any]
) -> Callable[[Callable[[], Awaitable[None]]], Awaitable[httpx.Response]]:
    """`await archkit_boom(handler)`: serve `handler` at `[http] boom_path` on a fresh app.

    The route is added with FastAPI's `add_api_route`, outside the OpenAPI document, so a
    check can make a route fail a specific way without touching the shared app."""
    path = http_table(archkit_config).get("boom_path", "/__archkit_boom")

    async def _serve(handler: Callable[[], Awaitable[None]]) -> httpx.Response:
        app = archkit_app_factory()
        app.add_api_route(path, handler, methods=["GET"], include_in_schema=False)
        async with _client(app) as client:
            return await client.get(path)

    return _serve
