"""The request-id middleware is outermost: every response carries a non-empty request id.

Proved on a 2xx, a 404, a 405, a 401 and an unhandled-exception 500; a header missing from
the 500 means the middleware sits inside the error handling.

`[request_id]`: `header` (default `X-Request-Id`), `ok_path` (a public GET answering 200,
required), `unknown_path` (default `/__archkit_no_such_route`), `method_not_allowed`
(`"METHOD /path"`, required) and `unauthenticated_path` (a GET answering 401 without
credentials, required).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from http import HTTPStatus

import httpx

from archkit.config import Config, parse_operation
from archkit.support import probe_headers, section

RULE = "request_id"


def _header(config: Config) -> str:
    return str(section(config, RULE).get("header", "X-Request-Id"))


def _setting(config: Config, key: str) -> str:
    return str(section(config, RULE)[key])


def _assert_has_id(config: Config, response: httpx.Response, *, where: str) -> None:
    header = _header(config)
    assert response.headers.get(header), f"{where}: the response carries no {header}"


async def test_a_2xx_response_carries_the_request_id(
    archkit_config: Config, archkit_client: httpx.AsyncClient
) -> None:
    response = await archkit_client.get(
        _setting(archkit_config, "ok_path"), headers=probe_headers(archkit_config)
    )
    assert response.status_code == HTTPStatus.OK
    _assert_has_id(archkit_config, response, where="2xx")


async def test_a_404_response_carries_the_request_id(
    archkit_config: Config, archkit_client: httpx.AsyncClient
) -> None:
    path = section(archkit_config, RULE).get("unknown_path", "/__archkit_no_such_route")
    response = await archkit_client.get(path, headers=probe_headers(archkit_config))
    assert response.status_code == HTTPStatus.NOT_FOUND
    _assert_has_id(archkit_config, response, where="404 (no route matched)")


async def test_a_405_response_carries_the_request_id(
    archkit_config: Config, archkit_client: httpx.AsyncClient
) -> None:
    method, path = parse_operation(_setting(archkit_config, "method_not_allowed"))
    response = await archkit_client.request(
        method.upper(), path, headers=probe_headers(archkit_config)
    )
    assert response.status_code == HTTPStatus.METHOD_NOT_ALLOWED
    _assert_has_id(archkit_config, response, where="405")


async def test_a_401_response_carries_the_request_id(
    archkit_config: Config, archkit_client: httpx.AsyncClient
) -> None:
    response = await archkit_client.get(
        _setting(archkit_config, "unauthenticated_path"), headers=probe_headers(archkit_config)
    )
    assert response.status_code == HTTPStatus.UNAUTHORIZED
    _assert_has_id(archkit_config, response, where="401")


async def _raise_runtime_error() -> None:
    msg = "archkit induced failure"
    raise RuntimeError(msg)


async def test_an_unhandled_exception_response_carries_the_request_id(
    archkit_config: Config,
    archkit_boom: Callable[[Callable[[], Awaitable[None]]], Awaitable[httpx.Response]],
) -> None:
    section(archkit_config, RULE)
    response = await archkit_boom(_raise_runtime_error)
    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    _assert_has_id(archkit_config, response, where="500 (unhandled exception)")
