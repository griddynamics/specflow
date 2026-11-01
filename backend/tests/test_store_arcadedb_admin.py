"""Offline tests of the ArcadeDB HTTP admin client: version, database, SQL, error mapping, no secret leaks.

The server is an ``httpx.MockTransport`` that answers like the 26.10.2 HTTP API; nothing leaves the process.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from graph.core.errors import ConfigurationError, InfrastructureUnavailableError
from graph.stores.arcadedb.admin import AdminClient, admin_url, check_database_name, parse_version

LEAK_PROBE = "pw-never-in-errors-77"


class Server:
    """Records requests and answers from a table of ``(method, path) -> response factory``."""

    def __init__(self, routes: dict[tuple[str, str], Callable[[httpx.Request], httpx.Response]]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self.routes.get((request.method, request.url.path))
        if route is None:
            return httpx.Response(404, json={"error": "not found"})
        return route(request)


def client(server: Server | Callable[[httpx.Request], httpx.Response]) -> AdminClient:
    return AdminClient("http://arcade:2480", "root", SecretStr(LEAK_PROBE), transport=httpx.MockTransport(server))


def ok(body: Any) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _request: httpx.Response(200, json=body)


# admin_url


@pytest.mark.parametrize(
    ("uri", "admin", "expected"),
    [
        ("bolt://db:7687", None, "http://db:2480"),
        ("neo4j://db.internal:7687", None, "http://db.internal:2480"),
        ("bolt+s://db:7687", None, "https://db:2480"),
        ("neo4j+s://db", None, "https://db:2480"),
        ("bolt+ssc://db:7687", None, "https://db:2480"),
        ("bolt://[::1]:7687", None, "http://[::1]:2480"),
        ("bolt://db:7687", "https://admin.example.org:8443/", "https://admin.example.org:8443"),
    ],
)
def test_the_admin_endpoint_is_given_or_the_bolt_host_on_2480(uri: str, admin: str | None, expected: str) -> None:
    assert admin_url(uri, admin) == expected


@pytest.mark.parametrize(
    ("uri", "admin", "setting"),
    [
        ("postgresql://db:5432/x", None, "uri"),
        ("bolt://", None, "uri"),
        ("bolt://db:7687", "ftp://db", "admin_uri"),
        ("bolt://db:7687", "http://", "admin_uri"),
    ],
)
def test_an_endpoint_that_cannot_be_derived_is_a_configuration_error(uri: str, admin: str | None, setting: str) -> None:
    with pytest.raises(ConfigurationError) as raised:
        admin_url(uri, admin)
    assert raised.value.code == "store_uri_invalid" and raised.value.fields["setting"] == setting


# version


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("26.10.2 (build e854692736b4/1792085686249/main)", (26, 10, 2)),
        ("26.11.1-SNAPSHOT", (26, 11, 1)),
        ("27.1", (27, 1, 0)),
    ],
)
def test_the_version_text_is_parsed(text: str, expected: tuple[int, int, int]) -> None:
    assert parse_version(text) == expected


def test_an_unreadable_version_is_a_configuration_error() -> None:
    with pytest.raises(ConfigurationError) as raised:
        parse_version("unknown")
    assert raised.value.code == "store_version_unknown"


async def test_the_real_version_comes_from_the_server_endpoint_with_basic_auth() -> None:
    server = Server({("GET", "/api/v1/server"): ok({"version": "26.10.2 (build x)"})})
    assert await client(server).server_version() == (26, 10, 2)
    request = server.requests[0]
    assert request.headers["authorization"].startswith("Basic ")


async def test_an_older_server_is_refused_with_both_versions_named() -> None:
    server = Server({("GET", "/api/v1/server"): ok({"version": "25.9.1"})})
    with pytest.raises(ConfigurationError) as raised:
        await client(server).require_version()
    assert raised.value.code == "store_version_unsupported"
    assert raised.value.fields == {"store": "arcadedb", "found": "25.9.1", "minimum": "26.10.0"}


async def test_the_minimum_version_and_later_pass() -> None:
    server = Server({("GET", "/api/v1/server"): ok({"version": "26.10.0"})})
    assert await client(server).require_version() == (26, 10, 0)


# database


def test_a_database_name_must_be_safe() -> None:
    assert check_database_name("specflow-1_a") == "specflow-1_a"
    for name in ("", "a b", "a/b", "a;drop", "x" * 65):
        with pytest.raises(ConfigurationError) as raised:
            check_database_name(name)
        assert raised.value.code == "store_database_name_invalid"


async def test_database_existence_and_creation_use_the_server_endpoints() -> None:
    created: list[Any] = []

    def create(request: httpx.Request) -> httpx.Response:
        created.append(json.loads(request.content))
        return httpx.Response(200, json={"result": "ok"})

    server = Server(
        {
            ("GET", "/api/v1/exists/there"): ok({"result": True}),
            ("GET", "/api/v1/exists/gone"): ok({"result": False}),
            ("POST", "/api/v1/server"): create,
        }
    )
    admin = client(server)
    assert await admin.database_exists("there") is True
    assert await admin.database_exists("gone") is False
    await admin.create_database("gone")
    assert created == [{"command": "create database gone"}]


async def test_ensure_database_creates_only_a_missing_one() -> None:
    calls: list[str] = []
    server = Server(
        {
            ("GET", "/api/v1/exists/there"): ok({"result": True}),
            ("GET", "/api/v1/exists/gone"): ok({"result": False}),
            ("POST", "/api/v1/server"): lambda _r: (calls.append("create"), httpx.Response(200, json={}))[1],
        }
    )
    admin = client(server)
    assert await admin.ensure_database("there") is False and calls == []
    assert await admin.ensure_database("gone") is True and calls == ["create"]


async def test_ensure_database_accepts_one_a_concurrent_start_created() -> None:
    answers = iter([False, True])
    server = Server(
        {
            ("GET", "/api/v1/exists/db1"): lambda _r: httpx.Response(200, json={"result": next(answers)}),
            ("POST", "/api/v1/server"): lambda _r: httpx.Response(
                500, json={"exception": "java.lang.IllegalArgumentException", "detail": "already exists"}
            ),
        }
    )
    assert await client(server).ensure_database("db1") is False


async def test_ensure_database_raises_when_the_create_fails_and_the_database_is_still_missing() -> None:
    server = Server(
        {
            ("GET", "/api/v1/exists/db1"): ok({"result": False}),
            ("POST", "/api/v1/server"): lambda _r: httpx.Response(500, json={"exception": "java.io.IOException"}),
        }
    )
    with pytest.raises(ConfigurationError) as raised:
        await client(server).ensure_database("db1")
    assert raised.value.code == "schema_setup_failed" and raised.value.fields["status"] == 500


# sql and schema


async def test_sql_posts_the_command_to_the_database_and_returns_rows() -> None:
    sent: list[Any] = []

    def command(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"result": [{"operation": "create index", "created": True}]})

    server = Server({("POST", "/api/v1/command/graphdb"): command})
    rows = await client(server).sql("graphdb", "CREATE VERTEX TYPE T IF NOT EXISTS")
    assert rows == [{"operation": "create index", "created": True}]
    assert sent == [{"language": "sql", "command": "CREATE VERTEX TYPE T IF NOT EXISTS"}]


async def test_types_reads_the_schema_types() -> None:
    server = Server({("POST", "/api/v1/command/graphdb"): ok({"result": [{"name": "SpecNode", "indexes": []}]})})
    assert await client(server).types("graphdb") == [{"name": "SpecNode", "indexes": []}]
    assert json.loads(server.requests[0].content)["command"] == "SELECT FROM schema:types"


async def test_an_empty_or_unexpected_body_is_an_empty_result() -> None:
    server = Server(
        {
            ("POST", "/api/v1/command/a"): lambda _r: httpx.Response(204),
            ("POST", "/api/v1/command/b"): ok({"result": "ok"}),
            ("POST", "/api/v1/command/c"): ok(["not", "a", "dict"]),
        }
    )
    admin = client(server)
    assert await admin.sql("a", "x") == [] and await admin.sql("b", "x") == [] and await admin.sql("c", "x") == []


async def test_a_body_that_is_not_json_is_a_setup_failure() -> None:
    server = Server({("GET", "/api/v1/server"): lambda _r: httpx.Response(200, content=b"<html>")})
    with pytest.raises(ConfigurationError) as raised:
        await client(server).server_version()
    assert raised.value.code == "schema_setup_failed"


# errors never carry the server's text or the password


@pytest.mark.parametrize(
    ("status", "code"),
    [(401, "store_auth_rejected"), (403, "store_access_denied")],
)
async def test_a_rejected_login_or_denied_access_is_a_configuration_error(status: int, code: str) -> None:
    server = Server({("GET", "/api/v1/server"): lambda _r: httpx.Response(status, json={"detail": LEAK_PROBE})})
    with pytest.raises(ConfigurationError) as raised:
        await client(server).server_version()
    assert raised.value.code == code and raised.value.fields["store"] == "arcadedb"
    assert LEAK_PROBE not in str(raised.value) and LEAK_PROBE not in repr(raised.value.fields)


async def test_a_missing_database_is_a_neutral_configuration_error() -> None:
    body = {"exception": "java.lang.IllegalArgumentException", "detail": "Database 'nope' does not exist"}
    server = Server({("POST", "/api/v1/command/nope"): lambda _r: httpx.Response(500, json=body)})
    with pytest.raises(ConfigurationError) as raised:
        await client(server).sql("nope", "SELECT 1")
    assert raised.value.code == "store_database_not_found"


async def test_a_refused_command_carries_the_status_and_exception_class_only() -> None:
    body = {"exception": "com.arcadedb.exception.SchemaException", "detail": f"Type 'X' not found {LEAK_PROBE}"}
    server = Server({("POST", "/api/v1/command/graphdb"): lambda _r: httpx.Response(400, json=body)})
    with pytest.raises(ConfigurationError) as raised:
        await client(server).sql("graphdb", "CREATE INDEX ...")
    assert raised.value.code == "schema_setup_failed"
    assert raised.value.fields == {
        "store": "arcadedb",
        "status": 400,
        "error_type": "com.arcadedb.exception.SchemaException",
    }
    assert LEAK_PROBE not in str(raised.value) and "Type 'X'" not in str(raised.value)


async def test_an_error_response_with_no_usable_exception_name_has_none() -> None:
    server = Server(
        {
            ("POST", "/api/v1/command/a"): lambda _r: httpx.Response(500, content=b"boom"),
            ("POST", "/api/v1/command/b"): lambda _r: httpx.Response(500, json={"exception": "bad name!"}),
        }
    )
    for database in ("a", "b"):
        with pytest.raises(ConfigurationError) as raised:
            await client(server).sql(database, "x")
        assert raised.value.fields["error_type"] is None


async def test_an_unreachable_server_is_an_infrastructure_error_without_the_url_or_password() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"connection refused {LEAK_PROBE}")

    with pytest.raises(InfrastructureUnavailableError) as raised:
        await client(refuse).server_version()
    assert raised.value.code == "store_unreachable"
    assert raised.value.fields == {"store": "arcadedb", "error_type": "ConnectError"}
    assert LEAK_PROBE not in str(raised.value)


async def test_close_closes_the_http_client() -> None:
    admin = client(Server({}))
    await admin.close()
    with pytest.raises(RuntimeError):
        await admin.server_version()
