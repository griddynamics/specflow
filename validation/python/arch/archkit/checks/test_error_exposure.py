"""An unknown route, a wrong method and an unhandled exception answer the error envelope.

No traceback, file path or raw exception message reaches the client. Only the envelope's
shape is asserted here, never a code (that is `[error_codes]`).

`[error_exposure]`: `unknown_path` (default `/__archkit_no_such_route`),
`method_not_allowed` (`"METHOD /path"` of an existing path the method is not allowed on,
required) and `leak_markers` (text no error response may contain). The envelope's code and
message are read at `[http] envelope_code` / `envelope_message` (default `error.code`,
`error.message`); probes carry the `[http] probe_headers`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from http import HTTPStatus

import httpx

from archkit.config import Config, parse_operation
from archkit.support import dotted_get, http_table, json_body, probe_headers, section

RULE = "error_exposure"

_DEFAULT_LEAK_MARKERS = (
    "Traceback (most recent call last)",
    "site-packages",
    '.py", line',
    "RuntimeError",
)
#: Not a credential: the text a probe exception carries, checked for absence in the response.
_INTERNAL_FAILURE_MARKER = "this internal detail must never reach the client"


def _envelope_problems(config: Config, response: httpx.Response) -> list[str]:
    table = http_table(config)
    body = json_body(response)
    code = dotted_get(body, table.get("envelope_code", "error.code"))
    message = dotted_get(body, table.get("envelope_message", "error.message"))
    problems: list[str] = []
    if not isinstance(code, str) or not code:
        problems.append(f"no non-empty string code at {table.get('envelope_code', 'error.code')}")
    if not isinstance(message, str):
        problems.append(f"no string message at {table.get('envelope_message', 'error.message')}")
    return problems


def _leaks(config: Config, text: str) -> list[str]:
    markers = section(config, RULE).get("leak_markers", list(_DEFAULT_LEAK_MARKERS))
    return [marker for marker in markers if marker in text]


def _assert_envelope(config: Config, response: httpx.Response, *, where: str) -> None:
    problems = _envelope_problems(config, response)
    assert not problems, f"{where}: not the error envelope: {problems}"
    leaks = _leaks(config, response.text)
    assert not leaks, f"{where}: the response leaks internals {leaks}"


async def test_an_unknown_route_answers_the_envelope(
    archkit_config: Config, archkit_client: httpx.AsyncClient
) -> None:
    table = section(archkit_config, RULE)
    path = table.get("unknown_path", "/__archkit_no_such_route")
    response = await archkit_client.get(path, headers=probe_headers(archkit_config))
    assert response.status_code == HTTPStatus.NOT_FOUND
    _assert_envelope(archkit_config, response, where="404 unknown route")


async def test_a_method_not_allowed_answers_the_envelope(
    archkit_config: Config, archkit_client: httpx.AsyncClient
) -> None:
    table = section(archkit_config, RULE)
    method, path = parse_operation(table["method_not_allowed"])
    response = await archkit_client.request(
        method.upper(), path, headers=probe_headers(archkit_config)
    )
    assert response.status_code == HTTPStatus.METHOD_NOT_ALLOWED
    _assert_envelope(archkit_config, response, where="405 method not allowed")


async def _raise_with_internal_detail() -> None:
    msg = f"internal failure: {_INTERNAL_FAILURE_MARKER}"
    raise RuntimeError(msg)


async def test_an_unhandled_exception_answers_the_envelope_without_internals(
    archkit_config: Config,
    archkit_boom: Callable[[Callable[[], Awaitable[None]]], Awaitable[httpx.Response]],
) -> None:
    section(archkit_config, RULE)
    response = await archkit_boom(_raise_with_internal_detail)
    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    _assert_envelope(archkit_config, response, where="500 unhandled exception")
    assert _INTERNAL_FAILURE_MARKER not in response.text, (
        "the 500 envelope leaks the raw exception message"
    )
