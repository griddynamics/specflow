"""The route contract, read from the app's OpenAPI document and real ASGI requests.

Typed JSON (or another declared media type) on every 2xx, no body on a 204, 201 on every
create, no credential-free success on a protected operation and no credential demand on
a public one.

`[route_contract]`: `public_operations` (`"METHOD /path"` of every anonymous operation;
every other operation is protected), `create_operation_prefix` (operationId prefix of a
create, default `create_`) and `request_headers` (sent with every probe; default the
`[http] probe_headers`).
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

import httpx

from archkit.config import Config, parse_operation
from archkit.support import fill, iter_operations, probe_headers, section

RULE = "route_contract"


def _public(table: dict[str, Any]) -> set[tuple[str, str]]:
    return {parse_operation(spec) for spec in table.get("public_operations", [])}


def _headers(config: Config, table: dict[str, Any]) -> dict[str, str]:
    if "request_headers" in table:
        return {str(k): str(v) for k, v in table["request_headers"].items()}
    return probe_headers(config)


def _body(method: str) -> dict[str, Any] | None:
    """An empty JSON body for a method that carries one, so validation never hides auth."""
    return {} if method in ("post", "put", "patch") else None


def test_every_2xx_response_is_typed_json_or_a_declared_media_type(
    archkit_config: Config, archkit_openapi: dict[str, Any]
) -> None:
    section(archkit_config, RULE)
    untyped: list[str] = []
    for method, path, operation in iter_operations(archkit_openapi):
        for code, response in operation.get("responses", {}).items():
            if not code.startswith("2") or code == str(HTTPStatus.NO_CONTENT.value):
                continue
            content = response.get("content") or {}
            where = f"{method.upper()} {path} -> {code}"
            if not content:
                untyped.append(f"{where} (no content declared)")
            elif "application/json" in content and not content["application/json"].get("schema"):
                untyped.append(f"{where} (application/json, no schema)")
    assert not untyped, (
        f"2xx responses with no typed JSON schema and no other declared content: {untyped}"
    )


def test_204_responses_carry_no_body(
    archkit_config: Config, archkit_openapi: dict[str, Any]
) -> None:
    section(archkit_config, RULE)
    no_content = str(HTTPStatus.NO_CONTENT.value)
    bodied = [
        f"{method.upper()} {path}"
        for method, path, operation in iter_operations(archkit_openapi)
        if (operation.get("responses", {}).get(no_content) or {}).get("content")
    ]
    assert not bodied, f"204 operations that declare a body: {bodied}"


def test_create_operations_return_201(
    archkit_config: Config, archkit_openapi: dict[str, Any]
) -> None:
    table = section(archkit_config, RULE)
    prefix = table.get("create_operation_prefix", "create_")
    created = str(HTTPStatus.CREATED.value)
    mismatched: list[str] = []
    for method, path, operation in iter_operations(archkit_openapi):
        operation_id = operation.get("operationId", "")
        successes = sorted(c for c in operation.get("responses", {}) if c.startswith("2"))
        if operation_id.startswith(prefix) and created not in successes:
            mismatched.append(f"{method.upper()} {path} ({operation_id}) -> {successes}")
    assert not mismatched, f"creates must return 201: {mismatched}"


def test_every_public_operation_exists(
    archkit_config: Config, archkit_openapi: dict[str, Any]
) -> None:
    table = section(archkit_config, RULE)
    present = {(method, path) for method, path, _ in iter_operations(archkit_openapi)}
    unknown = sorted(f"{m.upper()} {p}" for m, p in _public(table) - present)
    assert not unknown, f"[{RULE}] public_operations not in the OpenAPI document: {unknown}"


async def test_no_protected_operation_succeeds_without_credentials(
    archkit_config: Config, archkit_client: httpx.AsyncClient, archkit_openapi: dict[str, Any]
) -> None:
    table = section(archkit_config, RULE)
    public = _public(table)
    headers = _headers(archkit_config, table)
    succeeded: list[str] = []
    checked = 0
    for method, path, _operation in iter_operations(archkit_openapi):
        if (method, path) in public:
            continue
        checked += 1
        response = await archkit_client.request(
            method.upper(), fill(archkit_config, path), headers=headers, json=_body(method)
        )
        if response.status_code < HTTPStatus.BAD_REQUEST:
            succeeded.append(f"{method.upper()} {path} -> {response.status_code}")
    assert checked, "the OpenAPI document has no protected operation to probe"
    assert not succeeded, (
        f"protected operations that answer a credential-free caller with 2xx/3xx: {succeeded}"
    )


async def test_no_public_operation_demands_a_credential(
    archkit_config: Config, archkit_client: httpx.AsyncClient
) -> None:
    table = section(archkit_config, RULE)
    headers = _headers(archkit_config, table)
    demanding: list[str] = []
    for method, path in sorted(_public(table)):
        response = await archkit_client.request(
            method.upper(), fill(archkit_config, path), headers=headers, json=_body(method)
        )
        if response.status_code == HTTPStatus.UNAUTHORIZED:
            demanding.append(f"{method.upper()} {path}")
    assert not demanding, f"operations listed as public that demand a credential: {demanding}"
